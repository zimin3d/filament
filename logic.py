"""Предметная логика: фильтры списка, цвета, журнал с отменой, расход, фото/модели/превью, импорт."""
import functools
import json
import os
import re
import shutil
import struct
import uuid
from datetime import date

import qrcode
import qrcode.image.svg
from PIL import Image, ImageOps
from flask import flash, request, session, url_for
from markupsafe import Markup

import importer
import stlpreview
from core import *  # noqa: F401,F403

@app.context_processor
def inject_globals():
    def known_locations():
        return [r[0] for r in get_db().execute(
            "SELECT DISTINCT location FROM spools WHERE location != '' ORDER BY location COLLATE NOCASE")]

    def known_manufacturers():
        return [r[0] for r in get_db().execute(
            "SELECT DISTINCT manufacturer FROM spools ORDER BY manufacturer COLLATE NOCASE")]
    theme = request.cookies.get("theme", "") if has_request_context() else ""
    return {"is_admin": is_admin(), "csrf_token": csrf_token, "phone": PHONE,
            "theme": theme if theme in ("dark", "light", "system") else "",
            "sheet_url": load_config().get("sheet_url", "") if is_admin() else "",
            "inbox_count": get_db().execute("SELECT COUNT(*) FROM inbox WHERE status = 'pending'").fetchone()[0]
            if is_admin() else 0, "materials": MATERIALS,
            "maint_due": sum(1 for m in maintenance_status()[1] if m["due"]) if is_admin() else 0,
            "known_manufacturers": known_manufacturers, "known_locations": known_locations, "list_url": list_url}


# --- страницы ---------------------------------------------------------------

SORTS = {
    "new": "id DESC",
    "number": "number = '', CAST(number AS INTEGER), number",
    "manufacturer": "manufacturer COLLATE NOCASE, material, color_name",
    "material": "material, manufacturer COLLATE NOCASE, color_name",
    "left": "weight_left ASC",
    "purchase": "purchase_date DESC",
}


LIST_ARGS = ("q", "material", "manufacturer", "location", "status", "sort", "color")
COLOR_NEAR = 0.12     # расстояние в OKLab: «похожий оттенок»
PALETTE_MERGE = 0.07  # образцы ближе этого в палитре сливаются в один
UNKNOWN_HEX = "#888888"


def oklab(hex_):
    """#rrggbb -> (L, a, b) в OKLab — расстояние в нём близко к тому, как различает глаз."""
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(int(hex_[i:i + 2], 16)) for i in (1, 3, 5))
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def color_distance(h1, h2):
    a, b = oklab(h1), oklab(h2)
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def spool_colors(s):
    """Цвета катушки: выбранный образец + цвета из названия (для многоцветных)."""
    out = [s["color_hex"]] if s["color_hex"] and s["color_hex"] != UNKNOWN_HEX else []
    for h in importer.colors_from_name(s["color_name"] or ""):
        if h not in out:
            out.append(h)
    return [h for h in out if re.fullmatch(r"#[0-9a-fA-F]{6}", h)]


def palette(spools):
    """Образцы для поиска по цвету: близкие оттенки сливаются, у каждого — сколько катушек."""
    groups = []
    for s in spools:
        for h in spool_colors(s)[:1]:
            for g in groups:
                if color_distance(g["hex"], h) < PALETTE_MERGE:
                    g["n"] += 1
                    break
            else:
                groups.append({"hex": h.lower(), "n": 1})
    return sorted(groups, key=lambda g: oklab(g["hex"])[0])


def list_url():
    return url_for("index", **session.get("list_args", {}))


