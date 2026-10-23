"""Страницы катушек: список, карточка, создание/правка, взвешивание и расход, журнал, профили."""
import json
import os
import shutil
import sqlite3
import uuid
from datetime import date

from flask import abort, flash, redirect, render_template, request, session, url_for

from core import *  # noqa: F401,F403
from logic import *  # noqa: F401,F403

@app.route("/")
def index():
    return spool_list(empty_page=False)


@app.route("/empty")
def empty_spools():
    """Закончившиеся катушки — отдельно от основного списка."""
    return spool_list(empty_page=True)


def spool_list(empty_page):
    db = get_db()
    args = request.args.to_dict()
    if empty_page:
        args["status"] = "empty"
    elif args.get("status") == "empty":
        args.pop("status")
    spools, f = filtered_spools(args)
    if not empty_page:
        # запоминаем фильтры, чтобы «← к списку» с других страниц возвращал к ним
        args = {k: v for k, v in args.items() if k in LIST_ARGS and v}
        if session.get("list_args") != args:
            session["list_args"] = args
    stats = {
        "count": len(spools),
        "weight_left": sum(s["weight_left"] for s in spools),
        "value_left": sum(s["value_left"] or 0 for s in spools),
        "spent": sum(s["price"] or 0 for s in spools),
    }
    manufacturers = [r[0] for r in db.execute(
        "SELECT DISTINCT manufacturer FROM spools ORDER BY manufacturer COLLATE NOCASE")]
    locations = [r[0] for r in db.execute(
        "SELECT DISTINCT location FROM spools WHERE location != '' ORDER BY location COLLATE NOCASE")]
    used_materials = [r[0] for r in db.execute(
        "SELECT DISTINCT material FROM spools ORDER BY material")]
    stats["needs_drying"] = sum(1 for s in spools if s["needs_drying"])
    return render_template(
        "index.html", spools=spools, stats=stats, manufacturers=manufacturers, empty_page=empty_page,
        no_spools=not db.execute("SELECT 1 FROM spools LIMIT 1").fetchone(),
        list_endpoint="empty_spools" if empty_page else "index",
        used_materials=used_materials, locations=locations, f=f,
        palette=palette([spool_view(r) for r in db.execute("SELECT * FROM spools WHERE ordered = 0 AND weight_left > 0")]),
        bulk_actions=[(k, label) for k, label, _ in BULK_ACTIONS],
        bulk_kinds={k: kind for k, _, kind in BULK_ACTIONS})


@app.route("/bulk", methods=["POST"])
@admin_required
def bulk():
    """Массовая правка выбранных на главной катушек; каждая — отдельная запись в журнале."""
    db = get_db()
    kinds = {k: kind for k, _, kind in BULK_ACTIONS}
    labels = {k: label for k, label, _ in BULK_ACTIONS}
    action = request.form.get("action", "")
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    if action not in kinds or not ids:
        flash("Выберите катушки и действие.", "error")
        return redirect(list_url())
    raw = request.form.get("value", "").strip()
    try:
        if kinds[action] == "num":
            value = float(raw.replace(",", ".").replace(" ", "").replace("₽", "")) if raw else None
            if value is not None and value < 0:
                raise ValueError
        elif kinds[action] == "date":
            value = date.fromisoformat(raw).isoformat() if raw else None
        else:
            value = raw
    except ValueError:
        flash("Неверное значение для «%s»." % labels[action], "error")
        return redirect(list_url())
    today = date.today().isoformat()
    column, sql_value, extra = {
        "location": ("location", value, ""),
        "tare": ("tare", value, ""),
        "purchase_date": ("purchase_date", value, ""),
        "price": ("price", value, ""),
        "opened": ("opened_date", today, " AND opened_date IS NULL"),
        "dried": ("dried_date", today, ""),
        "arrived": ("ordered", 0, " AND ordered = 1"),
    }[action]
    changed = 0
    for sid in ids:
        state = audit_begin(sid)
        if state[0] is None:
            continue
        cur = db.execute("UPDATE spools SET %s = ? WHERE id = ?%s" % (column, extra), (sql_value, sid))
        if cur.rowcount and spool_snapshot(sid) != state[0]:
            audit_end("Массовая правка: " + labels[action].lower(), sid, state)
            changed += 1
    db.commit()
    flash("«%s» — изменено катушек: %d из %d." % (labels[action], changed, len(ids)), "ok")
    return redirect(list_url())


