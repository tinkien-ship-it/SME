#!/bin/bash
# VPS: kiểm tra + vá nhanh Nginx/Gunicorn/SQLite (chạy trên server: bash scripts/vps_smooth_fix.sh)
set -uo pipefail

APP_DIR="${APP_DIR:-/root/pos}"
SERVICE="${SERVICE:-pos}"
cd "$APP_DIR" || { echo "Khong vao duoc $APP_DIR"; exit 1; }

echo "=== [1] Tai nguyen ==="
free -h | head -n 2
uptime
df -h / | tail -n 1

echo "=== [2] Gunicorn / port 8000 ==="
ss -lntp | grep -E ':8000|:80|:443' || true
ps -o pid,pcpu,pmem,etime,cmd -C gunicorn 2>/dev/null || ps aux | grep gunicorn | grep -v grep || true

echo "=== [3] WAL SQLite (giam database locked) ==="
if [ -f "$APP_DIR/venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$APP_DIR/venv/bin/activate"
fi
python scripts/ensure_sqlite_wal.py 2>/dev/null || echo "  (ensure_sqlite_wal bo qua)"

echo "=== [4] Doi timeout Nginx (neu thieu) ==="
NGX=$(ls /etc/nginx/sites-enabled/* 2>/dev/null | head -n 1 || true)
if [ -n "$NGX" ]; then
  if ! grep -q 'proxy_read_timeout' "$NGX" 2>/dev/null; then
    echo "  ! $NGX chua co proxy_read_timeout — tham khao deploy/nginx-ketoshop.conf.example"
  else
    grep -n 'proxy_.*timeout' "$NGX" | head -n 20
  fi
  nginx -t 2>&1 | tail -n 5
else
  echo "  (khong thay sites-enabled)"
fi

echo "=== [5] Restart $SERVICE ==="
systemctl restart "$SERVICE" || {
  echo "  ! systemctl restart $SERVICE that bai — thu pkill + start tay"
  pkill -f 'gunicorn.*app:app' 2>/dev/null || true
  sleep 1
}
sleep 2
systemctl is-active "$SERVICE" 2>/dev/null || true

echo "=== [6] Smoke localhost ==="
curl -o /dev/null -s -w 'GET /      ttfb:%{time_starttransfer}s total:%{time_total}s code:%{http_code}\n' http://127.0.0.1:8000/ || true
curl -o /dev/null -s -w 'GET /login ttfb:%{time_starttransfer}s total:%{time_total}s code:%{http_code}\n' http://127.0.0.1:8000/login || true

echo "=== [7] Goi y .env (SQLite VPS) ==="
cat <<'EOF'
  GUNICORN_WORKERS=3
  GUNICORN_THREADS=2
  GUNICORN_WORKER_CLASS=gthread
  GUNICORN_TIMEOUT=90
  SME_SQLITE_BUSY_TIMEOUT_MS=15000
  SME_CRM_ANALYTICS_BUDGET_SEC=8
  SME_GEOIP=0
EOF

echo "Xong. Neu van 504: sudo tail -n 30 /var/log/nginx/error.log"
