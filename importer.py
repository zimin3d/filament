"""Импорт катушек из Google Таблиц (по ссылке) или CSV-файла."""
import csv
import io
import ipaddress
import socket
import os
import re
import posixpath
import secrets
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections import defaultdict
from datetime import date, datetime

FETCH_TIMEOUT = 15
PHOTO_TIMEOUT = 8
PHOTO_BUDGET = 20          # секунд на скачивание всех фото — укладываемся в таймаут gunicorn
PHOTO_MAX_BYTES = 10 * 1024 * 1024
SHEET_MAX_BYTES = 5 * 1024 * 1024
XLSX_MAX_BYTES = 100 * 1024 * 1024
TMP_MAX_AGE = 24 * 3600

# поле -> (подпись, ключевые слова в заголовке). Порядок важен: более
# специфичные поля проверяются раньше («остаток» раньше «вес», «hex» раньше «цвет»,
# «фото» раньше «ссылка»).
FIELDS = [
    ("number", "№ катушки", ["№", "номер", "number", "num"]),
    ("manufacturer", "Производитель", ["производ", "бренд", "brand", "manufact", "vendor", "марка", "фирма"]),
    ("weight_left", "Остаток, г", ["остат", "осталось", "remain", "left"]),
    ("weight_total", "Вес, г", ["вес", "масса", "weight"]),
    ("color_hex", "Код цвета (#hex)", ["hex", "код цвета", "rgb"]),
    ("color_name", "Цвет", ["цвет", "color", "colour"]),
    ("diameter", "Диаметр, мм", ["диаметр", "diam"]),
    ("price", "Цена, ₽", ["цен", "стоим", "price", "cost", "руб", "₽"]),
    ("photo_url", "Фото (ссылка)", ["фото", "photo", "image", "картин", "изображ"]),
    ("purchase_url", "Ссылка на покупку", ["ссылк", "url", "link", "магазин", "где куп"]),
    ("opened_date", "Дата вскрытия", ["вскрыт", "открыт", "opened"]),
    ("purchase_date", "Дата покупки", ["покуп", "куплен", "purchase", "bought", "заказ", "дата", "date"]),
    ("location", "Место хранения", ["место", "хранен", "полк", "ящик", "бокс", "location", "shelf", "box"]),
    ("alert", "Важно (на карточке)", ["важн", "внимани", "коммент", "примеч", "warning", "comment", "alert"]),
    ("notes", "Заметки", ["замет", "note", "описан"]),
    ("material", "Тип пластика", ["тип", "материал", "пластик", "material", "type"]),
]
FIELD_LABELS = {k: label for k, label, _ in FIELDS}

COLOR_WORDS = [
    (["аметист"], "#9966cc"),
    (["фукси", "малинов", "crimson"], "#c2185b"),
    (["горчич", "mustard"], "#c9a227"),
    (["латун", "brass", "бронз", "bronze"], "#b5a642"),
    (["слонов", "кремов", "молочн", "ivory", "cream"], "#efe6d2"),
    (["кофе", "coffee"], "#6f4e37"),
    (["мрамор", "marble"], "#cfcfcf"),
    (["голуб", "light blue", "sky", "cyan", "бирюз", "teal"], "#4fc3f7"),
    (["черн", "black"], "#1a1a1a"),
    (["бел", "white"], "#f5f5f5"),
    (["серебр", "silver"], "#bdbdbd"),
    (["сер", "grey", "gray"], "#8a8a8a"),
    (["бордов", "вишн", "burgundy", "maroon"], "#7b1f2e"),
    (["красн", "red"], "#d32f2f"),
    (["оранж", "orange"], "#f57c00"),
    (["желт", "yellow"], "#fbc02d"),
    (["золот", "gold"], "#c9a227"),
    (["зелен", "салат", "green", "lime"], "#388e3c"),
    (["син", "blue", "navy"], "#1976d2"),
    (["фиолет", "сирен", "purple", "violet"], "#7b1fa2"),
    (["розов", "pink", "magenta"], "#ec407a"),
    (["коричн", "шоколад", "brown"], "#6d4c41"),
    (["беж", "beige", "дерев", "wood"], "#d7c4a3"),
    (["прозрач", "натурал", "transparent", "clear", "natural"], "#e0e0e0"),
]

