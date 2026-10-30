"""Учёт филамента для 3D-принтера — точка входа (gunicorn app:app) и командная строка.

Без входа всё доступно только для чтения; редактируют администраторы.
Код разложен по модулям: core (инфраструктура), logic (предметная логика), views/* (страницы), cli (команды).
"""
import os
import re
import sys

from core import *  # noqa: F401,F403
from logic import *  # noqa: F401,F403
from cli import *  # noqa: F401,F403
import views  # noqa: F401,E402  — регистрирует маршруты

if __name__ == "__main__":
    if sys.argv[1:2] == ["set-password"]:
        cli_set_password(name=sys.argv[2] if len(sys.argv) > 2 else None)
    elif sys.argv[1:2] == ["add-admin"] and len(sys.argv) == 3:
        name = sys.argv[2].strip().lower()
        if not re.fullmatch(r"[\w.-]{1,32}", name):
            sys.exit("Имя: буквы, цифры, точка, дефис.")
        if name in admin_users():
            sys.exit("Уже есть — сменить пароль: app.py set-password %s" % name)
        set_admin(name, ask_password("для «%s»" % name))
        print("Администратор «%s» добавлен." % name)
    elif sys.argv[1:2] == ["remove-admin"] and len(sys.argv) == 3:
        try:
            remove_admin(sys.argv[2].strip().lower())
        except ValueError as e:
            sys.exit(str(e))
        print("Удалён; его входы больше не действуют.")
    elif sys.argv[1:] == ["list-admins"]:
        print("\n".join(admin_users()) or "Администраторов нет.")
    elif sys.argv[1:] == ["set-viewer-password"]:
        cli_set_password("viewer_password_hash", "для просмотра")
    elif sys.argv[1:] == ["clear-viewer-password"]:
        cfg = load_config()
        cfg.pop("viewer_password_hash", None)
        save_config(cfg)
        print("Пароль на просмотр снят — сайт снова открыт для всех.")
    elif sys.argv[1:2] == ["cleanup"]:
        cli_cleanup(dry_run="--dry-run" in sys.argv)
    elif sys.argv[1:] == ["offsite-setup"]:
        cli_offsite_setup()
    elif sys.argv[1:] == ["printer-token"]:
        cli_printer_token()
    elif sys.argv[1:] == ["set-telegram"]:
        cli_set_telegram()
    elif sys.argv[1:2] == ["notify"]:
        cli_notify(force="--force" in sys.argv, quiet="--quiet" in sys.argv)
    elif sys.argv[1:] == ["sync"]:
        cli_sync()
    else:
        app.config["SESSION_COOKIE_SECURE"] = False
        app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8080)), debug=True)
