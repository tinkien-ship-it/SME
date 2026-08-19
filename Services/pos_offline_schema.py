"""Schema hỗ trợ POS offline-first — client_uuid trên sale để dedupe đồng bộ."""
from __future__ import annotations

import sqlite3


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    except sqlite3.Error:
        return set()


def ensure_pos_offline_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    cols = _cols(conn, 'sale')
    if 'client_uuid' not in cols:
        try:
            conn.execute('ALTER TABLE sale ADD COLUMN client_uuid TEXT')
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_sale_client_uuid '
            'ON sale(client_uuid) WHERE client_uuid IS NOT NULL AND client_uuid != \'\''
        )
    except sqlite3.OperationalError:
        pass
    if commit:
        conn.commit()


def find_sale_by_client_uuid(conn: sqlite3.Connection, client_uuid: str) -> dict | None:
    uid = (client_uuid or '').strip()
    if not uid:
        return None
    ensure_pos_offline_schema(conn, commit=False)
    cols = _cols(conn, 'sale')
    if 'client_uuid' not in cols:
        return None
    row = conn.execute(
        'SELECT id, status, sale_no FROM sale WHERE client_uuid = ? LIMIT 1',
        (uid,),
    ).fetchone()
    if not row:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return {'id': row[0], 'status': row[1], 'sale_no': row[2] if len(row) > 2 else None}
