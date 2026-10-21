#!/usr/bin/env python3
"""Агент для Creality K2 (и любого Klipper/Moonraker): запускается на компьютере в одной
сети с принтером, раз в минуту смотрит историю печатей и отправляет законченные в учёт.

    python3 k2_agent.py --server https://example.com/filament --printer 192.168.1.50 --token КЛЮЧ
    python3 k2_agent.py --server … --printer 192.168.1.50 --token КЛЮЧ --once   # один проход (для планировщика)

Ключ выдаёт сервер: venv/bin/python app.py printer-token. Только стандартная библиотека Python 3.6+.
При первом запуске старые печати пропускаются (--backfill N — отправить последние N).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

DEFAULT_SERVER = os.environ.get("FILAMENT_SERVER")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "k2_agent_state.json")
FINAL = {"completed", "cancelled", "error"}


def get_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def num(value):
    """Число из поля истории; мусор, NaN и отрицательные — 0."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if value == value and 0 <= value < 1e12 else 0.0


def job_payload(job):
    """Запись истории Moonraker -> данные для учёта. Граммы — по филаментам, если слайсер их указал;
    для отменённых печатей — пропорционально реально израсходованной длине."""
    meta = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    weights = [num(w) for w in meta.get("filament_weights") or [] if isinstance(meta.get("filament_weights"), list)]
    if not any(weights) and num(meta.get("filament_weight_total")):
        weights = [num(meta["filament_weight_total"])]
    used_mm, total_mm = num(job.get("filament_used")), num(meta.get("filament_total"))
    if job.get("status") != "completed" and total_mm and used_mm < total_mm:
        k = used_mm / total_mm
        weights = [w * k for w in weights]
    if not weights and used_mm:  # нет веса в метаданных — по длине, PLA 1,75 мм
        weights = [used_mm * 2.405 * 1.24 / 1000]
    end = num(job.get("end_time")) or num(job.get("start_time")) or time.time()
    try:
        day = datetime.fromtimestamp(end).date().isoformat()
    except (OverflowError, OSError, ValueError):
        day = datetime.now().date().isoformat()
    return {
        "job_id": ("%s" % job.get("job_id"))[:64],
        "name": os.path.basename(str(job.get("filename") or ""))[:200],
        "status": str(job.get("status") or "")[:20],
        "minutes": min(round(num(job.get("print_duration")) / 60), 60 * 24 * 30) or None,
        "day": day,
        "weights": [round(w, 1) for w in weights if w and w > 0],
    }


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


def send(server, token, payload):
    req = urllib.request.Request(server.rstrip("/") + "/api/printer/job", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token,
                                          "User-Agent": "filament-k2-agent"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def poll(args, state):
    jobs = get_json("http://%s:%d/server/history/list?limit=50&order=desc" % (args.printer, args.port))
    jobs = jobs.get("result", {}).get("jobs", [])
    if state is None:  # первый запуск: всё старое считаем отправленным
        keep = jobs[:args.backfill] if args.backfill else []
        state = {"sent": [j["job_id"] for j in jobs if j not in keep]}
        save_state(state)
    for job in reversed(jobs):
        if job["job_id"] in state["sent"] or job.get("status") not in FINAL:
            continue
        payload = job_payload(job)
        if payload["weights"]:
            result = send(args.server, args.token, payload)
            print(datetime.now().strftime("%H:%M"), payload["name"], payload["weights"], "г",
                  "→ списано" if result.get("auto") else "→ во входящих", flush=True)
        state["sent"] = (state["sent"] + [job["job_id"]])[-500:]
        save_state(state)
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--printer", required=True, help="IP принтера в домашней сети")
    p.add_argument("--port", type=int, default=7125, help="порт Moonraker (у K2 — 7125)")
    p.add_argument("--server", default=DEFAULT_SERVER, required=not DEFAULT_SERVER,
                   help="адрес приложения, например https://example.com/filament (или FILAMENT_SERVER)")
    p.add_argument("--token", default=os.environ.get("FILAMENT_TOKEN"), help="ключ от app.py printer-token")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--backfill", type=int, default=0, help="при первом запуске отправить последние N печатей")
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    if not args.token:
        sys.exit("Нужен --token (или переменная FILAMENT_TOKEN).")
    state = load_state()
    while True:
        try:
            state = poll(args, state)
        except urllib.error.HTTPError as e:
            print("Ошибка сервера учёта: HTTP %s %s" % (e.code, "— неверный ключ?" if e.code == 403 else ""), flush=True)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print("Нет связи (%s) — повторю позже." % getattr(e, "reason", e), flush=True)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
