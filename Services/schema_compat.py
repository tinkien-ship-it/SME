"""Tương thích schema giữa tenant cũ/mới — sale_items, sỉ/lẻ, khóa dòng."""
from __future__ import annotations

import sqlite3

from db.dialect import is_postgres
from db.errors import OPERATIONAL_ERROR
from db.schema_helpers import column_exists, table_cols_lower, table_exists
from db_utils import sqlite_commit

_CANONICAL_USE_UNIT = 'use_sale_unit'
_LEGACY_USE_UNIT = 'UseSaleUnit'


def table_cols_lower_cursor(cursor, table: str) -> set[str]:
    """Tương thích cursor-only call sites."""
    conn = getattr(cursor, 'connection', None) or cursor
    return table_cols_lower(conn, table)


def _col_exists(cols: set[str], name: str) -> bool:
    return (name or '').lower() in cols


def use_sale_unit_expr(cursor, alias: str = 'si') -> str:
    """Biểu thức SQL đọc đơn vị sỉ/lẻ — hỗ trợ cả UseSaleUnit và use_sale_unit."""
    cols = table_cols_lower_cursor(cursor, 'sale_items')
    a = alias
    has_upper = _col_exists(cols, _LEGACY_USE_UNIT)
    has_lower = _col_exists(cols, _CANONICAL_USE_UNIT)
    if has_upper and has_lower:
        return f'COALESCE({a}.{_LEGACY_USE_UNIT}, {a}.{_CANONICAL_USE_UNIT}, 0)'
    if has_upper:
        return f'COALESCE({a}.{_LEGACY_USE_UNIT}, 0)'
    if has_lower:
        return f'COALESCE({a}.{_CANONICAL_USE_UNIT}, 0)'
    return '0'


def normalize_use_sale_unit(raw) -> int:
    if raw in (1, '1', True, 'true', 'True'):
        return 1
    return 0


def sale_item_pk_column(cursor) -> str:
    """Tên cột khóa dòng trên sale_items (không alias)."""
    cols = table_cols_lower_cursor(cursor, 'sale_items')
    if _col_exists(cols, 'id'):
        return 'id'
    if is_postgres():
        return 'id'
    return 'rowid'


def sale_item_pk_expr(cursor, alias: str = 'si') -> str:
    """Trả biểu thức khóa dòng: id nếu có, ngược lại rowid."""
    col = sale_item_pk_column(cursor)
    if alias:
        return f'{alias}.{col}'
    return col


def use_sale_unit_where_clause(cursor, alias: str | None = 'si') -> str:
    if alias:
        return f'({use_sale_unit_expr(cursor, alias)}) = ?'
    cols = table_cols_lower_cursor(cursor, 'sale_items')
    has_upper = _col_exists(cols, _LEGACY_USE_UNIT)
    has_lower = _col_exists(cols, _CANONICAL_USE_UNIT)
    if has_upper and has_lower:
        return f'(COALESCE({_LEGACY_USE_UNIT}, {_CANONICAL_USE_UNIT}, 0)) = ?'
    if has_upper:
        return f'COALESCE({_LEGACY_USE_UNIT}, 0) = ?'
    if has_lower:
        return f'COALESCE({_CANONICAL_USE_UNIT}, 0) = ?'
    return '0 = ?'


def use_sale_unit_insert_columns(cursor) -> list[str]:
    """Tên cột ghi khi INSERT — ghi cả hai nếu tenant có cả hai."""
    cols = table_cols_lower_cursor(cursor, 'sale_items')
    names: list[str] = []
    if _col_exists(cols, _LEGACY_USE_UNIT):
        names.append(_LEGACY_USE_UNIT)
    if _col_exists(cols, _CANONICAL_USE_UNIT):
        names.append(_CANONICAL_USE_UNIT)
    return names


def expand_use_sale_unit_values(cursor, value: int) -> list[int]:
    """Giá trị tương ứng với use_sale_unit_insert_columns (cùng giá trị lặp)."""
    n = len(use_sale_unit_insert_columns(cursor))
    v = normalize_use_sale_unit(value)
    return [v] * n