def filtered_spools(args):
    db = get_db()
    material = args.get("material", "")
    manufacturer = args.get("manufacturer", "")
    location = args.get("location", "")
    color = args.get("color", "").strip().lower()
    if not re.fullmatch(r"#[0-9a-f]{6}", color):
        color = ""
    status = args.get("status", "active")
    q = args.get("q", "").strip()
    sort = args.get("sort", "new")

    where, params = [], []
    if material:
        where.append("material = ?")
        params.append(material)
    if location:
        where.append("location = ?")
        params.append(location)
    if manufacturer:
        where.append("manufacturer = ?")
        params.append(manufacturer)
    if q:
        where.append("(number = ? OR ulower(manufacturer) LIKE ? OR ulower(color_name) LIKE ? OR ulower(notes) LIKE ?"
                     " OR ulower(material) LIKE ? OR ulower(location) LIKE ? OR ulower(alert) LIKE ?)")
        params += [q.lstrip("№#").strip()] + ["%" + q.lower() + "%"] * 6
    sql = "SELECT * FROM spools"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + SORTS.get(sort, SORTS["new"])

    spools = [spool_view(r) for r in db.execute(sql, params)]
    if status == "active":
        spools = [s for s in spools if s["status"] not in ("empty", "ordered")]
    elif status == "all":  # закончившиеся — на отдельной странице /empty
        spools = [s for s in spools if s["status"] != "empty"]
    elif status in ("sealed", "opened", "empty", "ordered"):
        spools = [s for s in spools if s["status"] == status]
    if color:  # похожие оттенки, ближайшие сверху
        for s in spools:
            s["color_dist"] = min([color_distance(color, h) for h in spool_colors(s)] or [9])
        spools = sorted((s for s in spools if s["color_dist"] < COLOR_NEAR), key=lambda s: s["color_dist"])
    f = {"material": material, "manufacturer": manufacturer, "location": location, "status": status,
         "q": q, "sort": sort, "color": color}
    return spools, f


# действие -> (подпись, тип значения: text/num/date или None — без значения)
BULK_ACTIONS = [
    ("location", "Место хранения", "text"),
    ("tare", "Вес пустой катушки, г", "num"),
    ("purchase_date", "Дата покупки", "date"),
    ("price", "Цена, ₽", "num"),
    ("opened", "Вскрыты сегодня", None),
    ("dried", "Высушены сегодня", None),
    ("arrived", "Заказ пришёл", None),
]


EXPORT_COLUMNS = [  # заголовки совпадают с теми, что узнаёт импорт
    ("number", "№"), ("manufacturer", "Производитель"), ("material", "Тип пластика"),
    ("color_name", "Цвет"), ("color_hex", "Код цвета hex"), ("diameter", "Диаметр, мм"),
    ("weight_total", "Вес, г"), ("weight_left", "Остаток, г"), ("price", "Цена, ₽"),
    ("purchase_date", "Дата покупки"), ("opened_date", "Дата вскрытия"),
    ("purchase_url", "Ссылка на покупку"), ("location", "Место хранения"), ("alert", "Важно"), ("photo", "Фото"), ("notes", "Заметки"),
]


FILL_FIELDS = [("number", "№", "text"), ("price", "Цена, ₽", "num"), ("purchase_date", "Куплена", "date"),
               ("weight_total", "Вес, г", "num"), ("weight_left", "Остаток, г", "num"),
               ("opened_date", "Вскрыта", "date"), ("location", "Место", "text")]


def fill_values(row, form):
    """Значения из строки таблицы заполнения, которые отличаются от базы."""
    new = {}
    for key, label, kind in FILL_FIELDS:
        name = "f_%d_%s" % (row["id"], key)
        if name not in form:
            continue
        raw = form[name].strip()
        if kind == "num":
            raw = raw.replace(",", ".").replace(" ", "").replace("₽", "")
            try:
                val = float(raw) if raw else None
            except ValueError:
                raise ValueError("«%s» — не число" % label)
            if val is not None and val < 0:
                raise ValueError("«%s» меньше нуля" % label)
            if key in ("weight_total", "weight_left") and val is None:
                continue  # вес пустым не бывает — пустое поле не трогаем
        elif kind == "date":
            try:
                val = date.fromisoformat(raw).isoformat() if raw else None
            except ValueError:
                raise ValueError("«%s» — неверная дата" % label)
        else:
            val = raw.lstrip("№#").strip() if key == "number" else raw
        old = row[key]
        same = val == old or (isinstance(val, float) and old is not None and abs(val - old) < 1e-9)
        if not same:
            new[key] = val
    total = new.get("weight_total", row["weight_total"])
    if total <= 0:
        raise ValueError("вес должен быть больше нуля")
    if new.get("weight_left", 0) > total:
        raise ValueError("остаток больше веса")
    return new


