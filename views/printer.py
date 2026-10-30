"""Принтер: API агента, входящие задания, импорт истории, обслуживание, разбор файлов слайсера."""
import json
import os
import re
import secrets
import time
from datetime import date

from flask import abort, flash, jsonify, redirect, render_template, request, url_for

import bambu
import slicer
from core import *  # noqa: F401,F403
from logic import *  # noqa: F401,F403

@app.route("/import-history", methods=["GET", "POST"])
@admin_required
def import_history():
    """История печатей принтера (JSON Moonraker /server/history/list): старые печати идут в наработку
    и статистику без списания пластика (иначе остатки посчитаются дважды) или во «Входящие»."""
    if request.method == "POST":
        file = request.files.get("history")
        try:
            data = json.loads((file.read() if file else b"").decode("utf-8-sig"))
            jobs_ = (data.get("result") or data).get("jobs") if isinstance(data, dict) else None
            if not isinstance(jobs_, list):
                raise ValueError
        except (ValueError, UnicodeDecodeError, AttributeError):
            flash("Это не история Moonraker: нужен JSON со списком jobs (адрес — ниже).", "error")
            return redirect(url_for("import_history"))
        agent = agent_module()
        status = "pending" if request.form.get("to_inbox") else "history"
        db = get_db()
        added = dup = skipped = 0
        minutes = 0
        for job in jobs_:
            if not isinstance(job, dict) or job.get("status") not in agent.FINAL:
                skipped += 1
                continue
            try:
                p = agent.job_payload(job)
            except (TypeError, ValueError, AttributeError, OverflowError):
                skipped += 1
                continue
            if not p["weights"] and not p["minutes"]:
                skipped += 1
                continue
            name = re.sub(r"(\.gcode)?(\.3mf)?$", "", p["name"])[:200]
            if p["status"] != "completed":
                name = "%s (%s)" % (name, {"cancelled": "отменена", "error": "ошибка"}.get(p["status"], p["status"]))
            cur = db.execute("INSERT OR IGNORE INTO inbox (ext_id, day, name, minutes, weights, status, printer_status) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)", (p["job_id"], p["day"], name, p["minutes"],
                                                             json.dumps(p["weights"] or [0]), status, p["status"]))
            if cur.rowcount:
                added += 1
                minutes += p["minutes"] or 0
            else:
                dup += 1
        db.commit()
        flash("История: добавлено %d печатей (%.1f ч), уже были %d, пропущено %d (идут/без данных)%s." % (
            added, minutes / 60, dup, skipped, " — разнесите их во «Входящих»" if status == "pending" else ""), "ok")
        return redirect(url_for("inbox") if status == "pending" and added else url_for("printer"))
    db = get_db()
    hist = db.execute("SELECT COUNT(*), COALESCE(SUM(minutes), 0), MIN(day), MAX(day) FROM inbox WHERE status = 'history'").fetchone()
    return render_template("import_history.html", hist=hist)


