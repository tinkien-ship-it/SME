"""Nhận diện backend DB và helper SQL đa dialect (SQLite / PostgreSQL)."""
from __future__ import annotations

import os
import re
from typing import Any

_PARAM_RE = re.compile(r'\?(?=(?:[^\']*\'[^\']*\')*[^\']*$)')


def db_backend() -> str:
    raw = (os.environ.get('SME_DB_BACKEND') or os.environ.get('DATABASE_BACKEND') or '').strip().lower()
    if raw in ('postgres', 'postgresql', 'pg'):
        return BACKEND_POSTGRES
    if raw == BACKEND_SQLITE:
        return BACKEND_SQLITE
    url = (os.environ.get('DATABASE_URL') or '').strip().lower()
    if (
        url.startswith('postgres://')
        or url.startswith('postgresql://')
        or url.startswith('postgresql+psycopg://')
    ):
        return BACKEND_POSTGRES
    return BACKEND_SQLITE


def is_postgres() -> bool:
    return db_backend() == BACKEND_POSTGRES


def is_sqlite() -> bool:
    return db_backend() == BACKEND_SQLITE


def adapt_sql(sql: str, backend: str | None = None) -> str:
    """Chuyển placeholder SQLite ``?`` → PostgreSQL ``%s``."""
    bk = backend or db_backend()
    if bk != BACKEND_POSTGRES:
        return sql
    return _PARAM_RE.sub('%s', sql)


def pg_schema_from_db_path(db_path: str | None, *, tenant_id: str | None = None) -> str:
    """Suy ra tên schema Postgres từ đường dẫn file tenant (tương thích db_path cũ)."""
    if tenant_id:
        return sanitize_pg_schema(f't_{tenant_id}')
    text = (db_path or '').replace('\\', '/').strip()
    if not text:
        return 'public'
    if '/firms/' in text and '/clients/' in text:
        parts = text.split('/')
        try:
            fi = parts.index('firms')
            ci = parts.index('clients')
            firm_id = parts[fi + 1]
            client_file = parts[ci + 1]
            client_id = client_file.rsplit('.', 1)[0]
            return sanitize_pg_schema(f'firm_{firm_id}_c_{client_id}')
        except (ValueError, IndexError):
            pass
    base = os.path.basename(text)
    name = base.rsplit('.', 1)[0] if base.endswith('.db') else base
    if name in ('database', 'registry'):
        return (os.environ.get('SME_PG_REGISTRY_SCHEMA') or 'public').strip() or 'public'
    return sanitize_pg_schema(f't_{name}')


def sanitize_pg_schema(name: str) -> str:
    raw = re.sub(r'[^a-zA-Z0-9_]', '_', str(name or '').strip().lower())
    raw = re.sub(r'_+', '_', raw).strip('_')
    if not raw:
        raw = 'tenant_default'
    if raw[0].isdigit():
        raw = f't_{raw}'
    return raw[:63]


BACKEND_SQLITE = 'sqlite'
BACKEND_POSTGRES = 'postgres'


def table_exists(conn, name: str) -> bool:
    bk = getattr(conn, '_sme_backend', None) or db_backend()
    if bk == BACKEND_POSTGRES:
        schema = getattr(conn, '_sme_pg_schema', None) or 'public'
        row = conn.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
            LIMIT 1
            """,
            (schema, name),
        ).fetchone()
        return bool(row)
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return bool(row)


def column_names(conn, table: str) -> set[str]:
    bk = getattr(conn, '_sme_backend', None) or db_backend()
    if bk == BACKEND_POSTGRES:
        schema = getattr(conn, '_sme_pg_schema', None) or 'public'
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            (schema, table),
        ).fetchall()
        out: set[str] = set()
        for r in rows:
            if isinstance(r, dict):
                out.add(str(r.get('column_name') or ''))
            elif hasattr(r, 'keys'):
                out.add(str(r['column_name']))
            else:
                out.add(str(r[0]))
        return {c for c in out if c}
    rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
    cols: set[str] = set()
    for r in rows:
        if isinstance(r, dict):
            cols.add(str(r.get('name') or ''))
        elif hasattr(r, 'keys'):
            cols.add(str(r['name']))
        else:
            cols.add(str(r[1]))
    return {c for c in cols if c}


def is_locked_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if 'database is locked' in msg or 'database table is locked' in msg:
        return True
    if 'deadlock detected' in msg:
        return True
    if 'could not serialize access' in msg:
        return True
    return False
