#!/bin/bash
# Кладёт код приложения в Android-проект (Chaquopy берёт Python из app/src/main/python).
# Запускается перед сборкой: bash android/prepare.sh && cd android && gradle assembleRelease
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/android/app/src/main/python"
RES="$ROOT/android/app/src/main/res"
rm -rf "$PY" && mkdir -p "$PY"
cp "$ROOT"/{app,core,logic,cli,importer,labelpng,notify,slicer,stlpreview,sync}.py "$ROOT/android/launcher.py" "$PY/"
cp -r "$ROOT"/{views,templates,static,fonts} "$PY/"
mkdir -p "$PY/agent" && cp "$ROOT/agent/k2_agent.py" "$PY/agent/"  # разбор истории Moonraker
find "$PY" -name __pycache__ -prune -exec rm -rf {} +
# значок — тот же, что у сайта (Android сам масштабирует)
mkdir -p "$RES/mipmap-xxxhdpi"
cp "$ROOT/static/icon-192.png" "$RES/mipmap-xxxhdpi/ic_launcher.png"
echo "готово: $PY"
