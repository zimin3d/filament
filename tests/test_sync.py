import json
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager

import pytest

import app as filament
import core
import sync
from conftest import ADMIN_HASH, add_spool, jpeg

PATH_NAMES = ("DATA_DIR", "DB_PATH", "UPLOAD_DIR", "THUMB_DIR", "CONFIG_PATH", "IMPORT_DIR")


def paths_for(data):
    return {"DATA_DIR": data, "DB_PATH": os.path.join(data, "filament.db"), "UPLOAD_DIR": os.path.join(data, "uploads"),
            "THUMB_DIR": os.path.join(data, "thumbs"), "CONFIG_PATH": os.path.join(data, "config.json"),
            "IMPORT_DIR": os.path.join(data, "import")}


@contextmanager
def device(paths):
    """Переключает приложение на данные другого «устройства» (во всех модулях, где пути импортированы)."""
    mods = [m for name, m in list(sys.modules.items())
            if m and (name in ("core", "logic", "cli", "app") or name.startswith("views."))]
    saved = [(m, k, getattr(m, k)) for m in mods for k in PATH_NAMES if hasattr(m, k)]
    for m, k, _ in saved:
        setattr(m, k, paths[k])
    try:
        yield
    finally:
        for m, k, v in saved:
            setattr(m, k, v)


