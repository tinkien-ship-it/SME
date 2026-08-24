#!/bin/bash
# Chay tren VPS Ubuntu sau khi git push tu local:
#   /root/deploy_pos.sh
#
# Bien moi truong (trong /root/pos/.env — KHONG source bang bash):
#   DEPLOY_BRANCH=main
#   MASTER_USERNAME=master
#   MASTER_PASSWORD=...
#   MASTER_EMAIL=...
#   AUTO_REPAIR_REGISTRY=1
#   SME_SQLITE_TIMEOUT=60
#   SME_SKIP_RUNTIME_MIGRATE=1
#   SME_LOGIN_WRITE_RETRIES=3
#   SME_ACCOUNTING_QUEUE_SEC=30
#   SME_DISABLE_SCHEDULERS=0          # 1 = tắt mọi background scheduler (debug)
#   SME_FORCE_SCHEDULERS=0            # 1 = bỏ qua check reloader (test)
#   SME_CANONICAL_HOST=ketoshop.pro.vn          # www → apex (tranh mat OAuth session)
#   SME_SESSION_COOKIE_DOMAIN=.ketoshop.pro.vn  # cookie chung www + apex
#   PUBLIC_BASE_URL=https://ketoshop.pro.vn

set -uo pipefail

APP_DIR="/root/pos"
BRANCH="${DEPLOY_BRANCH:-main}"
VENV="$APP_DIR/venv/bin/activate"
SERVICE="pos"
AUTO_REPAIR_REGISTRY="${AUTO_REPAIR_REGISTRY:-1}"
DB_BACKUP_DIR="/root/pos_db_backups"

fail() { echo "LOI: $*" >&2; exit 1; }

cd "$APP_DIR" || fail "khong vao duoc $APP_DIR"
# shellcheck disable=SC1090
source "$VENV" || fail "khong activate duoc venv: $VENV"

# Nap .env bang Python (tranh loi bash khi value co khoang trang: KIEN TRUNG TIN)
eval "$(python - <<'PY'
import os, shlex
from pathlib import Path
p = Path('.env')
if not p.exists():
    raise SystemExit(0)
try:
    from dotenv import dotenv_values
    vals = dotenv_values(p)
except Exception:
    vals = {}
    for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
        s = line.strip()
        if not s or s.startswith('#') or '=' not in s:
            continue
        k, _, v = s.partition('=')
        vals[k.strip()] = v.strip().strip('"').strip("'")
keys = (
    'DEPLOY_BRANCH', 'MASTER_USERNAME', 'MASTER_PASSWORD', 'MASTER_EMAIL',
    'MASTER_FULL_NAME', 'AUTO_REPAIR_REGISTRY', 'SME_SQLITE_TIMEOUT',
    'SME_SQLITE_WRITE_RETRIES', 'SME_SKIP_RUNTIME_MIGRATE',
    'SME_LOGIN_WRITE_RETRIES', 'SME_ACCOUNTING_QUEUE_SEC',
    'SME_DISABLE_SCHEDULERS', 'SME_FORCE_SCHEDULERS',
    'SME_ACCT_QUEUE_PROBE_TIMEOUT', 'SME_ACCT_QUEUE_MAX_DBS',
    'SME_CANONICAL_HOST', 'SME_SESSION_COOKIE_DOMAIN', 'PUBLIC_BASE_URL',
)
for k in keys:
    v = vals.get(k)
    if v is not None and str(v) != '':
        print('export %s=%s' % (k, shlex.quote(str(v))))
PY
)"

BRANCH="${DEPLOY_BRANCH:-$BRANCH}"

echo "=== [0/6] Stop $SERVICE (tranh database is locked khi migrate/backup) ==="
systemctl stop "$SERVICE" || true
sleep 2
# Gỡ worker treo (neu co)
pkill -f 'gunicorn.*app:app' 2>/dev/null || true
sleep 1

echo "=== [1/6] Backup SQLite online ==="
mkdir -p "$DB_BACKUP_DIR"
STAMP=$(date +%Y%m%d_%H%M%S)
STAMP="$STAMP" DB_BACKUP_DIR="$DB_BACKUP_DIR" python - <<'PY'
import glob, os, sqlite3
stamp = os.environ['STAMP']
out_dir = os.environ['DB_BACKUP_DIR']
os.makedirs(out_dir, exist_ok=True)
targets = []
if os.path.isfile('database.db'):
    targets.append('database.db')
