import json
import socket
import struct
import threading

import pytest

import app as filament
import bambu
from conftest import add_spool, flashes


def _str(b):
    return struct.pack("!H", len(b)) + b


class FakeBambu:
    """MQTT-брокер принтера: проверяет логин bblp/код и на pushall отдаёт текущий отчёт."""

    def __init__(self, code="12345678", serial="01P00A000000001"):
        self.code, self.serial, self.report = code, serial, {}
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        try:
            while True:
                kind, body = bambu._read_packet(conn)
                if kind >> 4 == 1:  # CONNECT: …, user, password в конце
                    ok = body.endswith(_str(b"bblp") + _str(self.code.encode()))
                    conn.sendall(bytes([0x20, 2, 0, 0 if ok else 5]))
                elif kind >> 4 == 8:
                    conn.sendall(bytes([0x90, 3]) + body[:2] + b"\x00")
                elif kind >> 4 == 3 and b"pushall" in body:
                    topic = ("device/%s/report" % self.serial).encode()
                    conn.sendall(bambu._packet(0x30, _str(topic) + json.dumps({"print": self.report}).encode()))
                elif kind >> 4 == 14:
                    return
        except (OSError, bambu.BambuError):
            pass
        finally:
            conn.close()


def report(state, remain, percent=0):
    return {"gcode_state": state, "subtask_name": "benchy", "task_id": "777", "mc_percent": percent,
            "mc_remaining_time": 30, "nozzle_temper": 219.5, "nozzle_target_temper": 220, "bed_temper": 55,
            "bed_target_temper": 55, "ams": {"ams": [{"id": "0", "tray": [
                {"id": "0", "tray_type": "PLA", "tray_color": "FF0000FF", "remain": remain, "tray_weight": "1000"}]}]}}


@pytest.fixture
def fake():
    f = FakeBambu()
    yield f
    f.srv.close()


def setup_printer(admin, fake, code="12345678"):
    r = admin.post("/printer/link", data={"csrf": "t", "action": "save", "kind": "bambu", "host": "127.0.0.1",
                                          "port": "7125", "serial": fake.serial, "code": code})
    cfg = filament.load_config()
    cfg.update(bambu_mqtt_port=fake.port, bambu_plain=True, bambu_ftp_port=1)  # без TLS; FTPS недоступен
    filament.save_config(cfg)
    return r


def test_bambu_status_and_job_by_ams(admin, db, fake):
    sid = add_spool(admin, number="9")
    admin.post("/spool/%d/on-printer" % sid, data={"csrf": "t"})
    assert ("ok", "Сохранено.") in flashes(admin, setup_printer(admin, fake))

    fake.report = report("RUNNING", 80, 40)
    html = admin.get("/printer/link").get_data(as_text=True)
    assert "печатает" in html and "40%" in html and "AMS" in html and "#ff0000" in html and "80%" in html

    admin.post("/printer/link", data={"csrf": "t", "action": "poll"})  # началась: запомнили остаток 80%
    fake.report = report("FINISH", 77, 100)
    r = admin.post("/printer/link", data={"csrf": "t", "action": "poll"})
    assert ("ok", "Новых печатей: 1 (списано сразу 1, во «Входящих» 0).") in flashes(admin, r)
    assert db.execute("SELECT weight_left FROM spools WHERE id = ?", (sid,)).fetchone()[0] == 970  # 3% от 1 кг
    r = admin.post("/printer/link", data={"csrf": "t", "action": "poll"})  # та же печать — не дважды
    assert "Новых печатей: 0" in flashes(admin, r)[0][1]


def test_bambu_file_weights_and_failed(admin, db, fake, monkeypatch):
    monkeypatch.setattr(bambu, "print_file_weights", lambda *a, **k: [20.0, 10.0])
    setup_printer(admin, fake)
    fake.report = report("RUNNING", -1, 10)
    admin.post("/printer/link", data={"csrf": "t", "action": "poll"})
    fake.report = report("FAILED", -1, 50)
    admin.post("/printer/link", data={"csrf": "t", "action": "poll"})
    row = db.execute("SELECT name, weights, printer_status FROM inbox").fetchone()
    assert json.loads(row["weights"]) == [10.0, 5.0] and row["printer_status"] == "error"  # половина сделана


def test_bambu_finished_before_connect_ignored(admin, db, fake):
    setup_printer(admin, fake)
    fake.report = report("FINISH", 50, 100)
    admin.post("/printer/link", data={"csrf": "t", "action": "poll"})
    assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0


def test_bambu_wrong_code(admin, fake):
    setup_printer(admin, fake, code="wrong000")
    assert "не принял код доступа" in admin.get("/printer/link").get_data(as_text=True)
