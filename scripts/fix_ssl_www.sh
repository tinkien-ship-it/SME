#!/bin/bash
# Cap lai SSL cho ca ketoshop.pro.vn va www.ketoshop.pro.vn
# Chay tren VPS (root):
#   bash /root/pos/scripts/fix_ssl_www.sh
#
# Dieu kien:
#   - DNS A (hoac CNAME) cua www.ketoshop.pro.vn da tro ve IP VPS
#   - Nginx dang phuc vu domain, certbot da cai

set -euo pipefail

DOMAIN="${DOMAIN:-ketoshop.pro.vn}"
WWW="www.${DOMAIN}"
EMAIL="${CERTBOT_EMAIL:-}"

echo "=== Kiem tra DNS ==="
getent hosts "$DOMAIN" || true
getent hosts "$WWW" || true

echo ""
echo "=== Cap nhat Nginx server_name (neu co file site) ==="
SITE=""
for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
  [ -f "$f" ] || continue
  if grep -q "$DOMAIN" "$f" 2>/dev/null; then
    SITE="$f"
    break
  fi
done

if [ -n "$SITE" ]; then
  echo "  Tim thay: $SITE"
  # Dam bao server_name co ca apex va www
  if grep -qE "server_name[[:space:]].*${DOMAIN}" "$SITE"; then
    if ! grep -qE "server_name[[:space:]].*${WWW}" "$SITE"; then
      sed -i -E "s/(server_name[[:space:]]+)([^;]*${DOMAIN}[^;]*);/\1\2 ${WWW};/" "$SITE" || true
      # Neu van chua co www, them dong rieng an toan hon bang comment huong dan
      if ! grep -q "$WWW" "$SITE"; then
        echo "  ! Chua tu dong them www. Sua tay server_name thanh:"
        echo "    server_name ${DOMAIN} ${WWW};"
      fi
    fi
  fi
  nginx -t
  systemctl reload nginx
else
  echo "  ! Khong tim thay file nginx chua $DOMAIN — van thu cap cert"
fi

echo ""
echo "=== Cap chung chi Let's Encrypt (apex + www) ==="
CERTBOT_ARGS=(certonly --nginx -d "$DOMAIN" -d "$WWW" --agree-tos --non-interactive --expand)
if [ -n "$EMAIL" ]; then
  CERTBOT_ARGS+=(--email "$EMAIL")
else
  CERTBOT_ARGS+=(--register-unsafely-without-email)
fi

# expand cert hien co; neu fail thu certonly lai
if ! certbot "${CERTBOT_ARGS[@]}"; then
  echo "  ! certbot --expand that bai — thu lai..."
  certbot certonly --nginx -d "$DOMAIN" -d "$WWW" --agree-tos --non-interactive \
    ${EMAIL:+--email "$EMAIL"} ${EMAIL:---register-unsafely-without-email} || {
      echo ""
      echo "LOI: Khong cap duoc SSL cho $WWW"
      echo "Kiem tra:"
      echo "  1) DNS www phai tro ve IP VPS: dig +short $WWW"
      echo "  2) Mo port 80/443"
      echo "  3) Nginx listen 80 cho ca $DOMAIN va $WWW"
      exit 1
    }
fi

nginx -t
systemctl reload nginx

echo ""
echo "=== Kiem tra ==="
echo | openssl s_client -servername "$WWW" -connect "$WWW:443" 2>/dev/null \
  | openssl x509 -noout -subject -ext subjectAltName 2>/dev/null | head -20 || true

echo ""
echo "Xong. Thu: https://$WWW/login  va  https://$DOMAIN/login"
echo "Google OAuth: them ca 2 origin:"
echo "  https://$DOMAIN"
echo "  https://$WWW"
