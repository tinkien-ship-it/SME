#!/usr/bin/env python3
"""Dựng lại bảng registry (tenants / user_tenant_mapping) trong database.db.

Dùng khi database.db bị hỏng rồi thay bằng file cứu/rỗng, khiến app báo
"no such table: tenants". Dữ liệu từng cửa hàng vẫn nằm nguyên trong
tenants/<tenant_id>.db, nên registry có thể suy ra lại từ đó:

    tenant_id            <- tên file tenants/<tenant_id>.db
    business_name/phone  <- bảng business_info trong tenant DB
    danh sách user       <- bảng users trong tenant DB
    business_line/regime <- role của user + business_info.accounting_regime

Mặc định chỉ BÁO CÁO (không ghi gì). Thêm --apply để ghi thật.

    python scripts/rebuild_registry_db.py                    # xem trước
    python scripts/rebuild_registry_db.py --apply
    python scripts/rebuild_registry_db.py --apply --schema-from tenants/hongphat.db
"""
import argparse
import glob
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Schema gốc của registry — chỉ tạo khi bảng chưa tồn tại.
# Dùng chung định nghĩa với app; bản dưới chỉ là dự phòng khi không import được.
_FALLBACK_DDL = {
    'tenants': """
        CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT UNIQUE NOT NULL,
            db_path TEXT NOT NULL,
            business_name TEXT,
            phone TEXT,
            address TEXT,
            email TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            settings TEXT DEFAULT '{}',
            master_settings TEXT DEFAULT '{}',
            expiry_date TEXT,
            is_2fa_enabled INTEGER DEFAULT 1,
            google_login_allowed INTEGER DEFAULT 1,
            business_type TEXT
        )
    """,
    'user_tenant_mapping': """
        CREATE TABLE IF NOT EXISTS user_tenant_mapping (
            username TEXT PRIMARY KEY,
            email TEXT,
            tenant_id TEXT,
            otp_secret TEXT,
            twofa_type TEXT DEFAULT 'email',
            last_2fa_at DATETIME,
            trust_device_token TEXT,
            is_active INTEGER DEFAULT 1,
            google_login_allowed INTEGER DEFAULT 1,
            is_2fa_enabled INTEGER DEFAULT 1,
            business_type TEXT
        )
    """,
    'user_trusted_devices': """
        CREATE TABLE IF NOT EXISTS user_trusted_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            device_fingerprint TEXT,
            last_login DATETIME,
            UNIQUE(username, device_fingerprint)
        )
    """,
}

try:
    from db.init import REGISTRY_TABLES_DDL as REGISTRY_DDL
except Exception:
    REGISTRY_DDL = _FALLBACK_DDL

# role của chủ tenant -> business_line trong registry.business_type
ROLE_TO_BUSINESS_LINE = {
    'manager': 'pos', 'admin': 'pos',
    'managerFB': 'fb_service', 'adminFB': 'fb_service',
    'manager*': 'rental_service', 'admin*': 'rental_service',
}


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table):
    try:
        return {row[1] for row in conn.execute('PRAGMA table_info("%s")' % table)}
    except sqlite3.DatabaseError:
        return set()


