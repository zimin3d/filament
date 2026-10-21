#!/bin/bash
# Перевод filament на отдельного пользователя `filament` (однократно).
# Запуск: sudo bash ~/filament/deploy/migrate-user.sh
# Данные: ~/filament/data → /var/lib/filament/data, копии: ~/filament-backups → /var/lib/filament/filament-backups.
# Старые каталоги не удаляются (переименовываются), при сбое запуска — автоматический откат.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "запустите через sudo"; exit 1; }
. "$(dirname "$0")/site.sh"   # DIR, OWNER, OWNER_HOME, FILAMENT_URL, render
STATE=/var/lib/filament
TS=$(date +%Y%m%d-%H%M%S)
UNIT=/etc/systemd/system/filament.service

[[ -d "$DIR/data" && ! -L "$DIR/data" ]] || { echo "!!! $DIR/data не найден или уже ссылка — миграция уже была?"; exit 1; }

check() {  # приложение отвечает и база открывается
    for i in $(seq 1 15); do
        code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/ || true)
        [[ "$code" =~ ^(200|302)$ ]] && { echo "filament отвечает: $code"; return 0; }
        sleep 1
    done
    return 1
}

echo "== 0. Проверка до миграции"
systemctl is-active filament
check

echo "== 1. Свежая копия данных (старым способом, от $OWNER) + полный архив в /root"
sudo -u "$OWNER" -H bash "$DIR/deploy/backup.sh"
tar -czf "/root/filament-data-$TS.tar.gz" -C "$DIR" data
chmod 600 "/root/filament-data-$TS.tar.gz"

echo "== 2. Пользователь filament"
id filament >/dev/null 2>&1 || useradd --system --user-group --home-dir "$STATE" --shell /usr/sbin/nologin filament
install -d -m 750 -o filament -g filament "$STATE"

echo "== 3. Остановка и перенос данных"
cp -a "$UNIT" "/root/filament.service.bak-$TS"
systemctl stop filament
rsync -a "$DIR/data/" "$STATE/data/"
[[ -d $OWNER_HOME/filament-backups ]] && rsync -a "$OWNER_HOME/filament-backups/" "$STATE/filament-backups/"
chown -R filament:filament "$STATE"
chmod 750 "$STATE/data"
mv "$DIR/data" "$DIR/data.old-$TS"; chmod 700 "$DIR/data.old-$TS"
# Ссылка вместо каталога: запуск app.py без as-filament.sh упадёт с «нет доступа», а не создаст пустую базу
ln -s "$STATE/data" "$DIR/data"; chown -h "$OWNER:$OWNER" "$DIR/data"

rollback() {
    echo "!!! Откат"
    systemctl stop filament || true
    rm -f "$DIR/data"; mv "$DIR/data.old-$TS" "$DIR/data"
    cp -a "/root/filament.service.bak-$TS" "$UNIT"
    systemctl daemon-reload; systemctl start filament
    check && echo "Откат выполнен, сервис работает по-старому." || echo "!!! После отката не отвечает — journalctl -u filament"
    exit 1
}

echo "== 4. Новый unit и запуск"
render "$DIR/deploy/filament.service" "$UNIT"
systemctl daemon-reload
systemctl start filament || rollback
check || { journalctl -u filament -n 30 --no-pager; rollback; }
systemctl show filament -p User -p MainPID
curl -s -o /dev/null -w "снаружи $FILAMENT_URL → %{http_code}\n" "$FILAMENT_URL" || true

echo "== 5. Ежедневная копия: cron $OWNER → systemd-таймер от filament"
render "$DIR/deploy/filament-backup.service" /etc/systemd/system/filament-backup.service
install -m 644 "$DIR/deploy/filament-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now filament-backup.timer
if crontab -u "$OWNER" -l 2>/dev/null | grep -q 'deploy/backup.sh'; then
    crontab -u "$OWNER" -l > "/root/crontab-$OWNER.bak-$TS"
    grep -v 'deploy/backup.sh' "/root/crontab-$OWNER.bak-$TS" | crontab -u "$OWNER" -
    echo "строка backup.sh убрана из crontab $OWNER (копия: /root/crontab-$OWNER.bak-$TS)"
fi
echo "-- пробный запуск копии:"
systemctl start filament-backup.service && journalctl -u filament-backup -n 5 --no-pager -o cat \
    || echo "!!! копия не прошла — journalctl -u filament-backup (сервис при этом работает)"

echo "== 6. sudo для $OWNER: перезагрузка кода и CLI от имени filament"
cat > /etc/sudoers.d/filament <<EOF
$OWNER ALL=(root) NOPASSWD: /bin/systemctl reload filament, /usr/bin/systemctl reload filament, /bin/systemctl restart filament, /usr/bin/systemctl restart filament, /bin/systemctl start filament-backup, /usr/bin/systemctl start filament-backup
$OWNER ALL=(filament) NOPASSWD: ALL
EOF
chmod 440 /etc/sudoers.d/filament
visudo -cf /etc/sudoers.d/filament

echo
echo "Готово. Старые данные: $DIR/data.old-$TS и $OWNER_HOME/filament-backups — удалить через неделю, если всё в порядке."
echo "Полный архив до миграции: /root/filament-data-$TS.tar.gz"
echo "Ручной откат: systemctl stop filament; rm $DIR/data; mv $DIR/data.old-$TS $DIR/data;"
echo "              cp /root/filament.service.bak-$TS $UNIT; systemctl daemon-reload; systemctl start filament"
