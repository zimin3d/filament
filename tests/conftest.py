import io
import os
import re
import shutil
import sys
import tempfile

import pytest

# данные — во временной папке, до импорта приложения
DATA = tempfile.mkdtemp(prefix="filament-test-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)  # в RAM — быстрее
os.environ["FILAMENT_DATA"] = DATA
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as filament  # noqa: E402
from PIL import Image  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

ADMIN_PW = "admin-secret"
ADMIN_HASH = generate_password_hash(ADMIN_PW, method="pbkdf2:sha256:1000")  # быстрый хэш для тестов


@pytest.fixture(autouse=True)
def fresh_data():
    """Чистая база и config перед каждым тестом."""
    for name in os.listdir(DATA):
        path = os.path.join(DATA, name)
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    filament.save_config({"secret_key": "test", "admin_password_hash": ADMIN_HASH})
    filament.init_db()
    sys.modules["cli"].PUBLIC_URL = "https://filament.example/filament/"
    yield


@pytest.fixture
def anon():
    return filament.app.test_client()


@pytest.fixture
def admin():
    c = filament.app.test_client()
    with c.session_transaction() as s:
        s["admin"] = True
        s["pwv"] = filament.pw_version(ADMIN_HASH)
        s["csrf"] = "t"
    return c


@pytest.fixture
def db():
    import sqlite3
    conn = sqlite3.connect(filament.DB_PATH)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def jpeg(size=(300, 200), color="blue"):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "JPEG")
    b.seek(0)
    return b


def flashes(client, response):
    """Сообщения после редиректа."""
    html = client.get(response.location).get_data(as_text=True) if response.location else response.get_data(as_text=True)
    return re.findall(r'flash (\w+)">([^<]*)', html)


def add_spool(client, **kw):
    """Создаёт катушку через форму; photo=(файл, имя) — с фото."""
    data = {"csrf": "t", "manufacturer": "Plastiq", "material": "PETG", "color_name": "красный",
            "weight_total": "1000", "price": "1000"}
    data.update(kw)
    r = client.post("/spool/new", data=data, content_type="multipart/form-data")
    assert r.status_code == 302, r.get_data(as_text=True)[:500]
    return int(r.location.rstrip("/").rsplit("/", 1)[1])
