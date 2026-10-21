# Общее для install.sh и migrate-user.sh: настройки сервера и подстановка путей в unit-файлы.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$DIR/deploy/site.env" ]] || { echo "нет deploy/site.env — скопируйте deploy/site.env.example и поправьте"; exit 1; }
set -a; . "$DIR/deploy/site.env"; set +a
OWNER="${OWNER:-$(stat -c %U "$DIR")}"
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
# интерпретатор, на который ссылается venv (uv кладёт его в домашний каталог владельца)
UV_PYTHON="$OWNER_HOME/.local/share/uv/python"

render() {  # render шаблон назначение
    sed -e "s|@APP_DIR@|$DIR|g" -e "s|@UV_PYTHON@|$UV_PYTHON|g" -e "s|@OWNER@|$OWNER|g" "$1" > "$2.tmp"
    chmod 644 "$2.tmp" && mv "$2.tmp" "$2"
}
