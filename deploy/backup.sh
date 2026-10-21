#!/bin/bash
# Копия data/ (база через sqlite backup — консистентно на ходу). Хранит последние 30.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${FILAMENT_DATA:-$DIR/data}"
DEST="${FILAMENT_BACKUPS:-$HOME/filament-backups}"
KEEP=30
STAMP=$(date +%Y%m%d-%H%M%S)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$DEST"
"$DIR/venv/bin/python" -c "
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()" "$DATA/filament.db" "$TMP/filament.db"
cp "$DATA/config.json" "$TMP/"
tar -czf "$DEST/filament-$STAMP.tar.gz" -C "$TMP" filament.db config.json -C "$DATA" uploads
chmod 600 "$DEST/filament-$STAMP.tar.gz"
ls -1t "$DEST"/filament-*.tar.gz | tail -n +$((KEEP + 1)) | xargs -r rm --
# сразу проверяем, что из копии можно восстановиться (при ошибке — сообщение в Telegram)
"$DIR/venv/bin/python" "$DIR/deploy/verify_backup.py" "$DEST/filament-$STAMP.tar.gz"
# после проверенной копии — убрать неиспользуемые файлы (фото удалённого держим 30 дней ради отмены)
cd "$DIR" && "$DIR/venv/bin/python" app.py cleanup
# зашифрованная копия вне сервера (если настроена: app.py offsite-setup); сбой не мешает остальному
"$DIR/venv/bin/python" "$DIR/deploy/offsite.py" "$DEST/filament-$STAMP.tar.gz" || true