targets += sorted(p for p in glob.glob('tenants/*.db') if not p.endswith('registry.db'))
ok = fail = 0
for src in targets:
    name = src.replace('/', '_').replace('\\', '_')
    dst = os.path.join(out_dir, '%s_%s' % (stamp, name))
    try:
        s = sqlite3.connect('file:%s?mode=ro' % src.replace('?', '%3f'), uri=True)
        d = sqlite3.connect(dst)
        s.backup(d)
        d.close(); s.close()
        print('  +', src, '->', dst)
        ok += 1
    except Exception as exc:
        print('  !', src, exc)
        fail += 1
print('  -> backup %d file, loi %d' % (ok, fail))
stamps = sorted({
    '_'.join(fn.split('_')[:2])
    for fn in os.listdir(out_dir)
    if len(fn) > 15 and fn[8] == '_'
}, reverse=True)
for old in stamps[14:]:
    for fn in os.listdir(out_dir):
        if fn.startswith(old):
            try: os.remove(os.path.join(out_dir, fn))
            except OSError: pass
PY

echo "=== [2/6] Backup thu muc (khong gom venv) ==="
tar -czf "/root/pos_backup_${STAMP}.tar.gz" \
  --exclude='pos/venv' \
  --exclude='pos/pos_env' \
  --exclude='pos/__pycache__' \
  --exclude='pos/**/__pycache__' \
  -C /root pos || echo "  ! Backup tar khong thanh cong, van tiep tuc"
ls -1t /root/pos_backup_*.tar.gz 2>/dev/null | tail -n +11 | xargs -r rm -f

echo "=== [3/6] Dong bo code tu origin/$BRANCH ==="
[ -d .git ] || fail "/root/pos chua co Git. Chay scripts/setup_git_vps.sh truoc."

# Tranh CRLF / file cu / index.lock gay reset fail
rm -f .git/index.lock 2>/dev/null || true
git fetch --prune origin "$BRANCH" || fail "git fetch that bai (kiem tra mang / credential)"
git rev-parse --verify --quiet "origin/$BRANCH" >/dev/null \
  || fail "khong thay origin/$BRANCH. Kiem tra ten branch: git branch -r"

if ! git reset --hard "origin/$BRANCH"; then
  echo "  ! git reset --hard that bai — thu khoi phuc:"
  git status -sb || true
  git clean -fd || true
  git reset --hard "origin/$BRANCH" || fail "git reset --hard that bai (lan 2)"