TRUE_WORDS = {"да", "yes", "y", "true", "истина", "1", "+", "✓", "✔", "x", "х", "вскрыта", "открыта"}
FALSE_WORDS = {"нет", "no", "n", "false", "ложь", "0", "-", "—"}


class SheetError(Exception):
    pass


# --- получение таблицы -------------------------------------------------------

def sheet_csv_url(url, fmt="csv"):
    """Ссылка на Google Таблицу -> ссылка на её выгрузку в CSV (или xlsx)."""
    url = url.strip()
    gid = re.search(r"[#&?]gid=(\d+)", url)
    gid = gid.group(1) if gid else "0"
    m = re.search(r"/spreadsheets/d/e/([\w-]+)", url)
    if m:  # «Опубликовать в интернете»
        return "https://docs.google.com/spreadsheets/d/e/%s/pub?output=%s&gid=%s" % (m.group(1), fmt, gid)
    m = re.search(r"/spreadsheets/d/([\w-]+)", url)
    if m:
        return "https://docs.google.com/spreadsheets/d/%s/export?format=%s&gid=%s" % (m.group(1), fmt, gid)
    if fmt == "csv" and re.match(r"https?://", url) and "docs.google.com" not in url:
        return url  # прямая ссылка на CSV
    raise SheetError("Не похоже на ссылку на Google Таблицу.")


