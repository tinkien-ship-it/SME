"""Chuyển DDL / dữ liệu SQLite → PostgreSQL (schema-per-tenant)."""
from __future__ import annotations

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


def _safe_rollback(pg) -> None:
    try:
        pg.rollback()
    except Exception:
        pass


def _exec(pg, sql: str, params: Any = None) -> None:
    """Chạy 1 câu; lỗi thì rollback để không kẹt InFailedSqlTransaction."""
    try:
        if params is None:
            pg.execute(sql)
        else:
            pg.execute(sql, params)
    except Exception:
        _safe_rollback(pg)
        raise


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
            # Autocommit từng câu DDL/DML thất bại không abort cả block
            try:
                pg.autocommit = True
            except Exception:
                pass
            _exec(pg, f'SET search_path TO "{sch}", public')

            for table in tables:
                ddl = _sqlite_create_sql(src, table)
                if not ddl:
                    continue
                try:
                    _exec(pg, f'DROP TABLE IF EXISTS "{table}" CASCADE')
                    _exec(pg, convert_sqlite_ddl(ddl))
                except Exception as exc:
                    stats['errors'].append(f'{table} DDL: {exc}')
                    continue

                cols = [c[1] for c in src.execute(f'PRAGMA table_info({table})').fetchall()]
                if not cols:
                    stats['tables'] += 1
                    continue
                col_list = ', '.join(f'"{c}"' for c in cols)
                placeholders = ', '.join(['%s'] * len(cols))
                rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
                inserted = 0
                for row in rows:
                    vals = [row[c] for c in cols]
                    try:
                        _exec(
                            pg,
                            f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})',
                            vals,
                        )
                        inserted += 1
                    except Exception as exc:
                        if len(stats['errors']) < 20:
                            stats['errors'].append(f'{table} row: {exc}')
                stats['rows'] += inserted
                stats['tables'] += 1

            for table in tables:
                try:
                    _exec(
                        pg,
                        f"""
                        SELECT setval(
                            pg_get_serial_sequence('"{table}"', 'id'),
                            GREATEST(COALESCE((SELECT MAX(id) FROM "{table}"), 1), 1)
                        )
                        """,
                    )
                except Exception:
                    _safe_rollback(pg)
    finally:
        src.close()
    return stats
