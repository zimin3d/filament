import re

from werkzeug.security import generate_password_hash

import app as filament
from conftest import ADMIN_PW, add_spool, flashes

FAST = "pbkdf2:sha256:1000"


def login(client, password, user=None):
    client.get("/login")
    with client.session_transaction() as s:
        tok = s["csrf"]
    data = {"csrf": tok, "password": password}
    if user:
        data["user"] = user
    return client.post("/login", data=data)


def test_legacy_single_admin_still_works(anon):
    assert 'name="user"' not in anon.get("/login").get_data(as_text=True)
    r = login(anon, ADMIN_PW)
    assert r.status_code == 302 and anon.get("/spool/new").status_code == 200


def test_two_admins_and_journal_author(anon, db):
    filament.set_admin("vasya", generate_password_hash("vasya-pw", method=FAST))
    cfg = filament.load_config()
    assert set(cfg["admins"]) == {"admin", "vasya"} and "admin_password_hash" not in cfg
    assert 'name="user"' in anon.get("/login").get_data(as_text=True)
    assert "Неверное имя или пароль" in login(anon, ADMIN_PW, "vasya").get_data(as_text=True)
    login(anon, "vasya-pw", "Vasya")
    with anon.session_transaction() as s:
        s["csrf"] = "t"
    sid = add_spool(anon)
    assert db.execute("SELECT user FROM audit WHERE spool_id = ?", (sid,)).fetchone()[0] == "vasya"
    assert ">vasya<" in anon.get("/history").get_data(as_text=True)


def test_removed_admin_loses_access(anon):
    filament.set_admin("vasya", generate_password_hash("vasya-pw", method=FAST))
    login(anon, "vasya-pw", "vasya")
    assert anon.get("/spool/new").status_code == 200
    filament.remove_admin("vasya")
    assert anon.get("/spool/new").status_code == 302
    try:
        filament.remove_admin("admin")
        assert False, "последнего удалять нельзя"
    except ValueError:
        pass


def test_printer_author(anon, db):
    cfg = filament.load_config()
    cfg["printer_token"] = "secret"
    cfg["printer_spool"] = None
    filament.save_config(cfg)
    admin = filament.app.test_client()
    with admin.session_transaction() as s:
        s["admin"] = True
        s["pwv"] = filament.pw_version(filament.admin_users()["admin"])
        s["csrf"] = "t"
    sid = add_spool(admin)
    admin.post("/spool/%d/on-printer" % sid, data={"csrf": "t"})
    anon.post("/api/printer/job", json={"job_id": "1", "weights": [5]}, headers={"Authorization": "Bearer secret"})
    assert db.execute("SELECT user FROM audit ORDER BY id DESC").fetchone()[0] == "принтер"