def slicer_accuracy(spool_id=None):
    """План/факт: между двумя взвешиваниями расход по слайсеру сравнивается с реальным.
    Взвешивание после печатей записывает поправку = факт − слайсер. Ручные правки остатка
    обрывают интервал (неизвестно, что было)."""
    sql = "SELECT u.*, s.material, s.manufacturer FROM usage u JOIN spools s ON s.id = u.spool_id"
    rows = get_db().execute(sql + (" WHERE u.spool_id = ?" if spool_id else "") + " ORDER BY u.spool_id, u.id",
                            (spool_id,) if spool_id else ()).fetchall()
    out, acc, cur = [], 0.0, None
    for r in rows:
        if r["spool_id"] != cur:
            cur, acc = r["spool_id"], 0.0
        if r["source"] in ("slicer", "job"):
            acc += r["used"]
        elif r["source"] == "weigh":
            if acc >= 5:  # меньше 5 г — тонет в погрешности весов
                out.append({"spool_id": r["spool_id"], "material": r["material"], "manufacturer": r["manufacturer"],
                            "slicer": acc, "actual": acc + r["used"], "day": r["day"]})
            acc = 0.0
        else:
            acc = 0.0
    return out


def printer_hours():
    """Наработка принтера, часы: записанные печати + печати с принтера, не ставшие записями
    (импортированная история, пропущенные и ещё не разобранные во «Входящих»)."""
    db = get_db()
    jobs_min = db.execute("SELECT COALESCE(SUM(minutes), 0) FROM jobs").fetchone()[0] or 0
    other = db.execute("SELECT COALESCE(SUM(minutes), 0) FROM inbox WHERE status != 'done'").fetchone()[0] or 0
    return (jobs_min + other) / 60


@functools.lru_cache(maxsize=1)
def agent_module():
    """agent/k2_agent.py как модуль — одна логика разбора истории Moonraker для агента и импорта."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("k2_agent", os.path.join(BASE_DIR, "agent", "k2_agent.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def maintenance_status():
    hours = printer_hours()
    today = date.today()
    items = []
    for r in get_db().execute("SELECT * FROM maintenance ORDER BY id"):
        m = dict(r)
        parts = []
        if m["every_hours"]:
            done = hours - m["last_hours"]
            parts.append(done / m["every_hours"])
            m["hours_left"] = m["every_hours"] - done
        if m["every_days"] and m["last_date"]:
            days = (today - date.fromisoformat(m["last_date"])).days
            parts.append(days / m["every_days"])
            m["days_left"] = m["every_days"] - days
        m["pct"] = min(100, round(max(parts or [0]) * 100))
        m["due"] = bool(parts) and max(parts) >= 1
        m["soon"] = not m["due"] and bool(parts) and max(parts) >= 0.85
        items.append(m)
    return hours, items


QR_QUIET = 2  # белое поле вокруг, в модулях (у этикетки и так белые края)


def qr_svg(data):
    """QR одним контуром: соседние модули строки сливаются в один прямоугольник, crispEdges —
    без белых щелей между квадратиками, которые термопринтер печатает и сбивают сканер."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix) + 2 * QR_QUIET
    path = []
    for y, row in enumerate(matrix):
        x = 0
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                path.append("M%d %dh%dv1h-%dz" % (start + QR_QUIET, y + QR_QUIET, x - start, x - start))
            else:
                x += 1
    return Markup('<svg class="qr" viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" shape-rendering="crispEdges" '
                  'role="img" aria-label="QR-код"><rect width="%d" height="%d" fill="#fff"/><path d="%s" fill="#000"/></svg>'
                  % (n, n, n, n, "".join(path)))


# типовые размеры термоэтикеток, мм (ширина × высота)
LABEL_SIZES = [(58, 40), (58, 30), (50, 30), (40, 30), (30, 20), (100, 50), (60, 40)]


CFS_PROBLEM = re.compile(r"cfs|cms|не\s*лез", re.I)  # пометка «не лезет в CFS» и похожие
CFS_MAX_SLOTS = 16  # до 4 блоков CFS по 4 ячейки