@pytest.fixture
def phone():
    server = {k: getattr(core, k) for k in PATH_NAMES}
    data = tempfile.mkdtemp(prefix="filament-phone-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
    paths = paths_for(data)
    with device(paths):
        filament.save_config({"secret_key": "test", "admin_password_hash": ADMIN_HASH,
                              "sync_url": "https://server.example/", "sync_token": "tok"})
        core.init_db()
    cfg = filament.load_config()
    cfg["sync_token"] = "tok"
    filament.save_config(cfg)

    client = filament.app.test_client()

    def transport(path, data=None, method=None, raw=False):
        # запрос к «серверу» внутри контекста «телефона»: Flask переиспользует контекст приложения,
        # поэтому соединение и config телефона из g убираем на время запроса
        from flask import g
        stash = {k: g.pop(k) for k in ("db", "_cfg") if k in g}
        try:
            return server_request(path, data, method)
        finally:
            server_db = g.pop("db", None)
            if server_db is not None:
                server_db.close()
            g.pop("_cfg", None)
            for k, v in stash.items():
                setattr(g, k, v)

    def server_request(path, data, method):
        with device(server):
            headers = {"Authorization": "Bearer tok"}
            if path == "api/sync":
                r = client.post("/" + path, json=data, headers=headers)
            elif method == "PUT":
                r = client.put("/" + path, data=data, headers=headers)
            else:
                r = client.get("/" + path, headers=headers)
            if r.status_code == 404:
                import urllib.error
                raise urllib.error.HTTPError(path, 404, "not found", {}, None)
            assert r.status_code == 200, r.get_data(as_text=True)[:300]
            return r.get_data()

    def run():
        with device(paths), filament.app.test_request_context():
            return filament.views.sync.run_sync(transport)

    @contextmanager
    def use():
        with device(paths):
            yield

    yield type("Phone", (), {"run": staticmethod(run), "use": staticmethod(use), "paths": paths})


def mem_db():
    db = sqlite3.connect(":memory:")
    db.executescript(core.SCHEMA)
    sync.setup(db)
    return db


def test_engine_fk_by_uid_update_delete_conflict():
    a, b = mem_db(), mem_db()
    b.execute("INSERT INTO spools (manufacturer, material) VALUES ('X', 'PLA')")  # сдвигаем id на b
    a.execute("INSERT INTO spools (manufacturer, material, color_name) VALUES ('Plastiq', 'PETG', 'red')")
    a.execute("INSERT INTO usage (spool_id, day, used, left_after) VALUES (1, '2026-10-01', 50, 950)")
    sync.apply(b, sync.export(a, 0), log=False)
    sid = b.execute("SELECT id FROM spools WHERE manufacturer = 'Plastiq'").fetchone()[0]
    assert sid == 2 and b.execute("SELECT spool_id, used FROM usage").fetchone() == (2, 50)
    assert b.execute("SELECT COUNT(*) FROM sync_log WHERE tbl = 'usage'").fetchone()[0] == 0  # не уйдёт назад

    seen = sync.last_seq(a)
    b.execute("UPDATE spools SET weight_left = 700 WHERE id = 2")
    b.execute("DELETE FROM usage")
    sync.apply(a, sync.export(b, 0), newer_after=seen)
    assert a.execute("SELECT weight_left FROM spools").fetchone()[0] == 700
    assert a.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0

    # конфликт: на a поменяли позже, чем на b — a не уступает
    b.execute("UPDATE spools SET color_name = 'blue' WHERE id = 2")
    seen = sync.last_seq(a)
    a.execute("UPDATE spools SET color_name = 'green'")
    changes = [c for c in sync.export(b, 0) if c["t"] == "spools" and c.get("row", {}).get("color_name") == "blue"]
    changes[0]["ts"] -= 1
    lost = []
    assert sync.apply(a, changes, newer_after=seen, conflicts=lost) == (0, 1)
    assert a.execute("SELECT color_name FROM spools").fetchone()[0] == "green"
    assert [sync.describe(a, t, u, r) for t, u, r in lost] == ["PETG green"]
    a.execute("UPDATE spools SET number = '12'")
    a.execute("INSERT INTO usage (spool_id, day, used, left_after) VALUES (1, '2026-10-02', 5, 695)")
    uid = a.execute("SELECT uid FROM usage").fetchone()[0]
    assert sync.describe(a, "usage", uid) == "№12 PETG green — расход"
    assert sync.describe(a, "profiles", "0" * 32) == "настройки печати"


def test_phone_roundtrip(admin, db, phone):
    sid = add_spool(admin, number="7", photo=(jpeg(), "a.jpg"))
    admin.post("/spool/%d/use" % sid, data={"csrf": "t", "grams": "100"})
    photo = db.execute("SELECT photo FROM spools").fetchone()[0]

    r = phone.run()
    assert r["received"] >= 2 and r["got_files"] == 1
    with phone.use():
        core.init_db()  # перезапуск приложения
    assert phone.run()["sent"] == 0
    with phone.use():
        pdb = sqlite3.connect(phone.paths["DB_PATH"])
        assert pdb.execute("SELECT number, weight_left, photo FROM spools").fetchall() == [("7", 900, photo)]
        assert pdb.execute("SELECT COUNT(*) FROM maintenance").fetchone()[0] == \
            db.execute("SELECT COUNT(*) FROM maintenance").fetchone()[0]  # стартовый регламент не задвоился
        assert os.path.exists(os.path.join(phone.paths["UPLOAD_DIR"], photo))
        # правка на телефоне без сети + новая катушка с фото
        pid = pdb.execute("SELECT id FROM spools").fetchone()[0]
        admin.post("/spool/%d/use" % pid, data={"csrf": "t", "grams": "50"})
        add_spool(admin, number="8", color_name="синий", photo=(jpeg(color="red"), "b.jpg"))

    r = phone.run()
    assert r["sent"] >= 3 and r["sent_files"] == 1
    rows = db.execute("SELECT number, weight_left, photo FROM spools ORDER BY number").fetchall()
    assert [(n, w) for n, w, _ in rows] == [("7", 850), ("8", 1000)]
    assert os.path.exists(os.path.join(filament.UPLOAD_DIR, rows[1][2]))
    assert db.execute("SELECT SUM(used) FROM usage WHERE spool_id = ?", (sid,)).fetchone()[0] == 150

    # удаление на сервере доходит до телефона
    admin.post("/spool/%d/delete" % sid, data={"csrf": "t"})
    phone.run()
    with phone.use():
        pdb = sqlite3.connect(phone.paths["DB_PATH"])
        assert [r[0] for r in pdb.execute("SELECT number FROM spools")] == ["8"]
        assert pdb.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0
    assert phone.run()["sent"] == 0  # повтор — без лишних отправок


def test_sync_api_needs_token(anon):
    assert anon.post("/api/sync", json={}).status_code == 403
    cfg = filament.load_config()
    cfg["sync_token"] = "tok"
    filament.save_config(cfg)
    assert anon.post("/api/sync", json={}, headers={"Authorization": "Bearer bad"}).status_code == 403
    assert anon.get("/api/sync/file/" + "0" * 32 + ".jpg", headers={"Authorization": "Bearer bad"}).status_code == 403
    assert anon.get("/api/sync/file/..%2fconfig.json", headers={"Authorization": "Bearer tok"}).status_code in (403, 404)
    r = anon.post("/api/sync", json={"after": 0}, headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200 and r.get_json()["ok"]


def test_sync_page_token(admin):
    r = admin.post("/sync", data={"csrf": "t", "action": "token"})
    tok = filament.load_config()["sync_token"]
    assert tok in r.get_data(as_text=True)
    page = admin.get("/sync").get_data(as_text=True)
    assert tok not in page  # показывается один раз
    assert "github.com/zimin3d/filament/releases/latest" in page


def test_app_update_check(monkeypatch):
    vs = filament.views.sync
    assert vs.version_key("v0.10") > vs.version_key("v0.9") > vs.version_key("dev") == (0,)
    release = {"tag_name": "v0.4", "assets": [{"name": "filament-v0.4.apk",
               "browser_download_url": vs.APK_PREFIX + "v0.4/filament-v0.4.apk"}]}
    fetch = lambda url: json.dumps(release).encode()  # noqa: E731
    monkeypatch.setenv("FILAMENT_APP_VERSION", "v0.3")
    r = vs.check_app_update(fetch)
    assert r["newer"] and r["apk"].endswith("v0.4.apk")
    monkeypatch.setenv("FILAMENT_APP_VERSION", "v0.4")
    assert not vs.check_app_update(fetch)["newer"]
    release["assets"][0]["browser_download_url"] = "https://evil.example/x.apk"  # чужой адрес не предлагаем
    monkeypatch.setenv("FILAMENT_APP_VERSION", "v0.3")
    assert not vs.check_app_update(fetch)["newer"]


def test_app_update_install_outside_app(admin):
    r = admin.get("/app-update/install?url=x")
    assert r.status_code == 302 and r.location.endswith("/sync")


def test_phone_sees_lost_edits(admin, db, phone):
    """Одну катушку правили и на телефоне, и (позже) на сервере — телефон показывает, что его правка не прошла."""
    import time
    add_spool(admin, number="5", color_name="красный")
    phone.run()
    with phone.use():
        pdb = sqlite3.connect(phone.paths["DB_PATH"])
        pdb.execute("UPDATE spools SET color_name = 'алый'")
        pdb.commit()
    time.sleep(0.05)
    db.execute("UPDATE spools SET color_name = 'бордовый'")
    db.commit()
    r = phone.run()
    assert r["conflicts"] == ["№5 PETG бордовый"]
    assert db.execute("SELECT color_name FROM spools").fetchone()[0] == "бордовый"
    with phone.use():
        assert sqlite3.connect(phone.paths["DB_PATH"]).execute("SELECT color_name FROM spools").fetchone()[0] == "бордовый"
        assert "Не применено с телефона" in filament.views.sync.sync_summary(r)
    # следующая (авто)синхронизация без конфликтов список не стирает — только «Понятно»
    assert phone.run()["conflicts"] == []
    with phone.use(), filament.app.test_request_context():
        assert [c["what"] for c in json.loads(sync.state(filament.get_db(), "conflicts", "[]"))] == ["№5 PETG бордовый"]


def test_conflict_names_deleted_spool():
    a = mem_db()
    a.execute("INSERT INTO spools (manufacturer, material, color_name, number) VALUES ('X', 'PLA', 'синий', '9')")
    sid_uid = a.execute("SELECT uid FROM spools").fetchone()[0]
    assert sync.describe(a, "spools", "f" * 32, {"number": "4", "material": "PETG", "color_name": "белый"}) \
        == "№4 PETG белый (удалена здесь)"
    assert sync.describe(a, "usage", "e" * 32, {"spool_id": sid_uid}) == "№9 PLA синий — расход"