def ensure_sale_items_canonical(conn: sqlite3.Connection, *, commit: bool = True) -> list[str]:
    """Đồng bộ UseSaleUnit ↔ use_sale_unit; thêm cột id mirror rowid nếu thiếu."""
    changed: list[str] = []
    if not table_exists(conn, 'sale_items'):
        return changed

    c = conn.cursor()
    cols = table_cols_lower(conn, 'sale_items')
    has_upper = _col_exists(cols, _LEGACY_USE_UNIT)
    has_lower = _col_exists(cols, _CANONICAL_USE_UNIT)

    if not has_upper:
        try:
            c.execute(
                f'ALTER TABLE sale_items ADD COLUMN {_LEGACY_USE_UNIT} INTEGER DEFAULT 0'
            )
            changed.append(f'alter:sale_items.{_LEGACY_USE_UNIT}')
            has_upper = True
        except OPERATIONAL_ERROR:
            pass

    if not has_lower:
        try:
            c.execute(
                f'ALTER TABLE sale_items ADD COLUMN {_CANONICAL_USE_UNIT} INTEGER DEFAULT 0'
            )
            changed.append(f'alter:sale_items.{_CANONICAL_USE_UNIT}')
            has_lower = True
        except OPERATIONAL_ERROR:
            pass

    if has_upper and has_lower:
        try:
            c.execute(f"""
                UPDATE sale_items
                SET {_CANONICAL_USE_UNIT} = COALESCE({_LEGACY_USE_UNIT}, {_CANONICAL_USE_UNIT}, 0)
                WHERE {_CANONICAL_USE_UNIT} IS NULL
                   OR {_CANONICAL_USE_UNIT} != COALESCE({_LEGACY_USE_UNIT}, {_CANONICAL_USE_UNIT}, 0)
            """)
            if c.rowcount:
                changed.append('sync:sale_items.use_sale_unit')
            c.execute(f"""
                UPDATE sale_items
                SET {_LEGACY_USE_UNIT} = COALESCE({_CANONICAL_USE_UNIT}, {_LEGACY_USE_UNIT}, 0)
                WHERE {_LEGACY_USE_UNIT} IS NULL
                   OR {_LEGACY_USE_UNIT} != COALESCE({_CANONICAL_USE_UNIT}, {_LEGACY_USE_UNIT}, 0)
            """)
            if c.rowcount:
                changed.append('sync:sale_items.UseSaleUnit')
        except OPERATIONAL_ERROR:
            pass
    elif has_upper and not has_lower:
        try:
            c.execute(
                f'ALTER TABLE sale_items ADD COLUMN {_CANONICAL_USE_UNIT} INTEGER DEFAULT 0'
            )
            c.execute(f"""
                UPDATE sale_items SET {_CANONICAL_USE_UNIT} = COALESCE({_LEGACY_USE_UNIT}, 0)
            """)
            changed.append('backfill:sale_items.use_sale_unit')
        except OPERATIONAL_ERROR:
            pass
    elif has_lower and not has_upper:
        try:
            c.execute(
                f'ALTER TABLE sale_items ADD COLUMN {_LEGACY_USE_UNIT} INTEGER DEFAULT 0'
            )
            c.execute(f"""
                UPDATE sale_items SET {_LEGACY_USE_UNIT} = COALESCE({_CANONICAL_USE_UNIT}, 0)
            """)
            changed.append('backfill:sale_items.UseSaleUnit')
        except OPERATIONAL_ERROR:
            pass

    cols = table_cols_lower(conn, 'sale_items')
    if is_postgres():
        changed.extend(_ensure_sale_items_id_postgres(conn, c, cols))
    elif not _col_exists(cols, 'id'):
        try:
            c.execute('ALTER TABLE sale_items ADD COLUMN id INTEGER')
            c.execute('UPDATE sale_items SET id = rowid WHERE id IS NULL')
            changed.append('alter:sale_items.id')
        except OPERATIONAL_ERROR:
            pass
    else:
        try:
            c.execute('UPDATE sale_items SET id = rowid WHERE id IS NULL')
        except OPERATIONAL_ERROR:
            pass

    if commit:
        sqlite_commit(conn, label='schema_compat')
    return changed