def cfs_plan(requests):
    """Подбор катушек под филаменты проекта. requests: [{color, material, grams}].
    Сначала самые «тяжёлые» филаменты; одна катушка — в одну ячейку. Критерии: хватает остатка
    (+5%), материал, похожий цвет (OKLab), без пометки «не лезет в CFS», вскрытые раньше, меньший остаток раньше."""
    spools = [spool_view(r) for r in get_db().execute("SELECT * FROM spools WHERE ordered = 0 AND weight_left > 0")]
    taken, plan = set(), [None] * len(requests)
    order = sorted(range(len(requests)), key=lambda i: -(requests[i]["grams"] or 0))
    for i in order:
        req = requests[i]
        need = (req["grams"] or 0) * (1 + ENOUGH_RESERVE)
        base = base_material(req["material"]) if req["material"] else None
        options = []
        for s in spools:
            if s["id"] in taken or s["weight_left"] < need:
                continue
            if base and base_material(s["material"]) != base:
                continue
            dist = min([color_distance(req["color"], h) for h in spool_colors(s)] or [1]) if req["color"] else 0
            s = dict(s, dist=dist, cfs_problem=bool(CFS_PROBLEM.search(s["alert"] or "")),
                     match="тот же цвет" if dist < 0.05 else "похожий" if dist < COLOR_NEAR else "другой оттенок")
            options.append(s)
        # сперва похожие по цвету, среди них — без «не лезет в CFS», затем ближе по оттенку, вскрытые, меньший остаток
        options.sort(key=lambda s: (s["dist"] >= COLOR_NEAR, s["cfs_problem"], round(s["dist"] / 0.05),
                                    s["status"] != "opened", s["weight_left"]))
        if options and not options[0]["cfs_problem"]:
            taken.add(options[0]["id"])
        plan[i] = {"request": req, "best": options[0] if options else None, "alternatives": options[1:4]}
    return plan