@app.route("/fill", methods=["GET", "POST"])
@admin_required
def fill():
    """Таблица для быстрого заполнения пробелов: правки во всех строках, одна кнопка."""
    db = get_db()
    only_gaps = request.values.get("all") != "1"
    if request.method == "POST":
        saved, errors = 0, []
        for row in db.execute("SELECT * FROM spools").fetchall():
            try:
                new = fill_values(row, request.form)
            except ValueError as e:
                errors.append("%s: %s" % ("№" + row["number"] if row["number"] else row["color_name"], e))
                continue
            if not new:
                continue
            state = audit_begin(row["id"])
            if "weight_left" in new:
                log_usage(db, row, new["weight_left"], new.get("weight_total"), new.get("price"), source="edit")
            db.execute("UPDATE spools SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in new),
                       list(new.values()) + [row["id"]])
            audit_end("Быстрое заполнение", row["id"], state)
            saved += 1
        db.commit()
        if saved:
            flash("Сохранено катушек: %d." % saved, "ok")
        if errors:
            flash("Не сохранено, проверьте: " + "; ".join(errors), "error")
        if not saved and not errors:
            flash("Изменений нет.", "ok")
        return redirect(url_for("fill", all=None if only_gaps else "1"))
    spools = [spool_view(r) for r in db.execute("SELECT * FROM spools ORDER BY " + SORTS["number"])]
    if only_gaps:
        spools = [s for s in spools if not s["price"] or not s["purchase_date"]]
    locations = [r[0] for r in db.execute("SELECT DISTINCT location FROM spools WHERE location != '' ORDER BY 1")]
    return render_template("fill.html", spools=spools, fields=FILL_FIELDS, only_gaps=only_gaps, locations=locations)


@app.route("/spool/<int:spool_id>")
def spool_detail(spool_id):
    row = get_db().execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone()
    if row is None:
        abort(404)
    tare, tare_src = effective_tare(row)
    on_printer = load_config().get("printer_spool") == spool_id
    return render_template("detail.html", s=spool_view(row), tare=tare, tare_src=tare_src,
                           profile=find_profile(row["material"], row["manufacturer"]),
                           profile_fields=PROFILE_FIELDS, on_printer=on_printer,
                           prices=price_comparison(row), accuracy=slicer_accuracy(spool_id),
                           jobs=get_db().execute("SELECT * FROM jobs WHERE spool_id = ? ORDER BY id DESC",
                                                 (spool_id,)).fetchall(),
                           models=get_db().execute("SELECT * FROM models WHERE spool_id = ? ORDER BY id DESC",
                                                   (spool_id,)).fetchall(),
                           prints=get_db().execute("SELECT * FROM prints WHERE spool_id = ? ORDER BY id DESC",
                                                   (spool_id,)).fetchall(),
                           history=get_db().execute("SELECT * FROM usage WHERE spool_id = ? ORDER BY id DESC LIMIT 30",
                                                    (spool_id,)).fetchall())


@app.route("/profiles")
def profiles():
    rows = get_db().execute("SELECT * FROM profiles ORDER BY material, manufacturer").fetchall()
    return render_template("profiles.html", profiles=rows, fields=PROFILE_FIELDS)


@app.route("/profiles/new", methods=["GET", "POST"])
@app.route("/profiles/<int:pid>/edit", methods=["GET", "POST"])
@admin_required
def profile_edit(pid=None):
    db = get_db()
    row = None
    if pid is not None:
        row = db.execute("SELECT * FROM profiles WHERE id = ?", (pid,)).fetchone()
        if row is None:
            abort(404)
    if request.method == "POST":
        if request.form.get("delete") and pid is not None:
            db.execute("DELETE FROM profiles WHERE id = ?", (pid,))
            db.commit()
            flash("Удалено.", "ok")
            return redirect(url_for("profiles"))
        data = {k: request.form.get(k, "").strip() for k in ["material", "manufacturer"] + [f for f, _ in PROFILE_FIELDS]}
        if not data["material"]:
            flash("Укажите материал.", "error")
            return render_template("profile_form.html", p=data, fields=PROFILE_FIELDS, pid=pid)
        try:
            if pid is None:
                db.execute("INSERT INTO profiles (%s) VALUES (%s)" % (", ".join(data), ", ".join("?" * len(data))),
                           list(data.values()))
            else:
                db.execute("UPDATE profiles SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in data),
                           list(data.values()) + [pid])
            db.commit()
        except sqlite3.IntegrityError:
            flash("Настройки для этого материала и производителя уже есть.", "error")
            return render_template("profile_form.html", p=data, fields=PROFILE_FIELDS, pid=pid)
        flash("Сохранено.", "ok")
        return redirect(url_for("profiles"))
    p = dict(row) if row else {"material": request.args.get("material", ""),
                               "manufacturer": request.args.get("manufacturer", "")}
    return render_template("profile_form.html", p=p, fields=PROFILE_FIELDS, pid=pid)


