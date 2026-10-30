"""Команды обслуживания: пароли, администраторы, синхронизация, уведомления, очистка, резервные копии."""
import glob
import os
import re
import secrets
import shutil
import subprocess
import time
import sys
from datetime import date
from getpass import getpass


import importer
import notify
from werkzeug.security import generate_password_hash
from core import *  # noqa: F401,F403
from logic import *  # noqa: F401,F403

def ask_password(title):
    pw = clean_password(getpass("Новый пароль %s: " % title))
    if len(pw) < 10:
        sys.exit("Пароль должен быть не короче 10 символов.")
    if clean_password(getpass("Повторите: ")) != pw:
        sys.exit("Пароли не совпадают.")
    print("Принят пароль из %d символов, начинается на «%s», заканчивается на «%s»." % (len(pw), pw[0], pw[-1]))
    return generate_password_hash(pw)


def set_admin(name, pw_hash):
    """Добавить/сменить пароль админа; старый одиночный пароль переносится в список как «admin»."""
    cfg = load_config()
    users = admin_users(cfg)
    users[name] = pw_hash
    cfg["admins"] = users
    cfg.pop("admin_password_hash", None)
    save_config(cfg)


def remove_admin(name):
    cfg = load_config()
    users = admin_users(cfg)
    if name not in users:
        raise ValueError("Нет администратора «%s». Есть: %s" % (name, ", ".join(users) or "никого"))
    if len(users) == 1:
        raise ValueError("Это последний администратор — удалять нельзя.")
    del users[name]
    cfg["admins"] = users
    cfg.pop("admin_password_hash", None)
    save_config(cfg)


def cli_set_password(key="admin_password_hash", title="администратора", name=None):
    if key == "admin_password_hash":
        name = (name or "admin").lower()
        set_admin(name, ask_password("администратора «%s»" % name))
        print("Пароль администратора «%s» сохранён." % name)
        return
    cfg = load_config()
    cfg[key] = ask_password(title)
    save_config(cfg)
    print("Пароль сохранён в", CONFIG_PATH)


def cli_sync():
    """Ночная синхронизация с сохранённой таблицей: только новые катушки и недостающие фото.
    Изменения существующих не применяются — их надо подтвердить на странице импорта."""
    url = load_config().get("sheet_url")
    if not url:
        sys.exit("Ссылка на таблицу не сохранена — сначала сделайте импорт по ссылке.")
    with app.test_request_context(base_url=PUBLIC_URL):
        try:
            token, photo_error = fetch_to_tmp(url)
            header, body, _ = importer.parse_csv(importer.load_tmp(IMPORT_DIR, token))
        except importer.SheetError as e:
            sys.exit("Ошибка: %s" % e)
        plan = plan_import(header, body, saved_mapping(header, body),
                           importer.tmp_photos(IMPORT_DIR, token), do_update=False)
        pending = sum(1 for r in plan_import(header, body, saved_mapping(header, body), {})["rows"] if r["changes"])
        msg = apply_import(token, plan)
    line = " ".join(filter(None, [msg, "Ждут подтверждения изменений: %d." % pending if pending else "",
                                  "Фото: %s" % photo_error if photo_error else ""]))
    print(date.today().isoformat(), line)
    if msg != "Изменений нет." or pending or photo_error:
        telegram("<b>Синхронизация с таблицей</b>\n" + notify.esc(line)
                 + ("\nПодтвердить изменения: %simport" % PUBLIC_URL if pending else ""))


