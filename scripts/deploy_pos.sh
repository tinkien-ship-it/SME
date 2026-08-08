#!/bin/bash
# Chay tren VPS Ubuntu sau khi git push tu local:
#   /root/deploy_pos.sh

set -uo pipefail

APP_DIR="/root/pos"
BRANCH="${DEPLOY_BRANCH:-main}"
VENV="$APP_DIR/venv/bin/activate"
SERVICE="pos"

fail() { echo "LOI: $*" >&2; exit 1; }

cd "$APP_DIR" || fail "khong vao duoc $APP_DIR"

echo "=== [1/5] Backup nhanh (khong gom venv) ==="
tar -czf "/root/pos_backup_$(date +%Y%m%d_%H%M).tar.gz" \
  --exclude='pos/venv' \
  --exclude='pos/pos_env' \
  --exclude='pos/__pycache__' \
  --exclude='pos/**/__pycache__' \
  -C /root pos || echo "  ! Backup khong thanh cong, van tiep tuc"
# Chi giu 10 ban backup moi nhat
ls -1t /root/pos_backup_*.tar.gz 2>/dev/null | tail -n +11 | xargs -r rm -f

echo "=== [2/5] Dong bo code tu origin/$BRANCH ==="
[ -d .git ] || fail "/root/pos chua co Git. Chay scripts/setup_git_vps.sh truoc."

git fetch --prune origin "$BRANCH" || fail "git fetch that bai (kiem tra mang / credential)"
git rev-parse --verify --quiet "origin/$BRANCH" >/dev/null \
  || fail "khong thay origin/$BRANCH. Kiem tra ten branch: git branch -r"

# VPS chi la ban trien khai: bo moi thay doi/commit cuc bo de tranh
# "divergent branches" lam treo deploy. File .env, database.db, tenants/
# nam trong .gitignore nen KHONG bi anh huong.
git reset --hard "origin/$BRANCH" || fail "git reset --hard that bai"
find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null
echo "  -> $(git rev-parse --short HEAD) $(git log -1 --pretty=%s)"

echo "=== [3/5] Cai dependency (bo pywin32 tren Linux) ==="
# shellcheck disable=SC1090
source "$VENV" || fail "khong activate duoc venv: $VENV"
grep -v pywin32 requirements.txt | pip install -r /dev/stdin -q \
  || fail "pip install that bai"

echo "=== [4/5] Kiem tra + migrate database ==="
python - <<'PY'
import glob
import os
import sqlite3

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
    print('  ! Cuu du lieu: python scripts/recover_main_db.py <file.db> <file_new.db>')
else:
    print('  -> Tat ca database: ok')
PY

# Migrate khong duoc lam dung deploy (1 DB loi khong the chan restart)
python scripts/migrate_all_dbs.py || echo "  ! Migrate co DB loi — xem log phia tren"

# Registry rong (mat bang tenants sau khi thay database.db) => moi request 500
python - <<'PY'
import glob
import sqlite3

files = [p for p in glob.glob('tenants/*.db') if not p.endswith('registry.db')]
try:
    with sqlite3.connect('file:database.db?mode=ro', uri=True) as conn:
        rows = conn.execute('SELECT COUNT(*) FROM tenants').fetchone()[0]
except Exception as exc:
    rows = None
    print('  ! Doc bang tenants that bai: %s' % exc)

if rows is not None:
    print('  -> registry: %d tenant / %d file tenants/*.db' % (rows, len(files)))
if files and not rows:
    print('  ! REGISTRY RONG nhung van con file tenant — dung lai bang:')
    print('    python scripts/rebuild_registry_db.py            # xem truoc')
    print('    python scripts/rebuild_registry_db.py --apply')
PY

echo "=== [5/5] Restart $SERVICE ==="
systemctl restart "$SERVICE" || true
sleep 3

if systemctl is-active --quiet "$SERVICE"; then
  echo "  -> $SERVICE dang chay"
else
  echo "  ! $SERVICE KHONG chay — traceback gan nhat:"
  journalctl -u "$SERVICE" -n 40 --no-pager -o cat
  echo "  ! Thu chay truc tiep de xem loi: cd $APP_DIR && venv/bin/python -c 'import app'"
  exit 1
fi

HTTP=$(curl -s -m 15 -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/login || echo "000")
echo "HTTP /login => $HTTP"
[ "$HTTP" = "200" ] || echo "  ! /login chua tra ve 200 — xem logs/app_error.log"

echo "=== Deploy xong ==="