BASE_MATERIALS = ["PLA+", "PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "HIPS", "PVA"]


def base_material(material):
    """«CR-PETG», «PETG+» -> PETG; «EN-PLA+» -> PLA+ (для сравнения цен)."""
    mat = material.upper()
    for m in BASE_MATERIALS:  # PLA+ раньше PLA
        if m in mat:
            return m
    return material


def price_comparison(spool):
    """Цена за кг этой катушки против остальных того же материала: средняя и самая дешёвая."""
    if not spool["price"] or not spool["weight_total"]:
        return None
    base = base_material(spool["material"])
    offers = {}
    for r in get_db().execute("SELECT manufacturer, material, price * 1000.0 / weight_total AS kg FROM spools "
                              "WHERE price > 0 AND weight_total > 0"):
        if base_material(r["material"]) == base:
            offers.setdefault(r["manufacturer"], []).append(r["kg"])
    if sum(len(v) for v in offers.values()) < 2:
        return None
    mine = spool["price"] * 1000 / spool["weight_total"]
    avg = sum(sum(v) for v in offers.values()) / sum(len(v) for v in offers.values())
    cheapest_mf, cheapest = min(((mf, min(v)) for mf, v in offers.items()), key=lambda x: x[1])
    return {"base": base, "mine": mine, "avg": avg, "diff": round((mine - avg) / avg * 100),
            "cheapest_mf": cheapest_mf, "cheapest": cheapest,
            "is_cheapest": abs(mine - cheapest) < 0.5}


def find_profile(material, manufacturer):
    """Настройки для катушки: материал+производитель, иначе материал; для «CR-PETG»,
    «EN-PLA+» и т.п. — самый длинный известный материал внутри названия."""
    profiles = [dict(r) for r in get_db().execute("SELECT * FROM profiles")]
    mat, mf = material.strip().lower(), manufacturer.strip().lower()

    def rank(p):
        pm = p["material"].lower()
        if pm == mat:
            score = 2000
        elif pm and pm in mat:
            score = 1000 + len(pm)
        else:
            return None
        if p["manufacturer"]:
            if p["manufacturer"].lower() != mf:
                return None
            score += 10000
        return score
    ranked = [(rank(p), p) for p in profiles]
    ranked = [x for x in ranked if x[0] is not None]
    return max(ranked, key=lambda x: x[0])[1] if ranked else None


def valid_hex(value, default=UNKNOWN_HEX):
    value = (value or "").strip().lower()
    return value if re.fullmatch(r"#[0-9a-f]{6}", value) else default


def form_to_spool(form):
    def num(name, default=None):
        raw = form.get(name, "").replace(",", ".").replace(" ", "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            raise ValueError("Поле «%s» должно быть числом." % name)

    def iso_date(name):
        raw = form.get(name, "").strip()
        if not raw:
            return None
        date.fromisoformat(raw)  # валидация
        return raw

    data = {
        "number": form.get("number", "").strip().lstrip("№#").strip(),
        "manufacturer": form.get("manufacturer", "").strip(),
        "material": form.get("material", "").strip(),
        "color_name": form.get("color_name", "").strip(),
        "color_hex": valid_hex(form.get("color_hex", "")),
        "diameter": num("diameter", 1.75),
        "weight_total": num("weight_total", 1000),
        "price": num("price"),
        "purchase_url": form.get("purchase_url", "").strip(),
        "purchase_date": iso_date("purchase_date"),
        "opened_date": iso_date("opened_date"),
        "notes": form.get("notes", "").strip(),
        "location": form.get("location", "").strip(),
        "alert": form.get("alert", "").strip()[:200],
    }
    data["weight_left"] = num("weight_left", data["weight_total"])
    if not data["manufacturer"] or not data["material"]:
        raise ValueError("Производитель и тип пластика обязательны.")
    if data["purchase_url"] and not data["purchase_url"].startswith(("http://", "https://")):
        raise ValueError("Ссылка на покупку должна начинаться с http:// или https://")
    if data["weight_total"] <= 0 or data["weight_left"] < 0:
        raise ValueError("Проверьте вес катушки.")
    return data


def save_photo(file):
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_PHOTO_EXT:
        raise ValueError("Фото должно быть в формате: " + ", ".join(sorted(ALLOWED_PHOTO_EXT)))
    name = uuid.uuid4().hex + "." + ext
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, name)
    file.save(path)
    shrink_photo(path)
    return name


def shrink_photo(path):
    """Фото с телефона: поворот по EXIF и уменьшение до PHOTO_MAX_SIDE."""
    try:
        with Image.open(path) as im:
            if getattr(im, "is_animated", False):
                return
            fixed = ImageOps.exif_transpose(im)
            if max(fixed.size) <= PHOTO_MAX_SIDE and fixed is im:
                return
            fixed.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))
            if fixed.mode not in ("RGB", "L") and path.lower().endswith((".jpg", ".jpeg")):
                fixed = fixed.convert("RGB")
            fixed.save(path, quality=88)
    except (OSError, ValueError):
        os.remove(path)
        raise ValueError("Не удалось прочитать фото — это точно картинка?")


def save_model(file):
    """STL (бинарный или текстовый): проверка формата, имя — uuid. Возвращает (файл, размер, треугольников)."""
    if not file or not file.filename:
        return None
    if not file.filename.lower().endswith(".stl"):
        raise ValueError("Модель — файл .stl")
    data = file.read()
    triangles = None
    if len(data) >= 84:
        n = int.from_bytes(data[80:84], "little")
        if len(data) == 84 + 50 * n:
            triangles = n
    if triangles is None:  # не бинарный — может, текстовый
        head = data[:1024].lstrip().lower()
        if not (head.startswith(b"solid") and b"facet" in data[:200000].lower()):
            raise ValueError("Файл «%s» не похож на STL." % file.filename[:60])
        triangles = data.lower().count(b"endfacet")
    if not triangles:
        raise ValueError("В «%s» нет треугольников." % file.filename[:60])
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = uuid.uuid4().hex + ".stl"
    with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
        f.write(data)
    return name, len(data), triangles, os.path.basename(file.filename)[:120]


def delete_photo(name):
    if name:
        for path in (os.path.join(UPLOAD_DIR, name), os.path.join(THUMB_DIR, thumb_name(name))):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def next_number():
    """Следующий свободный номер для новой катушки, если номера — числа."""
    row = get_db().execute("SELECT MAX(CAST(number AS INTEGER)) FROM spools WHERE number GLOB '[0-9]*'").fetchone()
    return str(row[0] + 1) if row[0] is not None else ""


