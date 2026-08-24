"""Tương thích schema SQLite giữa tenant cũ/mới — sale_items, sỉ/lẻ, khóa dòng."""
from __future__ import annotations

import sqlite3
from db_utils import sqlite_commit

_CANONICAL_USE_UNIT = 'use_sale_unit'
_LEGACY_USE_UNIT = 'UseSaleUnit'


def table_cols_lower(cursor, table: str) -> set[str]:
    try:
        return {(r[1] or '').lower() for r in cursor.execute(f'PRAGMA table_info({table})')}
    except sqlite3.Error:
        return set()


def _col_exists(cols: set[str], name: str) -> bool:
    return (name or '').lower() in cols


def use_sale_unit_expr(cursor, alias: str = 'si') -> str:
    """Biểu thức SQL đọc đơn vị sỉ/lẻ — hỗ trợ cả UseSaleUnit và use_sale_unit."""
    cols = table_cols_lower(cursor, 'sale_items')
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
    cols = table_cols_lower(cursor, 'sale_items')
    if _col_exists(cols, 'id'):
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
    cols = table_cols_lower(cursor, 'sale_items')
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
    cols = table_cols_lower(cursor, 'sale_items')
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
    if not _table_exists(conn, 'sale_items'):
        return changed

    c = conn.cursor()
    cols = table_cols_lower(c, 'sale_items')
    has_upper = _col_exists(cols, _LEGACY_USE_UNIT)
    has_lower = _col_exists(cols, _CANONICAL_USE_UNIT)

    if not has_upper:
        try:
            c.execute(
                f'ALTER TABLE sale_items ADD COLUMN {_LEGACY_USE_UNIT} INTEGER DEFAULT 0'
            )
            changed.append(f'alter:sale_items.{_LEGACY_USE_UNIT}')
            has_upper = True
        except sqlite3.OperationalError:
            pass

    if not has_lower:
        try:
            c.execute(
                f'ALTER TABLE sale_items ADD COLUMN {_CANONICAL_USE_UNIT} INTEGER DEFAULT 0'
            )
            changed.append(f'alter:sale_items.{_CANONICAL_USE_UNIT}')
            has_lower = True
        except sqlite3.OperationalError:
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
        except sqlite3.OperationalError:
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
        except sqlite3.OperationalError:
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
        except sqlite3.OperationalError:
            pass

    cols = table_cols_lower(c, 'sale_items')
    if not _col_exists(cols, 'id'):
        try:
            c.execute('ALTER TABLE sale_items ADD COLUMN id INTEGER')
            c.execute('UPDATE sale_items SET id = rowid WHERE id IS NULL')
            changed.append('alter:sale_items.id')
        except sqlite3.OperationalError:
            pass
    else:
        try:
            c.execute('UPDATE sale_items SET id = rowid WHERE id IS NULL')
        except sqlite3.OperationalError:
            pass

    if commit:
        sqlite_commit(conn, label='schema_compat')
    return changed


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return bool(row)
