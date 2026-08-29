#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""In backend DB đang active + smoke kết nối. Dùng cuối deploy / debug VPS.

Exit 0 luôn (không chặn deploy), trừ khi SME_REQUIRE_POSTGRES=1 và không phải PG.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Nạp .env nếu có (không phụ thuộc Flask)
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
except Exception:
    pass


def main() -> int:
    from db.dialect import db_backend, is_postgres

    backend = db_backend()
    print('  -> runtime db_backend=%s' % backend)
    print('     SME_DB_BACKEND=%r' % (os.environ.get('SME_DB_BACKEND') or ''))
    url = (os.environ.get('SME_PG_URL') or os.environ.get('DATABASE_URL') or '')
    if url:
        # Che mật khẩu
        safe = url
        if '@' in url and '://' in url:
            try:
                pre, rest = url.split('://', 1)
                creds, host = rest.split('@', 1)
                if ':' in creds:
                    user = creds.split(':', 1)[0]
                    safe = '%s://%s:***@%s' % (pre, user, host)
            except ValueError:
                safe = url[:32] + '...'
        print('     DATABASE/SME_PG_URL=%s' % safe)
    else:
        print('     DATABASE/SME_PG_URL=(trống)')

    if is_postgres():
        try:
            from db.postgres_backend import open_pg
            from db.dialect import pg_schema_from_db_path
            with open_pg(schema=pg_schema_from_db_path(None)) as conn:
                n = int(conn.execute('SELECT 1').fetchone()[0] or 0)
                tenants = 0
                try:
                    tenants = int(conn.execute('SELECT COUNT(*) FROM tenants').fetchone()[0] or 0)
                except Exception:
                    pass
            print('  -> Postgres OK (SELECT 1=%s, tenants≈%s)' % (n, tenants))
        except Exception as exc:
            print('  ! Postgres KET NOI THAT BAI:', exc)
            if (os.environ.get('SME_REQUIRE_POSTGRES') or '').strip().lower() in (
                '1', 'true', 'yes', 'on',
            ):
                return 1
            return 0

        print('  -> App đang dùng PostgreSQL (không bị SQLite lock sau deploy).')
        return 0

    print('  ! App đang dùng SQLITE — Postgres trên VPS chưa được nạp vào process.')
    print('    Nguyên nhân thường gặp:')
    print('      1) /root/pos/.env thiếu SME_DB_BACKEND=postgres')
    print('      2) thiếu SME_PG_URL hoặc DATABASE_URL=postgresql://...')
    print('      3) pos.service không có EnvironmentFile=-/root/pos/.env')
    print('      4) DATABASE_URL=sqlite:///... ghi đè / app fallback sqlite')
    print('    Sửa rồi: systemctl daemon-reload && systemctl restart pos')
    if (os.environ.get('SME_REQUIRE_POSTGRES') or '').strip().lower() in (
        '1', 'true', 'yes', 'on',
    ):
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
