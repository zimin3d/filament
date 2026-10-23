import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import app as filament
from conftest import add_spool, flashes

NOW = time.time()
HISTORY = [  # новые сверху, как отдаёт Moonraker
    {"job_id": "000B", "status": "completed", "filename": "cube.gcode", "end_time": NOW + 60, "print_duration": 1800,
     "filament_used": 1000, "metadata": {"filament_weight_total": 12.5}},
    {"job_id": "000A", "status": "completed", "filename": "old.gcode", "end_time": NOW - 3600, "print_duration": 600,
     "filament_used": 500, "metadata": {"filament_weight_total": 5}},
]


class Moonraker(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/printer/objects/query"):
            body = {"result": {"status": {"print_stats": {"state": "printing", "filename": "cube.gcode", "print_duration": 600},
                                          "display_status": {"progress": 0.42},
                                          "extruder": {"temperature": 219.6, "target": 220},
                                          "heater_bed": {"temperature": 60.1, "target": 60}}}}
        elif self.path.startswith("/server/history/list"):
            body = {"result": {"count": len(HISTORY), "jobs": HISTORY}}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def printer():
    srv = HTTPServer(("127.0.0.1", 0), Moonraker)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port
    srv.shutdown()


def test_printer_link(admin, db, printer):
    sid = add_spool(admin, number="3")
    admin.post("/spool/%d/on-printer" % sid, data={"csrf": "t"})
    r = admin.post("/printer/link", data={"csrf": "t", "action": "save", "host": "127.0.0.1", "port": str(printer)})
    assert ("ok", "Сохранено.") in flashes(admin, r)
    cfg = filament.load_config()
    cfg["printer_since"] = NOW  # старая печать 000A закончилась до подключения
    filament.save_config(cfg)

    html = admin.get("/printer/link").get_data(as_text=True)
    assert "печатает" in html and "42%" in html and "220°" in html

    r = admin.post("/printer/link", data={"csrf": "t", "action": "poll"})
    assert ("ok", "Новых печатей: 1 (списано сразу 1, во «Входящих» 0).") in flashes(admin, r)
    assert db.execute("SELECT weight_left FROM spools WHERE id = ?", (sid,)).fetchone()[0] == 987.5
    assert [r[0] for r in db.execute("SELECT ext_id FROM inbox")] == ["000B"]
    r = admin.post("/printer/link", data={"csrf": "t", "action": "poll"})  # повтор — без двойного списания
    assert ("ok", "Новых печатей: 0 (списано сразу 0, во «Входящих» 0).") in flashes(admin, r)


def test_printer_link_offline_and_validation(admin):
    r = admin.post("/printer/link", data={"csrf": "t", "action": "save", "host": "bad host;rm", "port": "7125"})
    assert flashes(admin, r)[0][0] == "error"
    admin.post("/printer/link", data={"csrf": "t", "action": "save", "host": "127.0.0.1", "port": "1"})
    assert "нет связи" in admin.get("/printer/link").get_data(as_text=True)