TEMPLATE_FIELDS = ["manufacturer", "material", "color_name", "color_hex", "diameter", "weight_total", "price",
                   "purchase_url", "location", "tare", "alert", "notes"]


# --- журнал изменений с отменой ------------------------------------------------

AUDIT_TABLES = ["spools", "usage", "jobs", "prints", "models"]
AUDIT_HIDDEN = {"created_at"}


def spool_snapshot(spool_id):
    row = get_db().execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone() if spool_id else None
    return dict(row) if row else None


def audit_begin(spool_id):
    db = get_db()
    return (spool_snapshot(spool_id),
            {t: db.execute("SELECT COALESCE(MAX(id), 0) FROM %s" % t).fetchone()[0] for t in AUDIT_TABLES})


def audit_end(action, spool_id, state, deleted=None):
    """Запоминает, что изменилось у катушки: её строку до/после и добавленные строки
    истории, печатей и фото — чтобы действие можно было отменить."""
    before, max_ids = state
    db = get_db()
    created = {}
    for t in AUDIT_TABLES:
        col = "id" if t == "spools" else "spool_id"
        ids = [r[0] for r in db.execute("SELECT id FROM %s WHERE id > ? AND %s = ?" % (t, col), (max_ids[t], spool_id))]
        if ids:
            created[t] = ids
    db.execute("INSERT INTO audit (action, spool_id, before, after, created, deleted, user) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (action, spool_id, json.dumps(before, ensure_ascii=False) if before else None,
                json.dumps(spool_snapshot(spool_id), ensure_ascii=False) if spool_snapshot(spool_id) else None,
                json.dumps(created), json.dumps(deleted or {}, ensure_ascii=False), current_user()))


def audit_undoable(entry):
    """Отменить можно только последнее неотменённое действие с этой катушкой."""
    last = get_db().execute("SELECT MAX(id) FROM audit WHERE spool_id = ? AND undone = 0",
                            (entry["spool_id"],)).fetchone()[0]
    return not entry["undone"] and last == entry["id"]


def audit_undo(entry):
    db = get_db()
    created = json.loads(entry["created"])
    for t in reversed(AUDIT_TABLES):
        for rid in created.get(t, []):
            db.execute("DELETE FROM %s WHERE id = ?" % t, (rid,))
    before = json.loads(entry["before"]) if entry["before"] else None
    if before:
        cols = list(before)
        if spool_snapshot(before["id"]):
            db.execute("UPDATE spools SET %s WHERE id = ?" % ", ".join("%s = ?" % c for c in cols),
                       [before[c] for c in cols] + [before["id"]])
        else:
            db.execute("INSERT INTO spools (%s) VALUES (%s)" % (", ".join(cols), ", ".join("?" * len(cols))),
                       [before[c] for c in cols])
    for t, rows in json.loads(entry["deleted"]).items():
        if t not in AUDIT_TABLES:
            continue
        for r in rows:
            db.execute("INSERT OR IGNORE INTO %s (%s) VALUES (%s)" % (t, ", ".join(r), ", ".join("?" * len(r))),
                       list(r.values()))
    db.execute("UPDATE audit SET undone = 1 WHERE id = ?", (entry["id"],))


def audit_changes(entry):
    """Человекочитаемые изменения полей для журнала."""
    labels = dict(EXPORT_COLUMNS, weight_left="Остаток, г", opened_date="Дата вскрытия", dried_date="Сушка",
                  ordered="Заказана", tare="Пустая катушка, г")
    before = json.loads(entry["before"]) if entry["before"] else {}
    after = json.loads(entry["after"]) if entry["after"] else {}
    out = []
    for k in sorted(set(before) | set(after)):
        if k in AUDIT_HIDDEN or k == "id" or before.get(k) == after.get(k):
            continue
        out.append((labels.get(k, k), before.get(k), after.get(k)))
    return out