@app.route("/spool/new", methods=["GET", "POST"])
@admin_required
def spool_new():
    if request.method == "POST":
        try:
            data = form_to_spool(request.form)
            data["photo"] = save_photo(request.files.get("photo"))
        except ValueError as e:
            flash(str(e), "error")
            return render_template("form.html", s=request.form, new=True)
        cols = ", ".join(data)
        marks = ", ".join("?" * len(data))
        db = get_db()
        audit_state = audit_begin(None)
        cur = db.execute("INSERT INTO spools (%s) VALUES (%s)" % (cols, marks), list(data.values()))
        audit_end("Добавлена", cur.lastrowid, audit_state)
        db.commit()
        flash("Катушка добавлена.", "ok")
        return redirect(url_for("spool_detail", spool_id=cur.lastrowid))
    copy_id = request.args.get("copy", type=int)
    template_id = request.args.get("template", type=int)
    s = {"number": next_number(), "diameter": 1.75, "weight_total": 1000, "weight_left": 1000, "color_hex": "#888888",
         "purchase_date": date.today().isoformat()}
    db = get_db()
    if copy_id:
        row = db.execute("SELECT * FROM spools WHERE id = ?", (copy_id,)).fetchone()
        if row:
            s = dict(row)
            s.update(number=next_number(), photo=None, opened_date=None, dried_date=None, ordered=0,
                     weight_left=s["weight_total"], purchase_date=date.today().isoformat())
    elif template_id:
        row = db.execute("SELECT data FROM templates WHERE id = ?", (template_id,)).fetchone()
        if row:
            s.update(json.loads(row["data"]))
            s["weight_left"] = s.get("weight_total", 1000)
    templates = db.execute("SELECT id, name FROM templates ORDER BY name").fetchall()
    return render_template("form.html", s=s, new=True, templates=templates, template_id=template_id)


@app.route("/spool/<int:spool_id>/template", methods=["POST"])
@admin_required
def template_save(spool_id):
    """Запомнить катушку как шаблон для быстрого добавления таких же покупок."""
    db = get_db()
    row = db.execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone()
    if row is None:
        abort(404)
    name = request.form.get("name", "").strip()[:80] or " ".join(
        filter(None, [row["manufacturer"], row["material"], row["color_name"]]))
    data = json.dumps({k: row[k] for k in TEMPLATE_FIELDS}, ensure_ascii=False)
    db.execute("INSERT INTO templates (name, data) VALUES (?, ?) "
               "ON CONFLICT(name) DO UPDATE SET data = excluded.data", (name, data))
    db.commit()
    flash("Шаблон «%s» сохранён — он на странице «+ Катушка»." % name, "ok")
    return redirect(url_for("spool_detail", spool_id=spool_id))


@app.route("/templates/<int:template_id>/delete", methods=["POST"])
@admin_required
def template_delete(template_id):
    db = get_db()
    db.execute("DELETE FROM templates WHERE id = ?", (template_id,))
    db.commit()
    flash("Шаблон удалён.", "ok")
    return redirect(url_for("spool_new"))


@app.route("/spool/<int:spool_id>/edit", methods=["GET", "POST"])
@admin_required
def spool_edit(spool_id):
    db = get_db()
    row = db.execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone()
    audit_state = audit_begin(spool_id)
    if row is None:
        abort(404)
    if request.method == "POST":
        try:
            data = form_to_spool(request.form)
            new_photo = save_photo(request.files.get("photo"))
        except ValueError as e:
            flash(str(e), "error")
            return render_template("form.html", s=dict(request.form, id=spool_id, photo=row["photo"]), new=False)
        if new_photo or request.form.get("remove_photo"):
            delete_photo(row["photo"])
            data["photo"] = new_photo
        log_usage(db, row, data["weight_left"], data["weight_total"], data["price"], source="edit")
        sets = ", ".join("%s = ?" % k for k in data)
        db.execute("UPDATE spools SET %s WHERE id = ?" % sets, list(data.values()) + [spool_id])
        audit_end("Изменена", spool_id, audit_state)
        db.commit()
        flash("Сохранено.", "ok")
        return redirect(url_for("spool_detail", spool_id=spool_id))
    return render_template("form.html", s=dict(row), new=False)


