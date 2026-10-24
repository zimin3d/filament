"""Ядро: пути, схема БД, конфигурация, Flask-приложение, вход/CSRF/заголовки, фильтры шаблонов."""
import copy
import hashlib
import json
import math
import os
import secrets
import sqlite3
import tempfile
from datetime import date
from functools import wraps

from flask import Flask, abort, flash, g, has_request_context, redirect, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import importer
import slicer
import sync

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("FILAMENT_DATA", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "filament.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
THUMB_SIZE = 480
PHOTO_MAX_SIDE = 2000
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
IMPORT_DIR = os.path.join(DATA_DIR, "import")
# приложение на телефоне: тот же код, сервер только на 127.0.0.1, данные синхронизируются с основным
PHONE = os.environ.get("FILAMENT_MODE") == "phone"
ALLOWED_PHOTO_EXT = {"jpg", "jpeg", "png", "webp", "gif"}

MATERIALS = ["PLA", "PLA+", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "HIPS", "PVA", "Другое"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS spools (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          TEXT NOT NULL DEFAULT '',
    manufacturer    TEXT NOT NULL,
    material        TEXT NOT NULL,
    color_name      TEXT NOT NULL DEFAULT '',
    color_hex       TEXT NOT NULL DEFAULT '#888888',
    diameter        REAL NOT NULL DEFAULT 1.75,
    weight_total    REAL NOT NULL DEFAULT 1000,
    weight_left     REAL NOT NULL DEFAULT 1000,
    price           REAL,
    purchase_url    TEXT NOT NULL DEFAULT '',
    purchase_date   TEXT,
    opened_date     TEXT,
    photo           TEXT,
    notes           TEXT NOT NULL DEFAULT '',
    tare            REAL,
    dried_date      TEXT,
    location        TEXT NOT NULL DEFAULT '',
    ordered         INTEGER NOT NULL DEFAULT 0,   -- заказана, ещё не пришла
    alert           TEXT NOT NULL DEFAULT '',     -- важная пометка: «не лезет в CFS» и т.п.
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    material        TEXT NOT NULL COLLATE NOCASE,
    manufacturer    TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    nozzle          TEXT NOT NULL DEFAULT '',
    bed             TEXT NOT NULL DEFAULT '',
    speed           TEXT NOT NULL DEFAULT '',
    fan             TEXT NOT NULL DEFAULT '',
    drying          TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    UNIQUE (material, manufacturer)
);
CREATE TABLE IF NOT EXISTS usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    spool_id        INTEGER NOT NULL REFERENCES spools(id) ON DELETE CASCADE,
    day             TEXT NOT NULL,
    used            REAL NOT NULL,          -- г; отрицательное — поправка вверх
    left_after      REAL NOT NULL,
    cost            REAL,                   -- ₽ израсходованного, если известна цена
    source          TEXT NOT NULL DEFAULT ''  -- weigh | slicer | job | edit
);
CREATE INDEX IF NOT EXISTS usage_spool ON usage (spool_id);
CREATE TABLE IF NOT EXISTS prints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    spool_id        INTEGER NOT NULL REFERENCES spools(id) ON DELETE CASCADE,
    photo           TEXT NOT NULL,
    caption         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS prints_spool ON prints (spool_id);
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    spool_id        INTEGER NOT NULL REFERENCES spools(id) ON DELETE CASCADE,
    day             TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    grams           REAL,
    minutes         INTEGER,
    cost            REAL
);
CREATE INDEX IF NOT EXISTS jobs_spool ON jobs (spool_id);
CREATE TABLE IF NOT EXISTS inbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ext_id          TEXT NOT NULL UNIQUE,      -- job_id из Moonraker
    day             TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    minutes         INTEGER,
    weights         TEXT NOT NULL,             -- JSON: граммы по филаментам
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | done | skipped
    printer_status  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    action          TEXT NOT NULL,
    spool_id        INTEGER NOT NULL,
    before          TEXT,                 -- JSON строки spools до изменения (NULL — катушка создана)
    after           TEXT,                 -- JSON после (NULL — удалена)
    created         TEXT NOT NULL,        -- JSON {таблица: [id]} — что добавилось
    deleted         TEXT NOT NULL,        -- JSON {таблица: [строки]} — что удалилось
    undone          INTEGER NOT NULL DEFAULT 0,
    user            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS audit_spool ON audit (spool_id);