def check_public_url(url):
    """Защита от SSRF: только http(s) и только публичные адреса (не localhost,
    не локальная сеть, не служебные 169.254.x и т.п.)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SheetError("Нужна ссылка http(s).")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror:
        raise SheetError("Не найден сервер %s." % parts.hostname)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise SheetError("Адрес %s недоступен для загрузки (внутренняя сеть)." % parts.hostname)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


def safe_urlopen(url, timeout):
    check_public_url(url)
    return _opener.open(urllib.request.Request(url, headers={"User-Agent": "filament-import"}), timeout=timeout)


def download(url, limit):
    try:
        with safe_urlopen(url, FETCH_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise SheetError("Google не отдал таблицу (%s). Откройте доступ «Все, у кого есть ссылка → "
                             "Читатель» или загрузите CSV-файл." % e.code)
        raise SheetError("Ошибка загрузки таблицы: HTTP %s." % e.code)
    except (urllib.error.URLError, OSError) as e:
        raise SheetError("Не удалось скачать таблицу: %s" % getattr(e, "reason", e))
    if "text/html" in ctype:
        raise SheetError("Таблица закрыта. Откройте доступ «Все, у кого есть ссылка → Читатель» "
                         "или загрузите CSV-файл (Файл → Скачать → CSV).")
    if len(body) > limit:
        raise SheetError("Таблица слишком большая.")
    return body


def fetch_sheet(url):
    return decode(download(sheet_csv_url(url), SHEET_MAX_BYTES))


def fetch_sheet_photos(url, csv_text):
    """Картинки, вставленные поверх ячеек, в CSV не попадают — берём их из xlsx-выгрузки.
    Возвращает {номер строки таблицы: (расширение, байты)}."""
    if "docs.google.com" not in url:
        return {}
    raw = download(sheet_csv_url(url, "xlsx"), XLSX_MAX_BYTES)
    try:
        return xlsx_photos(zipfile.ZipFile(io.BytesIO(raw)), csv_text)
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        raise SheetError("Не удалось разобрать фото из таблицы: %s" % e)


def _rels(z, part):
    """Связи части пакета: rId -> полный путь цели."""
    path = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if path not in z.namelist():
        return {}
    base = posixpath.dirname(part)
    return {m.group(1): posixpath.normpath(posixpath.join(base, m.group(2)))
            for m in re.finditer(r'<Relationship [^>]*?Id="([^"]+)"[^>]*?Target="([^"]+)"',
                                 z.read(path).decode())}


def _sheet_strings(z, sheet_xml, shared):
    out = set(re.findall(r"<t[^>]*>([^<]*)</t>", sheet_xml))  # inline-строки
    for idx in re.findall(r'<c [^>]*t="s"[^>]*>\s*<v>(\d+)</v>', sheet_xml):
        if int(idx) < len(shared):
            out.add(shared[int(idx)])
    return {v.strip() for v in out if v.strip()}


def xlsx_photos(z, csv_text):
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        shared = [re.sub(r"<[^>]+>", "", si) for si in
                  re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode(), re.S)]
    csv_values = {c.strip() for r in csv.reader(io.StringIO(csv_text)) for c in r if c.strip()}

    # выгрузка содержит все листы — берём тот, чьи строки совпадают с CSV
    wb_rels = _rels(z, "xl/workbook.xml")
    best, best_score = None, -1
    for rid in re.findall(r'<sheet [^>]*r:id="([^"]+)"', z.read("xl/workbook.xml").decode()):
        part = wb_rels.get(rid)
        if not part or part not in z.namelist():
            continue
        score = len(_sheet_strings(z, z.read(part).decode(), shared) & csv_values)
        if score > best_score:
            best, best_score = part, score
    if best is None:
        return {}

    photos = {}
    sheet_rels = _rels(z, best)
    for rid in re.findall(r'<drawing r:id="([^"]+)"', z.read(best).decode()):
        drawing = sheet_rels.get(rid)
        if not drawing:
            continue
        media = _rels(z, drawing)
        xml = z.read(drawing).decode()
        for anchor in re.findall(r"<xdr:(?:one|two)CellAnchor.*?</xdr:(?:one|two)CellAnchor>", xml, re.S):
            row = re.search(r"<xdr:from>.*?<xdr:row>(\d+)</xdr:row>", anchor, re.S)
            col = re.search(r"<xdr:from>.*?<xdr:col>(\d+)</xdr:col>", anchor, re.S)
            emb = re.search(r'r:embed="([^"]+)"', anchor)
            if not (row and emb and emb.group(1) in media):
                continue
            target = media[emb.group(1)]
            ext = target.rsplit(".", 1)[-1].lower()
            if ext == "jpeg":
                ext = "jpg"
            if ext not in ("jpg", "png", "webp", "gif"):
                continue
            line = int(row.group(1)) + 1  # в xlsx строки с нуля
            key = (int(col.group(1)) if col else 0)
            if line not in photos or key < photos[line][0]:  # несколько фото в строке — берём левое
                photos[line] = (key, ext, target)
    total = 0
    for _, _, target in photos.values():
        size = z.getinfo(target).file_size  # размер после распаковки — проверяем до чтения
        total += size
        if size > PHOTO_MAX_BYTES or total > XLSX_MAX_BYTES:
            raise ValueError("слишком большие картинки в таблице")
    return {line: (ext, z.read(target)) for line, (_, ext, target) in photos.items()}


# встроенные форматы Excel, которые означают дату
XLSX_DATE_FORMATS = set(range(14, 23)) | {45, 46, 47}
XLSX_MAX_CELLS = 200_000


def _xml_text(s):
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
            .replace("&amp;", "&"))


def _col_index(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def xlsx_read(raw):
    """Файл Excel (.xlsx) → (CSV-текст первого листа, фото {строка: (расширение, байты)})."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        if sum(i.file_size for i in z.infolist()) > XLSX_MAX_BYTES:  # zip-бомба
            raise SheetError("Файл слишком большой.")
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            shared = [_xml_text("".join(re.findall(r"<t[^>]*>([^<]*)</t>", si))) for si in
                      re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode(), re.S)]
        date_styles = set()
        if "xl/styles.xml" in z.namelist():
            styles = z.read("xl/styles.xml").decode()
            custom = {int(i) for i, code in re.findall(r'<numFmt [^>]*numFmtId="(\d+)"[^>]*formatCode="([^"]*)"', styles)
                      if re.search(r"[dy]", re.sub(r'"[^"]*"|\[[^]]*\]', "", code), re.I)}
            xfs = re.search(r"<cellXfs[^>]*>(.*?)</cellXfs>", styles, re.S)
            for i, fmt in enumerate(re.findall(r'<xf [^>]*?numFmtId="(\d+)"', xfs.group(1) if xfs else "")):
                if int(fmt) in XLSX_DATE_FORMATS or int(fmt) in custom:
                    date_styles.add(i)
        wb_rels = _rels(z, "xl/workbook.xml")
        first = re.search(r'<sheet [^>]*r:id="([^"]+)"', z.read("xl/workbook.xml").decode())
        sheet = wb_rels.get(first.group(1)) if first else None
        if not sheet or sheet not in z.namelist():
            raise SheetError("В файле нет листов.")
        xml = z.read(sheet).decode()
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError):
        raise SheetError("Это не файл Excel (.xlsx).")
    rows = {}
    cells = re.findall(r'<c ([^>]*?)(?:/>|>(.*?)</c>)', xml, re.S)
    if len(cells) > XLSX_MAX_CELLS:
        raise SheetError("Таблица слишком большая.")
    for attrs, body in cells:
        ref = re.search(r'r="([A-Z]+)(\d+)"', attrs)
        if not ref:
            continue
        kind = re.search(r'\bt="(\w+)"', attrs)
        kind = kind.group(1) if kind else "n"
        style = re.search(r'\bs="(\d+)"', attrs)
        v = re.search(r"<v>([^<]*)</v>", body or "")
        if kind == "s" and v:
            value = shared[int(v.group(1))] if int(v.group(1)) < len(shared) else ""
        elif kind == "inlineStr":
            value = _xml_text("".join(re.findall(r"<t[^>]*>([^<]*)</t>", body or "")))
        elif v:
            value = _xml_text(v.group(1))
            if kind == "n" and style and int(style.group(1)) in date_styles:
                try:  # дата Excel — дни от 1899-12-30
                    value = date.fromordinal(date(1899, 12, 30).toordinal() + int(float(value))).isoformat()
                except (ValueError, OverflowError):
                    pass
            elif kind == "n" and re.fullmatch(r"-?\d+\.0+", value):
                value = value.split(".")[0]
        else:
            value = ""
        rows.setdefault(int(ref.group(2)), {})[_col_index(ref.group(1))] = value
    if not rows:
        raise SheetError("Лист пустой.")
    width = max(max(r) for r in rows.values() if r) + 1
    if width * max(rows) > XLSX_MAX_CELLS * 10:  # одна ячейка в XFD1048576 — миллиарды пустых клеток
        raise SheetError("Таблица слишком большая.")
    out = io.StringIO()
    w = csv.writer(out)
    for line in range(1, max(rows) + 1):  # пустые строки сохраняем — номера строк совпадают с фото
        r = rows.get(line, {})
        w.writerow([r.get(i, "") for i in range(width)])
    text = out.getvalue()
    try:
        photos = xlsx_photos(z, text)
    except (KeyError, ValueError):
        photos = {}
    return text, photos