def _ensure_sale_items_id_postgres(conn, cursor, cols: set[str]) -> list[str]:
    """Postgres: sale_items.id phải có sequence/DEFAULT — INTEGER trần → RETURNING id = NULL → int(None)."""
    changed: list[str] = []
    has_id = _col_exists(cols, 'id')
    if not has_id:
        try:
            # BIGSERIAL: tạo sequence, DEFAULT, backfill hàng cũ, NOT NULL
            cursor.execute('ALTER TABLE sale_items ADD COLUMN id BIGSERIAL')
            changed.append('alter:sale_items.id:bigserial')
            return changed
        except Exception as exc:
            print('[MIGRATE] sale_items.id BIGSERIAL: %s' % exc)
            try:
                cursor.execute('ALTER TABLE sale_items ADD COLUMN id BIGINT')
                has_id = True
                changed.append('alter:sale_items.id:bigint')
            except Exception as exc2:
                print('[MIGRATE] sale_items.id BIGINT: %s' % exc2)
                return changed

    # Cột id đã có (thường INTEGER nullable, không DEFAULT) — gắn sequence + backfill NULL
    try:
        needs = False
        if cursor.execute(
            'SELECT 1 FROM sale_items WHERE id IS NULL LIMIT 1'
        ).fetchone():
            needs = True
        else:
            def_row = cursor.execute(
                """
                SELECT column_default
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'sale_items'
                  AND column_name = 'id'
                """
            ).fetchone()
            default_val = def_row[0] if def_row is not None else None
            if not default_val:
                needs = True
        if not needs:
            return changed

        cursor.execute('CREATE SEQUENCE IF NOT EXISTS sale_items_id_seq')
        mx_row = cursor.execute(
            'SELECT COALESCE(MAX(id), 0) FROM sale_items'
        ).fetchone()
        try:
            mx = int(mx_row[0] if mx_row is not None else 0) or 0
        except (TypeError, ValueError):
            mx = 0
        # PG sequence minvalue=1 — setval(0) bị từ chối và abort transaction
        if mx < 1:
            cursor.execute(
                "SELECT setval('sale_items_id_seq', 1, false)"
            )
        else:
            cursor.execute(
                "SELECT setval('sale_items_id_seq', ?, true)", (mx,)
            )
        cursor.execute(
            "UPDATE sale_items SET id = nextval('sale_items_id_seq') WHERE id IS NULL"
        )
        mx_row2 = cursor.execute(
            'SELECT COALESCE(MAX(id), 0) FROM sale_items'
        ).fetchone()
        try:
            mx2 = int(mx_row2[0] if mx_row2 is not None else 0) or 0
        except (TypeError, ValueError):
            mx2 = 0
        if mx2 < 1:
            cursor.execute(
                "SELECT setval('sale_items_id_seq', 1, false)"
            )
        else:
            cursor.execute(
                "SELECT setval('sale_items_id_seq', ?, true)", (mx2,)
            )
        cursor.execute(
            "ALTER TABLE sale_items ALTER COLUMN id "
            "SET DEFAULT nextval('sale_items_id_seq')"
        )
        try:
            cursor.execute(
                'ALTER SEQUENCE sale_items_id_seq OWNED BY sale_items.id'
            )
        except Exception:
            pass
        try:
            cursor.execute('ALTER TABLE sale_items ALTER COLUMN id SET NOT NULL')
        except Exception:
            pass
        try:
            cursor.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS ux_sale_items_id ON sale_items (id)'
            )
        except Exception:
            pass
        changed.append('repair:sale_items.id:serial')
    except Exception as exc:
        print('[MIGRATE] repair sale_items.id: %s' % exc)
        try:
            from db_utils import rollback_quietly
            rollback_quietly(conn)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
    return changed
