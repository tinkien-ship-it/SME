#!/bin/bash
# Chay tren VPS Ubuntu sau khi git push tu local:
#   /root/deploy_pos.sh
#
# Bien moi truong huu ich (trong /root/pos/.env hoac shell):
#   DEPLOY_BRANCH=main
#   MASTER_USERNAME=master
#   MASTER_PASSWORD=...          # neu thieu user master, deploy tu tao/reset
#   MASTER_EMAIL=...
#   AUTO_REPAIR_REGISTRY=1       # mac dinh 1: registry rong thi rebuild tu tenants/*.db

set -uo pipefail

APP_DIR="/root/pos"
BRANCH="${DEPLOY_BRANCH:-main}"
VENV="$APP_DIR/venv/bin/activate"
SERVICE="pos"
AUTO_REPAIR_REGISTRY="${AUTO_REPAIR_REGISTRY:-1}"
DB_BACKUP_DIR="/root/pos_db_backups"

fail() { echo "LOI: $*" >&2; exit 1; }

cd "$APP_DIR" || fail "khong vao duoc $APP_DIR"

# Nap .env neu co (MASTER_PASSWORD, ...)
if [ -f "$APP_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$APP_DIR/.env"
  set +a
fi

echo "=== [1/6] Backup SQLite online (an toan hon cp khi app dang chay) ==="
mkdir -p "$DB_BACKUP_DIR"
STAMP=$(date +%Y%m%d_%H%M%S)
# shellcheck disable=SC1090
source "$VENV" || fail "khong activate duoc venv: $VENV"
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
        d.close()
        s.close()
        print('  +', src, '->', dst)
        ok += 1
    except Exception as exc:
        print('  !', src, exc)
        fail += 1
print('  -> backup %d file, loi %d' % (ok, fail))
# Giu 14 moc backup gan nhat
stamps = sorted({
    '_'.join(fn.split('_')[:2])
    for fn in os.listdir(out_dir)
    if len(fn) > 15 and fn[8] == '_'
}, reverse=True)
for old in stamps[14:]:
    for fn in os.listdir(out_dir):
        if fn.startswith(old):
            try:
                os.remove(os.path.join(out_dir, fn))
            except OSError:
                pass
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

git fetch --prune origin "$BRANCH" || fail "git fetch that bai (kiem tra mang / credential)"
git rev-parse --verify --quiet "origin/$BRANCH" >/dev/null \
  || fail "khong thay origin/$BRANCH. Kiem tra ten branch: git branch -r"

# VPS chi la ban trien khai: bo moi thay doi/commit cuc bo.
# File .env, database.db, tenants/ nam trong .gitignore nen KHONG bi anh huong.
git reset --hard "origin/$BRANCH" || fail "git reset --hard that bai"
find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null
echo "  -> $(git rev-parse --short HEAD) $(git log -1 --pretty=%s)"
cp -f scripts/deploy_pos.sh /root/deploy_pos.sh 2>/dev/null || true
chmod +x /root/deploy_pos.sh scripts/*.sh 2>/dev/null || true

echo "=== [4/6] Cai dependency (bo pywin32 tren Linux) ==="
grep -v pywin32 requirements.txt | pip install -r /dev/stdin -q \
  || fail "pip install that bai"

echo "=== [5/6] Kiem tra + migrate + tu sua registry/master ==="
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
    print('  ! Cuu: python scripts/recover_main_db.py <file.db> <file_new.db>')
    print('  ! Roi: python scripts/repair_vps_main_db.py --apply --password ...')
else:
    print('  -> Tat ca database: ok')
PY

python scripts/migrate_all_dbs.py || echo "  ! Migrate co DB loi — xem log phia tren"

# Registry rong + con file tenant => tu dung lai (tranh 500 no such table / login fail)
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
    print('  -> Tu sua registry/master (AUTO_REPAIR_REGISTRY=1)...')
    cmd = [sys.executable, 'scripts/repair_vps_main_db.py', '--apply']
    if os.environ.get('MASTER_PASSWORD'):
        # password lay tu env trong script con
        pass
    else:
        print('  ! Chua co MASTER_PASSWORD trong .env — se rebuild registry,')
        print('    nhung KHONG tao master. Sau do chay:')
        print('    python scripts/ensure_master_user.py --apply --password \"...\"')
    rc = subprocess.call(cmd)
    if rc != 0:
        print('  ! repair_vps_main_db thoat ma', rc)
elif need:
    print('  ! REGISTRY/MASTER thieu — bat AUTO_REPAIR_REGISTRY=1 hoac chay:')
    print('    python scripts/repair_vps_main_db.py --apply --password \"...\"')
PY

echo "=== [6/6] Restart $SERVICE ==="
systemctl restart "$SERVICE" || true
sleep 3

if systemctl is-active --quiet "$SERVICE"; then
  echo "  -> $SERVICE dang chay"
else
  echo "  ! $SERVICE KHONG chay — traceback gan nhat:"
  journalctl -u "$SERVICE" -n 40 --no-pager -o cat
  echo "  ! Thu: cd $APP_DIR && venv/bin/python -c 'import app'"
  exit 1
fi

HTTP=$(curl -s -m 15 -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/login || echo "000")
echo "HTTP /login => $HTTP"
[ "$HTTP" = "200" ] || echo "  ! /login chua tra ve 200 — xem logs/app_error.log"

# Canh bao cuoi neu van thieu master
python - <<'PY'
import sqlite3, os
try:
    with sqlite3.connect('file:database.db?mode=ro', uri=True) as c:
        n = c.execute("SELECT COUNT(*) FROM users WHERE role='master'").fetchone()[0]
        t = c.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
except Exception as exc:
    print('  ! Khong kiem tra duoc master/tenants:', exc)
else:
    print('  -> sau deploy: tenants=%d master=%d' % (t, n))
    if n == 0:
        print('  ! CHUA CO MASTER — them vao .env roi chay:')
        print('    MASTER_PASSWORD=... python scripts/ensure_master_user.py --apply')
PY

echo "=== Deploy xong ==="
