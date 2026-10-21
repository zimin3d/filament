"""Сводки: остатки, CSV, витрина, отчёт, статистика, «хватит ли», CFS, этикетки, сканер."""
import csv
import io
import secrets
from datetime import date

from flask import Response, abort, flash, redirect, render_template, request, url_for

import labelpng
from core import *  # noqa: F401,F403
from logic import *  # noqa: F401,F403

@app.route("/stock")
def stock():
    """Сводка по материалам и список катушек на исходе."""
    try:
        threshold = max(0, int(request.args.get("min", 200)))
    except ValueError:
        threshold = 200
    spools = [spool_view(r) for r in get_db().execute("SELECT * FROM spools ORDER BY " + SORTS["number"])]
    by_material = {}
    for s in spools:
        m = by_material.setdefault(s["material"], {"material": s["material"], "count": 0, "empty": 0,
                                                   "left": 0, "value": 0})
        if s["status"] == "ordered":
            continue
        if s["status"] == "empty":
            m["empty"] += 1
            continue
        m["count"] += 1
        m["left"] += s["weight_left"]
        m["value"] += s["value_left"] or 0
    materials = sorted(by_material.values(), key=lambda m: -m["left"])
    ordered = [s for s in spools if s["status"] == "ordered"]
    # на исходе; если такая же уже заказана — отмечаем
    ordered_keys = {(o["manufacturer"].lower(), o["material"].lower(), o["color_name"].lower()) for o in ordered}
    low = sorted((s for s in spools if s["weight_left"] < threshold and s["status"] != "ordered"),
                 key=lambda s: s["weight_left"])
    for s in low:
        s["prices"] = price_comparison(s)
        s["reordered"] = (s["manufacturer"].lower(), s["material"].lower(), s["color_name"].lower()) in ordered_keys
    months = [dict(r) for r in get_db().execute(
        "SELECT substr(day, 1, 7) AS month, SUM(used) AS used, SUM(cost) AS cost FROM usage "
        "GROUP BY month ORDER BY month DESC LIMIT 24")]
    peak = max([m["used"] for m in months if m["used"] > 0] or [1])
    for m in months:
        m["pct"] = max(0, round(m["used"] / peak * 100))
    token = load_config().get("showcase_token") if is_admin() else None
    return render_template("stock.html", materials=materials, low=low, threshold=threshold, months=months,
                           ordered=ordered, showcase_url=url_for("showcase", token=token, _external=True) if token else None)


@app.route("/export.csv")
def export_csv():
    """Выгрузка (с фильтрами списка) — открывается в Excel и Google Таблицах, читается импортом."""
    spools, _ = filtered_spools(request.args)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([title for _, title in EXPORT_COLUMNS])
    for s in spools:
        row = []
        for key, _ in EXPORT_COLUMNS:
            v = s[key]
            if key == "photo":
                v = url_for("uploaded", name=v, _external=True) if v else ""
            elif key in ("purchase_date", "opened_date"):
                v = ru_date(v) if v else ""
            elif isinstance(v, float):
                v = "{:g}".format(v).replace(".", ",")
            row.append("" if v is None else v)
        w.writerow(row)
    name = "filament-%s.csv" % date.today().isoformat()
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=" + name})


@app.route("/showcase/<token>")
def showcase(token):
    """Публичная витрина по секретной ссылке: что есть в наличии — без цен, ссылок, заметок,
    мест и пометок. Работает и при пароле на просмотр."""
    current = load_config().get("showcase_token")
    if not current or not secrets.compare_digest(token, current):
        abort(404)
    groups = {}
    for r in get_db().execute("SELECT * FROM spools WHERE ordered = 0 AND weight_left > 0 ORDER BY " + SORTS["material"]):
        s = spool_view(r)
        level = "полная" if s["status"] == "sealed" else "больше половины" if s["percent_left"] >= 50 \
            else "меньше половины" if s["percent_left"] >= 15 else "почти закончилась"
        groups.setdefault(base_material(s["material"]), []).append(
            {"swatch": s["swatch"], "color": s["color_name"], "manufacturer": s["manufacturer"],
             "material": s["material"], "level": level, "pct": s["percent_left"]})
    resp = app.make_response(render_template("showcase.html", groups=sorted(groups.items()),
                                             total=sum(len(v) for v in groups.values())))
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@app.route("/showcase-settings", methods=["POST"])
@admin_required
def showcase_settings():
    cfg = load_config()
    if request.form.get("action") == "off":
        cfg.pop("showcase_token", None)
        flash("Витрина выключена — старая ссылка больше не работает.", "ok")
    else:
        cfg["showcase_token"] = secrets.token_urlsafe(12)
        flash("Новая ссылка на витрину создана (старая перестала работать).", "ok")
    save_config(cfg)
    return redirect(url_for("stock") + "#showcase")


