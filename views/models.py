"""Фото печатей и STL-модели: загрузка, просмотр, превью, галерея, выдача файлов."""
import os
import re
import uuid
from datetime import date

from PIL import Image, ImageOps
from flask import abort, flash, redirect, render_template, request, send_from_directory, url_for

from core import *  # noqa: F401,F403
from logic import *  # noqa: F401,F403

@app.route("/gallery")
def gallery():
    """Все фото печатей и 3D-модели в одной ленте: поиск, фильтр по материалу и виду."""
    db = get_db()
    q = request.args.get("q", "").strip().lower().replace("ё", "е")
    material = request.args.get("material", "")
    kind = request.args.get("kind", "")
    items = []
    if kind in ("", "photo"):
        for r in db.execute("SELECT p.id, p.photo, p.caption AS title, p.created_at, p.spool_id, s.material, s.color_name, "
                            "s.color_hex, s.number FROM prints p JOIN spools s ON s.id = p.spool_id"):
            items.append(dict(r, kind="photo"))
    if kind in ("", "model"):
        for r in db.execute("SELECT m.id, m.file, m.name AS title, m.size, m.created_at, m.spool_id, s.material, "
                            "s.color_name, s.color_hex, s.number FROM models m JOIN spools s ON s.id = m.spool_id"):
            items.append(dict(r, kind="model"))
    if material:
        items = [i for i in items if base_material(i["material"]) == material]
    if q:
        items = [i for i in items if q in " ".join(filter(None, [i["title"], i["color_name"], i["material"]]))
                 .lower().replace("ё", "е") or q.lstrip("№") == (i["number"] or "")]
    items.sort(key=lambda i: i["created_at"], reverse=True)
    materials = sorted({base_material(r[0]) for r in db.execute(
        "SELECT DISTINCT s.material FROM spools s WHERE s.id IN (SELECT spool_id FROM prints UNION SELECT spool_id FROM models)")})
    return render_template("gallery.html", items=items[:300], total=len(items), materials=materials,
                           f={"q": request.args.get("q", ""), "material": material, "kind": kind})


@app.route("/spool/<int:spool_id>/prints", methods=["POST"])
@admin_required
def print_add(spool_id):
    """Запись о печати: название, граммы (списываются с катушки), время, фото модели."""
    db = get_db()
    row = db.execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone()
    audit_state = audit_begin(spool_id)
    if row is None:
        abort(404)
    back = redirect(url_for("spool_detail", spool_id=spool_id) + "#prints")
    fail = redirect(url_for("spool_detail", spool_id=spool_id, panel="print") + "#record")  # с открытой формой
    caption = request.form.get("caption", "").strip()[:200]
    try:
        raw_g = request.form.get("grams", "").replace(",", ".").replace(" ", "")
        grams = float(raw_g) if raw_g else None
        if grams is not None and grams <= 0:
            raise ValueError("Граммы должны быть больше нуля.")
        minutes = parse_duration(request.form.get("duration", ""))
        if grams and over_limit(row, grams):
            return fail
        models = [save_model(f) for f in request.files.getlist("models")]
        photos = [save_photo(f) for f in request.files.getlist("photos")]
    except ValueError as e:
        flash(str(e) if any(w in str(e) for w in ("Фото", "Время", "Граммы", "STL", "stl", "треугольник"))
              else "Граммы — числом.", "error")
        return fail
    photos = [p for p in photos if p]
    models = [m for m in models if m]
    if not photos and not models and grams is None and minutes is None:
        flash("Укажите граммы, время, фото или модель.", "error")
        return fail
    job_id = None
    if grams is not None or minutes is not None:
        job_id = record_job(db, spool_id, date.today().isoformat(), caption, grams, minutes)
    for name in photos:
        db.execute("INSERT INTO prints (spool_id, photo, caption, job_id) VALUES (?, ?, ?, ?)",
                   (spool_id, name, caption, job_id))
    for file, size, triangles, original in models:
        db.execute("INSERT INTO models (spool_id, job_id, name, file, size, triangles) VALUES (?, ?, ?, ?, ?, ?)",
                   (spool_id, job_id, original, file, size, triangles))
        build_preview(file, row["color_hex"])  # сразу, пока это запрос админа
    audit_end("Печать", spool_id, audit_state)
    db.commit()
    msg = []
    if job_id:
        msg.append("Печать записана" + (", списано %g г" % grams if grams else "") + ".")
    if photos:
        msg.append("Фото: %d." % len(photos))
    if models:
        msg.append("Моделей: %d." % len(models))
    flash(" ".join(msg), "ok")
    return back


