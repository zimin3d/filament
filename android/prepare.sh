#!/bin/bash
# Кладёт код приложения в Android-проект (Chaquopy берёт Python из app/src/main/python).
# Запускается перед сборкой: bash android/prepare.sh && cd android && gradle assembleRelease
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/android/app/src/main/python"
RES="$ROOT/android/app/src/main/res"
rm -rf "$PY" && mkdir -p "$PY"
cp "$ROOT"/*.py "$ROOT/android/launcher.py" "$PY/"  # все модули приложения — новые не потеряются
cp -r "$ROOT"/{views,templates,static,fonts} "$PY/"
mkdir -p "$PY/agent" && cp "$ROOT/agent/k2_agent.py" "$PY/agent/"  # разбор истории Moonraker
find "$PY" -name __pycache__ -prune -exec rm -rf {} +
# значок — тот же, что у сайта (Android сам масштабирует)
mkdir -p "$RES/mipmap-xxxhdpi"
cp "$ROOT/static/icon-192.png" "$RES/mipmap-xxxhdpi/ic_launcher.png"
# проверка: приложение запускается из того, что попадёт в APK (как на телефоне)
if [ "${SKIP_CHECK:-}" != 1 ]; then
    CHECK="$(mktemp -d)"
    (cd "$CHECK" && FILAMENT_DATA="$CHECK/data" FILAMENT_MODE=phone PYTHONDONTWRITEBYTECODE=1 \
        "${PYTHON:-python3}" -c "import sys; sys.path.insert(0, '$PY'); import app; c = app.app.test_client(); assert c.get('/help').status_code == 200") \
        || { echo "!!! приложение не запускается из android/app/src/main/python"; rm -rf "$CHECK"; exit 1; }
    rm -rf "$CHECK"
fi
echo "готово: $PY"