def effective_tare(spool):
    """Вес пустой катушки: свой, иначе известный для производителя, иначе типовой."""
    if spool["tare"] is not None:
        return spool["tare"], "spool"
    row = get_db().execute("SELECT weight FROM tares WHERE manufacturer = ?", (spool["manufacturer"],)).fetchone()
    return (row[0], "manufacturer") if row else (DEFAULT_TARE, "default")


def log_usage(db, row, new_left, total=None, price=None, source=""):
    """Записывает изменение остатка в историю расхода. source: weigh (весы), slicer/job (по слайсеру), edit."""
    used = row["weight_left"] - new_left
    if abs(used) < 0.5:
        return
    total = row["weight_total"] if total is None else total
    price = row["price"] if price is None else price
    cost = used * price / total if price and total else None
    db.execute("INSERT INTO usage (spool_id, day, used, left_after, cost, source) VALUES (?, ?, ?, ?, ?, ?)",
               (row["id"], date.today().isoformat(), used, new_left, cost, source))


def over_limit(row, grams):
    """Расход больше остатка — скорее опечатка или остаток не взвешен. Без подтверждения не пишем."""
    if grams <= row["weight_left"] + 0.5 or request.form.get("confirm_over"):
        return False
    flash("На катушке по учёту %d г, а списывается %g г. Взвесьте катушку или подтвердите "
          "«всё равно записать» — остаток станет 0." % (row["weight_left"], grams), "error")
    return True


def parse_duration(raw):
    """«1:30», «90», «1ч 30м», «2,5 ч» -> минуты."""
    raw = raw.strip().lower().replace(",", ".")
    if not raw:
        return None
    m = re.fullmatch(r"(\d+):(\d{1,2})", raw)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*ч\w*)?\s*(?:(\d+)\s*м\w*)?", raw)
    if m and (m.group(1) or m.group(2)):
        return round(float(m.group(1) or 0) * 60) + int(m.group(2) or 0)
    if re.fullmatch(r"\d+", raw):
        return int(raw)
    raise ValueError("Время печати: например «1:30», «90» или «1ч 30м».")


ENOUGH_RESERVE = 0.05  # запас на продувку/юбку/ошибку взвешивания


# --- принтер (Creality K2 через агента в домашней сети) -------------------------

def record_job(db, spool_id, day, name, grams, minutes):
    """Запись о печати + списание с катушки (общая для формы и принтера)."""
    row = db.execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone()
    if row is None:
        raise ValueError("Катушка не найдена.")
    cost = grams * row["price"] / row["weight_total"] if grams and row["price"] and row["weight_total"] else None
    job_id = db.execute("INSERT INTO jobs (spool_id, day, name, grams, minutes, cost) VALUES (?, ?, ?, ?, ?, ?)",
                        (spool_id, day, name, grams, minutes, cost)).lastrowid
    if grams:
        left = max(0.0, row["weight_left"] - grams)
        log_usage(db, row, left, source="job")
        db.execute("UPDATE spools SET weight_left = ?, opened_date = COALESCE(opened_date, ?) WHERE id = ?",
                   (left, date.today().isoformat(), spool_id))
    return job_id


def printer_spool():
    sid = load_config().get("printer_spool")
    row = get_db().execute("SELECT * FROM spools WHERE id = ?", (sid,)).fetchone() if sid else None
    return row


def preview_name(stl_file):
    return stl_file.rsplit(".", 1)[0] + ".png"  # cleanup держит миниатюры, пока жив исходник с тем же именем


def build_preview(stl_file, color_hex):
    try:
        with open(os.path.join(UPLOAD_DIR, stl_file), "rb") as f:
            img = stlpreview.render(f.read(), color_hex if color_hex != UNKNOWN_HEX else "#4f8cff")
    except (OSError, ValueError, struct.error):
        return False
    os.makedirs(THUMB_DIR, exist_ok=True)
    path = os.path.join(THUMB_DIR, preview_name(stl_file))
    tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex[:8])
    img.save(tmp, "PNG", optimize=True)
    os.replace(tmp, path)
    return True


def copy_import_photo(token, name):
    ext = name.rsplit(".", 1)[-1]
    new = uuid.uuid4().hex + "." + ext
    shutil.copyfile(os.path.join(importer.tmp_path(IMPORT_DIR, token), "photos", name),
                    os.path.join(UPLOAD_DIR, new))
    return new


