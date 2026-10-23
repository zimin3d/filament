"""Импорт катушек из Google Таблиц / CSV."""
import os

from flask import abort, flash, redirect, render_template, request, send_from_directory, url_for

import importer
from core import *  # noqa: F401,F403
from logic import *  # noqa: F401,F403

# --- импорт из Google Таблиц / CSV -------------------------------------------

@app.route("/import", methods=["GET", "POST"])
@admin_required
def import_start():
    saved_url = load_config().get("sheet_url", "")
    if request.method == "POST":
        url = request.form.get("sheet_url", "").strip()
        file = request.files.get("csv_file")
        try:
            if file and file.filename:
                raw = file.read()
                photos = {}
                if raw[:2] == b"PK":  # .xlsx — это zip
                    text, photos = importer.xlsx_read(raw)
                else:
                    text = importer.decode(raw)
                importer.parse_csv(text)
                token = importer.save_tmp(IMPORT_DIR, text, photos)
            elif url:
                token, photo_error = fetch_to_tmp(url)
                if photo_error:
                    flash("Данные загружены, но фото — нет: %s" % photo_error, "error")
                if url != saved_url:
                    cfg = load_config()
                    cfg["sheet_url"] = url
                    save_config(cfg)
            else:
                raise importer.SheetError("Укажите ссылку на таблицу или выберите файл (Excel или CSV).")
        except importer.SheetError as e:
            flash(str(e), "error")
            return render_template("import.html", sheet_url=url, saved_url=saved_url)
        return redirect(url_for("import_preview", token=token))
    return render_template("import.html", sheet_url=saved_url, saved_url=saved_url)


@app.route("/import/<token>/photo/<int:line>")
@admin_required
def import_photo(token, line):
    try:
        name = importer.tmp_photos(IMPORT_DIR, token).get(line)
        folder = os.path.join(importer.tmp_path(IMPORT_DIR, token), "photos")
    except importer.SheetError:
        abort(404)
    if not name:
        abort(404)
    return send_from_directory(folder, name)


@app.route("/import/<token>", methods=["GET", "POST"])
@admin_required
def import_preview(token):
    try:
        header, body, header_line = importer.parse_csv(importer.load_tmp(IMPORT_DIR, token))
        sheet_photos = importer.tmp_photos(IMPORT_DIR, token)
    except importer.SheetError as e:
        flash(str(e), "error")
        return redirect(url_for("import_start"))
    valid = {k for k, _, _ in importer.FIELDS}
    if request.method == "POST":
        mapping = [request.form.get("map_%d" % i, "") for i in range(len(header))]
        mapping = [m if m in valid else "" for m in mapping]
        skip_dups = bool(request.form.get("skip_dups"))
        use_photos = bool(request.form.get("use_photos"))
        do_update = bool(request.form.get("do_update"))
    else:
        mapping = saved_mapping(header, body)
        skip_dups = use_photos = do_update = True

    plan = plan_import(header, body, mapping, sheet_photos, skip_dups, use_photos, do_update)
    rows, todo, photo_only, updates = plan["rows"], plan["todo"], plan["photo_only"], plan["updates"]

    missing = [importer.FIELD_LABELS[k] for k in ("manufacturer", "material") if k not in mapping]
    if request.form.get("action") == "run":
        if "material" not in mapping:
            flash("Укажите, в какой колонке тип пластика.", "error")
        elif not todo and not photo_only and not updates:
            flash("Нечего импортировать.", "error")
        else:
            cfg = load_config()
            cfg["import_mapping"] = {"header": header, "mapping": mapping}
            save_config(cfg)
            flash(apply_import(token, plan), "ok")
            return redirect(url_for("index", status="all"))

    columns = [{"title": t, "samples": [r[i] for _, r in body if r[i]][:3], "field": mapping[i]}
               for i, t in enumerate(importer.column_titles(header))]
    counts = {"total": len(rows), "new": len(todo), "photo_only": len(photo_only), "updates": len(updates),
              "dups": sum(1 for r in rows if r["duplicate"]),
              "errors": sum(1 for r in rows if r["errors"]),
              "sheet_photos": len(sheet_photos)}
    return render_template("import_preview.html", token=token, columns=columns, rows=rows,
                           fields=importer.FIELDS, counts=counts, skip_dups=skip_dups,
                           use_photos=use_photos, do_update=do_update, header_line=header_line,
                           missing=missing, labels=importer.FIELD_LABELS)