@app.route("/report")
def report():
    """Годовой отчёт для печати / PDF (в браузере: Печать → Сохранить как PDF)."""
    db = get_db()
    years = sorted({int(r[0]) for r in db.execute(
        "SELECT substr(purchase_date, 1, 4) FROM spools WHERE purchase_date IS NOT NULL "
        "UNION SELECT substr(day, 1, 4) FROM usage UNION SELECT substr(day, 1, 4) FROM jobs") if r[0]},
        reverse=True) or [date.today().year]
    year = request.args.get("year", type=int) or years[0]
    y = "%04d" % year
    bought = db.execute("SELECT COUNT(*), COALESCE(SUM(price), 0), COALESCE(SUM(weight_total), 0) FROM spools "
                        "WHERE substr(purchase_date, 1, 4) = ? AND ordered = 0", (y,)).fetchone()
    used = db.execute("SELECT COALESCE(SUM(used), 0), COALESCE(SUM(cost), 0) FROM usage "
                      "WHERE substr(day, 1, 4) = ?", (y,)).fetchone()
    jobs_ = db.execute("SELECT COUNT(*), COALESCE(SUM(minutes), 0) FROM jobs WHERE substr(day, 1, 4) = ?", (y,)).fetchone()
    months = []
    for m in range(1, 13):
        key = "%s-%02d" % (y, m)
        b = db.execute("SELECT COUNT(*), COALESCE(SUM(price), 0) FROM spools WHERE substr(purchase_date, 1, 7) = ? "
                       "AND ordered = 0", (key,)).fetchone()
        u = db.execute("SELECT COALESCE(SUM(used), 0), COALESCE(SUM(cost), 0) FROM usage WHERE substr(day, 1, 7) = ?",
                       (key,)).fetchone()
        j = db.execute("SELECT COUNT(*) FROM jobs WHERE substr(day, 1, 7) = ?", (key,)).fetchone()
        months.append({"m": m, "bought_n": b[0], "bought": b[1], "used": u[0], "cost": u[1], "jobs": j[0]})
    peak = max([m["used"] for m in months] + [1])
    for m in months:
        m["pct"] = round(m["used"] / peak * 100)
    by_material = [dict(r) for r in db.execute(
        "SELECT s.material AS name, SUM(u.used) AS used, SUM(u.cost) AS cost FROM usage u "
        "JOIN spools s ON s.id = u.spool_id WHERE substr(u.day, 1, 4) = ? GROUP BY s.material "
        "HAVING SUM(u.used) > 0 ORDER BY 2 DESC", (y,))]
    finished = [spool_view(r) for r in db.execute(
        "SELECT s.* FROM spools s WHERE s.weight_left <= 0 AND EXISTS (SELECT 1 FROM usage u "
        "WHERE u.spool_id = s.id AND substr(u.day, 1, 4) = ? AND u.left_after <= 0)", (y,))]
    return render_template("report.html", year=year, years=years, months=months, by_material=by_material,
                           finished=finished, today=date.today().isoformat(), totals={
                               "bought_n": bought[0], "bought": bought[1], "bought_kg": bought[2] / 1000,
                               "used": used[0], "used_cost": used[1], "jobs": jobs_[0], "minutes": jobs_[1]})


