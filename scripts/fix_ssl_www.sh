#!/bin/bash
# Cap lai SSL cho ca ketoshop.pro.vn va www.ketoshop.pro.vn (khong hoi interactive)
# Chay tren VPS (root):
#   bash /root/pos/scripts/fix_ssl_www.sh
#
# Dieu kien:
#   - DNS A/CNAME cua www da tro ve IP VPS
#   - Nginx + certbot da cai

set -euo pipefail

DOMAIN="${DOMAIN:-ketoshop.pro.vn}"
WWW="www.${DOMAIN}"
EMAIL="${CERTBOT_EMAIL:-}"
CERT_NAME="${CERTBOT_CERT_NAME:-$DOMAIN}"

echo "=== Kiem tra DNS ==="
APEX_IP=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1; exit}' || true)
WWW_IP=$(getent ahostsv4 "$WWW" 2>/dev/null | awk '{print $1; exit}' || true)
echo "  $DOMAIN -> ${APEX_IP:-?}"
echo "  $WWW -> ${WWW_IP:-?}"
if [ -n "$APEX_IP" ] && [ -n "$WWW_IP" ] && [ "$APEX_IP" != "$WWW_IP" ]; then
  echo "LOI: DNS www khong trung IP apex. Sua DNS roi chay lai."
  exit 1
fi

echo ""
echo "=== Cap nhat Nginx server_name (neu co) ==="
SITE=""
for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
  [ -f "$f" ] || continue
  if grep -qE "(^|[[:space:]])${DOMAIN}([[:space:;]]|$)" "$f" 2>/dev/null; then
    SITE="$f"
    break
  fi
done

if [ -n "$SITE" ]; then
  echo "  Tim thay: $SITE"
  if ! grep -qE "(^|[[:space:]])${WWW}([[:space:;]]|$)" "$SITE"; then
    # Them www vao dong server_name dau tien chua domain
    python3 - <<PY || true
from pathlib import Path
import re
p = Path("$SITE")
text = p.read_text(encoding="utf-8", errors="replace")
www = "$WWW"
domain = "$DOMAIN"
def repl(m):
    names = m.group(2)
    if www in names.split():
        return m.group(0)
    return f"{m.group(1)}{names} {www};"
new, n = re.subn(
    r"(server_name\s+)([^;]*\b" + re.escape(domain) + r"\b[^;]*);",
    repl,
    text,
    count=1,
)
if n:
    p.write_text(new, encoding="utf-8")
    print("  -> Da them", www, "vao server_name")
else:
    print("  ! Khong sua duoc server_name tu dong — sua tay:")
    print(f"    server_name {domain} {www};")
PY
  fi
  nginx -t
  systemctl reload nginx
else
  echo "  ! Khong tim thay file nginx chua $DOMAIN"
fi

echo ""
echo "=== Danh sach cert hien co ==="
certbot certificates 2>/dev/null | sed -n '1,80p' || true

EMAIL_ARGS=()
if [ -n "$EMAIL" ]; then
  EMAIL_ARGS=(--email "$EMAIL")
else
  EMAIL_ARGS=(--register-unsafely-without-email)
fi

echo ""
echo "=== Expand cert '$CERT_NAME' them $WWW (non-interactive) ==="
# --cert-name + --expand + --non-interactive: khong hoi "Do you want to expand..."
if ! certbot certonly \
  --nginx \
  --cert-name "$CERT_NAME" \
  -d "$DOMAIN" \
  -d "$WWW" \
  --expand \
  --non-interactive \
  --agree-tos \
  "${EMAIL_ARGS[@]}"; then
  echo ""
  echo "  ! Expand theo cert-name that bai — thu tao/cap nhat bang ten domain..."
  certbot certonly \
    --nginx \
    -d "$DOMAIN" \
    -d "$WWW" \
    --expand \
    --non-interactive \
    --agree-tos \
    "${EMAIL_ARGS[@]}" || {
      echo ""
      echo "LOI: Khong cap duoc SSL cho $WWW"
      echo "Chay tay:"
      echo "  certbot certificates"
      echo "  certbot certonly --nginx --cert-name $CERT_NAME -d $DOMAIN -d $WWW --expand --non-interactive --agree-tos"
      echo "Kiem tra them:"
      echo "  1) dig +short $WWW"
      echo "  2) port 80/443 mo"
      echo "  3) nginx: server_name $DOMAIN $WWW;"
      exit 1
    }
fi

nginx -t
systemctl reload nginx

echo ""
echo "=== Kiem tra SAN tren chung chi ==="
echo | openssl s_client -servername "$WWW" -connect "127.0.0.1:443" 2>/dev/null \
  | openssl x509 -noout -subject -ext subjectAltName 2>/dev/null | head -30 || true

echo ""
echo "=== HTTP check ==="
curl -sI -o /dev/null -w "https://$DOMAIN/login => %{http_code}\n" "https://$DOMAIN/login" || true
curl -sI -o /dev/null -w "https://$WWW/login => %{http_code}\n" "https://$WWW/login" || true

echo ""
echo "Xong. Thu trinh duyet: https://$WWW/login"
echo "Google OAuth origins can co:"
echo "  https://$DOMAIN"
echo "  https://$WWW"
