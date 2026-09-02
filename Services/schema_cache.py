"""Cache ensure_*_schema theo DB — tránh DDL/PRAGMA lặp mỗi API GET."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

_READY: dict[tuple[str, str], str] = {}


def _conn_key(conn: sqlite3.Connection) -> str:
    try:
        from db.dialect import is_postgres
        if is_postgres():
            return str(
                getattr(conn, '_schema', None)
                or getattr(conn, '_sme_pg_schema', None)
                or 'pg'
            )
        from db_utils import sqlite_db_file
        return str(sqlite_db_file(conn) or id(conn))
    except Exception:
        return str(id(conn))


def ensure_schema_once(
    conn: sqlite3.Connection,
    namespace: str,
    fn: Callable[..., Any],
    *,
    version: str = '1',
    commit: bool = False,
) -> None:
    """Gọi ``fn(conn, commit=...)`` tối đa một lần / process / DB / namespace."""
    key = (_conn_key(conn), namespace)
    if _READY.get(key) == version:
        return
    try:
        fn(conn, commit=commit)
    except Exception:
        try:
            from db_utils import ignore_db_error
            ignore_db_error(conn)
        except Exception:
            pass
        raise
    _READY[key] = version


def invalidate_schema_cache(
    *,
    conn: sqlite3.Connection | None = None,
    namespace: str | None = None,
) -> None:
    if conn is None and namespace is None:
        _READY.clear()
        return
    prefix = _conn_key(conn) if conn is not None else None
    for key in list(_READY):
        if namespace is not None and key[1] != namespace:
            continue
        if prefix is not None and key[0] != prefix:
            continue
        del _READY[key]
