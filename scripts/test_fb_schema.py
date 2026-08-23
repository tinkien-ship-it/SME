#!/usr/bin/env python3
"""Kiểm tra schema F&B + sale_items trên tenant DB (chạy sau migrate)."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db_utils import open_sqlite
from Services.schema_compat import (
    ensure_sale_items_canonical,
    sale_item_pk_column,
    table_cols_lower,
    use_sale_unit_expr,
)


def check_db(path: str) -> bool:
    print(f'\n== {path} ==')
    ok = True
    with open_sqlite(path) as conn:
        mode = conn.execute('PRAGMA journal_mode').fetchone()
        print('journal_mode:', mode[0] if mode else '?')

        changed = ensure_sale_items_canonical(conn, commit=True)
        if changed:
            print('migration:', ', '.join(changed))

        c = conn.cursor()
        cols = table_cols_lower(c, 'sale_items')
        if not cols:
            print('sale_items: (no table)')
            return True

        print('sale_items cols:', sorted(cols))
        pk = sale_item_pk_column(c)
        print('pk column:', pk)
        expr = use_sale_unit_expr(c, 'si')
        print('use_sale_unit expr:', expr)

        mismatch = conn.execute("""
            SELECT COUNT(*) FROM sale_items
            WHERE COALESCE(UseSaleUnit, -1) != COALESCE(use_sale_unit, -1)
        """).fetchone()
        if mismatch and mismatch[0]:
            print('WARN: UseSaleUnit != use_sale_unit rows:', mismatch[0])
            ok = False
        else:
            print('UseSaleUnit/use_sale_unit: synced')

        null_id = conn.execute(
            f'SELECT COUNT(*) FROM sale_items WHERE {pk} IS NOT NULL AND id IS NULL'
        ).fetchone() if 'id' in cols else (0,)
        if null_id and null_id[0]:
            print('WARN: rows missing id:', null_id[0])
            ok = False

    return ok


def main():
    tenants = os.path.join(ROOT, 'tenants')
    paths = []
    if os.path.isdir(tenants):
        paths.extend(
            os.path.join(tenants, f)
            for f in sorted(os.listdir(tenants))
            if f.endswith('.db') and f.lower() not in ('registry.db',)
        )
    if not paths:
        print('No tenant DB found')
        return 0

    all_ok = all(check_db(p) for p in paths)
    print('\nRESULT:', 'OK' if all_ok else 'ISSUES FOUND')
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
