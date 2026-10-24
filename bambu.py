"""Принтеры Bambu Lab в локальной сети: состояние по MQTT (TLS, порт 8883), файл печати по FTPS (990).

Вход — пользователь «bblp» и код доступа LAN (на экране принтера: Настройки → Сеть / WLAN).
Истории печатей у Bambu по сети нет: конец печати видно по смене gcode_state (RUNNING → FINISH/FAILED);
граммы — из Metadata/slice_info.config файла .3mf на принтере, а если его не достать — по убыли
остатка катушки в AMS. Только стандартная библиотека: свой маленький MQTT 3.1.1-клиент.
"""
import ftplib
import io
import json
import os
import socket
import ssl
import struct
import time
import zipfile

MQTT_PORT = 8883
FTPS_PORT = 990
TIMEOUT = 8
MAX_3MF = 64 * 1024 * 1024  # .3mf целиком в памяти — на телефоне больше не тянем
STATES = {"IDLE": "ожидает", "PREPARE": "готовится", "RUNNING": "печатает", "PAUSE": "пауза",
          "FINISH": "готово", "FAILED": "ошибка", "SLICING": "нарезка"}


class BambuError(Exception):
    pass


def _tls():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # у принтера самоподписанный сертификат
    return ctx


# --- MQTT 3.1.1, только то, что нужно: CONNECT, SUBSCRIBE, PUBLISH (QoS 0) -------------------

def _str(s):
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def _packet(kind, body):
    n, out = len(body), bytearray([kind])
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out) + body


def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise BambuError("принтер закрыл соединение")
        buf += chunk
    return buf


def _read_packet(sock):
    kind = _read_exact(sock, 1)[0]
    mult, length = 1, 0
    for _ in range(4):
        b = _read_exact(sock, 1)[0]
        length += (b & 0x7F) * mult
        mult *= 128
        if not b & 0x80:
            break
    if length > 4 * 1024 * 1024:
        raise BambuError("слишком большое сообщение")
    return kind, _read_exact(sock, length)


def report(host, serial, code, timeout=TIMEOUT, port=MQTT_PORT, tls=True):
    """Полное состояние принтера (раздел «print» отчёта). tls=False — только для тестов."""
    raw = socket.create_connection((host, port), timeout=timeout)
    sock = _tls().wrap_socket(raw, server_hostname=host) if tls else raw
    try:
        sock.settimeout(timeout)
        connect = _str("MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60)
        connect += _str("filament-%d" % os.getpid()) + _str("bblp") + _str(code)
        sock.sendall(_packet(0x10, connect))
        kind, body = _read_packet(sock)
        if kind >> 4 != 2 or len(body) < 2 or body[1] != 0:
            raise BambuError("принтер не принял код доступа" if len(body) > 1 and body[1] in (4, 5)
                             else "MQTT: отказ в подключении")
        sock.sendall(_packet(0x82, struct.pack("!H", 1) + _str("device/%s/report" % serial) + b"\x00"))
        request = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}).encode()
        sock.sendall(_packet(0x30, _str("device/%s/request" % serial) + request))
        deadline = time.time() + timeout
        while time.time() < deadline:
            kind, body = _read_packet(sock)
            if kind >> 4 != 3:
                continue
            tlen = struct.unpack("!H", body[:2])[0]
            payload = body[2 + tlen + (2 if (kind >> 1) & 3 else 0):]
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            p = msg.get("print") if isinstance(msg, dict) else None
            if isinstance(p, dict) and "gcode_state" in p:
                return p
        raise BambuError("принтер не прислал состояние — проверьте серийный номер")
    except socket.timeout:
        raise BambuError("принтер не отвечает")
    finally:
        try:
            sock.sendall(b"\xe0\x00")  # DISCONNECT
        except OSError:
            pass
        sock.close()


