import json
import os
import sys

import app as filament
from conftest import add_spool

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))
import k2_agent  # noqa: E402


def api(client, payload, token="secret"):
    return client.post("/api/printer/job", data=json.dumps(payload), content_type="application/json",
                       headers={"Authorization": "Bearer " + token})


def set_token():
    cfg = filament.load_config()
    cfg["printer_token"] = "secret"
    filament.save_config(cfg)


def test_api_auth(anon):
    assert api(anon, {"job_id": "1", "weights": [5]}).status_code == 403  # токен не задан
    set_token()
    assert api(anon, {"job_id": "1", "weights": [5]}, token="bad").status_code == 403
    assert api(anon, {"job_id": "1"}).status_code == 400


def test_single_filament_auto(admin, anon, db):
    set_token()
    sid = add_spool(admin)
    admin.post("/spool/%d/on-printer" % sid, data={"csrf": "t"})
    r = api(anon, {"job_id": "7", "name": "cube.gcode", "weights": [12.5], "minutes": 30, "status": "completed"})
    assert r.get_json() == {"ok": True, "auto": True}
    assert db.execute("SELECT weight_left FROM spools").fetchone()[0] == 987.5
    assert tuple(db.execute("SELECT name, grams, minutes FROM jobs").fetchone()) == ("cube", 12.5, 30)
    assert api(anon, {"job_id": "7", "weights": [12.5]}).get_json()["duplicate"]  # повтор не списывает
    assert db.execute("SELECT weight_left FROM spools").fetchone()[0] == 987.5


def test_multicolor_goes_to_inbox(admin, anon, db):
    set_token()
    a, b = add_spool(admin, number="1"), add_spool(admin, number="2")
    api(anon, {"job_id": "8", "name": "logo", "weights": [20, 3.5], "status": "cancelled"})
    assert "Входящие: 1" in admin.get("/").get_data(as_text=True)
    html = admin.get("/inbox").get_data(as_text=True)
    assert "logo (отменена)" in html
    item = db.execute("SELECT id FROM inbox").fetchone()[0]
    admin.post("/inbox", data={"csrf": "t", "id": item, "spool_0": a, "grams_0": "20", "spool_1": b, "grams_1": "3,5"})
    left = {r[0]: r[1] for r in db.execute("SELECT number, weight_left FROM spools")}
    assert left == {"1": 980, "2": 996.5}
    assert "Входящие" not in admin.get("/").get_data(as_text=True)


def test_agent_payload():
    job = {"job_id": "00001A", "filename": "sub/benchy.gcode", "status": "cancelled", "print_duration": 1800,
           "end_time": 1760000000, "filament_used": 500,
           "metadata": {"filament_total": 1000, "filament_weight_total": 10.0}}
    p = k2_agent.job_payload(job)
    assert (p["name"], p["weights"], p["minutes"], p["status"]) == ("benchy.gcode", [5.0], 30, "cancelled")
    job["metadata"] = {"filament_weights": [8, 2], "filament_total": 1000}
    job["status"] = "completed"
    assert k2_agent.job_payload(job)["weights"] == [8, 2]


def test_agent_poll(admin, anon, db, monkeypatch, tmp_path):
    """Агент целиком: первый запуск пропускает старые печати, затем шлёт новые через API."""
    set_token()
    sid = add_spool(admin)
    admin.post("/spool/%d/on-printer" % sid, data={"csrf": "t"})
    monkeypatch.setattr(k2_agent, "STATE", str(tmp_path / "state.json"))
    history = [{"job_id": "old", "status": "completed", "filename": "old.gcode", "metadata": {"filament_weight_total": 50}}]
    monkeypatch.setattr(k2_agent, "get_json", lambda url, timeout=10: {"result": {"jobs": history}})

    def fake_send(server, token, payload):
        r = api(anon, payload, token)
        return r.get_json()
    monkeypatch.setattr(k2_agent, "send", fake_send)
    args = type("A", (), {"printer": "p", "port": 7125, "server": "s", "token": "secret", "backfill": 0})
    state = k2_agent.poll(args, None)
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    history.insert(0, {"job_id": "new", "status": "completed", "filename": "new.gcode", "print_duration": 600,
                       "metadata": {"filament_weight_total": 15}})
    history.insert(0, {"job_id": "running", "status": "in_progress", "filename": "x.gcode", "metadata": {}})
    k2_agent.poll(args, state)
    assert [tuple(r) for r in db.execute("SELECT name, grams FROM jobs")] == [("new", 15)]


