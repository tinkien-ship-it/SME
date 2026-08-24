"""Schema lô hàng / FIFO — inventory_lots + inventory_lot_consumptions."""
from __future__ import annotations

import sqlite3

from db_utils import sqlite_commit, sqlite_is_ready, sqlite_mark_ready, with_sqlite_write

_SCHEMA_FLAG = 'inventory_lot_schema_v1'


def _apply_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            warehouse_code TEXT,
            source_type TEXT NOT NULL,
            source_id INTEGER,
            source_line_id INTEGER,
            lot_no TEXT,
            received_at TEXT NOT NULL,
            expiry_date TEXT,
            qty_in REAL NOT NULL,
            qty_remaining REAL NOT NULL,
            unit_cost REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory_lot_consumptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            direction TEXT NOT NULL DEFAULT 'out',
            qty REAL NOT NULL,
            unit_cost REAL NOT NULL,
            ref_type TEXT,
            ref_id INTEGER,
            stock_move_id INTEGER,
            return_sales_id INTEGER,
            reversed_consumption_id INTEGER,
            actor_user_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (lot_id) REFERENCES inventory_lots(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_lots_product_fifo
        ON inventory_lots (product_id, received_at, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_lots_open
        ON inventory_lots (product_id, qty_remaining)
        WHERE qty_remaining > 0
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lot_consumptions_ref
        ON inventory_lot_consumptions (ref_type, ref_id, product_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lot_consumptions_sale
        ON inventory_lot_consumptions (ref_type, ref_id, direction)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_lots_expiry
        ON inventory_lots (product_id, expiry_date, received_at)
        WHERE qty_remaining > 0
        """
    )


def ensure_inventory_lot_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    if sqlite_is_ready(conn, _SCHEMA_FLAG):
        return
    with_sqlite_write(conn, _apply_schema, commit=commit, label='inventory_lot_schema')
    sqlite_mark_ready(conn, _SCHEMA_FLAG)