def decode(raw):
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    raise SheetError("Не удалось прочитать файл: неизвестная кодировка.")


def parse_csv(text):
    """CSV -> (заголовки, строки данных, номер строки заголовка в таблице)."""
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [(n, [c.strip() for c in r])
            for n, r in enumerate(csv.reader(io.StringIO(text), dialect), start=1)]
    rows = [(n, r) for n, r in rows if any(r)]
    if len(rows) < 2:
        raise SheetError("В таблице нет данных: нужна строка заголовков и хотя бы одна строка.")
    width = max(len(r) for _, r in rows)
    rows = [(n, r + [""] * (width - len(r))) for n, r in rows]
    # над таблицей бывает название — заголовок там, где узнаётся больше всего колонок
    head_i = max(range(min(5, len(rows) - 1)),
                 key=lambda i: (sum(1 for k in guess_by_header(rows[i][1]) if k), -i))
    header_line, header = rows[head_i]
    return list(header), rows[head_i + 1:], header_line


# --- временное хранение между шагами -----------------------------------------

# Каждый импорт — папка <tmp_dir>/<token>/ с sheet.csv и photos/<строка>.<ext>.

def save_tmp(tmp_dir, text, photos=None):
    os.makedirs(tmp_dir, exist_ok=True)
    now = time.time()
    for name in os.listdir(tmp_dir):
        path = os.path.join(tmp_dir, name)
        if now - os.path.getmtime(path) > TMP_MAX_AGE:
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    token = secrets.token_hex(16)
    os.makedirs(os.path.join(tmp_dir, token, "photos"))
    with open(os.path.join(tmp_dir, token, "sheet.csv"), "w", encoding="utf-8") as f:
        f.write(text)
    for line, (ext, data) in (photos or {}).items():
        with open(os.path.join(tmp_dir, token, "photos", "%d.%s" % (line, ext)), "wb") as f:
            f.write(data)
    return token