HISTORY = {"result": {"count": 4, "jobs": [
    {"job_id": "000A1", "filename": "benchy.gcode", "status": "completed", "print_duration": 3600, "end_time": 1750000000,
     "filament_used": 1000, "metadata": {"filament_weight_total": 12.0, "filament_total": 1000}},
    {"job_id": "000A2", "filename": "logo.3mf", "status": "cancelled", "print_duration": 1800, "end_time": 1750100000,
     "filament_used": 500, "metadata": {"filament_weights": [10, 10], "filament_total": 1000}},
    {"job_id": "000A3", "filename": "now.gcode", "status": "in_progress", "print_duration": 60, "metadata": {}},
    "garbage"]}}


def test_import_history(admin, db):
    import io
    up = lambda **extra: admin.post("/import-history", data=dict(
        {"csrf": "t", "history": (io.BytesIO(json.dumps(HISTORY).encode()), "history.json")}, **extra),
        content_type="multipart/form-data")
    sid = add_spool(admin)
    with filament.app.app_context():
        base = filament.printer_hours()
    r = up()
    assert "добавлено 2 печатей (1.5 ч)" in admin.get(r.location).get_data(as_text=True)
    assert db.execute("SELECT weight_left FROM spools WHERE id = ?", (sid,)).fetchone()[0] == 1000  # без списания
    with filament.app.app_context():
        assert filament.printer_hours() == base + 1.5
    up()  # повтор — без дублей
    assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 2
    assert "Входящие" not in admin.get("/").get_data(as_text=True)
    r = admin.post("/import-history", data={"csrf": "t", "history": (io.BytesIO(b"{}"), "x.json")},
                   content_type="multipart/form-data")
    assert "не история Moonraker" in admin.get(r.location).get_data(as_text=True)


def test_import_history_to_inbox_and_skip_all(admin, db):
    import io
    HISTORY["result"]["jobs"][0]["job_id"] = "B1"
    HISTORY["result"]["jobs"][1]["job_id"] = "B2"
    admin.post("/import-history", data={"csrf": "t", "to_inbox": "1",
                                        "history": (io.BytesIO(json.dumps(HISTORY).encode()), "h.json")},
               content_type="multipart/form-data")
    assert "Входящие: 2" in admin.get("/").get_data(as_text=True)
    admin.post("/inbox", data={"csrf": "t", "skip_all": "1"})
    assert db.execute("SELECT COUNT(*) FROM inbox WHERE status = 'pending'").fetchone()[0] == 0


def test_import_history_garbage(admin, db):
    import io
    for payload in [{"jobs": [{"status": "completed", "metadata": "x", "job_id": 1, "print_duration": 60}]},
                    {"jobs": [{"status": "completed", "end_time": "abc", "print_duration": "z",
                               "metadata": {"filament_weight_total": 5}, "job_id": "g2"}]},
                    {"jobs": [{"status": "completed", "end_time": 1e20, "print_duration": float("nan"),
                               "metadata": {"filament_weights": "oops", "filament_weight_total": 5}, "job_id": "g3"}]}]:
        r = admin.post("/import-history", data={"csrf": "t", "history": (io.BytesIO(json.dumps(payload).encode()), "h.json")},
                       content_type="multipart/form-data")
        assert r.status_code == 302, payload
