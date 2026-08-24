"""Chuyển DDL / dữ liệu SQLite → PostgreSQL (schema-per-tenant)."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from db.dialect import sanitize_pg_schema
from db.postgres_backend import ensure_pg_schema, get_pool
from db.sql_compat import convert_sqlite_ddl


def _sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _sqlite_create_sql(conn: sqlite3.Connection, table: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row[0] if row and row[0] else None


def import_sqlite_file(
    sqlite_path: str,
    pg_schema: str,
    *,
    skip_tables: set[str] | None = None,
) -> dict[str, Any]:
    """Import toàn bộ bảng từ file SQLite vào schema PostgreSQL."""
    skip = skip_tables or set()
    sch = sanitize_pg_schema(pg_schema)
    ensure_pg_schema(sch)
    stats = {'schema': sch, 'tables': 0, 'rows': 0, 'errors': []}

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    try:
        tables = [t for t in _sqlite_tables(src) if t not in skip]
        pool = get_pool()
        with pool.connection() as pg:
            pg.execute(f'SET search_path TO "{sch}", public')
            for table in tables:
                ddl = _sqlite_create_sql(src, table)
                if not ddl:
                    continue
                pg.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
                try:
                    pg.execute(convert_sqlite_ddl(ddl))
                except Exception as exc:
                    stats['errors'].append(f'{table} DDL: {exc}')
                    continue
                cols = [c[1] for c in src.execute(f'PRAGMA table_info({table})').fetchall()]
                if not cols:
                    continue
                col_list = ', '.join(f'"{c}"' for c in cols)
                placeholders = ', '.join(['%s'] * len(cols))
                rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
                for row in rows:
                    vals = [row[c] for c in cols]
                    try:
                        pg.execute(
                            f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})',
                            vals,
                        )
                        stats['rows'] += 1
                    except Exception as exc:
                        stats['errors'].append(f'{table} row: {exc}')
                stats['tables'] += 1
            # Cập nhật sequence sau import
            for table in tables:
                try:
                    pg.execute(
                        f"""
                        SELECT setval(
                            pg_get_serial_sequence('"{table}"', 'id'),
                            COALESCE((SELECT MAX(id) FROM "{table}"), 1)
                        )
                        """
                    )
                except Exception:
                    pass
            pg.commit()
    finally:
        src.close()
    return stats