def tmp_path(tmp_dir, token):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise SheetError("Данные импорта не найдены, загрузите таблицу заново.")
    return os.path.join(tmp_dir, token)


def load_tmp(tmp_dir, token):
    try:
        with open(os.path.join(tmp_path(tmp_dir, token), "sheet.csv"), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        raise SheetError("Данные импорта устарели, загрузите таблицу заново.")


def tmp_photos(tmp_dir, token):
    """{номер строки: имя файла} фото, извлечённых из таблицы."""
    folder = os.path.join(tmp_path(tmp_dir, token), "photos")
    if not os.path.isdir(folder):
        return {}
    return {int(n.split(".")[0]): n for n in os.listdir(folder) if n.split(".")[0].isdigit()}


def drop_tmp(tmp_dir, token):
    shutil.rmtree(tmp_path(tmp_dir, token), ignore_errors=True)


# --- сопоставление колонок ---------------------------------------------------

MULTI_FIELDS = {"notes"}  # в заметки можно свести несколько колонок


def guess_by_header(header):
    mapping, used = [], set()
    for h in header:
        low = h.lower().replace("ё", "е")
        found = ""
        if low:
            for key, _, words in FIELDS:
                if key not in used and any(w in low for w in words):
                    found = key
                    break
        if found and found not in MULTI_FIELDS:
            used.add(found)
        mapping.append(found)
    return mapping


def guess_by_values(values):
    values = [v for v in values if v]
    if not values:
        return ""
    share = lambda pred: sum(1 for v in values if pred(v)) / len(values)
    if share(lambda v: re.fullmatch(r"[\d\s.,]+\s*(₽|р\.?|руб\.?)?", v, re.I)) > .8 and \
            share(lambda v: "₽" in v or "руб" in v.lower()) > .3:
        return "price"
    if share(lambda v: re.fullmatch(r"№?\s*\d{1,5}", v)) == 1 and len(set(values)) == len(values):
        return "number"  # уникальные целые без единиц — номера катушек
    if share(lambda v: v.startswith(("http://", "https://"))) > .8:
        return "purchase_url"
    if share(lambda v: re.fullmatch(r"#?[0-9a-fA-F]{6}", v)) > .8:
        return "color_hex"
    if share(lambda v: re.fullmatch(r"\d{1,4}[./-]\d{1,2}[./-]\d{2,4}", v)) > .8:
        return "purchase_date"
    if sum(len(v) for v in values) / len(values) > 30:
        return "notes"
    return ""


def guess_mapping(header, body):
    mapping = guess_by_header(header)
    used = set(mapping) - MULTI_FIELDS
    for i, h in enumerate(header):
        if h or mapping[i]:
            continue
        key = guess_by_values([r[i] for _, r in body])
        if key and key not in used:
            mapping[i] = key
            if key not in MULTI_FIELDS:
                used.add(key)
    return mapping


def column_titles(header):
    return [h or "Колонка %d" % (i + 1) for i, h in enumerate(header)]


# --- разбор значений ---------------------------------------------------------

def parse_number(raw):
    s = re.sub(r"[^\d,.\-]", "", raw)
    if not s:
        return None
    if "," in s and "." in s:  # 1,290.50 или 1.290,50
        thousands = "," if s.rfind(",") < s.rfind(".") else "."
        s = s.replace(thousands, "")
    s = s.replace(",", ".")
    if s.count(".") > 1:
        s = s.replace(".", "", s.count(".") - 1)
    try:
        return float(s)
    except ValueError:
        raise ValueError("«%s» — не число" % raw)


def parse_weight(raw):
    value = parse_number(raw)
    if value is None:
        return None
    low = raw.lower()
    if "кг" in low or "kg" in low or (value < 20 and "г" not in low and "g" not in low):
        value *= 1000  # «1», «0,75 кг» — это килограммы
    return value


def parse_date(raw):
    s = raw.strip().split(" ")[0].split("T")[0]
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y", "%m/%d/%Y", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("«%s» — непонятная дата" % raw)


def normalize_material(raw, materials):
    compact = re.sub(r"\s+", "", raw).upper()
    for m in materials:
        if m.upper() == compact:
            return m
    return raw


RAINBOW = ["#e53935", "#fb8c00", "#fdd835", "#43a047", "#1e88e5", "#8e24aa"]
CLEAR_HEX = "#e0e0e0"


def colors_from_name(name):
    """Все цвета из названия по порядку: «красный, зеленый» -> [красный, зеленый].
    «Прозрачный» — свойство, а не цвет, если есть другие цвета."""
    low = name.lower().replace("ё", "е")
    if "радуж" in low or "rainbow" in low or "мультицвет" in low:
        return RAINBOW
    found, taken = [], []
    for words, hex_ in COLOR_WORDS:
        for w in words:
            for m in re.finditer(re.escape(w), low):
                span = (m.start(), m.end())
                if any(a <= span[0] < b for a, b in taken):
                    continue
                taken.append((span[0], span[0] + len(w) + 4))  # «сер» не ловим внутри «серебр»
                found.append((span[0], hex_))
    found.sort()
    out = []
    for _, h in found:
        if h not in out:
            out.append(h)
    if len(out) > 1 and CLEAR_HEX in out:
        out.remove(CLEAR_HEX)
    return out


def color_from_name(name):
    low = name.lower().replace("ё", "е")
    for words, hex_ in COLOR_WORDS:
        if any(w in low for w in words):
            return hex_
    return None


def drive_direct(url):
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?.*id=)([\w-]+)", url)
    return "https://drive.google.com/uc?export=download&id=%s" % m.group(1) if m else url