@app.route("/printer", methods=["GET", "POST"])
def printer():
    """Обслуживание принтера: наработка из журнала печатей и регламент «раз в N часов / дней»."""
    db = get_db()
    if request.method == "POST":
        if not is_admin():
            abort(403)
        action, mid = request.form.get("action"), request.form.get("id", type=int)
        if action == "done":
            db.execute("UPDATE maintenance SET last_date = ?, last_hours = ? WHERE id = ?",
                       (date.today().isoformat(), printer_hours(), mid))
            flash("Отмечено: сделано сегодня.", "ok")
        elif action == "delete":
            db.execute("DELETE FROM maintenance WHERE id = ?", (mid,))
            flash("Пункт удалён.", "ok")
        elif action in ("add", "save"):
            name = request.form.get("name", "").strip()[:120]
            try:
                hours = float(request.form.get("every_hours", "").replace(",", ".")) if request.form.get("every_hours", "").strip() else None
                days = int(request.form.get("every_days", "")) if request.form.get("every_days", "").strip() else None
            except ValueError:
                flash("Периодичность — числом.", "error")
                return redirect(url_for("printer"))
            if not name or not (hours or days):
                flash("Нужны название и периодичность (часы и/или дни).", "error")
                return redirect(url_for("printer"))
            notes = request.form.get("notes", "").strip()[:300]
            if action == "add":
                db.execute("INSERT INTO maintenance (name, every_hours, every_days, notes, last_date, last_hours) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (name, hours, days, notes, date.today().isoformat(), printer_hours()))
            else:
                db.execute("UPDATE maintenance SET name = ?, every_hours = ?, every_days = ?, notes = ? WHERE id = ?",
                           (name, hours, days, notes, mid))
            flash("Сохранено.", "ok")
        db.commit()
        return redirect(url_for("printer"))
    hours, items = maintenance_status()
    return render_template("printer.html", hours=hours, items=items,
                           jobs_count=db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                           edit=request.args.get("edit", type=int))


@app.route("/spool/<int:spool_id>/slice", methods=["POST"])
@admin_required
def slice_parse(spool_id):
    """Разбор куска файла слайсера, присланного браузером (см. static/slice.js)."""
    row = get_db().execute("SELECT material, diameter FROM spools WHERE id = ?", (spool_id,)).fetchone()
    if row is None:
        abort(404)
    text = request.form.get("text", "")[:2_000_000]
    if request.form.get("kind") == "3mf":
        result = slicer.parse_slice_info(text)
    else:
        result = slicer.parse_gcode(text, row["material"], row["diameter"] or 1.75)
    return jsonify(result)


@app.route("/slice", methods=["POST"])
def slice_parse_any():
    """То же без катушки — для «Хватит ли?» (материал из формы, для Cura-файлов)."""
    text = request.form.get("text", "")[:2_000_000]
    if request.form.get("kind") == "3mf":
        return jsonify(slicer.parse_slice_info(text))
    return jsonify(slicer.parse_gcode(text, request.form.get("material", ""), 1.75))


@app.route("/api/printer/job", methods=["POST"])
def api_printer_job():
    """Агент присылает законченную печать. Один филамент и выбрана катушка «на принтере» —
    списываем сразу; иначе — во «Входящие» на ручное распределение."""
    token = load_config().get("printer_token")
    auth = request.headers.get("Authorization", "")
    if not token or not secrets.compare_digest(auth, "Bearer " + token):
        abort(403)
    result = accept_printer_job(request.get_json(silent=True) or {})
    return jsonify(result), (400 if result.get("error") else 200)


def accept_printer_job(data):
    """Законченная печать (от агента или из связи с принтером): {ok, auto} / {ok, duplicate} / {ok: False, error}."""
    ext_id = str(data.get("job_id") or "").strip()[:64]
    try:
        raw_weights = data.get("weights") or []
        if not isinstance(raw_weights, list):
            raise ValueError
        weights = [round(float(w), 1) for w in raw_weights[:16] if float(w) > 0]
        if any(w > 10000 for w in weights):
            raise ValueError
    except (TypeError, ValueError):
        return {"ok": False, "error": "weights — список чисел, граммы"}
    if not ext_id or not weights:
        return {"ok": False, "error": "нужны job_id и weights"}
    db = get_db()
    if db.execute("SELECT 1 FROM inbox WHERE ext_id = ?", (ext_id,)).fetchone():
        return {"ok": True, "duplicate": True}
    name = re.sub(r"(\.gcode)?(\.3mf)?$", "", str(data.get("name") or ""))[:200]
    minutes = int(data["minutes"]) if data.get("minutes") else None
    day = str(data.get("day") or date.today().isoformat())[:10]
    status = str(data.get("status") or "")[:20]
    if status and status != "completed":
        name = "%s (%s)" % (name, {"cancelled": "отменена", "error": "ошибка"}.get(status, status))
    spool = printer_spool()
    auto = len(weights) == 1 and spool is not None
    db.execute("INSERT INTO inbox (ext_id, day, name, minutes, weights, status, printer_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (ext_id, day, name, minutes, json.dumps(weights), "done" if auto else "pending", status))
    if auto:
        audit_state = audit_begin(spool["id"])
        record_job(db, spool["id"], day, name, weights[0], minutes)
        audit_end("Печать с принтера", spool["id"], audit_state)
    db.commit()
    return {"ok": True, "auto": auto}


@app.route("/inbox", methods=["GET", "POST"])
@admin_required
def inbox():
    """Печати с принтера, которые надо разнести по катушкам."""
    db = get_db()
    if request.method == "POST":
        if request.form.get("skip_all"):
            db.execute("UPDATE inbox SET status = 'skipped' WHERE status = 'pending'")
            db.commit()
            flash("Все пропущены (в наработку принтера они всё равно идут).", "ok")
            return redirect(url_for("inbox"))
        item = db.execute("SELECT * FROM inbox WHERE id = ? AND status = 'pending'",
                          (request.form.get("id", type=int),)).fetchone()
        if item is None:
            abort(404)
        if request.form.get("skip"):
            db.execute("UPDATE inbox SET status = 'skipped' WHERE id = ?", (item["id"],))
        else:
            weights = json.loads(item["weights"])
            for i, w in enumerate(weights):
                sid = request.form.get("spool_%d" % i, type=int)
                grams = request.form.get("grams_%d" % i, "").replace(",", ".")
                if sid:
                    audit_state = audit_begin(sid)
                    record_job(db, sid, item["day"], item["name"], float(grams) if grams else w,
                               item["minutes"] if i == 0 else None)
                    audit_end("Печать с принтера", sid, audit_state)
            db.execute("UPDATE inbox SET status = 'done' WHERE id = ?", (item["id"],))
        db.commit()
        flash("Готово.", "ok")
        return redirect(url_for("inbox"))
    items = [dict(r, weights=json.loads(r["weights"])) for r in
             db.execute("SELECT * FROM inbox WHERE status = 'pending' ORDER BY id")]
    spools = [spool_view(r) for r in db.execute("SELECT * FROM spools WHERE weight_left > 0 ORDER BY " + SORTS["number"])]
    current = printer_spool()
    return render_template("inbox.html", items=items, spools=spools, current=current["id"] if current else None)


@app.route("/spool/<int:spool_id>/on-printer", methods=["POST"])
@admin_required
def spool_on_printer(spool_id):
    cfg = load_config()
    if cfg.get("printer_spool") == spool_id:
        cfg.pop("printer_spool")
        flash("Катушка снята с принтера.", "ok")
    else:
        cfg["printer_spool"] = spool_id
        flash("Отмечено: эта катушка на принтере — однокрасочные печати будут списываться с неё.", "ok")
    save_config(cfg)
    return redirect(url_for("spool_detail", spool_id=spool_id))


@app.route("/jobs/<int:job_id>/delete", methods=["POST"])
@admin_required
def job_delete(job_id):
    """Удаляет запись о печати; списанные граммы не возвращаются (остаток поправьте взвешиванием)."""
    db = get_db()
    row = db.execute("SELECT spool_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("UPDATE prints SET job_id = NULL WHERE job_id = ?", (job_id,))
    db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    db.commit()
    flash("Запись о печати удалена. Остаток не менялся.", "ok")
    return redirect(url_for("spool_detail", spool_id=row["spool_id"]) + "#prints")


@app.route("/jobs")
def jobs():
    rows = [dict(r) for r in get_db().execute(
        "SELECT j.*, s.number, s.material, s.color_name, s.color_hex, s.manufacturer, "
        "(SELECT photo FROM prints p WHERE p.job_id = j.id ORDER BY p.id LIMIT 1) AS photo, "
        "(SELECT id FROM models md WHERE md.job_id = j.id ORDER BY md.id LIMIT 1) AS model_id "
        "FROM jobs j JOIN spools s ON s.id = j.spool_id ORDER BY j.day DESC, j.id DESC LIMIT 500")]
    totals = {"count": len(rows), "grams": sum(r["grams"] or 0 for r in rows),
              "minutes": sum(r["minutes"] or 0 for r in rows), "cost": sum(r["cost"] or 0 for r in rows)}
    return render_template("jobs.html", jobs=rows, totals=totals)


# --- связь с принтером в локальной сети (Creality K2 и другие Klipper/Moonraker) -------------

PRINTER_TIMEOUT = 5
PRINTER_STATES = {"standby": "ожидает", "printing": "печатает", "paused": "пауза", "complete": "готово",
                  "cancelled": "отменено", "error": "ошибка"}


def printer_addr(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    host = cfg.get("printer_host") or ""
    return ("http://%s:%d" % (host, int(cfg.get("printer_port") or 7125))) if host else None


def moonraker(path, cfg=None):
    import urllib.request
    base = printer_addr(cfg)
    if not base:
        raise ValueError("Адрес принтера не задан.")
    with urllib.request.urlopen(base + path, timeout=PRINTER_TIMEOUT) as r:
        return json.loads(r.read(2_000_000)).get("result") or {}


def is_bambu(cfg):
    return cfg.get("printer_kind") == "bambu"


def bambu_report(cfg):
    if not (cfg.get("printer_host") and cfg.get("bambu_serial") and cfg.get("bambu_code")):
        raise ValueError("Нужны IP, серийный номер и код доступа.")
    try:
        return bambu.report(cfg["printer_host"], cfg["bambu_serial"], cfg["bambu_code"],
                            port=int(cfg.get("bambu_mqtt_port") or bambu.MQTT_PORT), tls=not cfg.get("bambu_plain"))
    except bambu.BambuError as e:
        raise ValueError(str(e))


def printer_live():
    """Состояние принтера сейчас: печать, прогресс, температуры. Исключение — если не отвечает."""
    cfg = load_config()
    if is_bambu(cfg):
        return bambu.live(bambu_report(cfg))
    st = moonraker("/printer/objects/query?print_stats&display_status&extruder&heater_bed").get("status") or {}
    ps, ex, bed = st.get("print_stats") or {}, st.get("extruder") or {}, st.get("heater_bed") or {}
    progress = (st.get("display_status") or {}).get("progress")
    return {"state": PRINTER_STATES.get(ps.get("state"), ps.get("state") or "?"), "raw_state": ps.get("state"),
            "file": os.path.basename(str(ps.get("filename") or "")),
            "progress": round(progress * 100) if isinstance(progress, (int, float)) else None,
            "minutes": round((ps.get("print_duration") or 0) / 60),
            "nozzle": ex.get("temperature"), "nozzle_target": ex.get("target"),
            "bed": bed.get("temperature"), "bed_target": bed.get("target")}


def poll_printer():
    """Новые законченные печати из истории принтера → учёт (как от агента). Печати, закончившиеся
    до подключения принтера, не трогаем. Возвращает (списано, во входящих)."""
    cfg = load_config()
    if is_bambu(cfg):
        return poll_bambu(cfg)
    since = float(cfg.get("printer_since") or 0)
    agent = agent_module()
    jobs_ = moonraker("/server/history/list?limit=50&order=desc", cfg).get("jobs") or []
    auto = pending = 0
    for job in reversed(jobs_):
        if not isinstance(job, dict) or job.get("status") not in agent.FINAL:
            continue
        if float(job.get("end_time") or 0) <= since:
            continue
        payload = agent.job_payload(job)
        if not payload["weights"]:
            continue
        r = accept_printer_job(payload)
        if r.get("ok") and not r.get("duplicate"):
            auto, pending = auto + bool(r.get("auto")), pending + (not r.get("auto"))
    update_config(printer_checked=time.strftime("%Y-%m-%d %H:%M"))
    return auto, pending


def poll_bambu(cfg):
    """Bambu: истории нет — следим за печатью сами. Началась — запоминаем граммы из файла .3mf и
    остатки AMS; закончилась (FINISH/FAILED) — записываем, как печать от агента."""
    host = cfg.get("printer_host")
    p = bambu_report(cfg)
    state, key = str(p.get("gcode_state") or ""), bambu.job_key(p)
    job = cfg.get("bambu_job") or {}
    auto = pending = 0
    if state in ("PREPARE", "RUNNING", "PAUSE") and job.get("key") != key:
        weights = bambu.print_file_weights(cfg["printer_host"], cfg["bambu_code"], p, slicer.parse_slice_info,
                                           port=int(cfg.get("bambu_ftp_port") or bambu.FTPS_PORT))
        job = {"key": key, "name": p.get("subtask_name") or os.path.basename(str(p.get("gcode_file") or "")),
               "started": time.time(), "weights": weights, "ams": bambu.ams_remains(p), "done": False}
    elif state in ("FINISH", "FAILED") and job.get("key") == key and not job.get("done"):
        weights = list(job.get("weights") or [])
        if weights and state == "FAILED" and isinstance(p.get("mc_percent"), int):
            weights = [w * p["mc_percent"] / 100 for w in weights]  # прервалась — пропорционально сделанному
        if not weights:  # файла не было — по убыли остатка в AMS
            now = bambu.ams_remains(p)
            weights = [(was[0] - now[slot][0]) / 100 * was[1] for slot, was in (job.get("ams") or {}).items()
                       if slot in now and was[0] > now[slot][0]]
        weights = [round(w, 1) for w in weights if w > 0]
        if weights:
            r = accept_printer_job({"job_id": ("bambu-" + key)[:64], "name": job.get("name") or "печать",
                                    "status": "completed" if state == "FINISH" else "error",
                                    "minutes": max(1, round((time.time() - float(job.get("started") or time.time())) / 60)),
                                    "day": date.today().isoformat(), "weights": weights})
            if r.get("ok") and not r.get("duplicate"):
                auto, pending = auto + bool(r.get("auto")), pending + (not r.get("auto"))
        job["done"] = True
    if load_config(fresh=True).get("printer_host") != host:  # пока ждали принтер, его сменили — печать не его
        return auto, pending
    update_config(bambu_job=job, printer_checked=time.strftime("%Y-%m-%d %H:%M"))
    return auto, pending


@app.route("/printer/link", methods=["GET", "POST"])
@admin_required
def printer_link():
    cfg = load_config()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "save":
            kind = "bambu" if request.form.get("kind") == "bambu" else "klipper"
            host = request.form.get("host", "").strip()
            port = request.form.get("port", "").strip() or "7125"
            serial = request.form.get("serial", "").strip().upper()
            code = request.form.get("code", "").strip()
            if host and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) or not port.isdigit() or not 0 < int(port) < 65536:
                flash("Адрес — IP или имя (например 192.168.1.50), порт — число.", "error")
                return redirect(url_for("printer_link"))
            if kind == "bambu" and host and (not re.fullmatch(r"[0-9A-Z]{8,20}", serial)
                                             or not (code or cfg.get("bambu_code"))):
                flash("Для Bambu Lab нужны серийный номер (на наклейке или в Настройках принтера) и код доступа LAN.", "error")
                return redirect(url_for("printer_link"))
            if host != cfg.get("printer_host") or kind != cfg.get("printer_kind", "klipper"):
                cfg["printer_since"] = time.time()  # новый принтер: старую историю не списываем
                cfg.pop("bambu_job", None)
            cfg.update(printer_kind=kind, printer_host=host, printer_port=int(port), bambu_serial=serial)
            if code:
                cfg["bambu_code"] = code
            save_config(cfg)
            flash("Сохранено." if host else "Связь с принтером отключена.", "ok")
        elif action == "poll":
            try:
                auto, pending = poll_printer()
                flash("Новых печатей: %d (списано сразу %d, во «Входящих» %d)." % (auto + pending, auto, pending), "ok")
            except (OSError, ValueError) as e:
                flash("Принтер не отвечает: %s" % (getattr(e, "reason", None) or e), "error")
        return redirect(url_for("printer_link"))
    live = error = None
    if cfg.get("printer_host"):
        try:
            live = printer_live()
        except (OSError, ValueError) as e:
            error = str(getattr(e, "reason", None) or e)
    return render_template("printer_link.html", cfg=cfg, live=live, error=error, spool=printer_spool(),
                           phone=PHONE)


DUMP_OBJECTS = re.compile(r"box|cfs|filament|rfid|material|spool|feeder|buffer|extruder|print_stats|virtual_sdcard",
                          re.I)


@app.route("/printer/dump")
@admin_required
def printer_dump():
    """Снимок данных принтера в файл — чтобы научить приложение новому (ячейки CFS у K2, отчёт Bambu).
    Секретов в нём нет: код доступа Bambu не входит, у Moonraker — только состояние."""
    import urllib.parse
    cfg = load_config()
    out = {"kind": cfg.get("printer_kind", "klipper"), "taken_at": time.strftime("%Y-%m-%d %H:%M"),
           "app_version": os.environ.get("FILAMENT_APP_VERSION", "")}

    def grab(key, fn):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001 — в снимок идёт текст ошибки, а не отказ целиком
            out[key + "_error"] = str(getattr(e, "reason", None) or e)[:300]

    if is_bambu(cfg):
        grab("report", lambda: bambu_report(cfg))
    else:
        grab("printer_info", lambda: moonraker("/printer/info", cfg))
        grab("objects", lambda: moonraker("/printer/objects/list", cfg).get("objects") or [])
        # макросы (gcode_macro BOX_…, LOAD_FILAMENT) тоже совпадают — их в запрос не берём, чтобы не вытеснили объекты
        names = [o for o in out.get("objects") or [] if isinstance(o, str) and DUMP_OBJECTS.search(o)
                 and not o.startswith(("gcode_macro", "gcode_button", "gcode_shell_command"))][:120]
        if names:
            grab("status", lambda: moonraker("/printer/objects/query?" +
                                             "&".join(urllib.parse.quote(n) for n in names), cfg).get("status"))
        grab("history", lambda: moonraker("/server/history/list?limit=3&order=desc", cfg).get("jobs"))
    resp = app.response_class(json.dumps(out, ensure_ascii=False, indent=1, default=str), mimetype="application/json")
    resp.headers["Content-Disposition"] = "attachment; filename=printer-%s.json" % time.strftime("%Y%m%d-%H%M")
    return resp
