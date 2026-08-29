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
#
# PostgreSQL (production VPS — khuyến nghị thay SQLite khi nhiều user):
#   SME_DB_BACKEND=postgres
#   DATABASE_URL=postgresql://sme:SECRET@127.0.0.1:5432/sme
#   SME_PG_POOL_MIN=2
#   SME_PG_POOL_MAX=30
#   SME_PG_REGISTRY_SCHEMA=public
# Sau khi cấu hình Postgres lần đầu:
#   python scripts/migrate_sqlite_to_postgres.py
#
# Gunicorn (systemd unit pos.service — ExecStart):
#   gunicorn -c gunicorn.conf.py app:app
#   Hoặc: gunicorn -w ${GUNICORN_WORKERS:-4} -b 127.0.0.1:8000 app:app
#
# SQLite dev — bật WAL mọi file DB:
#   python scripts/ensure_sqlite_wal.py
# Offline POS: trang /sale cache qua service worker; đơn offline lưu IndexedDB
# và đồng bộ khi có mạng (client_uuid chống trùng).

set -uo pipefail

APP_DIR="/root/pos"
BRANCH="${DEPLOY_BRANCH:-main}"
VENV="$APP_DIR/venv/bin/activate"
SERVICE="pos"
AUTO_REPAIR_REGISTRY="${AUTO_REPAIR_REGISTRY:-1}"
DB_BACKUP_DIR="/root/pos_db_backups"

fail() { echo "LOI: $*" >&2; exit 1; }

# Bo CRLF neu file bi copy tu Windows (tranh loi: ue / fi)
if [ -f "$0" ]; then
  sed -i 's/\r$//' "$0" 2>/dev/null || true
fi
if [ -f "$APP_DIR/scripts/deploy_pos.sh" ]; then
  sed -i 's/\r$//' "$APP_DIR/scripts/deploy_pos.sh" 2>/dev/null || true
fi

cd "$APP_DIR" || fail "khong vao duoc $APP_DIR"
mkdir -p "$APP_DIR/logs" "$APP_DIR/tenants"
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
    'SME_DB_BACKEND', 'DATABASE_URL', 'SME_PG_URL',
    'GUNICORN_BIND', 'GUNICORN_WORKERS', 'GUNICORN_THREADS', 'GUNICORN_TIMEOUT',
    'GUNICORN_PRELOAD', 'GUNICORN_WORKER_CLASS', 'GUNICORN_MAX_REQUESTS',
    'SME_CRM_ANALYTICS_BUDGET_SEC', 'SME_SQLITE_BUSY_TIMEOUT_MS', 'SME_GEOIP',
    'SME_PG_POOL_MIN', 'SME_PG_POOL_MAX', 'SME_PG_REGISTRY_SCHEMA',
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

# Tranh index.lock / CRLF gay reset fail
rm -f .git/index.lock 2>/dev/null || true
git fetch --prune origin "$BRANCH" || fail "git fetch that bai"
git rev-parse --verify --quiet "origin/$BRANCH" >/dev/null \
  || fail "khong thay origin/$BRANCH"

REF="origin/$BRANCH"
if ! git reset --hard "$REF"; then
  echo "  ! git reset that bai - thu clean roi reset lai"
  git status -sb || true
  git clean -fd || true
  git reset --hard "$REF" || fail "git reset that bai lan 2"
