"""Копия вне сервера: архив шифруется (gpg, AES-256, пароль из config) и уходит
на WebDAV (Яндекс Диск, Nextcloud…) и/или в Telegram. Настройка: app.py offsite-setup.

    venv/bin/python deploy/offsite.py АРХИВ.tar.gz [--force-telegram]

Расшифровать где угодно: gpg -d filament-….tar.gz.gpg > копия.tar.gz (спросит пароль).
"""
import base64
import datetime
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("FILAMENT_DATA", os.path.join(ROOT, "data"))
TELEGRAM_MAX = 49 * 1024 * 1024
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class OffsiteError(Exception):
    pass


def load_config():
    with open(os.path.join(DATA, "config.json")) as f:
        return json.load(f)


def encrypt(archive, passphrase, out_dir):
    out = os.path.join(out_dir, os.path.basename(archive) + ".gpg")
    r = subprocess.run(["gpg", "--batch", "--yes", "--quiet", "--pinentry-mode", "loopback", "--symmetric",
                        "--cipher-algo", "AES256", "--passphrase-fd", "0", "--homedir", out_dir,
                        "--output", out, archive], input=passphrase.encode(), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    if r.returncode:
        raise OffsiteError("gpg: %s" % r.stderr.decode(errors="replace").strip()[-200:])
    return out


def remote_names(today):
    """Имена на WebDAV: 7 ежедневных по кругу + по одной на каждый месяц (1-го числа)."""
    names = ["filament-daily-%s.tar.gz.gpg" % DAYS[today.weekday()]]
    if today.day == 1:
        names.append("filament-monthly-%s.tar.gz.gpg" % today.strftime("%Y-%m"))
    return names


def webdav_put(base, user, password, name, path):
    auth = "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
    base = base.rstrip("/") + "/"
    if not base.startswith("https://"):
        raise OffsiteError("WebDAV — только https.")
    mk = urllib.request.Request(base, method="MKCOL", headers={"Authorization": auth})
    try:
        urllib.request.urlopen(mk, timeout=30).close()
    except urllib.error.HTTPError as e:
        if e.code not in (405, 409, 301):  # 405 — папка уже есть
            raise OffsiteError("WebDAV MKCOL: HTTP %s" % e.code)
    with open(path, "rb") as f:
        data = f.read()
    req = urllib.request.Request(base + name, data=data, method="PUT",
                                 headers={"Authorization": auth, "Content-Type": "application/octet-stream"})
    try:
        urllib.request.urlopen(req, timeout=300).close()
    except urllib.error.HTTPError as e:
        raise OffsiteError("WebDAV PUT: HTTP %s%s" % (e.code, " — неверный логин/пароль приложения?" if e.code == 401 else ""))
    except (urllib.error.URLError, OSError) as e:
        raise OffsiteError("WebDAV недоступен: %s" % getattr(e, "reason", e))


def telegram_document(token, chat, path, caption):
    if os.path.getsize(path) > TELEGRAM_MAX:
        raise OffsiteError("архив больше 49 МБ — Telegram не примет, используйте WebDAV")
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        payload = f.read()
    parts = []
    for name, value in (("chat_id", str(chat)), ("caption", caption)):
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, name, value)).encode())
    parts.append(('--%s\r\nContent-Disposition: form-data; name="document"; filename="%s"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n' % (boundary, os.path.basename(path))).encode())
    body = b"".join(parts) + payload + ("\r\n--%s--\r\n" % boundary).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendDocument" % token, data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        urllib.request.urlopen(req, timeout=300).close()
    except urllib.error.HTTPError as e:
        raise OffsiteError("Telegram: HTTP %s" % e.code)
    except (urllib.error.URLError, OSError) as e:
        raise OffsiteError("Telegram недоступен: %s" % getattr(e, "reason", e))


def run(archive, today=None, force_telegram=False):
    """Возвращает список отправок; без настройки — пустой список."""
    cfg = load_config()
    off = cfg.get("offsite") or {}
    if not off.get("passphrase") or not (off.get("webdav_url") or off.get("telegram")):
        return []
    today = today or datetime.date.today()
    done = []
    with tempfile.TemporaryDirectory(prefix="filament-offsite-") as tmp:
        os.chmod(tmp, 0o700)
        enc = encrypt(archive, off["passphrase"], tmp)
        if off.get("webdav_url"):
            for name in remote_names(today):
                webdav_put(off["webdav_url"], off.get("webdav_user", ""), off.get("webdav_password", ""), name, enc)
                done.append("WebDAV: " + name)
        weekday = int(off.get("telegram_weekday", 0))  # по умолчанию раз в неделю, в понедельник
        if off.get("telegram") and cfg.get("telegram_token") and (force_telegram or today.weekday() == weekday):
            telegram_document(cfg["telegram_token"], cfg["telegram_chat"], enc,
                              "Копия учёта филамента %s (gpg -d)" % today.isoformat())
            done.append("Telegram")
    return done


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    try:
        done = run(sys.argv[1], force_telegram="--force-telegram" in sys.argv)
    except (OffsiteError, OSError, ValueError) as e:
        msg = "⚠️ Копия вне сервера не отправлена: %s" % e
        print(msg)
        try:
            sys.path.insert(0, ROOT)
            import app
            app.telegram(app.notify.esc(msg))
        except Exception as err:  # уведомление — не главное
            print("Telegram: %s" % err)
        sys.exit(1)
    print("Вне сервера: " + (", ".join(done) if done else "не настроено (app.py offsite-setup)"))


if __name__ == "__main__":
    main()
