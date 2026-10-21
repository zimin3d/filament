#!/bin/bash
# CLI приложения от имени сервиса: deploy/as-filament.sh set-password | list-admins | sync | ...
# (данные принадлежат пользователю filament; правило sudo ставит deploy/migrate-user.sh)
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
exec sudo -u filament env FILAMENT_DATA=/var/lib/filament/data HOME=/var/lib/filament \
    PYTHONDONTWRITEBYTECODE=1 "$DIR/venv/bin/python" "$DIR/app.py" "$@"