fi
find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null
echo "  -> $(git rev-parse --short HEAD) $(git log -1 --pretty=%s)"
if [ -f scripts/deploy_pos.sh ]; then
  sed -i 's/\r$//' scripts/deploy_pos.sh 2>/dev/null || true
  cp -f scripts/deploy_pos.sh /root/deploy_pos.sh
  chmod +x /root/deploy_pos.sh scripts/*.sh 2>/dev/null || true
fi

echo "=== [4/6] Cai dependency (bo pywin32 tren Linux) ==="
grep -v pywin32 requirements.txt | pip install -r /dev/stdin -q \
  || fail "pip install that bai"
pip install "psycopg[binary]" psycopg-pool -q 2>/dev/null || true

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
import os
backend = (os.environ.get('SME_DB_BACKEND') or '').strip().lower()
db_url = (os.environ.get('DATABASE_URL') or '').strip().lower()
if backend in ('postgres', 'postgresql', 'pg') or db_url.startswith('postgres'):
    print('  -> PostgreSQL backend — bo qua PRAGMA quick_check SQLite')
else:
    import glob, sqlite3
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

python scripts/verify_pg_sql_compat.py 2>/dev/null || echo "  (verify_pg_sql_compat — bo qua)"
python scripts/pg_staging_smoke.py 2>/dev/null || echo "  (pg_staging_smoke unit — bo qua)"

python - <<'PY'
# Postgres: neu chua co du lieu → import tu SQLite file truoc khi migrate schema
import os, subprocess, sys
backend = (os.environ.get('SME_DB_BACKEND') or '').strip().lower()
db_url = (os.environ.get('DATABASE_URL') or os.environ.get('SME_PG_URL') or '').strip().lower()
is_pg = backend in ('postgres', 'postgresql', 'pg') or db_url.startswith('postgres')
if not is_pg:
    raise SystemExit(0)
os.environ.setdefault('SME_DB_BACKEND', 'postgres')
need_import = True
try:
    from db.postgres_backend import open_pg
    from db.dialect import pg_schema_from_db_path, table_exists
    with open_pg(schema=pg_schema_from_db_path(None)) as conn:
        if table_exists(conn, 'tenants'):
            n = int(conn.execute('SELECT COUNT(*) FROM tenants').fetchone()[0] or 0)
            need_import = n == 0
            print('  -> Postgres tenants: %d' % n)
        else:
            print('  -> Postgres chua co bang tenants')
except Exception as exc:
    print('  ! Kiem tra Postgres:', exc)
if need_import:
    print('  -> Import SQLite → PostgreSQL (lan dau)...')
    rc = subprocess.call([sys.executable, 'scripts/migrate_sqlite_to_postgres.py'])
    if rc != 0:
        print('  ! migrate_sqlite_to_postgres thoat ma', rc)
else:
    print('  -> Bo qua import SQLite (Postgres da co tenants)')
# Live smoke neu co DATABASE_URL
rc = subprocess.call([sys.executable, 'scripts/pg_staging_smoke.py', '--live'])
if rc != 0:
    print('  ! pg_staging_smoke --live that bai (rc=%s)' % rc)
else:
    print('  -> pg_staging_smoke --live OK')
PY

python scripts/migrate_all_dbs.py || echo "  ! Migrate co DB loi — xem log phia tren"

python - <<'PY'
import os, subprocess, sys
backend = (os.environ.get('SME_DB_BACKEND') or '').strip().lower()
db_url = (os.environ.get('DATABASE_URL') or '').strip().lower()
is_pg = backend in ('postgres', 'postgresql', 'pg') or db_url.startswith('postgres')
if is_pg:
    print('  -> PostgreSQL: bo qua ensure_sqlite_wal')
else:
    rc = subprocess.call([sys.executable, 'scripts/ensure_sqlite_wal.py'])
    if rc != 0:
        print('  ! ensure_sqlite_wal co loi (rc=%s)' % rc)
PY

python - <<'PY'
import glob, os, sqlite3, subprocess, sys
backend = (os.environ.get('SME_DB_BACKEND') or '').strip().lower()
db_url = (os.environ.get('DATABASE_URL') or '').strip().lower()
is_pg = backend in ('postgres', 'postgresql', 'pg') or db_url.startswith('postgres')
files = [p for p in glob.glob('tenants/*.db') if not p.endswith('registry.db')]

if is_pg:
    print('  -> PostgreSQL: bo qua repair_vps_main_db.py (script SQLite)')
    try:
        from db.postgres_backend import open_pg
        from db.dialect import pg_schema_from_db_path
        with open_pg(schema=pg_schema_from_db_path(None)) as conn:
            tenants = int(conn.execute('SELECT COUNT(*) FROM tenants').fetchone()[0] or 0)
            masters = int(conn.execute("SELECT COUNT(*) FROM users WHERE role='master'").fetchone()[0] or 0)
        print('  -> registry Postgres: %d tenant / %d file SQLite | master: %d' % (tenants, len(files), masters))
        if tenants == 0 and files:
            print('  ! Postgres registry RONG — chay: python scripts/migrate_sqlite_to_postgres.py')
    except Exception as exc:
        print('  ! Doc registry Postgres that bai:', exc)
    raise SystemExit(0)

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
import os, sqlite3
backend = (os.environ.get('SME_DB_BACKEND') or '').strip().lower()
db_url = (os.environ.get('DATABASE_URL') or '').strip().lower()
is_pg = backend in ('postgres', 'postgresql', 'pg') or db_url.startswith('postgres')
if is_pg:
    try:
        from db.postgres_backend import open_pg
        from db.dialect import pg_schema_from_db_path
        with open_pg(schema=pg_schema_from_db_path(None)) as c:
            n = int(c.execute("SELECT COUNT(*) FROM users WHERE role='master'").fetchone()[0] or 0)
            t = int(c.execute('SELECT COUNT(*) FROM tenants').fetchone()[0] or 0)
        print('  -> Postgres tenants=%d master=%d' % (t, n))
        if t == 0:
            print('  ! Postgres registry RONG — import: python scripts/migrate_sqlite_to_postgres.py')
        if n == 0:
            print('  ! CHUA CO MASTER — them MASTER_PASSWORD vao .env roi:')
            print('    python scripts/ensure_master_user.py --apply')
    except Exception as exc:
        print('  ! Khong kiem tra duoc Postgres:', exc)
    raise SystemExit(0)
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