def site_env(key, default=""):
    """Значение из deploy/site.env (настройки конкретного сервера, не в git)."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy", "site.env")) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k == key:
                    return v.strip().strip("\"'")
    except OSError:
        pass
    return default


PUBLIC_URL = os.environ.get("FILAMENT_URL") or site_env("FILAMENT_URL", "http://127.0.0.1:8080/")


def telegram(text):
    """Отправка, если Telegram настроен; ошибки не роняют вызывающий код."""
    cfg = load_config()
    if not (cfg.get("telegram_token") and cfg.get("telegram_chat")):
        return False
    try:
        notify.send(cfg["telegram_token"], cfg["telegram_chat"], text)
        return True
    except notify.NotifyError as e:
        print(e, file=sys.stderr)
        return False


def cli_notify(force=False, quiet=False):
    """Сводка: на исходе и пора сушить. Без --force шлёт, только если список изменился.
    --quiet (ночная копия): Telegram не подключён — молча ничего не делать."""
    cfg = load_config()
    if quiet and not (cfg.get("telegram_token") and cfg.get("telegram_chat")):
        return
    with app.test_request_context(base_url=PUBLIC_URL):
        text = digest_text(PUBLIC_URL)
    fp = notify.fingerprint(text)
    if not force and fp == cfg.get("notify_last"):
        print("Без изменений — не отправляю.")
        return
    if text is None and not force:
        cfg["notify_last"] = fp
        save_config(cfg)
        print("Сообщать нечего.")
        return
    if telegram(text or notify.ALL_GOOD):
        cfg = load_config()
        cfg["notify_last"] = fp
        save_config(cfg)
        print("Отправлено.")
    else:
        sys.exit("Не отправлено: Telegram не настроен или недоступен (app.py set-telegram).")


def cleanup(dry_run=False):
    """Удаляет фото, на которые никто не ссылается (и которые уже не вернуть отменой
    в журнале за UNDO_DAYS дней), сиротские миниатюры и старые временные файлы импорта."""
    db = get_db()
    used = {r[0] for r in db.execute("SELECT photo FROM spools WHERE photo IS NOT NULL UNION SELECT photo FROM prints "
                                     "UNION SELECT file FROM models")}
    recent = db.execute("SELECT before, deleted FROM audit WHERE undone = 0 AND ts >= datetime('now', 'localtime', ?)",
                        ("-%d days" % UNDO_DAYS,)).fetchall()
    for before, deleted in recent:
        used.update(re.findall(r"[0-9a-f]{32}\.\w+", (before or "") + deleted))
    now = time.time()
    removed = {"uploads": 0, "thumbs": 0, "import": 0, "bytes": 0}

    def rm(path, kind):
        removed[kind] += 1
        removed["bytes"] += os.path.getsize(path) if os.path.isfile(path) else 0
        if not dry_run:
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)

    for name in os.listdir(UPLOAD_DIR) if os.path.isdir(UPLOAD_DIR) else []:
        path = os.path.join(UPLOAD_DIR, name)
        if name not in used and now - os.path.getmtime(path) > 3600:  # свежие — может, идёт загрузка
            rm(path, "uploads")
    sources = {n.rsplit(".", 1)[0] for n in os.listdir(UPLOAD_DIR)} if os.path.isdir(UPLOAD_DIR) else set()
    for name in os.listdir(THUMB_DIR) if os.path.isdir(THUMB_DIR) else []:
        if name.rsplit(".", 1)[0] not in sources or name.endswith(".tmp"):
            rm(os.path.join(THUMB_DIR, name), "thumbs")
    for name in os.listdir(IMPORT_DIR) if os.path.isdir(IMPORT_DIR) else []:
        path = os.path.join(IMPORT_DIR, name)
        if now - os.path.getmtime(path) > importer.TMP_MAX_AGE:
            rm(path, "import")
    return removed


def cli_cleanup(dry_run=False):
    with app.app_context():
        r = cleanup(dry_run)
    print("%sфото: %d, миниатюр: %d, импортов: %d, освобождено %.1f МБ" % (
        "[проверка] " if dry_run else "", r["uploads"], r["thumbs"], r["import"], r["bytes"] / 1e6))


def cli_offsite_setup():
    """Шифрованная копия вне сервера: WebDAV (Яндекс Диск и т.п.) и/или Telegram."""
    cfg = load_config()
    off = dict(cfg.get("offsite") or {})
    if not off.get("passphrase"):
        off["passphrase"] = secrets.token_urlsafe(24)
    print("Пароль шифрования копий (СОХРАНИТЕ в менеджере паролей — без него копию не открыть,\n"
          "а если сервер умрёт, вместе с ним пропадёт и config):\n\n    %s\n" % off["passphrase"])
    url = input("WebDAV-адрес папки (Яндекс Диск: https://webdav.yandex.ru/filament-backup), Enter — пропустить: ").strip()
    if url:
        off["webdav_url"] = url
        off["webdav_user"] = input("Логин WebDAV: ").strip()
        off["webdav_password"] = getpass("Пароль (для Яндекса — пароль приложения): ")
    if cfg.get("telegram_token"):
        off["telegram"] = input("Отправлять копию в Telegram раз в неделю? [Д/н]: ").strip().lower() not in ("н", "n", "нет", "no")
    elif not url:
        sys.exit("Нужен WebDAV или настроенный Telegram (app.py set-telegram).")
    cfg["offsite"] = off
    save_config(cfg)
    backups = sorted(glob.glob(os.path.expanduser("~/filament-backups/filament-*.tar.gz")))
    if backups:
        print("Пробная отправка %s…" % os.path.basename(backups[-1]))
        r = subprocess.run([sys.executable, os.path.join(BASE_DIR, "deploy", "offsite.py"), backups[-1], "--force-telegram"])
        sys.exit(r.returncode)


def cli_printer_token():
    cfg = load_config()
    cfg["printer_token"] = secrets.token_urlsafe(24)
    save_config(cfg)
    print("Ключ для агента принтера (старый перестал действовать):\n" + cfg["printer_token"])


def cli_set_telegram():
    token = getpass("Токен бота от @BotFather: ").strip()
    try:
        me = notify.call(token, "getMe")
        print("Бот: @%s. Откройте его в Telegram и отправьте /start, затем нажмите Enter." % me["username"])
        input()
        chat, who, bot = notify.connect(token)
    except notify.NotifyError as e:
        sys.exit(str(e))
    cfg = load_config()
    cfg.update(telegram_token=token, telegram_chat=chat, telegram_bot=bot)
    cfg.pop("notify_last", None)  # новый чат — первая сводка придёт, даже если список не менялся
    save_config(cfg)
    print("Готово, уведомления пойдут в чат %s (%s)." % (chat, who))