def _open_ro(path):
    conn = sqlite3.connect('file:%s?mode=ro' % path.replace('?', '%3f'), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _build_settings(business_line, regime, hkd_storage_sector, revenue_tier):
    """Settings JSON cho registry. Ưu tiên hàm thật của app, có fallback."""
    try:
        from Services.hkd_sector import storage_code_to_nn
        from Services.tenant_profile import build_tenant_settings

        nn = storage_code_to_nn(hkd_storage_sector) if hkd_storage_sector else 'NN1'
        return build_tenant_settings(
            business_line=business_line,
            hkd_sector=nn or 'NN1',
            revenue_tier=revenue_tier or 'DT1',
            accounting_regime=regime or 'HKD',
            onboarding_completed=True,
        )
    except Exception as exc:
        print('  ! Không dùng được build_tenant_settings (%s) — dùng settings tối giản' % exc)
        return {
            'accounting_regime': regime or 'HKD',
            'business_line': business_line,
            'onboarding_completed': True,
        }


def _read_tenant_db(path, base_dir):
    """Đọc thông tin cần thiết từ 1 tenant DB. None nếu không đọc được.

    base_dir là thư mục chứa database.db — db_path lưu tương đối theo nó,
    đúng như cách app ghép đường dẫn (BASE_DIR + db_path).
    """
    tenant_id = os.path.splitext(os.path.basename(path))[0]
    info = {
        'tenant_id': tenant_id,
        'db_path': os.path.relpath(os.path.abspath(path), base_dir).replace('\\', '/'),
        'business_name': tenant_id,
        'phone': '',
        'address': '',
        'email': '',
        'regime': 'HKD',
        'hkd_sector': None,
        'revenue_tier': None,
        'users': [],
        'owner_role': '',
    }
    try:
        conn = _open_ro(path)
    except sqlite3.DatabaseError as exc:
        print('  ! %s: không mở được (%s)' % (path, exc))
        return None

    try:
        if _table_exists(conn, 'business_info'):
            cols = _columns(conn, 'business_info')
            wanted = [c for c in (
                'business_name', 'phone', 'address', 'email', 'tax_code',
                'accounting_regime', 'default_hkd_sector', 'revenue_tier_declared',
            ) if c in cols]
            row = conn.execute(
                'SELECT %s FROM business_info LIMIT 1' % ','.join('"%s"' % c for c in wanted)
            ).fetchone()
            if row:
                data = dict(row)
                info['business_name'] = (data.get('business_name') or tenant_id).strip()
                info['phone'] = (data.get('phone') or '').strip()
                info['address'] = (data.get('address') or '').strip()
                info['email'] = (data.get('email') or '').strip()
                info['regime'] = (data.get('accounting_regime') or 'HKD').strip() or 'HKD'
                info['hkd_sector'] = data.get('default_hkd_sector')
                info['revenue_tier'] = data.get('revenue_tier_declared')

        if _table_exists(conn, 'users'):
            cols = _columns(conn, 'users')
            support_col = 'is_support_account' if 'is_support_account' in cols else None
            select = 'SELECT username, role, %s AS email, %s AS is_support FROM users ORDER BY id' % (
                'email' if 'email' in cols else "''",
                support_col or '0',
            )
            for row in conn.execute(select):
                username = (row['username'] or '').strip()
                if not username:
                    continue
                info['users'].append({
                    'username': username,
                    'role': (row['role'] or '').strip(),
                    'email': (row['email'] or '').strip(),
                    'is_support': int(row['is_support'] or 0),
                })
    except sqlite3.DatabaseError as exc:
        print('  ! %s: lỗi đọc dữ liệu (%s)' % (path, exc))
    finally:
        conn.close()

    owner = next((u for u in info['users'] if not u['is_support']), None)
    info['owner_role'] = (owner or {}).get('role', '')
    if not info['phone'] and owner:
        info['phone'] = owner['username']
    if not info['email'] and owner:
        info['email'] = owner['email']
    info['business_line'] = ROLE_TO_BUSINESS_LINE.get(info['owner_role'], 'pos')
    if info['db_path'].startswith('..'):
        # Tenant DB nằm ngoài thư mục app → phải lưu đường dẫn tuyệt đối
        info['db_path'] = os.path.abspath(path).replace('\\', '/')
    try:
        info['created_at'] = datetime.fromtimestamp(
            os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M:%S')
    except OSError:
        info['created_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return info


def _restore_missing_schema(main, sample_path):
    """Tạo lại các bảng/index còn thiếu trong main DB, lấy schema từ 1 tenant DB."""
    print('\n--- Bù schema thiếu từ %s ---' % sample_path)
    try:
        src = _open_ro(sample_path)
    except sqlite3.DatabaseError as exc:
        print('  ! Không mở được %s: %s' % (sample_path, exc))
        return 0

    try:
        items = src.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        print('  ! Không đọc được schema: %s' % exc)
        src.close()
        return 0

    have = {row[0] for row in main.execute(
        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    created = 0
    for row in items:
        if row['name'] in have or row['name'] in REGISTRY_DDL:
            continue
        try:
            main.execute(row['sql'])
            created += 1
            print('  + %-6s %s' % (row['type'], row['name']))
        except sqlite3.DatabaseError as exc:
            print('  ! %s %s: %s' % (row['type'], row['name'], exc))
    src.close()
    main.commit()
    print('  -> tạo mới %d đối tượng (không copy dữ liệu)' % created)
    return created


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--main', default=os.path.join(ROOT, 'database.db'),
                        help='file registry (mặc định database.db)')
    parser.add_argument('--tenants-dir', default=os.path.join(ROOT, 'tenants'),
                        help='thư mục chứa tenant DB')
    parser.add_argument('--apply', action='store_true', help='ghi thật (mặc định chỉ xem)')
    parser.add_argument('--force', action='store_true',
                        help='ghi đè cả dòng tenants/mapping đã có')
    parser.add_argument('--schema-from', default='',
                        help='bù các bảng còn thiếu của main DB từ 1 tenant DB')
    args = parser.parse_args()

    main_path = os.path.abspath(args.main)
    if not os.path.exists(main_path):
        print('Không thấy %s' % main_path)
        return 1

    files = sorted(
        p for p in glob.glob(os.path.join(args.tenants_dir, '*.db'))
        if os.path.basename(p) != 'registry.db'
    )
    print('Registry : %s' % main_path)
    print('Tenant DB: %d file trong %s' % (len(files), args.tenants_dir))
    if not files:
        print('Không có tenant DB nào để suy ra registry.')
        return 1

    if args.apply:
        backup = '%s.bak_%s' % (main_path, datetime.now().strftime('%Y%m%d_%H%M%S'))
        shutil.copy2(main_path, backup)
        print('Backup   : %s' % backup)

    conn = sqlite3.connect(main_path)
    conn.row_factory = sqlite3.Row

    print('\n--- Bảng registry ---')
    for name, ddl in REGISTRY_DDL.items():
        if _table_exists(conn, name):
            print('  = %-22s đã có' % name)
            continue
        if args.apply:
            conn.execute(ddl)
            print('  + %-22s ĐÃ TẠO' % name)
        else:
            print('  + %-22s (sẽ tạo)' % name)
    if args.apply:
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tenant_id ON tenants(tenant_id)')
        conn.commit()

    if args.schema_from:
        sample = args.schema_from if os.path.isabs(args.schema_from) \
            else os.path.join(ROOT, args.schema_from)
        if args.apply:
            _restore_missing_schema(conn, sample)
        else:
            print('\n--- Bù schema từ %s: chỉ chạy khi có --apply ---' % sample)

    existing_tenants = set()
    existing_users = set()
    if args.apply or _table_exists(conn, 'tenants'):
        try:
            existing_tenants = {r[0] for r in conn.execute('SELECT tenant_id FROM tenants')}
            existing_users = {r[0] for r in conn.execute('SELECT username FROM user_tenant_mapping')}
        except sqlite3.DatabaseError:
            pass

    print('\n--- Tenant ---')
    added_t = added_u = skipped = 0
    base_dir = os.path.dirname(main_path)
    for path in files:
        info = _read_tenant_db(path, base_dir)
        if not info:
            continue
        tid = info['tenant_id']
        mark = ' '
        if tid in existing_tenants and not args.force:
            mark = '='
            skipped += 1
        elif args.apply:
            settings = _build_settings(
                info['business_line'], info['regime'],
                info['hkd_sector'], info['revenue_tier'],
            )
            conn.execute("""
                INSERT OR REPLACE INTO tenants
                (tenant_id, db_path, business_name, phone, address, email,
                 is_active, created_at, settings, business_type, is_2fa_enabled)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 1)
            """, (
                tid, info['db_path'], info['business_name'], info['phone'],
                info['address'], info['email'], info['created_at'],
                json.dumps(settings, ensure_ascii=False), info['business_line'],
            ))
            mark = '+'
            added_t += 1
        else:
            mark = '+'
            added_t += 1

        print('  %s %-22s %-28s %-11s %-13s %d user' % (
            mark, tid, info['business_name'][:28], info['regime'],
            info['business_line'], len(info['users'])))

        for user in info['users']:
            if user['username'] in existing_users and not args.force:
                continue
            # otp_secret không thể cứu được → 2FA app hỏng. Dùng email nếu có,
            # không có email thì tắt 2FA để chủ shop vào lại được rồi bật sau.
            has_email = bool(user['email'])
            if args.apply:
                conn.execute("""
                    INSERT OR REPLACE INTO user_tenant_mapping
                    (username, email, tenant_id, twofa_type, is_active,
                     business_type, is_2fa_enabled, google_login_allowed)
                    VALUES (?, ?, ?, ?, 1, ?, ?, 1)
                """, (
                    user['username'], user['email'] or None, tid,
                    'email' if has_email else 'none', info['business_line'],
                    1 if has_email else 0,
                ))
            added_u += 1
            print('      - %-18s role=%-12s %s' % (
                user['username'], user['role'],
                user['email'] or '(không email → 2FA tắt)'))

    if args.apply:
        conn.commit()

    print('\n--- Tổng kết ---')
    print('  tenant %s: %d | đã có, bỏ qua: %d' % (
        'đã ghi' if args.apply else 'sẽ ghi', added_t, skipped))
    print('  user mapping %s: %d' % ('đã ghi' if args.apply else 'sẽ ghi', added_u))

    if args.apply:
        try:
            print('  tenants trong registry     : %d' %
                  conn.execute('SELECT COUNT(*) FROM tenants').fetchone()[0])
            print('  user_tenant_mapping        : %d' %
                  conn.execute('SELECT COUNT(*) FROM user_tenant_mapping').fetchone()[0])
            print('  tổng số bảng trong main DB : %d' % conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            print('  ! không đếm được: %s' % exc)
        print("""
Cần kiểm tra lại trong Cài đặt > Khách hàng (master):
  - expiry_date để trống = không giới hạn hạn dùng, hãy set lại ngày hết hạn.
  - business_type suy từ role chủ shop; tenant SME luôn ra 'pos', sửa lại nếu
    thực tế là dịch vụ ăn uống / lưu trú.
  - 2FA app (otp_secret) không cứu được: user có email dùng OTP email,
    user không có email tạm tắt 2FA — bật lại sau khi bổ sung email.""")
    else:
        print('\nChưa ghi gì. Chạy lại với --apply để áp dụng.')

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
