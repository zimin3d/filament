"""Принтер: API агента, входящие задания, импорт истории, обслуживание, разбор файлов слайсера."""
import json
import re
import secrets
from datetime import date

from flask import abort, flash, jsonify, redirect, render_template, request, url_for

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
    data = request.get_json(silent=True) or {}
    ext_id = str(data.get("job_id") or "").strip()[:64]
    try:
        raw_weights = data.get("weights") or []
        if not isinstance(raw_weights, list):
            raise ValueError
        weights = [round(float(w), 1) for w in raw_weights[:16] if float(w) > 0]
        if any(w > 10000 for w in weights):
            raise ValueError
    except (TypeError, ValueError):
        return jsonify(ok=False, error="weights — список чисел, граммы"), 400
    if not ext_id or not weights:
        return jsonify(ok=False, error="нужны job_id и weights"), 400
    db = get_db()
    if db.execute("SELECT 1 FROM inbox WHERE ext_id = ?", (ext_id,)).fetchone():
        return jsonify(ok=True, duplicate=True)
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
    return jsonify(ok=True, auto=auto)


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
