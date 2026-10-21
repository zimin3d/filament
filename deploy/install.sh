#!/bin/bash
# Запускать от root: sudo bash deploy/install.sh
set -euo pipefail
. "$(dirname "$0")/site.sh"   # DIR, FILAMENT_URL, NGINX_SITE, render
SITE="$NGINX_SITE"

render "$DIR/deploy/filament.service" /etc/systemd/system/filament.service
install -m 644 "$DIR/deploy/nginx-filament.conf" /etc/nginx/snippets/filament.conf

# 443 по умолчанию: IP/чужой Host не попадает в наш сайт (заглушка с самоподписанным сертификатом)
if [ ! -f /etc/nginx/ssl/default.key ]; then
    install -d -m 700 /etc/nginx/ssl
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 -subj "/CN=invalid" \
        -keyout /etc/nginx/ssl/default.key -out /etc/nginx/ssl/default.crt 2>/dev/null
    chmod 600 /etc/nginx/ssl/default.key
fi
install -m 644 "$DIR/deploy/nginx-default-443.conf" /etc/nginx/sites-available/00-default-443
ln -sf /etc/nginx/sites-available/00-default-443 /etc/nginx/sites-enabled/00-default-443
# устаревшие TLS 1.0/1.1 в общем конфиге (сайт их и так переопределяет — чистим на всякий случай)
if grep -qE "^\s*ssl_protocols .*TLSv1 " /etc/nginx/nginx.conf; then
    cp /etc/nginx/nginx.conf "/etc/nginx/nginx.conf.bak-$(date +%Y%m%d%H%M%S)"
    sed -i -E "s/^(\s*)ssl_protocols [^;]*;/\1ssl_protocols TLSv1.2 TLSv1.3;/" /etc/nginx/nginx.conf
fi

if ! grep -q "snippets/filament.conf" "$SITE"; then
    cp "$SITE" "$SITE.bak-$(date +%Y%m%d%H%M%S)"
    # вставляем перед первым "location / {" (это HTTPS-блок)
    sed -i '0,/^\(\s*\)location \/ {/s//\1include snippets\/filament.conf;\n&/' "$SITE"
fi

systemctl daemon-reload
systemctl enable --now filament
systemctl restart filament
nginx -t
systemctl reload nginx
echo "Готово: $FILAMENT_URL"