@app.route("/stats")
def stats():
    """Сводка: по месяцам (куплено / израсходовано / печатей), лидеры расхода, цены по производителям."""
    db = get_db()
    months = {}

    def month(m):
        return months.setdefault(m, {"month": m, "bought": 0, "bought_n": 0, "used": 0, "used_cost": 0, "jobs": 0, "minutes": 0})
    for m, total, n in db.execute("SELECT substr(purchase_date, 1, 7), SUM(price), COUNT(*) FROM spools "
                                  "WHERE purchase_date IS NOT NULL GROUP BY 1"):
        month(m).update(bought=total or 0, bought_n=n)
    for m, used, cost in db.execute("SELECT substr(day, 1, 7), SUM(used), SUM(cost) FROM usage GROUP BY 1"):
        month(m).update(used=used or 0, used_cost=cost or 0)
    for m, n, mins in db.execute("SELECT substr(day, 1, 7), COUNT(*), SUM(minutes) FROM jobs GROUP BY 1"):
        month(m).update(jobs=n, minutes=mins or 0)
    months = sorted(months.values(), key=lambda x: x["month"], reverse=True)[:24]
    peak = max([m["used"] for m in months] + [1])
    for m in months:
        m["pct"] = max(0, round(m["used"] / peak * 100))

    def top(group_sql):
        rows = [dict(r) for r in db.execute(
            "SELECT %s AS name, MIN(s.color_hex) AS hex, SUM(u.used) AS used, SUM(u.cost) AS cost "
            "FROM usage u JOIN spools s ON s.id = u.spool_id GROUP BY 1 HAVING SUM(u.used) > 0 "
            "ORDER BY 3 DESC LIMIT 10" % group_sql)]
        peak = max([r["used"] for r in rows] + [1])
        for r in rows:
            r["pct"] = round(r["used"] / peak * 100)
        return rows
    prices = [dict(r) for r in db.execute(
        "SELECT manufacturer, material, COUNT(*) AS n, AVG(price * 1000.0 / weight_total) AS per_kg, "
        "MIN(price * 1000.0 / weight_total) AS min_kg, MAX(price * 1000.0 / weight_total) AS max_kg "
        "FROM spools WHERE price > 0 AND weight_total > 0 GROUP BY manufacturer COLLATE NOCASE, material "
        "ORDER BY material, per_kg")]
    accuracy = {}
    for a in slicer_accuracy():
        k = (a["manufacturer"], base_material(a["material"]))
        g = accuracy.setdefault(k, {"manufacturer": k[0], "material": k[1], "slicer": 0, "actual": 0, "n": 0})
        g["slicer"] += a["slicer"]
        g["actual"] += a["actual"]
        g["n"] += 1
    accuracy = sorted(accuracy.values(), key=lambda g: -abs(g["actual"] / g["slicer"] - 1))
    for g in accuracy:
        g["diff"] = round((g["actual"] / g["slicer"] - 1) * 100)
    totals = db.execute("SELECT COUNT(*), SUM(price), SUM(CASE WHEN price IS NULL THEN 1 ELSE 0 END) FROM spools").fetchone()
    return render_template("stats.html", months=months, top_materials=top("s.material"),
                           top_colors=top("lower(s.color_name)"), prices=prices, accuracy=accuracy,
                           totals={"count": totals[0], "spent": totals[1] or 0, "no_price": totals[2] or 0})


@app.route("/labels")
def labels():
    """Этикетки с QR-кодом для печати: по фильтрам списка или ?id=…"""
    ids = request.args.getlist("id", type=int)
    if ids:
        marks = ",".join("?" * len(ids))
        spools = [spool_view(r) for r in get_db().execute(
            "SELECT * FROM spools WHERE id IN (%s) ORDER BY %s" % (marks, SORTS["number"]), ids)]
    else:
        spools, _ = filtered_spools(request.args)
    for s in spools:
        # короткая ссылка — код реже и крупнее, лучше читается с термоэтикетки
        s["qr"] = qr_svg(url_for("short_link", spool_id=s["id"], _external=True))
    return render_template("labels.html", spools=spools, autoprint=request.args.get("print") == "1",
                           sizes=LABEL_SIZES)


