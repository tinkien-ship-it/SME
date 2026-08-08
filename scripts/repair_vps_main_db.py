#!/usr/bin/env python3
"""Sửa main DB trên VPS sau khi hỏng: registry + schema + master.

Một lệnh cho tình huống hiện tại (database.db mất tenants/users/master,
nhưng tenants/*.db còn nguyên):

    cd /root/pos
    systemctl stop pos
    venv/bin/python scripts/repair_vps_main_db.py --apply \\
        --password 'MatKhauMasterMoi' \\
        --email tinkien@gmail.com

Hoặc đọc MASTER_PASSWORD / MASTER_EMAIL từ .env.
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, '.env'))
except Exception:
    pass


def _run(cmd):
    print('>', ' '.join(cmd))
    return subprocess.call(cmd)


def _pick_schema_donor(tenants_dir):
    files = sorted(
        p for p in glob.glob(os.path.join(tenants_dir, '*.db'))
        if os.path.basename(p).lower() != 'registry.db'
    )
    best, best_n = None, -1
    for path in files:
        try:
            with sqlite3.connect('file:%s?mode=ro' % path.replace('?', '%3f'), uri=True) as c:
                n = c.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
            if n > best_n:
                best, best_n = path, n
        except sqlite3.DatabaseError:
            continue
    return best


def _registry_count(main_path):
    try:
        with sqlite3.connect('file:%s?mode=ro' % main_path.replace('?', '%3f'), uri=True) as c:
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            tenants = c.execute('SELECT COUNT(*) FROM tenants').fetchone()[0] \
                if 'tenants' in tables else 0
            masters = c.execute(
                "SELECT COUNT(*) FROM users WHERE role='master'"
            ).fetchone()[0] if 'users' in tables else 0
            return tenants, masters, len(tables)
    except Exception as exc:
        return None, None, str(exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--main', default=os.path.join(ROOT, 'database.db'))
    parser.add_argument('--tenants-dir', default=os.path.join(ROOT, 'tenants'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--force-rebuild', action='store_true',
                        help='Ghi đè registry kể cả khi đã có dòng tenants')
    parser.add_argument('--username', default=os.environ.get('MASTER_USERNAME', 'master'))
    parser.add_argument('--password', default=os.environ.get('MASTER_PASSWORD', ''))
    parser.add_argument('--email', default=os.environ.get('MASTER_EMAIL', ''))
    parser.add_argument('--full-name', default=os.environ.get('MASTER_FULL_NAME', 'Master'))
    parser.add_argument('--schema-from', default='')
    args = parser.parse_args()

    py = sys.executable
    main_path = os.path.abspath(args.main)
    tenants_dir = os.path.abspath(args.tenants_dir)

    print('=== repair_vps_main_db ===')
    print('main   :', main_path)
    print('tenants:', tenants_dir)
    print('mode   :', 'APPLY' if args.apply else 'DRY-RUN')

    before = _registry_count(main_path)
    print('trước  : tenants=%s master=%s tables=%s' % before)

    donor = args.schema_from or _pick_schema_donor(tenants_dir) or ''
    if donor and not os.path.isabs(donor):
        donor = os.path.join(ROOT, donor)
    print('schema :', donor or '(không có tenant DB)')

    tenant_files = [
        p for p in glob.glob(os.path.join(tenants_dir, '*.db'))
        if os.path.basename(p).lower() != 'registry.db'
    ]
    need_rebuild = args.force_rebuild or not before[0]
    if tenant_files and need_rebuild:
        rebuild = [
            py, os.path.join(ROOT, 'scripts', 'rebuild_registry_db.py'),
            '--main', main_path, '--tenants-dir', tenants_dir,
        ]
        if args.apply:
            rebuild.append('--apply')
            if args.force_rebuild:
                rebuild.append('--force')
            if donor:
                rebuild.extend(['--schema-from', donor])
        code = _run(rebuild)
        if code != 0:
            print('rebuild_registry_db thoát mã', code)
            return code
    else:
        print('Bỏ qua rebuild registry (đã có %s tenant hoặc không có file tenant).'
              % (before[0],))

    if args.apply:
        from db.init import ensure_registry_tables
        from Services.master_account import ensure_master, ensure_users_table

        conn = sqlite3.connect(main_path)
        conn.row_factory = sqlite3.Row
        created = ensure_registry_tables(conn)
        if created:
            print('Tạo bảng registry:', ', '.join(created))
        u_changes = ensure_users_table(conn)
        if u_changes:
            print('Schema users:', ', '.join(u_changes))
        conn.commit()

        if not args.password:
            print('CẢNH BÁO: chưa có MASTER_PASSWORD / --password — bỏ qua tạo master.')
        else:
            action = ensure_master(
                conn,
                username=args.username.strip(),
                password=args.password,
                email=(args.email or '').strip(),
                full_name=(args.full_name or 'Master').strip(),
                disable_2fa=True,
                force_password=True,
            )
            conn.commit()
            print('Master: %s (%s)' % (action, args.username))
            print('Đăng nhập /login — username=%s (2FA tạm TẮT)' % args.username)
        conn.close()
    else:
        print('Dry-run: sẽ gọi ensure_master với username=%r' % args.username)
        if not args.password:
            print('Dry-run: thiếu password (set MASTER_PASSWORD hoặc --password)')

    after = _registry_count(main_path)
    print('sau    : tenants=%s master=%s tables=%s' % after)
    print('xong   :', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    if args.apply and after[0] == 0 and tenant_files:
        print('LỖI: vẫn 0 tenant trong registry dù có file tenant.')
        return 3
    if args.apply and after[1] == 0 and args.password:
        print('LỖI: vẫn không có user role=master.')
        return 4
    return 0


if __name__ == '__main__':
    sys.exit(main())
