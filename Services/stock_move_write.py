"""Ghi stock_moves an toàn theo schema thực tế (intersect cột + stamp kho)."""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from db.schema_helpers import add_column_if_missing, table_cols

logger = logging.getLogger(__name__)

_SM_EXT_COLS: tuple[tuple[str, str], ...] = (
    ('warehouse_code', "TEXT DEFAULT 'KHO_001'"),
    ('ref_type', 'TEXT'),
    ('type1', 'TEXT'),
    ('unit1', 'TEXT'),
    ('unit_ratio', 'REAL'),
    ('note', 'TEXT'),
    ('ref_document', 'TEXT'),
    ('cost_price', 'REAL DEFAULT 0'),
    ('avg_cost', 'REAL DEFAULT 0'),
)


def ensure_stock_moves_extended_schema(conn: sqlite3.Connection) -> None:
    """Bổ sung cột mở rộng trên stock_moves nếu thiếu (idempotent)."""
    cols = table_cols(conn, 'stock_moves')
    if not cols:
        return
    for col, typ in _SM_EXT_COLS:
        add_column_if_missing(conn, 'stock_moves', col, typ)


def resolve_posting_warehouse_code(
    conn: sqlite3.Connection | None = None,
    preferred: str | None = None,
) -> str | None:
    """Kho ghi sổ: preferred → kho user được phép đầu tiên → kho mặc định → KHO_001."""
    pref = (preferred or '').strip()
    if pref:
        return pref
    try:
        from Services.user_branch import get_current_user_warehouse_codes
        codes = get_current_user_warehouse_codes()
        if codes:
            return str(codes[0]).strip() or None
    except Exception:
        pass
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT code FROM warehouses WHERE is_active = 1 AND is_default = 1 LIMIT 1"
            ).fetchone()
            if row:
                return str(row[0] if not isinstance(row, sqlite3.Row) else row['code']).strip() or None
            row = conn.execute(
                "SELECT code FROM warehouses WHERE is_active = 1 ORDER BY code LIMIT 1"
            ).fetchone()
            if row:
                return str(row[0] if not isinstance(row, sqlite3.Row) else row['code']).strip() or None
        except Exception:
            pass
    return 'KHO_001'


def insert_stock_move(
    cursor_or_conn,
    fields: dict[str, Any],
    *,
    ensure_schema: bool = True,
) -> int | None:
    """
    INSERT stock_moves chỉ với cột thực sự tồn tại.
    Trả lastrowid hoặc None nếu không ghi được.
    """
    conn = getattr(cursor_or_conn, 'connection', None) or cursor_or_conn
    cur = cursor_or_conn if hasattr(cursor_or_conn, 'execute') else conn.cursor()

    if ensure_schema:
        try:
            ensure_stock_moves_extended_schema(conn)
        except Exception as exc:
            logger.debug('ensure_stock_moves_extended_schema: %s', exc)

    sm_cols = table_cols(conn, 'stock_moves')
    if not sm_cols:
        return None

    data = {k: v for k, v in fields.items() if k in sm_cols and v is not None}
    # total_value thường NOT NULL trên schema cũ
    if 'total_value' in sm_cols and 'total_value' not in data:
        qty = float(data.get('quantity') or 0)
        cost = float(data.get('cost_price') or data.get('avg_cost') or 0)
        data['total_value'] = abs(qty) * cost

    if not data:
        return None

    cols = list(data.keys())
    vals = [data[c] for c in cols]
    placeholders = ', '.join(['?'] * len(vals))
    cur.execute(
        f"INSERT INTO stock_moves ({', '.join(cols)}) VALUES ({placeholders})",
        vals,
    )
    return getattr(cur, 'lastrowid', None)