@app.route("/history", methods=["GET", "POST"])
@admin_required
def history():
    db = get_db()
    if request.method == "POST":
        entry = db.execute("SELECT * FROM audit WHERE id = ?", (request.form.get("id", type=int),)).fetchone()
        if entry is None:
            abort(404)
        if not audit_undoable(entry):
            flash("Отменить можно только последнее действие с катушкой.", "error")
        else:
            audit_undo(entry)
            db.commit()
            flash("Отменено: %s." % entry["action"].lower(), "ok")
        return redirect(url_for("history", spool=request.args.get("spool")))
    spool = request.args.get("spool", type=int)
    sql = "SELECT * FROM audit" + (" WHERE spool_id = ?" if spool else "") + " ORDER BY id DESC LIMIT 300"
    entries = []
    for e in db.execute(sql, (spool,) if spool else ()):
        d = dict(e)
        snap = json.loads(e["after"] or e["before"] or "{}")
        d["title"] = ("№%s " % snap["number"] if snap.get("number") else "") + "%s %s" % (
            snap.get("material", ""), snap.get("color_name", ""))
        d["changes"] = audit_changes(e)
        d["undoable"] = audit_undoable(e)
        d["exists"] = spool_snapshot(e["spool_id"]) is not None
        entries.append(d)
    return render_template("history.html", entries=entries, spool=spool)


@app.route("/spool/<int:spool_id>/weigh", methods=["POST"])
@admin_required
def spool_weigh(spool_id):
    db = get_db()
    row = db.execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone()
    audit_state = audit_begin(spool_id)
    if row is None:
        abort(404)
    try:
        gross = float(request.form.get("gross", "").replace(",", ".").replace(" ", ""))
        tare = float(request.form.get("tare", "").replace(",", ".").replace(" ", ""))
        if gross < 0 or tare < 0:
            raise ValueError
    except ValueError:
        flash("Введите вес числом, в граммах.", "error")
        return redirect(url_for("spool_detail", spool_id=spool_id))
    left = round(max(0.0, min(gross - tare, row["weight_total"])))
    opened = row["opened_date"]
    if not opened and left < row["weight_total"]:
        opened = date.today().isoformat()  # уже расходовали — значит вскрыта
    log_usage(db, row, left, source="weigh")
    db.execute("UPDATE spools SET weight_left = ?, tare = ?, opened_date = ? WHERE id = ?",
               (left, tare, opened, spool_id))
    if request.form.get("tare_for_all"):
        db.execute("INSERT INTO tares (manufacturer, weight) VALUES (?, ?) "
                   "ON CONFLICT(manufacturer) DO UPDATE SET weight = excluded.weight",
                   (row["manufacturer"], tare))
        # у остальных катушек производителя — общий вес вместо своего
        db.execute("UPDATE spools SET tare = NULL WHERE manufacturer = ? COLLATE NOCASE AND id != ?",
                   (row["manufacturer"], spool_id))
    audit_end("Взвешена", spool_id, audit_state)
    db.commit()
    flash("Остаток: %d г (на весах %g г − катушка %g г)." % (left, gross, tare), "ok")
    return redirect(url_for("spool_detail", spool_id=spool_id))


@app.route("/spool/<int:spool_id>/use", methods=["POST"])
@admin_required
def spool_use(spool_id):
    """Списание по весу из слайсера."""
    db = get_db()
    row = db.execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone()
    audit_state = audit_begin(spool_id)
    if row is None:
        abort(404)
    try:
        grams = float(request.form.get("grams", "").replace(",", ".").replace(" ", ""))
        if grams <= 0:
            raise ValueError
    except ValueError:
        flash("Введите расход числом, в граммах.", "error")
        return redirect(url_for("spool_detail", spool_id=spool_id))
    if over_limit(row, grams):
        return redirect(url_for("spool_detail", spool_id=spool_id))
    left = max(0.0, row["weight_left"] - grams)
    log_usage(db, row, left, source="slicer")
    db.execute("UPDATE spools SET weight_left = ?, opened_date = COALESCE(opened_date, ?) WHERE id = ?",
               (left, date.today().isoformat(), spool_id))
    audit_end("Списание", spool_id, audit_state)
    db.commit()
    flash("Списано %g г, осталось %d г." % (grams, left), "ok")
    return redirect(url_for("spool_detail", spool_id=spool_id))


