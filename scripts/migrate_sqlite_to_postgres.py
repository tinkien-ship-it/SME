#!/usr/bin/env python3
"""Migrate SQLite → PostgreSQL (schema-per-tenant).

Cấu hình môi trường trước khi chạy:
  export SME_DB_BACKEND=postgres
  export DATABASE_URL=postgresql://user:pass@localhost:5432/sme

Chạy:
  python scripts/migrate_sqlite_to_postgres.py
  python scripts/migrate_sqlite_to_postgres.py --tenant cuahang1
  python scripts/migrate_sqlite_to_postgres.py --main-only
"""
from __future__ import annotations

import argparse
import os
import sys

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# Nap .env neu co (VPS: /root/pos/.env)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, '.env'))
except Exception:
    pass

os.environ.setdefault('SME_DB_BACKEND', 'postgres')

from db.dialect import pg_schema_from_db_path, sanitize_pg_schema  # noqa: E402
from db.pg_migrate import import_sqlite_file  # noqa: E402
from db_utils import BASE_DIR, MAIN_DB_PATH  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description='Migrate SQLite tenant DBs → PostgreSQL schemas')
    parser.add_argument('--tenant', help='Chỉ migrate một tenant (tên file không .db)')
    parser.add_argument('--main-only', action='store_true', help='Chỉ migrate database.db (registry)')
    parser.add_argument('--skip-firms', action='store_true', help='Bỏ qua thư mục firms/')
    args = parser.parse_args()

    if not (os.environ.get('DATABASE_URL') or os.environ.get('SME_PG_URL')):
        print('LOI: thiếu DATABASE_URL hoặc SME_PG_URL', file=sys.stderr)
        return 1

    targets: list[tuple[str, str]] = []

    if args.main_only or not args.tenant:
        targets.append((MAIN_DB_PATH, pg_schema_from_db_path(MAIN_DB_PATH)))

    tenants_dir = os.path.join(BASE_DIR, 'tenants')
    if args.tenant:
        p = os.path.join(tenants_dir, f'{args.tenant}.db')
        if not os.path.isfile(p):
            print(f'LOI: không tìm thấy {p}', file=sys.stderr)
            return 1
        targets.append((p, pg_schema_from_db_path(p, tenant_id=args.tenant)))
    elif not args.main_only:
        if os.path.isdir(tenants_dir):
            for name in sorted(os.listdir(tenants_dir)):
                if not name.endswith('.db'):
                    continue
                p = os.path.join(tenants_dir, name)
                if os.path.isfile(p):
                    tid = name[:-3]
                    targets.append((p, pg_schema_from_db_path(p, tenant_id=tid)))

        if not args.skip_firms:
            firms_root = os.path.join(tenants_dir, 'firms')
            if os.path.isdir(firms_root):
                for firm_id in os.listdir(firms_root):
                    clients_dir = os.path.join(firms_root, firm_id, 'clients')
                    if not os.path.isdir(clients_dir):
                        continue
                    for fname in os.listdir(clients_dir):
                        if not fname.endswith('.db'):
                            continue
                        p = os.path.join(clients_dir, fname)
                        schema = sanitize_pg_schema(f'firm_{firm_id}_c_{fname[:-3]}')
                        targets.append((p, schema))

    if not targets:
        print('Không có file SQLite để migrate.')
        return 0

    ok = 0
    for sqlite_path, schema in targets:
        print(f'Migrate {sqlite_path} → schema {schema} ...')
        try:
            stats = import_sqlite_file(sqlite_path, schema)
            print(
                f'  OK: {stats["tables"]} bảng, {stats["rows"]} dòng'
                + (f', {len(stats["errors"])} cảnh báo' if stats['errors'] else '')
            )
            if stats['errors'][:3]:
                for err in stats['errors'][:3]:
                    print(f'    - {err}')
            ok += 1
        except Exception as exc:
            print(f'  LOI: {exc}', file=sys.stderr)

    print(f'Hoàn tất: {ok}/{len(targets)} database.')
    return 0 if ok == len(targets) else 2


if __name__ == '__main__':
    raise SystemExit(main())
