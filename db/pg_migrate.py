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


def _sqlite_indexes(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Trả [(name, sql)] index người dùng (không phải autoindex)."""
    rows = conn.execute(
        """
        SELECT name, sql FROM sqlite_master
        WHERE type='index'
          AND sql IS NOT NULL
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [(r[0], r[1]) for r in rows if r[0] and r[1]]


def _convert_index_ddl(sql: str) -> str | None:
    text = (sql or '').strip().rstrip(';')
    if not text:
        return None
    # Bỏ UNIQUE INDEX trùng PK thường gây lỗi — vẫn thử tạo
    text = re.sub(r'\s+ON\s+', ' ON ', text, flags=re.I)
    text = convert_sqlite_ddl(text)
    # IF NOT EXISTS
    if re.match(r'^\s*CREATE\s+(UNIQUE\s+)?INDEX\b', text, re.I):
        if 'IF NOT EXISTS' not in text.upper():
            text = re.sub(
                r'^\s*CREATE\s+(UNIQUE\s+)?INDEX\b',
                lambda m: f"CREATE {m.group(1) or ''}INDEX IF NOT EXISTS",
                text,
                count=1,
                flags=re.I,
            )
    return text


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


def _ddl_fallback_plain_columns(ddl: str) -> str:
    """Bỏ generated / AFTER — tạo cột thường khi DDL Postgres vẫn lỗi."""
    text = convert_sqlite_ddl(ddl)
    # remaining_amount DOUBLE PRECISION GENERATED ALWAYS AS (...) STORED → cột thường
    text = re.sub(
        r'GENERATED\s+ALWAYS\s+AS\s*\([^)]*\)\s*STORED',
        '',
        text,
        flags=re.I,
    )
    text = re.sub(r'\s+AFTER\s+[`"\']?[\w]+[`"\']?', '', text, flags=re.I)
    return text


def import_sqlite_file(
    sqlite_path: str,
    pg_schema: str,
    *,
    skip_tables: set[str] | None = None,
) -> dict[str, Any]:
    """Import toàn bộ bảng + index từ file SQLite vào schema PostgreSQL."""
    skip = skip_tables or set()
    sch = sanitize_pg_schema(pg_schema)
    ensure_pg_schema(sch)
    stats = {
        'schema': sch,
        'tables': 0,
        'rows': 0,
        'indexes': 0,
        'errors': [],
    }

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    try:
        tables = [t for t in _sqlite_tables(src) if t not in skip]
        pool = get_pool()
        with pool.connection() as pg:
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
                    try:
                        _exec(pg, convert_sqlite_ddl(ddl))
                    except Exception:
                        _safe_rollback(pg)
                        _exec(pg, f'DROP TABLE IF EXISTS "{table}" CASCADE')
                        _exec(pg, _ddl_fallback_plain_columns(ddl))
                except Exception as exc:
                    stats['errors'].append(f'{table} DDL: {exc}')
                    continue

                cols = [c[1] for c in src.execute(f'PRAGMA table_info({table})').fetchall()]
                if not cols:
                    stats['tables'] += 1
                    continue
                # Bỏ cột generated không insert được (PG tự tính); lấy cột thật trên PG
                try:
                    pg_cols = [
                        r[0]
                        for r in pg.execute(
                            """
                            SELECT column_name FROM information_schema.columns
                            WHERE table_schema = %s AND table_name = %s
                              AND is_generated = 'NEVER'
                            ORDER BY ordinal_position
                            """,
                            (sch, table),
                        ).fetchall()
                    ]
                    if pg_cols:
                        cols = [c for c in cols if c in set(pg_cols)]
                except Exception:
                    _safe_rollback(pg)
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
                        if len(stats['errors']) < 40:
                            stats['errors'].append(f'{table} row: {exc}')
                stats['rows'] += inserted
                stats['tables'] += 1

            for _name, idx_sql in _sqlite_indexes(src):
                conv = _convert_index_ddl(idx_sql)
                if not conv:
                    continue
                try:
                    _exec(pg, conv)
                    stats['indexes'] += 1
                except Exception as exc:
                    if len(stats['errors']) < 60:
                        stats['errors'].append(f'index: {exc}')

            for table in tables:
                try:
                    _exec(
                        pg,
                        f"""
                        SELECT setval(
                            pg_get_serial_sequence('"{sch}"."{table}"', 'id'),
                            COALESCE((SELECT MAX(id) FROM "{table}"), 1),
                            true
                        )
                        """
                    )
                except Exception:
                    _safe_rollback(pg)
    finally:
        src.close()

    return stats