CREATE TABLE IF NOT EXISTS login_fails (
    ip              TEXT NOT NULL,
    ts              TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    data            TEXT NOT NULL            -- JSON полей катушки
);
CREATE TABLE IF NOT EXISTS maintenance (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    every_hours     REAL,                 -- раз в N часов печати
    every_days      INTEGER,              -- и/или раз в N дней
    last_date       TEXT,
    last_hours      REAL NOT NULL DEFAULT 0,
    notes           TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS models (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    spool_id        INTEGER NOT NULL REFERENCES spools(id) ON DELETE CASCADE,
    job_id          INTEGER,
    name            TEXT NOT NULL,           -- исходное имя файла
    file            TEXT NOT NULL,           -- uuid.stl в uploads/
    size            INTEGER NOT NULL,
    triangles       INTEGER,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS models_spool ON models (spool_id);
CREATE TABLE IF NOT EXISTS tares (
    manufacturer    TEXT PRIMARY KEY COLLATE NOCASE,
    weight          REAL NOT NULL
);
"""
DEFAULT_TARE = 250  # вес пустой катушки, если не известен
# стартовый регламент для K2 Pro — ориентиры, правятся на странице «Принтер»
DEFAULT_MAINTENANCE = [
    ("Протереть стол (изопропил)", 50, None, "Жир с рук — главная причина отлипания"),
    ("Осмотр и чистка сопла, обдува", 200, None, "Нагар, налипший пластик, силиконовый носок"),
    ("Чистка подающих шестерён экструдера и CFS", 200, 90, "Пыль от филамента, проскальзывание"),
    ("Смазка направляющих и ходовых винтов", 300, 90, "Тонкий слой смазки, лишнее убрать"),
    ("Проверка натяжения ремней", None, 90, "Звук/частота по приложению или на слух"),
    ("Осушитель CFS / сушка влагопоглотителя", None, 30, "Если индикатор влажности выше нормы — раньше"),
    ("Замена сопла", 1000, None, "Закалённое сопло изнашивается медленнее; ориентир — качество поверхности"),
]
# через сколько дней после вскрытия/сушки открытую катушку пора сушить (по гигроскопичности)
DRY_AFTER_DAYS = {"PA": 7, "PC": 14, "TPU": 14, "PVA": 7, "PETG": 30, "ASA": 45, "ABS": 60, "PLA": 60}
DRY_DEFAULT_DAYS = 45


def dry_after(material):
    mat = material.upper()
    hits = [k for k in DRY_AFTER_DAYS if k in mat]
    return DRY_AFTER_DAYS[max(hits, key=len)] if hits else DRY_DEFAULT_DAYS

PROFILE_FIELDS = [("nozzle", "Сопло, °C"), ("bed", "Стол, °C"), ("speed", "Скорость, мм/с"),
                  ("fan", "Обдув, %"), ("drying", "Сушка"), ("notes", "Заметки")]
# типовые настройки — стартовая точка, правятся на странице «Настройки печати»
DEFAULT_PROFILES = [
    ("PLA", "195–215", "55–60", "50–100", "100", "45–50 °C, 4–6 ч", ""),
    ("PLA+", "205–225", "55–65", "50–100", "100", "45–50 °C, 4–6 ч", ""),
    ("PETG", "230–250", "70–85", "40–80", "30–50", "65 °C, 4–6 ч", "Липнет к PEI — используйте клей или текстурный стол"),
    ("ABS", "235–255", "95–110", "40–80", "0–20", "60–80 °C, 2–4 ч", "Нужна закрытая камера, вытяжка"),
    ("ASA", "240–260", "90–110", "40–80", "0–30", "60–80 °C, 4 ч", "Закрытая камера"),
    ("TPU", "215–235", "40–60", "15–30", "50–100", "50 °C, 6–8 ч", "Медленно, ретракт минимальный"),
    ("PA", "250–280", "70–90", "30–60", "0–30", "70–80 °C, 8–12 ч", "Очень гигроскопичен — печатать из сушилки"),
    ("PC", "260–300", "100–120", "30–60", "0–20", "80–90 °C, 6 ч", "Закрытая камера"),
]


# --- конфигурация -----------------------------------------------------------

def load_config(fresh=False):
    """fresh=True — заново с диска: после долгой сети, чтобы не затереть то, что сохранил другой запрос."""
    if has_request_context() and "_cfg" in g and not fresh:
        return copy.deepcopy(g._cfg)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    if "secret_key" not in cfg:
        cfg["secret_key"] = secrets.token_hex(32)
        save_config(cfg)
    if has_request_context():
        g._cfg = copy.deepcopy(cfg)
    return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix=".config-")  # 0600, своё имя — без гонок
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    if has_request_context():
        g._cfg = copy.deepcopy(cfg)


config = load_config()

app = Flask(__name__)
app.secret_key = config["secret_key"]
app.config.update(
    MAX_CONTENT_LENGTH=15 * 1024 * 1024,
    MAX_FORM_PARTS=10000,  # таблица быстрого заполнения — много полей
    SESSION_COOKIE_NAME="filament_session",  # не пересекаться с другими приложениями на домене
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FILAMENT_SECURE_COOKIE", "1") == "1",
)
# nginx передаёт X-Forwarded-Proto/Host/Prefix — приложение живёт под /filament
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


# --- база данных ------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        # LIKE в SQLite без учёта регистра только для латиницы — для кириллицы своя функция
        g.db.create_function("ulower", 1, lambda v: v.lower() if isinstance(v, str) else v, deterministic=True)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)
        cols = {r[1] for r in db.execute("PRAGMA table_info(spools)")}
        if "number" not in cols:  # миграция: номер-наклейка на катушке
            db.execute("ALTER TABLE spools ADD COLUMN number TEXT NOT NULL DEFAULT ''")
        if "tare" not in cols:  # миграция: вес пустой катушки
            db.execute("ALTER TABLE spools ADD COLUMN tare REAL")
        if "dried_date" not in cols:  # миграция: дата последней сушки
            db.execute("ALTER TABLE spools ADD COLUMN dried_date TEXT")
        if "location" not in cols:  # миграция: место хранения
            db.execute("ALTER TABLE spools ADD COLUMN location TEXT NOT NULL DEFAULT ''")
        if not db.execute("SELECT 1 FROM maintenance LIMIT 1").fetchone():
            db.executemany("INSERT INTO maintenance (name, every_hours, every_days, notes, last_date) "
                           "VALUES (?, ?, ?, ?, date('now'))", DEFAULT_MAINTENANCE)
        if "source" not in {r[1] for r in db.execute("PRAGMA table_info(usage)")}:  # миграция: откуда расход
            db.execute("ALTER TABLE usage ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        if "user" not in {r[1] for r in db.execute("PRAGMA table_info(audit)")}:  # миграция: кто менял
            db.execute("ALTER TABLE audit ADD COLUMN user TEXT NOT NULL DEFAULT ''")
        if "alert" not in cols:  # миграция: важная пометка на карточке
            db.execute("ALTER TABLE spools ADD COLUMN alert TEXT NOT NULL DEFAULT ''")
        if "ordered" not in cols:  # миграция: заказанные катушки
            db.execute("ALTER TABLE spools ADD COLUMN ordered INTEGER NOT NULL DEFAULT 0")
        if "job_id" not in {r[1] for r in db.execute("PRAGMA table_info(prints)")}:  # фото привязаны к печати
            db.execute("ALTER TABLE prints ADD COLUMN job_id INTEGER")
        if not db.execute("SELECT 1 FROM profiles LIMIT 1").fetchone():
            db.executemany("INSERT INTO profiles (material, nozzle, bed, speed, fan, drying, notes) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)", DEFAULT_PROFILES)
        sync.setup(db)


init_db()


# --- авторизация и CSRF -----------------------------------------------------

def admin_users(cfg=None):
    """{имя: хэш}. Старый одиночный пароль (admin_password_hash) — пользователь «admin»."""
    cfg = cfg if cfg is not None else load_config()
    users = dict(cfg.get("admins") or {})
    if not users and cfg.get("admin_password_hash"):
        users["admin"] = cfg["admin_password_hash"]
    return users


def is_admin():
    """Админ, если вошёл и его ещё не удалили (старые сессии без имени — это «admin»)."""
    if session.get("admin") is not True:
        return False
    if PHONE:  # на телефоне вход — по ключу, который передаёт приложение при запуске
        return session.get("phone") is True
    if "admin_ok" not in g:
        pw_hash = admin_users().get(session.get("user", "admin"))
        g.admin_ok = bool(pw_hash) and secrets.compare_digest(session.get("pwv", ""), pw_version(pw_hash))
    return g.admin_ok


def clean_password(pw):
    """Пароль, вставленный из буфера: терминал может обернуть его невидимыми ESC[200~ … ESC[201~
    (bracketed paste), а буфер — добавить пробел или перевод строки. Ни то ни другое не считаем."""
    return pw.replace("\x1b[200~", "").replace("\x1b[201~", "").strip()


def pw_version(pw_hash):
    """Метка пароля в сессии: сменили пароль — старые сессии недействительны."""
    return hashlib.sha256(pw_hash.encode()).hexdigest()[:16]


def current_user():
    if not has_request_context():
        return ""
    if request.path.startswith("/api/printer"):
        return "принтер"
    if request.path.startswith("/api/sync"):
        return "телефон"
    return session.get("user", "admin") if session.get("admin") else ""


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            flash("Для изменений нужно войти.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def safe_next(url):
    """Адрес «вернуться на» — только путь этого сайта. Браузер выкидывает из адреса табуляции и переводы
    строк («/\\t/evil.com» → «//evil.com»), поэтому любые управляющие символы и пробелы — отказ."""
    return bool(url) and url.startswith("/") and not url.startswith("//") and "\\" not in url \
        and ":" not in url.split("?")[0] and not any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url)


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


PUBLIC_ENDPOINTS = {"login", "static", "service_worker", "manifest", "offline", "api_printer_job", "showcase",
                    "api_sync", "api_sync_file", "phone_start", "theme",
                    "help_page"}  # у API и запуска на телефоне — своя проверка ключа


@app.before_request
def check_viewer():
    """Если задан пароль на просмотр — без входа видна только страница входа."""
    if request.endpoint in PUBLIC_ENDPOINTS or is_admin():
        return None
    viewer_hash = load_config().get("viewer_password_hash")
    if session.get("viewer") is True and viewer_hash and \
            secrets.compare_digest(session.get("pwv", ""), pw_version(viewer_hash)):
        return None
    if viewer_hash:
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    return None


@app.after_request
def security_headers(resp):
    """CSP; не кэшировать страницы входа и админа (в них CSRF-токен)."""
    # HSTS, X-Frame-Options, nosniff, Referrer-Policy ставит nginx (snippets/security-headers.conf);
    # повтор с другими значениями браузеры считают ошибкой. CSP — только здесь.
    resp.headers.setdefault("Content-Security-Policy", CSP)
    if request.endpoint == "login" or (is_admin() and resp.mimetype == "text/html"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
       "form-action 'self'; frame-ancestors 'none'")
LOGIN_MAX_FAILS = 10       # неудачных попыток с одного адреса…
LOGIN_WINDOW_MIN = 15      # …за столько минут — дальше временный отказ


def login_blocked(ip):
    return get_db().execute("SELECT COUNT(*) FROM login_fails WHERE ip = ? AND ts > datetime('now', ?)",
                            (ip, "-%d minutes" % LOGIN_WINDOW_MIN)).fetchone()[0] >= LOGIN_MAX_FAILS


def login_failed(ip):
    db = get_db()
    db.execute("INSERT INTO login_fails (ip) VALUES (?)", (ip,))
    db.execute("DELETE FROM login_fails WHERE ts < datetime('now', '-1 day')")
    db.commit()


@app.before_request
def check_csrf():
    if request.method == "POST" and not request.path.startswith("/api/"):  # API — по токену
        token = request.form.get("csrf")
        if not token or not secrets.compare_digest(token, session.get("csrf", "")):
            abort(400, "Неверный CSRF-токен, обновите страницу.")


# --- вычисления -------------------------------------------------------------

def grams_to_meters(grams, material, diameter):
    """Длина прутка: масса / (плотность × площадь сечения). 1000 г PLA 1,75 мм ≈ 335 м."""
    area = math.pi * ((diameter or 1.75) / 2) ** 2  # мм²
    return grams / (slicer.density(material) * area) if grams and grams > 0 else 0  # г/см³·мм² → м


def spool_view(row):
    s = dict(row)
    total, left, price = s["weight_total"], s["weight_left"], s["price"]
    s["percent_left"] = max(0, min(100, round(left / total * 100))) if total else 0
    s["meters_left"] = grams_to_meters(left, s["material"], s["diameter"])
    s["price_per_kg"] = price / total * 1000 if price and total else None
    s["value_left"] = price * left / total if price and total else None
    s["needs_drying"] = False
    since = max(filter(None, [s["opened_date"], s.get("dried_date")]), default=None)
    if s["opened_date"] and left > 0 and since:
        try:
            age = (date.today() - date.fromisoformat(since)).days
            s["needs_drying"] = age >= dry_after(s["material"])
            s["dry_age"] = age
        except ValueError:
            pass
    names = importer.colors_from_name(s["color_name"] or "")
    if len(names) > 1:  # многоцветная/радужная: градиент, первый цвет — выбранный вручную
        stops = [s["color_hex"]] + [h for h in names[1:] if h != s["color_hex"]] if names != importer.RAINBOW else names
        s["swatch"] = "linear-gradient(135deg, %s)" % ", ".join(stops)
    else:
        s["swatch"] = s["color_hex"]
    if s.get("ordered"):
        s["status"], s["status_label"] = "ordered", "Заказана"
    elif left <= 0:
        s["status"], s["status_label"] = "empty", "Закончилась"
    elif s["opened_date"]:
        s["status"], s["status_label"] = "opened", "Вскрыта"
    else:
        s["status"], s["status_label"] = "sealed", "Запечатана"
    return s


@app.template_filter("money")
def money(value):
    if value is None:
        return "—"
    return "{:,.0f} ₽".format(value).replace(",", " ")


@app.template_filter("meters")
def meters(value):
    return "≈ {:,.0f} м".format(value).replace(",", " ") if value else ""


@app.template_filter("grams")
def grams(value):
    return "{:,.0f} г".format(value).replace(",", " ")


@app.template_filter("field_num")
def field_num(value, default=""):
    """Число для поля формы: 1000.0 -> "1000", пусто/нет -> default."""
    if value is None or value == "" or not isinstance(value, (int, float, str)):
        return default
    try:
        return "{:g}".format(float(str(value).replace(",", ".")))
    except ValueError:
        return value


@app.template_filter("duration")
def duration(minutes):
    if not minutes:
        return "—"
    h, m = divmod(int(minutes), 60)
    return ("%d ч %02d мин" % (h, m)) if h else ("%d мин" % m)


@app.template_filter("ru_date")
def ru_date(value):
    if not value:
        return "—"
    try:
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    except ValueError:
        return value
