#!/bin/bash
# Vá khẩn 504 Gateway Timeout trên VPS — chạy: bash scripts/vps_fix_504.sh
set -uo pipefail
APP_DIR="${APP_DIR:-/root/pos}"
SERVICE="${SERVICE:-pos}"
cd "$APP_DIR" || exit 1

echo "=== [A] Ghi/cap nhat .env VPS ==="
touch "$APP_DIR/.env"
grep -q '^GUNICORN_WORKERS=' "$APP_DIR/.env" 2>/dev/null || cat >> "$APP_DIR/.env" <<'EOF'

# --- VPS 504 / smooth ---
GUNICORN_WORKERS=3
GUNICORN_THREADS=2
GUNICORN_WORKER_CLASS=gthread
GUNICORN_TIMEOUT=90
SME_CRM_ANALYTICS_BUDGET_SEC=8
SME_GEOIP=0
SME_SQLITE_BUSY_TIMEOUT_MS=15000
EOF
# Dam bao khong con dong comment thua neu user da paste co #
sed -i 's/^# *GUNICORN_WORKERS=/GUNICORN_WORKERS=/' "$APP_DIR/.env" 2>/dev/null || true
sed -i 's/^# *GUNICORN_THREADS=/GUNICORN_THREADS=/' "$APP_DIR/.env" 2>/dev/null || true
sed -i 's/^# *GUNICORN_WORKER_CLASS=/GUNICORN_WORKER_CLASS=/' "$APP_DIR/.env" 2>/dev/null || true
sed -i 's/^# *GUNICORN_TIMEOUT=/GUNICORN_TIMEOUT=/' "$APP_DIR/.env" 2>/dev/null || true
sed -i 's/^# *SME_CRM_ANALYTICS_BUDGET_SEC=/SME_CRM_ANALYTICS_BUDGET_SEC=/' "$APP_DIR/.env" 2>/dev/null || true
sed -i 's/^# *SME_GEOIP=/SME_GEOIP=/' "$APP_DIR/.env" 2>/dev/null || true
sed -i 's/^# *SME_SQLITE_BUSY_TIMEOUT_MS=/SME_SQLITE_BUSY_TIMEOUT_MS=/' "$APP_DIR/.env" 2>/dev/null || true
echo "  -> .env OK"
grep -E '^(GUNICORN_|SME_CRM_|SME_GEOIP|SME_SQLITE_BUSY)' "$APP_DIR/.env" | tail -n 20

echo "=== [B] Nginx proxy_read_timeout 120s ==="
FIXED=0
for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
  [ -f "$f" ] || continue
  if grep -q 'proxy_pass' "$f" 2>/dev/null; then
    if ! grep -q 'proxy_read_timeout' "$f"; then
      # Chen truoc dong proxy_pass dau tien trong block (don gian: append vao file neu co location /)
      cp -a "$f" "${f}.bak.504"
      # Them vao cuoi moi block location co proxy_pass — cach an toan: them global map trong file
      if ! grep -q '### SME_504_FIX' "$f"; then
        cat >> "$f" <<'NGX'

### SME_504_FIX — tang timeout upstream (them bang vps_fix_504.sh)
# Neu da co proxy_read_timeout trong location / thi bo qua block nay.
NGX
      fi
      # Chen truc tiep vao tung location co proxy_pass bang sed
      python3 - <<PY
import re, pathlib
p = pathlib.Path("$f")
t = p.read_text(encoding="utf-8", errors="replace")
if "proxy_read_timeout" in t:
    print("  skip (da co timeout):", p)
else:
    # chen sau moi dong proxy_pass ...;
    def inject(m):
        return m.group(0) + "\n        proxy_connect_timeout 10s;\n        proxy_send_timeout 120s;\n        proxy_read_timeout 120s;"
    nt = re.sub(r'(proxy_pass\s+[^;]+;)', inject, t, count=3)
    if nt != t:
        p.write_text(nt, encoding="utf-8")
        print("  patched:", p)
    else:
        print("  ! khong chen duoc:", p)
PY
      FIXED=1
    else
      echo "  da co proxy_read_timeout: $f"
      grep -n 'proxy_.*timeout' "$f" | head -n 10
    fi
  fi
done
if nginx -t 2>&1 | tee /tmp/nginx_t.out | grep -q successful; then
  systemctl reload nginx
  echo "  -> nginx reload OK"
else
  echo "  ! nginx -t FAIL — khoi phuc .bak neu can"
  cat /tmp/nginx_t.out
fi

echo "=== [C] Systemd EnvironmentFile ==="
UNIT="/etc/systemd/system/${SERVICE}.service"
if [ -f "$UNIT" ]; then
  if ! grep -q 'EnvironmentFile=.*pos/.env' "$UNIT"; then
    cp -a "$UNIT" "${UNIT}.bak.504"
    # Chen sau [Service]
    if grep -q '^\[Service\]' "$UNIT"; then
      sed -i '/^\[Service\]/a EnvironmentFile=-/root/pos/.env' "$UNIT"
      echo "  -> da them EnvironmentFile vao $UNIT"
    fi
  else
    echo "  -> EnvironmentFile da co"
  fi
  systemctl daemon-reload
else
  echo "  ! Khong thay $UNIT — copy mau: cp deploy/pos.service.example $UNIT"
fi

echo "=== [D] WAL + restart app ==="
if [ -f venv/bin/activate ]; then source venv/bin/activate; fi
python scripts/ensure_sqlite_wal.py 2>/dev/null || true
systemctl stop "$SERVICE" 2>/dev/null || true
pkill -f 'gunicorn.*app:app' 2>/dev/null || true
sleep 2
systemctl start "$SERVICE" || true
sleep 3
systemctl is-active "$SERVICE" || echo "  ! service khong active"
ps -o pid,pcpu,pmem,etime,cmd -C gunicorn 2>/dev/null | head -n 10

echo "=== [E] Smoke ==="
curl -m 20 -o /dev/null -s -w 'localhost /login  %{http_code} ttfb=%{time_starttransfer}s\n' http://127.0.0.1:8000/login || echo 'curl fail'
curl -m 20 -o /dev/null -s -w 'localhost /       %{http_code} ttfb=%{time_starttransfer}s\n' http://127.0.0.1:8000/ || echo 'curl fail'

echo "=== [F] Error log Nginx (neu con 504) ==="
tail -n 15 /var/log/nginx/error.log 2>/dev/null | grep -i 'upstream\|timeout' || echo "  (khong thay timeout moi)"

echo "Xong. Neu van 504: deploy code moi bang bash /root/deploy_pos.sh roi chay lai script nay."