fi
find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null
echo "  -> $(git rev-parse --short HEAD) $(git log -1 --pretty=%s)"
# Cap nhat script deploy tren /root (sau khi reset thanh cong)
if [ -f scripts/deploy_pos.sh ]; then
  # Bo CRLF neu file bi commit tu Windows
  sed -i 's/\r$//' scripts/deploy_pos.sh 2>/dev/null || true
  cp -f scripts/deploy_pos.sh /root/deploy_pos.sh
  chmod +x /root/deploy_pos.sh scripts/*.sh 2>/dev/null || true
fi

echo "=== [4/6] Cai dependency (bo pywin32 tren Linux) ==="
grep -v pywin32 requirements.txt | pip install -r /dev/stdin -q \
  || fail "pip install that bai"

echo "=== [5/6] Kiem tra + migrate + tu sua registry/master (service DANG TAT) ==="
python - <<'PY'
# Sao luu Google OAuth da luu (tranh mat khi repair registry/main DB)
try:
    from Services.login_service import export_google_oauth_persist
    data = export_google_oauth_persist()
    cid = (data or {}).get('google_client_id') or ''
    print('  -> Google OAuth persist: %s' % ('OK (' + cid[:24] + '...)' if cid else 'chua co Client ID trong DB'))
except Exception as exc:
    print('  ! Khong export duoc Google OAuth:', exc)
PY

python - <<'PY'
import glob, os, sqlite3
bad = []
for path in ['database.db'] + sorted(glob.glob('tenants/*.db')):
    if not os.path.exists(path):
        continue
    try:
        with sqlite3.connect('file:%s?mode=ro' % path, uri=True) as conn:
            result = conn.execute('PRAGMA quick_check').fetchone()[0]
        if result != 'ok':
            bad.append((path, result[:120]))
    except Exception as exc:
        bad.append((path, str(exc)))
if bad:
    print('  ! DATABASE CO VAN DE:')
    for path, why in bad:
        print('    - %s: %s' % (path, why))
else:
    print('  -> Tat ca database: ok')
PY

python scripts/migrate_all_dbs.py || echo "  ! Migrate co DB loi — xem log phia tren"

python - <<'PY'
import glob, os, sqlite3, subprocess, sys
files = [p for p in glob.glob('tenants/*.db') if not p.endswith('registry.db')]
rows = masters = None
err = None
try:
    with sqlite3.connect('file:database.db?mode=ro', uri=True) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = conn.execute('SELECT COUNT(*) FROM tenants').fetchone()[0] if 'tenants' in tables else 0
        masters = conn.execute("SELECT COUNT(*) FROM users WHERE role='master'").fetchone()[0] if 'users' in tables else 0
except Exception as exc:
    err = str(exc)
if err:
    print('  ! Doc registry that bai:', err)
else:
    print('  -> registry: %d tenant / %d file | master users: %d' % (rows or 0, len(files), masters or 0))

auto = os.environ.get('AUTO_REPAIR_REGISTRY', '1') == '1'
need = bool(files) and (rows in (0, None) or masters in (0, None))
if need and auto:
    print('  -> Tu sua registry/master...')
    if not os.environ.get('MASTER_PASSWORD') and (masters in (0, None)):
        print('  ! Chua co MASTER_PASSWORD trong .env — rebuild registry, khong tao master.')
        print('    Them MASTER_PASSWORD=... vao .env roi: python scripts/ensure_master_user.py --apply')
    rc = subprocess.call([sys.executable, 'scripts/repair_vps_main_db.py', '--apply'])
    if rc != 0:
        print('  ! repair_vps_main_db thoat ma', rc)
elif need:
    print('  ! REGISTRY/MASTER thieu — chay: python scripts/repair_vps_main_db.py --apply')
PY

python - <<'PY'
# Khoi phuc Google Client ID/Secret neu DB bi mat sau repair
try:
    from Services.login_service import restore_google_oauth_persist, export_google_oauth_persist
    info = restore_google_oauth_persist()
    if info.get('restored'):
        print('  -> Da khoi phuc Google OAuth:', ', '.join(info.get('changed') or []))
    else:
        print('  -> Google OAuth DB OK (khong can restore)')
    export_google_oauth_persist()
except Exception as exc:
    print('  ! Khong restore duoc Google OAuth:', exc)
PY

echo "=== [6/6] Start $SERVICE ==="
systemctl start "$SERVICE" || true
sleep 3

if systemctl is-active --quiet "$SERVICE"; then
  echo "  -> $SERVICE dang chay"
else
  echo "  ! $SERVICE KHONG chay — traceback gan nhat:"
  journalctl -u "$SERVICE" -n 40 --no-pager -o cat
  exit 1
fi

HTTP=$(curl -s -m 15 -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/login || echo "000")
echo "HTTP /login => $HTTP"
[ "$HTTP" = "200" ] || echo "  ! /login chua tra ve 200 — xem logs/app_error.log"

python - <<'PY'
import sqlite3
try:
    with sqlite3.connect('file:database.db?mode=ro', uri=True) as c:
        mode = c.execute('PRAGMA journal_mode').fetchone()[0]
        n = c.execute("SELECT COUNT(*) FROM users WHERE role='master'").fetchone()[0]
        t = c.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
except Exception as exc:
    print('  ! Khong kiem tra duoc:', exc)
else:
    print('  -> journal_mode=%s tenants=%d master=%d' % (mode, t, n))
    if str(mode).lower() != 'wal':
        print('  ! Canh bao: journal_mode khong phai WAL')
    if n == 0:
        print('  ! CHUA CO MASTER — them MASTER_PASSWORD vao .env roi:')
        print('    python scripts/ensure_master_user.py --apply')
PY

echo "=== Deploy xong ==="