def plan_import(header, body, mapping, sheet_photos, skip_dups=True, use_photos=True, do_update=True):
    """Разбирает строки и решает, что с ними делать: новые, обновления, фото к существующим."""
    rows = importer.parse_rows(header, body, mapping, MATERIALS)
    existing_photo = {}
    if skip_dups:
        existing = [dict(r) for r in get_db().execute("SELECT * FROM spools")]
        existing_photo = {d["id"]: d["photo"] for d in existing}
        importer.mark_duplicates(rows, existing)
    for r in rows:
        r["sheet_photo"] = sheet_photos.get(r["line"]) if use_photos else None
        # уже есть в базе, но без фото — фото из таблицы добавим к ней
        r["add_photo"] = bool(r["duplicate"] and r["sheet_photo"] and not existing_photo.get(r["duplicate"]))
        if not do_update:
            r["changes"] = []
    return {"rows": rows,
            "todo": [r for r in rows if not r["errors"] and not r["duplicate"]],
            "photo_only": [r for r in rows if r["add_photo"]],
            "updates": [r for r in rows if r["changes"]]}


def apply_import(token, plan):
    """Записывает план в базу; возвращает сообщение для пользователя."""
    db = get_db()
    deadline = importer.photo_deadline()
    photo_fail = 0
    for r in plan["todo"]:
        data = dict(r["data"], photo=None)
        if r["sheet_photo"]:
            data["photo"] = copy_import_photo(token, r["sheet_photo"])
        elif r["photo_url"]:
            try:
                data["photo"] = importer.download_photo(r["photo_url"], UPLOAD_DIR, deadline)
            except importer.SheetError:
                photo_fail += 1
        db.execute("INSERT INTO spools (%s) VALUES (%s)" % (", ".join(data), ", ".join("?" * len(data))),
                   list(data.values()))
    for r in plan["updates"]:
        db.execute("UPDATE spools SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k, _, _ in r["changes"]),
                   [new for _, _, new in r["changes"]] + [r["duplicate"]])
    for r in plan["photo_only"]:
        db.execute("UPDATE spools SET photo = ? WHERE id = ? AND photo IS NULL",
                   (copy_import_photo(token, r["sheet_photo"]), r["duplicate"]))
    db.commit()
    importer.drop_tmp(IMPORT_DIR, token)
    msg = []
    if plan["todo"]:
        msg.append("Импортировано катушек: %d." % len(plan["todo"]))
    if plan["updates"]:
        msg.append("Обновлено катушек: %d." % len(plan["updates"]))
    if plan["photo_only"]:
        msg.append("Добавлено фото к уже существующим: %d." % len(plan["photo_only"]))
    if photo_fail:
        msg.append("Не удалось скачать фото: %d — добавьте их вручную." % photo_fail)
    return " ".join(msg) or "Изменений нет."


def saved_mapping(header, body):
    """Сопоставление колонок с прошлого импорта, если заголовки те же, иначе угаданное.
    Если в таблицу дописали колонки справа — прежние берутся из сохранённого, новые угадываются."""
    saved = load_config().get("import_mapping") or {}
    old_header, old_map = saved.get("header") or [], saved.get("mapping") or []
    n = len(old_header)
    if n and len(old_map) == n and header[:n] == old_header:
        guessed = importer.guess_mapping(header, body)
        extra = [g if g and g not in old_map else "" for g in guessed[n:]]
        return old_map + extra
    return importer.guess_mapping(header, body)


def fetch_to_tmp(url):
    text = importer.fetch_sheet(url)
    importer.parse_csv(text)  # проверка, что таблица читается
    photos, photo_error = {}, None
    try:
        photos = importer.fetch_sheet_photos(url, text)
    except importer.SheetError as e:
        photo_error = str(e)
    return importer.save_tmp(IMPORT_DIR, text, photos), photo_error


def thumb_name(name):
    return name.rsplit(".", 1)[0] + ".jpg"
NOTIFY_THRESHOLD = 200


UNDO_DAYS = 30  # столько дней держим файлы, чтобы удаление можно было отменить