def parse_rows(header, body, mapping, materials):
    """Строки таблицы -> список {line, data, photo_url, errors, warnings}."""
    result = []
    for n, raw_row in body:
        cells, notes = {}, []
        for value, key in zip(raw_row, mapping):
            if key == "notes" and value:
                notes.append(value)
            elif key and value:
                cells[key] = value
        if notes:
            cells["notes"] = "\n".join(notes)
        if not cells:
            continue
        errors, warnings = [], []

        def take(key, parser):
            if key not in cells:
                return None
            try:
                return parser(cells[key])
            except ValueError as e:
                errors.append(str(e))
                return None

        d = {
            "number": cells.get("number", "").lstrip("№#").strip(),
            "manufacturer": cells.get("manufacturer", ""),
            "material": normalize_material(cells.get("material", ""), materials),
            "color_name": cells.get("color_name", ""),
            "diameter": take("diameter", parse_number) or 1.75,
            "weight_total": take("weight_total", parse_weight) or 1000,
            "price": take("price", parse_number),
            "purchase_url": cells.get("purchase_url", ""),
            "purchase_date": take("purchase_date", parse_date),
            "opened_date": None,
            "notes": cells.get("notes", ""),
            "location": cells.get("location", ""),
            "alert": cells.get("alert", "")[:200],
        }
        left = take("weight_left", parse_weight)
        d["weight_left"] = d["weight_total"] if left is None else left

        hex_ = cells.get("color_hex", "")
        if not hex_ and re.fullmatch(r"#[0-9a-fA-F]{6}", d["color_name"]):
            hex_ = d["color_name"]  # в колонке цвета записан код
        if hex_ and not hex_.startswith("#"):
            hex_ = "#" + hex_
        if re.fullmatch(r"#[0-9a-fA-F]{6}", hex_):
            d["color_hex"] = hex_.lower()
        else:
            d["color_hex"] = color_from_name(d["color_name"]) or "#888888"

        opened = cells.get("opened_date", "")
        if opened and opened.lower() not in FALSE_WORDS:
            try:
                d["opened_date"] = parse_date(opened)
            except ValueError:
                if opened.lower() in TRUE_WORDS:
                    d["opened_date"] = d["purchase_date"] or date.today().isoformat()
                    warnings.append("дата вскрытия не указана — взята %s" % (
                        "дата покупки" if d["purchase_date"] else "сегодняшняя"))
                else:
                    errors.append("«%s» — непонятная дата вскрытия" % opened)

        if not d["manufacturer"]:
            d["manufacturer"] = "Не указан"
            warnings.append("нет производителя — записан «Не указан»")
        if not d["material"]:
            errors.append("нет типа пластика")
        if d["purchase_url"] and not d["purchase_url"].startswith(("http://", "https://")):
            warnings.append("в ссылке не адрес — перенесено в заметки")
            d["notes"] = "\n".join(filter(None, [d["notes"], d["purchase_url"]]))
            d["purchase_url"] = ""
        if d["weight_total"] <= 0 or d["weight_left"] < 0:
            errors.append("неверный вес")
        elif d["weight_left"] > d["weight_total"]:
            warnings.append("остаток больше веса катушки")

        photo = cells.get("photo_url", "")
        if photo and not photo.startswith(("http://", "https://")):
            warnings.append("фото пропущено: не ссылка")
            photo = ""
        given = set(cells) & set(d)
        if "color_name" in cells or "color_hex" in cells:
            given.add("color_hex")
        result.append({"line": n, "data": d, "photo_url": photo, "errors": errors,
                       "warnings": warnings, "duplicate": False, "given": given, "changes": [],
                       "hex_given": "color_hex" in cells})
    return result


