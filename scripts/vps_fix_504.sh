#!/bin/bash
# Vá khẩn 504 / database is locked trên VPS
# Chạy: bash /root/pos/scripts/vps_fix_504.sh
set -uo pipefail
APP_DIR="${APP_DIR:-/root/pos}"
SERVICE="${SERVICE:-pos}"
UNIT="/etc/systemd/system/${SERVICE}.service"
cd "$APP_DIR" || exit 1

echo "=== [A] .env VPS (Gunicorn + SQLite) ==="
touch "$APP_DIR/.env"
# Ghi/đè các key quan trọng (idempotent)
python3 - <<'PY'
from pathlib import Path
p = Path("/root/pos/.env")
text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
keys = {
    "GUNICORN_WORKERS": "2",
    "GUNICORN_THREADS": "3",
    "GUNICORN_WORKER_CLASS": "gthread",
    "GUNICORN_TIMEOUT": "90",
    "SME_CRM_ANALYTICS_BUDGET_SEC": "8",
    "SME_GEOIP": "0",
    "SME_SQLITE_BUSY_TIMEOUT_MS": "30000",
    "SME_SQLITE_WRITE_LOCK_SEC": "8",
    "SME_SQLITE_WRITE_RETRIES": "16",
}
kept = []
seen = set()
for ln in lines:
    if "=" not in ln:
        kept.append(ln)
        continue
    k = ln.split("=", 1)[0].strip()
    if k in keys:
        kept.append(f"{k}={keys[k]}")
        seen.add(k)
    else:
        kept.append(ln)
for k, v in keys.items():
    if k not in seen:
        kept.append(f"{k}={v}")
p.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
print("  -> .env OK")
for k in keys:
    print(f"     {k}={keys[k]}")
PY

echo "=== [B] Viết lại pos.service sạch (hết Assignment outside of section) ==="
cp -a "$UNIT" "${UNIT}.bak.504.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
cat > "$UNIT" <<'EOF'
[Unit]
Description=SME/KetoShop POS (Gunicorn)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/pos
EnvironmentFile=-/root/pos/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/root/pos/venv/bin/gunicorn -c /root/pos/gunicorn.conf.py app:app
ExecReload=/bin/kill -s HUP $MAINPID
Restart=always
RestartSec=3
TimeoutStopSec=35
KillMode=mixed

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
echo "  -> $UNIT rewritten OK"
systemctl cat "$SERVICE" 2>/dev/null | head -n 25

echo "=== [C] Nginx proxy_read_timeout >= 120s ==="
FIXED=0
for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
  [ -f "$f" ] || continue
  grep -q 'proxy_pass' "$f" 2>/dev/null || continue
  python3 - <<PY
from pathlib import Path
import re
p = Path("$f")
t = p.read_text(encoding="utf-8", errors="replace")
changed = False
# Đảm bảo mọi block proxy có timeout đủ lớn
if "proxy_read_timeout" not in t:
    def inject(m):
        return (m.group(0)
                + "\n        proxy_connect_timeout 10s;"
                + "\n        proxy_send_timeout 120s;"
                + "\n        proxy_read_timeout 120s;")
    nt = re.sub(r'(proxy_pass\s+[^;]+;)', inject, t, count=5)
    if nt != t:
        p.write_text(nt, encoding="utf-8")
        print("  patched timeouts:", p)
        changed = True
else:
    # Nâng timeout quá thấp (< 90) lên 120
    def bump(m):
        num = float(re.sub(r'[^0-9.]', '', m.group(1) or '0') or 0)
        if num < 90:
            return m.group(0).replace(m.group(1), '120s')
        return m.group(0)
    nt = re.sub(r'proxy_read_timeout\s+([^;]+);', bump, t)
    nt = re.sub(r'proxy_send_timeout\s+([^;]+);', bump, nt)
    if nt != t:
        p.write_text(nt, encoding="utf-8")
        print("  bumped timeouts:", p)
        changed = True
    else:
        print("  OK timeouts:", p)
        for line in t.splitlines():
            if "proxy_" in line and "timeout" in line:
                print("   ", line.strip())
if changed:
    open("/tmp/ngx_504_changed", "w").write("1")
PY
done
if [ -f /tmp/ngx_504_changed ]; then
  rm -f /tmp/ngx_504_changed
  if nginx -t 2>&1 | tee /tmp/nginx_t.out | grep -q successful; then
    systemctl reload nginx
    echo "  -> nginx reload OK"
  else
    echo "  ! nginx -t FAIL"
    cat /tmp/nginx_t.out
  fi
else
  nginx -t 2>&1 | tail -n 3 || true
fi

echo "=== [D] WAL + checkpoint nhẹ ==="
if [ -f venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi
python scripts/ensure_sqlite_wal.py 2>/dev/null || true
# Checkpoint WAL phình to (không block lâu)
python3 - <<'PY'
import sqlite3, pathlib
root = pathlib.Path("/root/pos")
files = [root / "database.db"] + list((root / "tenants").glob("*.db"))
for f in files:
    if not f.is_file():
        continue
    try:
        c = sqlite3.connect(str(f), timeout=30)
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
        print("  wal ok:", f.name)
    except Exception as e:
        print("  wal skip:", f.name, e)
PY

echo "=== [E] Restart app sạch ==="
systemctl stop "$SERVICE" 2>/dev/null || true
pkill -f 'gunicorn.*app:app' 2>/dev/null || true
sleep 2
# Xóa file lock SQLite stale nếu có
find "$APP_DIR" "$APP_DIR/tenants" -maxdepth 1 -name '*.db-journal' 2>/dev/null | head
systemctl start "$SERVICE"
sleep 3
systemctl is-active "$SERVICE" || { echo "SERVICE FAIL"; systemctl status "$SERVICE" --no-pager | head -n 40; exit 1; }
ps -o pid,pcpu,pmem,etime,cmd -C gunicorn 2>/dev/null | head -n 10
# Cảnh báo systemd cũ
systemctl status "$SERVICE" --no-pager 2>&1 | grep -i 'Assignment outside' && echo "  ! van con warning unit" || echo "  unit OK (khong Assignment outside)"

echo "=== [F] Smoke ==="
curl -m 15 -o /dev/null -s -w 'localhost /login  %{http_code} ttfb=%{time_starttransfer}s\n' http://127.0.0.1:8000/login || echo 'curl fail'
curl -m 15 -o /dev/null -s -w 'localhost /       %{http_code} ttfb=%{time_starttransfer}s\n' http://127.0.0.1:8000/ || echo 'curl fail'

echo "=== [G] Nginx timeout log (gan day) ==="
tail -n 40 /var/log/nginx/error.log 2>/dev/null | grep -iE 'upstream timed out|504' | tail -n 8 || echo "  (khong thay timeout moi)"

echo ""
echo "Xong. Neu code chua moi: bash /root/deploy_pos.sh roi chay lai script nay."
echo "Sau do thu lai PUT lead — neu con loi xem: journalctl -u pos -n 50 --no-pager"