@app.route("/label/<int:spool_id>.png")
def label_png(spool_id):
    """Этикетка картинкой под ленту Niimbot/Phomemo: ?w=40&h=30 (мм), dpi=203."""
    row = get_db().execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone()
    if row is None:
        abort(404)
    s = spool_view(row)
    w = min(150, max(15, request.args.get("w", 40, type=float)))
    h = min(150, max(10, request.args.get("h", 30, type=float)))
    dpi = request.args.get("dpi", 203, type=int) if request.args.get("dpi", type=int) in (203, 300) else 203
    small = h < 25 or w < 35
    lines = [("№" + s["number"] if s["number"] else "", 0.22), (s["material"], 0.15)]
    if not small:
        lines += [(s["color_name"], 0.1), ("⚠ " + s["alert"] if s["alert"] else "", 0.09), (s["manufacturer"], 0.09)]
    png = labelpng.render(url_for("short_link", spool_id=spool_id, _external=True), lines, w, h, dpi)
    name = "label-%s.png" % (s["number"] or spool_id)
    return Response(png, mimetype="image/png", headers={"Content-Disposition": "inline; filename=" + name,
                                                        "Cache-Control": "no-store"})


@app.route("/cfs")
def cfs():
    """Какие катушки поставить в ячейки CFS для многоцветной печати."""
    requests = []
    for i in range(1, CFS_MAX_SLOTS + 1):
        color = valid_hex(request.args.get("c%d" % i, ""), "")
        material = request.args.get("m%d" % i, "").strip()[:20]
        raw = request.args.get("g%d" % i, "").replace(",", ".").strip()
        try:
            grams = float(raw) if raw else None
        except ValueError:
            grams = None
        if color or material or grams:
            requests.append({"slot": i, "color": color, "material": material, "grams": grams})
    plan = cfs_plan(requests) if requests else []
    materials = sorted({base_material(r[0]) for r in get_db().execute("SELECT DISTINCT material FROM spools")})
    return render_template("cfs.html", plan=plan, requests=requests, materials=materials,
                           rows=max(4, len(requests)), max_rows=CFS_MAX_SLOTS)


@app.route("/scan")
def scan():
    """Сканер QR этикеток камерой телефона."""
    return render_template("scan.html")


@app.route("/s/<int:spool_id>")
def short_link(spool_id):
    """Короткий адрес для QR на этикетке."""
    return redirect(url_for("spool_detail", spool_id=spool_id))


@app.route("/enough")
def enough():
    """Какие катушки потянут печать на N грамм: сначала вскрытые, потом с меньшим остатком."""
    raw = request.args.get("grams", "").replace(",", ".").replace(" ", "")
    material = request.args.get("material", "").strip()
    color = request.args.get("color", "").strip().lower().replace("ё", "е")
    grams = None
    try:
        grams = float(raw) if raw else None
    except ValueError:
        flash("Граммы — числом.", "error")
    fit, close = [], []
    if grams and grams > 0:
        need = grams * (1 + ENOUGH_RESERVE)
        base = base_material(material) if material else None
        for r in get_db().execute("SELECT * FROM spools WHERE ordered = 0 AND weight_left > 0"):
            s = spool_view(r)
            if base and base_material(s["material"]) != base:
                continue
            if color and color not in (s["color_name"] or "").lower().replace("ё", "е"):
                continue
            s["after"] = s["weight_left"] - grams
            if s["weight_left"] >= need:
                fit.append(s)
            elif s["weight_left"] >= grams * 0.7:
                close.append(s)
        order = lambda s: (s["status"] != "opened", s["weight_left"])
        fit.sort(key=order)
        close.sort(key=lambda s: -s["weight_left"])
    materials = sorted({base_material(r[0]) for r in get_db().execute("SELECT DISTINCT material FROM spools")})
    return render_template("enough.html", grams=grams, material=material, color=request.args.get("color", ""),
                           fit=fit, close=close, materials=materials, reserve=int(ENOUGH_RESERVE * 100))