@app.route("/spool/<int:spool_id>/dried", methods=["POST"])
@admin_required
def spool_dried(spool_id):
    db = get_db()
    audit_state = audit_begin(spool_id)
    db.execute("UPDATE spools SET dried_date = ? WHERE id = ?", (date.today().isoformat(), spool_id))
    audit_end("Высушена", spool_id, audit_state)
    db.commit()
    flash("Отмечено: высушена сегодня.", "ok")
    return redirect(url_for("spool_detail", spool_id=spool_id))


@app.route("/spool/<int:spool_id>/reorder", methods=["POST"])
@admin_required
def spool_reorder(spool_id):
    """«Купить ещё»: копия катушки со статусом «заказана» (ссылка, цена, фото — те же)."""
    db = get_db()
    row = db.execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone()
    if row is None:
        abort(404)
    audit_state = audit_begin(None)
    keep = ["manufacturer", "material", "color_name", "color_hex", "diameter", "weight_total", "price",
            "purchase_url", "location", "tare"]
    data = {k: row[k] for k in keep}
    data.update(weight_left=row["weight_total"], purchase_date=date.today().isoformat(), ordered=1,
                number=next_number(), photo=None)
    if row["photo"] and os.path.exists(os.path.join(UPLOAD_DIR, row["photo"])):
        ext = row["photo"].rsplit(".", 1)[-1]
        data["photo"] = uuid.uuid4().hex + "." + ext
        shutil.copyfile(os.path.join(UPLOAD_DIR, row["photo"]), os.path.join(UPLOAD_DIR, data["photo"]))
    new_id = db.execute("INSERT INTO spools (%s) VALUES (%s)" % (", ".join(data), ", ".join("?" * len(data))),
                        list(data.values())).lastrowid
    audit_end("Заказана", new_id, audit_state)
    db.commit()
    flash("Заказ записан. Когда придёт — нажмите «Пришла».", "ok")
    return redirect(url_for("spool_detail", spool_id=new_id))


@app.route("/spool/<int:spool_id>/arrived", methods=["POST"])
@admin_required
def spool_arrived(spool_id):
    db = get_db()
    audit_state = audit_begin(spool_id)
    db.execute("UPDATE spools SET ordered = 0 WHERE id = ?", (spool_id,))
    audit_end("Пришла", spool_id, audit_state)
    db.commit()
    flash("Катушка в наличии.", "ok")
    return redirect(url_for("spool_detail", spool_id=spool_id))


@app.route("/spool/<int:spool_id>/open", methods=["POST"])
@admin_required
def spool_open(spool_id):
    db = get_db()
    audit_state = audit_begin(spool_id)
    db.execute("UPDATE spools SET opened_date = ? WHERE id = ? AND opened_date IS NULL",
               (date.today().isoformat(), spool_id))
    audit_end("Вскрыта", spool_id, audit_state)
    db.commit()
    flash("Отмечено: вскрыта сегодня.", "ok")
    return redirect(url_for("spool_detail", spool_id=spool_id))


@app.route("/spool/<int:spool_id>/delete", methods=["POST"])
@admin_required
def spool_delete(spool_id):
    db = get_db()
    row = db.execute("SELECT photo FROM spools WHERE id = ?", (spool_id,)).fetchone()
    if row is None:
        abort(404)
    audit_state = audit_begin(spool_id)
    related = {t: [dict(r) for r in db.execute("SELECT * FROM %s WHERE spool_id = ?" % t, (spool_id,))]
               for t in AUDIT_TABLES if t != "spools"}
    db.execute("DELETE FROM prints WHERE spool_id = ?", (spool_id,))
    db.execute("DELETE FROM jobs WHERE spool_id = ?", (spool_id,))
    db.execute("DELETE FROM models WHERE spool_id = ?", (spool_id,))
    db.execute("DELETE FROM usage WHERE spool_id = ?", (spool_id,))
    db.execute("DELETE FROM spools WHERE id = ?", (spool_id,))
    audit_end("Удалена", spool_id, audit_state, deleted=related)
    db.commit()
    # файлы фото не удаляем — удаление можно отменить в журнале
    flash("Катушка удалена.", "ok")
    return redirect(url_for("index"))
