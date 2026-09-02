#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kiểm tra hiệu năng / cấu hình VPS sau deploy.

Chạy:
  python scripts/vps_perf_verify.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
except Exception:
    pass


def main() -> int:
    issues: list[str] = []
    ok: list[str] = []

    from db.dialect import db_backend, is_postgres

    backend = db_backend()
    ok.append(f'db_backend={backend}')
    if backend != 'postgres':
        issues.append('SME_DB_BACKEND chưa là postgres — app có thể vẫn dùng SQLite file')

    raw = (os.environ.get('SME_DB_BACKEND') or '').strip().lower()
    if not raw and is_postgres():
        issues.append('Chỉ có DATABASE_URL postgres — nên set SME_DB_BACKEND=postgres rõ ràng')

    if (os.environ.get('SME_SKIP_RUNTIME_MIGRATE') or '').strip().lower() not in (
        '1', 'true', 'yes', 'on',
    ):
        issues.append('SME_SKIP_RUNTIME_MIGRATE chưa bật — migrate mỗi request làm chậm')

    if is_postgres():
        try:
            from db.postgres_backend import open_pg
            from db.dialect import pg_schema_from_db_path
            with open_pg(schema=pg_schema_from_db_path(None)) as conn:
                conn.execute('SELECT 1')
            ok.append('Postgres registry OK')
        except Exception as exc:
            issues.append(f'Postgres kết nối lỗi: {exc}')

    # Index mới (idempotent)
    try:
        from db.init import ensure_query_indexes
        from db_utils import get_main_db_connection
        conn = get_main_db_connection()
        try:
            ensure_query_indexes(conn)
            conn.commit()
            ok.append('ensure_query_indexes OK')
        finally:
            conn.close()
    except Exception as exc:
        issues.append(f'ensure_query_indexes: {exc}')

    print('=== VPS Performance Verify ===')
    for line in ok:
        print('  OK:', line)
    for line in issues:
        print('  WARN:', line.encode('ascii', 'replace').decode('ascii'))
    if issues:
        print('\nKhuyến nghị .env production:')
        print('  SME_DB_BACKEND=postgres')
        print('  SME_SKIP_RUNTIME_MIGRATE=1')
        print('  GUNICORN_PRELOAD=1')
        print('  GUNICORN_MAX_REQUESTS=3000')
        return 1
    print('\nTất cả kiểm tra đều OK.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
