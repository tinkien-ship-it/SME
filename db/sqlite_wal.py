# -*- coding: utf-8 -*-
"""Bật WAL cho mọi file SQLite (dev / VPS trước khi cutover Postgres)."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def ensure_wal_for_path(db_path: str) -> str | None:
    """Bật WAL trên một file DB; trả journal_mode hoặc None nếu bỏ qua."""
    from db.dialect import is_postgres
    from db_utils import open_sqlite, ensure_sqlite_wal

    if is_postgres():
        return 'postgres'
    path = os.path.abspath(db_path)
    if not os.path.isfile(path):
        return None
    with open_sqlite(path, timeout=30) as conn:
        raw = conn._raw() if hasattr(conn, '_raw') else conn
        mode = ensure_sqlite_wal(raw, path)
        try:
            raw.commit()
        except Exception:
            pass
        return mode


def ensure_all_sqlite_wal(*, verbose: bool = False) -> tuple[int, int]:
    """
    WAL cho main DB + mọi tenant *.db.
    Gọi lúc migrate / startup (SQLite backend).
    """
    from db.dialect import is_postgres
    from db.init import _discover_database_paths

    if is_postgres():
        if verbose:
            logger.info('ensure_all_sqlite_wal: skip (PostgreSQL backend)')
        return 0, 0

    ok, fail = 0, 0
    for path in _discover_database_paths():
        try:
            mode = ensure_wal_for_path(path)
            ok += 1
            if verbose:
                logger.info('WAL %s => %s', path, mode)
        except Exception as exc:
            fail += 1
            logger.warning('WAL failed %s: %s', path, exc)
    return ok, fail