def live(p):
    """Отчёт → то же, что показывает связь с Klipper-принтером, плюс ячейки AMS."""
    state = str(p.get("gcode_state") or "")
    trays = []
    for unit in ((p.get("ams") or {}).get("ams") or []):
        for t in unit.get("tray") or []:
            if not t.get("tray_type"):
                continue
            color = str(t.get("tray_color") or "")[:6]
            trays.append({"slot": "%d-%d" % (int(unit.get("id", 0)) + 1, int(t.get("id", 0)) + 1),
                          "type": t.get("tray_type"), "color": "#" + color.lower() if len(color) == 6 else "",
                          "remain": t.get("remain") if isinstance(t.get("remain"), int) and t["remain"] >= 0 else None})
    return {"state": STATES.get(state, state.lower() or "?"),
            "raw_state": {"RUNNING": "printing", "PAUSE": "paused"}.get(state, state.lower()),
            "file": p.get("subtask_name") or os.path.basename(str(p.get("gcode_file") or "")),
            "progress": p.get("mc_percent") if isinstance(p.get("mc_percent"), int) else None,
            "minutes": None, "remaining": p.get("mc_remaining_time"),
            "nozzle": p.get("nozzle_temper"), "nozzle_target": p.get("nozzle_target_temper"),
            "bed": p.get("bed_temper"), "bed_target": p.get("bed_target_temper"), "trays": trays}


def job_key(p):
    """Признак конкретной печати: task_id, если есть, иначе имя + время старта."""
    task = str(p.get("task_id") or p.get("subtask_id") or "")
    if task and task != "0":
        return task
    return "%s@%s" % (p.get("subtask_name") or p.get("gcode_file") or "", p.get("gcode_start_time") or "")


def ams_remains(p):
    """{ячейка: (остаток %, вес катушки г)} — для подсчёта граммов по убыли остатка."""
    out = {}
    for unit in ((p.get("ams") or {}).get("ams") or []):
        for t in unit.get("tray") or []:
            remain = t.get("remain")
            if isinstance(remain, int) and remain >= 0:
                try:
                    weight = float(t.get("tray_weight") or 1000)
                except ValueError:
                    weight = 1000.0
                out["%s-%s" % (unit.get("id"), t.get("id"))] = (remain, weight)
    return out


# --- FTPS: файл печати с принтера ----------------------------------------------------------

class _ImplicitFTPS(ftplib.FTP_TLS):
    """FTPS с шифрованием сразу (implicit, порт 990) и тем же TLS-сеансом для данных — так требует принтер."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


class _TooBig(Exception):
    pass


def _connect(host, port, code):
    ftp = _ImplicitFTPS(context=_tls(), timeout=30)
    ftp.connect(host, port)
    ftp.login("bblp", code)
    ftp.prot_p()
    return ftp


def print_file_weights(host, code, p, parse_slice_info, port=FTPS_PORT):
    """Граммы по филаментам из .3mf, который печатается сейчас. [] — если файл не достать."""
    name = str(p.get("subtask_name") or "")
    gfile = str(p.get("gcode_file") or "")
    candidates = [c for c in (gfile if gfile.endswith(".3mf") else "",
                              "/cache/%s.gcode.3mf" % name if name else "", "/cache/%s.3mf" % name if name else "",
                              "/%s.gcode.3mf" % name if name else "", "/%s.3mf" % name if name else "")
                  if c and "\r" not in c and "\n" not in c]  # перевод строки в имени — это уже вторая команда FTP
    ftp = None
    try:
        ftp = _connect(host, port, code)
        for path in candidates:
            buf = io.BytesIO()

            def keep(block):
                if buf.tell() + len(block) > MAX_3MF:  # не тянуть в память дальше лимита
                    raise _TooBig
                buf.write(block)
            try:
                ftp.retrbinary("RETR " + path, keep)
            except ftplib.error_perm:  # такого файла нет
                continue
            except _TooBig:
                ftp.close()  # оборванная передача — соединение дальше не годится
                ftp = _connect(host, port, code)
                continue
            try:
                with zipfile.ZipFile(buf) as z:
                    info = parse_slice_info(z.read("Metadata/slice_info.config").decode("utf-8", "replace"))
            except (zipfile.BadZipFile, KeyError):
                continue
            weights = info.get("per_filament") or ([info["grams"]] if info.get("grams") else [])
            if weights:
                return weights
    except (OSError, ftplib.Error, EOFError):
        pass
    finally:
        try:
            if ftp is not None:
                ftp.close()
        except OSError:
            pass
    return []
