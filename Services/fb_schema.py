"""Schema F&B (areas / tables / menu / menu_recipes / draft_inventory).

Đảm bảo mọi tenant DB — kể cả shop tạo trước khi có module F&B — có đủ bảng
khi deploy / migrate. Idempotent.
"""
from __future__ import annotations

import sqlite3
from db_utils import sqlite_commit, sqlite_is_ready, sqlite_mark_ready

_FB_SCHEMA_FLAG = 'fb_schema_v1'

_FB_TABLES = {
    'areas': """
        CREATE TABLE IF NOT EXISTS areas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        )
    """,
    'tables': """
        CREATE TABLE IF NOT EXISTS tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            area_id INTEGER,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'Available',
            current_sale_id INTEGER,
            FOREIGN KEY (area_id) REFERENCES areas(id)
        )
    """,
    'menu': """
        CREATE TABLE IF NOT EXISTS menu (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            category TEXT,
            unit TEXT NOT NULL,
            unit1 TEXT,
            base_price REAL DEFAULT 0,
            price REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            product_type TEXT DEFAULT 'processed',
            product_id INTEGER,
            image_path TEXT,
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """,
    'menu_recipes': """
        CREATE TABLE IF NOT EXISTS menu_recipes (
            menu_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (menu_id, product_id),
            FOREIGN KEY (menu_id) REFERENCES menu(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """,
    'draft_inventory': """
        CREATE TABLE IF NOT EXISTS draft_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            quantity REAL NOT NULL,
            note TEXT,
            is_processed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """,
}

_MENU_EXTRA_COLS = (
    ('category', 'TEXT'),
    ('unit1', 'TEXT'),
    ('base_price', 'REAL DEFAULT 0'),
    ('price', 'REAL DEFAULT 0'),
    ('is_active', 'INTEGER DEFAULT 1'),
    ('product_type', "TEXT DEFAULT 'processed'"),
    ('product_id', 'INTEGER'),
    ('image_path', 'TEXT'),
)

_TABLES_EXTRA_COLS = (
    ('area_id', 'INTEGER'),
    ('status', "TEXT DEFAULT 'Available'"),
    ('current_sale_id', 'INTEGER'),
)

# Cột đơn F&B trên sale / sale_items — tenant cũ thường thiếu → /api/fb/active-orders 500.
_SALE_EXTRA_COLS = (
    ('table_id', 'INTEGER'),
    ('sale_no', 'TEXT'),
    ('status', 'TEXT'),
    ('created_at', 'TEXT'),
    ('total_amount', 'REAL DEFAULT 0'),
    ('client_uuid', 'TEXT'),
)

_SALE_ITEMS_EXTRA_COLS = (
    ('menu_id', 'INTEGER'),
    ('UseSaleUnit', 'INTEGER DEFAULT 0'),
    ('product_name', 'TEXT'),
    ('item_name', 'TEXT'),
    ('unit', 'TEXT'),
    ('line_total', 'REAL'),
    ('created_at', 'TEXT'),
    ('quantity_served', 'REAL DEFAULT 0'),
    ('served_at', 'TEXT'),
)


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        from db.dialect import column_names
        return column_names(conn, table)
    except Exception:
        try:
            return {r[1] for r in conn.execute('PRAGMA table_info("%s")' % table)}
        except Exception:
            return set()


def _has_col(have: set[str], name: str) -> bool:
    want = (name or '').lower()
    return any((c or '').lower() == want for c in have)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        from db.dialect import table_exists
        return table_exists(conn, name)
    except Exception:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None


def _ensure_extra_cols(conn: sqlite3.Connection, table: str, extras, changed: list[str]) -> None:
    if not _table_exists(conn, table):
        return
    have = _cols(conn, table)
    c = conn.cursor()
    for col, decl in extras:
        if _has_col(have, col):
            continue
        try:
            c.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, decl))
            have.add(col)
            changed.append('alter:%s.%s' % (table, col))
        except sqlite3.OperationalError as exc:
            print('[MIGRATE] %s.%s: %s' % (table, col, exc))


def ensure_fb_schema(conn: sqlite3.Connection, *, commit: bool = True) -> list[str]:
    """Tạo bảng/cột F&B còn thiếu. Trả list mô tả thay đổi."""
    # SQLite + Postgres: chỉ migrate 1 lần / worker / schema (tránh chậm khi poll active-orders)
    if sqlite_is_ready(conn, _FB_SCHEMA_FLAG):
        return []

    changed: list[str] = []
    c = conn.cursor()
    for name, ddl in _FB_TABLES.items():
        existed = _table_exists(conn, name)
        c.execute(ddl)
        if not existed:
            changed.append('create:%s' % name)

    _ensure_extra_cols(conn, 'tables', _TABLES_EXTRA_COLS, changed)
    _ensure_extra_cols(conn, 'menu', _MENU_EXTRA_COLS, changed)
    _ensure_extra_cols(conn, 'sale', _SALE_EXTRA_COLS, changed)
    _ensure_extra_cols(conn, 'sale_items', _SALE_ITEMS_EXTRA_COLS, changed)

    try:
        from Services.schema_compat import ensure_sale_items_canonical
        for item in ensure_sale_items_canonical(conn, commit=False):
            changed.append(item)
    except Exception as exc:
        print('[MIGRATE] sale_items canonical: %s' % exc)
        try:
            from db_utils import rollback_quietly
            rollback_quietly(conn)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

    if commit:
        sqlite_commit(conn, label='fb_schema')
    sqlite_mark_ready(conn, _FB_SCHEMA_FLAG)
    return changed
