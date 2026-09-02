#!/usr/bin/env python3
"""Kiểm tra recover_pg_transaction — PG: lỗi SQL + tiếp tục không kẹt INERROR."""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main() -> int:
    from db.dialect import is_postgres
    if not is_postgres():
        print('SKIP: SME_DB_BACKEND != postgres')
        return 0

    from db_utils import open_sqlite, ignore_db_error, MAIN_DB_PATH

    with open_sqlite(MAIN_DB_PATH) as conn:
        cur = conn.cursor()
        try:
            cur.execute('SELECT no_such_column_xyz FROM products LIMIT 1')
        except Exception as exc:
            print('expected error:', type(exc).__name__)

        ignore_db_error(conn)
        row = cur.execute('SELECT 1 AS ok').fetchone()
        ok = row[0] if row else None
        if ok != 1:
            print('FAIL: query after recover returned', row)
            return 1
        print('OK: recovered after aborted statement')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
