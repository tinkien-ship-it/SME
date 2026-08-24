"""Ngoại lệ DB dùng chung SQLite + PostgreSQL."""
from __future__ import annotations

import sqlite3

DB_ERROR: tuple[type[BaseException], ...] = (sqlite3.Error,)
INTEGRITY_ERROR: tuple[type[BaseException], ...] = (sqlite3.IntegrityError,)
OPERATIONAL_ERROR: tuple[type[BaseException], ...] = (sqlite3.OperationalError,)

try:
    import psycopg
    DB_ERROR = (sqlite3.Error, psycopg.Error)
    INTEGRITY_ERROR = (sqlite3.IntegrityError, psycopg.IntegrityError)
    OPERATIONAL_ERROR = (sqlite3.OperationalError, psycopg.OperationalError)
except ImportError:  # pragma: no cover
    pass


def is_db_error(exc: BaseException) -> bool:
    return isinstance(exc, DB_ERROR)
