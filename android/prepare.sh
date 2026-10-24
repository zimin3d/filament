#!/bin/bash
# Кладёт код приложения в Android-проект (Chaquopy берёт Python из app/src/main/python).
# Запускается перед сборкой: bash android/prepare.sh && cd android && gradle assembleRelease
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/android/app/src/main/python"
RES="$ROOT/android/app/src/main/res"
rm -rf "$PY" && mkdir -p "$PY"
# все модули приложения из git (новые не потеряются, локальные черновики с паролями в APK не попадут)
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    mapfile -t MODS < <(git -C "$ROOT" ls-files -- '*.py' | grep -v / | sed "s|^|$ROOT/|")
else
    MODS=("$ROOT"/*.py)
fi
cp "${MODS[@]}" "$ROOT/android/launcher.py" "$PY/"
cp -r "$ROOT"/{views,templates,static,fonts} "$PY/"
mkdir -p "$PY/agent" && cp "$ROOT/agent/k2_agent.py" "$PY/agent/"  # разбор истории Moonraker
find "$PY" -name __pycache__ -prune -exec rm -rf {} +
# значок — тот же, что у сайта (Android сам масштабирует)
mkdir -p "$RES/mipmap-xxxhdpi"
cp "$ROOT/static/icon-192.png" "$RES/mipmap-xxxhdpi/ic_launcher.png"
# проверка: приложение запускается из того, что попадёт в APK (как на телефоне)
# (launcher — точка входа MainActivity; agent_module грузится лениво — проверяем, что файл на месте)
if [ "${SKIP_CHECK:-}" != 1 ]; then
    PYTHON="${PYTHON:-$([ -x "$ROOT/venv/bin/python" ] && echo "$ROOT/venv/bin/python" || echo python3)}"
    CHECK="$(mktemp -d)"
    trap 'rm -rf "$CHECK"' EXIT
    (cd "$CHECK" && APP_PY="$PY" FILAMENT_DATA="$CHECK/data" FILAMENT_MODE=phone FILAMENT_PHONE_KEY=check \
        PYTHONDONTWRITEBYTECODE=1 \
        "$PYTHON" -c "
import os, sys
sys.path.insert(0, os.environ['APP_PY'])
import app, launcher
app.agent_module()
c = app.app.test_client()
if c.get('/help').status_code != 403:
    sys.exit('без ключа запуска страницы открыты другим приложениям')
c.get('/phone-start?key=check')
if c.get('/help').status_code != 200:
    sys.exit('/help не открывается')") \
        || { echo "!!! приложение не запускается из android/app/src/main/python ($PYTHON)"; exit 1; }
fi
echo "готово: $PY"