@app.route("/model/<int:model_id>")
def model_view(model_id):
    """3D-просмотр STL (three.js, свой — CSP пускает только наши скрипты)."""
    m = get_db().execute("SELECT m.*, s.color_hex, s.material, s.color_name, s.number FROM models m "
                         "JOIN spools s ON s.id = m.spool_id WHERE m.id = ?", (model_id,)).fetchone()
    if m is None:
        abort(404)
    return render_template("model.html", m=m)


@app.route("/model/<int:model_id>/preview.png")
def model_preview(model_id):
    """Картинка модели. Считается при загрузке (админом); здесь — только отдать готовую.
    Построить на лету может лишь админ: тяжёлый расчёт (до ~10 с) по запросу гостя — способ
    занять все рабочие процессы."""
    m = get_db().execute("SELECT m.file, s.color_hex FROM models m JOIN spools s ON s.id = m.spool_id WHERE m.id = ?",
                         (model_id,)).fetchone()
    if m is None:
        abort(404)
    name = preview_name(m["file"])
    if not os.path.exists(os.path.join(THUMB_DIR, name)):
        if not is_admin() or not build_preview(m["file"], m["color_hex"]):
            abort(404)
    return send_from_directory(THUMB_DIR, name, max_age=86400 * 30)


@app.route("/stl")
def stl_local():
    """Просмотр STL с компьютера/телефона — файл не уходит на сервер."""
    return render_template("model.html", m=None)


@app.route("/models/<int:model_id>/delete", methods=["POST"])
@admin_required
def model_delete(model_id):
    db = get_db()
    m = db.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
    if m is None:
        abort(404)
    state = audit_begin(m["spool_id"])
    db.execute("DELETE FROM models WHERE id = ?", (model_id,))
    # строка — в журнал (можно отменить), файл чистится через UNDO_DAYS дней (cleanup)
    audit_end("Удалена модель", m["spool_id"], state, deleted={"models": [dict(m)]})
    db.commit()
    flash("Модель «%s» удалена. Отменить можно в журнале." % m["name"], "ok")
    return redirect(url_for("spool_detail", spool_id=m["spool_id"]) + "#models")


@app.route("/prints/<int:print_id>/delete", methods=["POST"])
@admin_required
def print_delete(print_id):
    db = get_db()
    row = db.execute("SELECT spool_id, photo FROM prints WHERE id = ?", (print_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("DELETE FROM prints WHERE id = ?", (print_id,))
    db.commit()
    delete_photo(row["photo"])
    flash("Фото удалено.", "ok")
    return redirect(url_for("spool_detail", spool_id=row["spool_id"]) + "#prints")


@app.route("/thumbs/<name>")
def thumb(name):
    """Уменьшенная копия фото для списка; создаётся при первом запросе."""
    if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|jpeg|png|webp|gif)", name):
        abort(404)
    src = os.path.join(UPLOAD_DIR, name)
    dst = os.path.join(THUMB_DIR, thumb_name(name))
    if not os.path.exists(dst):
        if not os.path.exists(src):
            abort(404)
        os.makedirs(THUMB_DIR, exist_ok=True)
        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((THUMB_SIZE, THUMB_SIZE))
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                tmp = "%s.%s.tmp" % (dst, uuid.uuid4().hex[:8])
                im.convert("RGB").save(tmp, "JPEG", quality=82)
                os.replace(tmp, dst)
        except (OSError, ValueError):
            return send_from_directory(UPLOAD_DIR, name, max_age=86400 * 30)
    return send_from_directory(THUMB_DIR, thumb_name(name), max_age=86400 * 30)


@app.route("/uploads/<path:name>")
def uploaded(name):
    return send_from_directory(UPLOAD_DIR, name, max_age=86400 * 30)
