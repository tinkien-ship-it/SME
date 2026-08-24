"""Schema hỗ trợ POS offline-first — client_uuid trên sale để dedupe đồng bộ."""
from __future__ import annotations

import sqlite3

from db.schema_helpers import add_column_if_missing, column_exists, ensure_index, row_to_dict
from db_utils import sqlite_commit


def ensure_pos_offline_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    if add_column_if_missing(conn, 'sale', 'client_uuid', 'TEXT'):
        pass
    ensure_index(
        conn,
        'idx_sale_client_uuid',
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sale_client_uuid "
        "ON sale(client_uuid) WHERE client_uuid IS NOT NULL AND client_uuid != ''",
    )
    if commit:
        sqlite_commit(conn, label='pos_offline_schema')


def find_sale_by_client_uuid(conn: sqlite3.Connection, client_uuid: str) -> dict | None:
    uid = (client_uuid or '').strip()
    if not uid:
        return None
    ensure_pos_offline_schema(conn, commit=False)
    if not column_exists(conn, 'sale', 'client_uuid'):
        return None
    row = conn.execute(
        'SELECT id, status, sale_no FROM sale WHERE client_uuid = ? LIMIT 1',
        (uid,),
    ).fetchone()
    if not row:
        return None
    d = row_to_dict(row)
    if 'id' not in d and 'value' in d:
        return None
    return {
        'id': d.get('id'),
        'status': d.get('status'),
        'sale_no': d.get('sale_no'),
    }
