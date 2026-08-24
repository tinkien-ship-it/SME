"""Helper schema runtime — thay PRAGMA / sqlite_master, dùng chung SQLite & PostgreSQL."""
from __future__ import annotations

import re
from typing import Any

from db.dialect import BACKEND_POSTGRES, column_names as _column_names, is_postgres, table_exists
from db.sql_compat import convert_sqlite_ddl

_DB_ERROR = (Exception,)


def table_cols(conn, table: str) -> set[str]:
    return _column_names(conn, table)


def table_cols_lower(conn, table: str) -> set[str]:
    return {c.lower() for c in table_cols(conn, table)}


def column_exists(conn, table: str, column: str) -> bool:
    return (column or '').lower() in table_cols_lower(conn, table)


def add_column_if_missing(
    conn,
    table: str,
    column: str,
    col_type: str,
    *,
    cursor=None,
) -> bool:
    """ALTER TABLE ADD COLUMN nếu chưa có. Trả True nếu vừa thêm."""
    if not table_exists(conn, table):
        return False
    if column_exists(conn, table, column):
        return False
    cur = cursor or conn.cursor()
    pg_type = _pg_col_type(col_type) if is_postgres() else col_type
    try:
        cur.execute(f'ALTER TABLE {table} ADD COLUMN {column} {pg_type}')
        return True
    except _DB_ERROR:
        return False


def _pg_col_type(sqlite_type: str) -> str:
    t = (sqlite_type or 'TEXT').strip()
    upper = t.upper()
    if 'DEFAULT' in upper:
        base, _, rest = t.partition('DEFAULT')
        return f'{_pg_col_type(base.strip())} DEFAULT {rest.strip()}'
    mapping = {
        'INTEGER': 'BIGINT',
        'REAL': 'DOUBLE PRECISION',
        'BLOB': 'BYTEA',
        'TEXT': 'TEXT',
        'DATETIME': 'TIMESTAMP',
        'DATE': 'DATE',
    }
    key = upper.split()[0] if upper else 'TEXT'
    return mapping.get(key, t)


def execute_ddl(conn, ddl: str) -> None:
    sql = convert_sqlite_ddl(ddl) if is_postgres() else ddl
    conn.execute(sql)


def create_table_if_not_exists(conn, ddl: str) -> None:
    execute_ddl(conn, ddl)


def reset_auto_increment(conn, table: str, *, id_col: str = 'id') -> None:
    """Đặt lại sequence / sqlite_sequence sau import hoặc xóa hàng loạt."""
    if not table_exists(conn, table):
        return
    if is_postgres():
        try:
            conn.execute(
                f"""
                SELECT setval(
                    pg_get_serial_sequence('{table}', '{id_col}'),
                    COALESCE((SELECT MAX({id_col}) FROM {table}), 1)
                )
                """
            )
        except _DB_ERROR:
            pass
        return
    try:
        row = conn.execute(f'SELECT MAX({id_col}) FROM {table}').fetchone()
        mx = 0
        if row:
            mx = int(row[0] if not hasattr(row, 'keys') else row[id_col] or 0)
        conn.execute('DELETE FROM sqlite_sequence WHERE name = ?', (table,))
        if mx > 0:
            conn.execute('INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)', (table, mx))
    except _DB_ERROR:
        pass


def set_foreign_keys(conn, enabled: bool) -> None:
    """PRAGMA foreign_keys — no-op trên PostgreSQL (luôn bật khi tạo FK)."""
    if is_postgres():
        return
    val = 'ON' if enabled else 'OFF'
    conn.execute(f'PRAGMA foreign_keys={val}')


def row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, 'keys') and hasattr(row, '_values'):
        return {k: row[k] for k in row.keys()}
    if hasattr(row, 'keys'):
        return {k: row[k] for k in row.keys()}
    return {'value': row[0]} if row else {}


def sync_rowid_mirror_id(conn, table: str, *, id_col: str = 'id') -> None:
    """SQLite: id = rowid. PostgreSQL: bỏ qua (SERIAL)."""
    if is_postgres() or not table_exists(conn, table):
        return
    if not column_exists(conn, table, id_col):
        return
    try:
        conn.execute(f'UPDATE {table} SET {id_col} = rowid WHERE {id_col} IS NULL')
    except _DB_ERROR:
        pass


def ensure_index(
    conn,
    name: str,
    ddl_sqlite: str,
    *,
    ddl_postgres: str | None = None,
) -> None:
    sql = ddl_postgres if is_postgres() and ddl_postgres else ddl_sqlite
    try:
        conn.execute(sql)
    except _DB_ERROR:
        pass