def spool_key(d):
    return (d["manufacturer"].strip().lower(), d["material"].strip().lower(),
            d["color_name"].strip().lower(), d["purchase_date"] or "")


# что импорт может обновить у существующей катушки; остаток и история — забота приложения
UPDATABLE = ["manufacturer", "material", "color_name", "color_hex", "diameter", "weight_total",
             "price", "purchase_url", "purchase_date", "opened_date", "notes", "location", "alert"]


def mark_duplicates(rows, existing):
    """Сопоставляет строки с катушками в базе (r["duplicate"] = id): по №, если он есть,
    иначе по производителю+типу+цвету+дате покупки. Для найденных собирает r["changes"]:
    поля, которые в таблице заполнены и отличаются. Пустые ячейки ничего не стирают."""
    by_number = {d["number"]: d for d in existing if d.get("number")}
    by_key = defaultdict(list)
    for d in existing:
        if not d.get("number"):
            by_key[spool_key(d)].append(d)
    for r in rows:
        if r["errors"]:
            continue
        num = r["data"].get("number")
        if num:
            old = by_number.pop(num, None)
        else:
            cands = by_key[spool_key(r["data"])]
            old = cands.pop(0) if cands else None
        if old is None:
            continue
        r["duplicate"] = old["id"]
        for k in UPDATABLE:
            if k not in r["given"]:
                continue
            new = r["data"][k]
            if k == "opened_date" and old[k]:
                continue  # дату вскрытия ведёт приложение
            if k == "color_hex" and not r["hex_given"] and r["data"]["color_name"] == old["color_name"]:
                continue  # колонки с кодом нет, название то же — образец, выбранный вручную, не трогаем
            if new in (None, ""):
                continue
            if k == "color_hex" and not r["hex_given"] and new == "#888888":
                continue  # по новому названию цвет не угадан — оставляем прежний образец
            if isinstance(new, float) and old[k] is not None and abs(new - old[k]) < 1e-6:
                continue
            if new != old[k]:
                r["changes"].append((k, old[k], new))


# --- фото --------------------------------------------------------------------

PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def download_photo(url, upload_dir, deadline):
    timeout = min(PHOTO_TIMEOUT, deadline - time.monotonic())
    if timeout <= 1:
        raise SheetError("не хватило времени на скачивание фото")
    try:
        with safe_urlopen(drive_direct(url), timeout) as resp:
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype not in PHOTO_TYPES:
                raise SheetError("по ссылке на фото не картинка")
            data = resp.read(PHOTO_MAX_BYTES + 1)
    except (urllib.error.URLError, OSError) as e:
        raise SheetError("фото не скачалось: %s" % getattr(e, "reason", e))
    if len(data) > PHOTO_MAX_BYTES:
        raise SheetError("фото больше 10 МБ")
    name = uuid.uuid4().hex + "." + PHOTO_TYPES[ctype]
    with open(os.path.join(upload_dir, name), "wb") as f:
        f.write(data)
    return name


def photo_deadline():
    return time.monotonic() + PHOTO_BUDGET
