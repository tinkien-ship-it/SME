"""Schema F&B (areas / tables / menu / menu_recipes / draft_inventory).

Đảm bảo mọi tenant DB — kể cả shop tạo trước khi có module F&B — có đủ bảng
khi deploy / migrate. Idempotent.
"""
from __future__ import annotations

import sqlite3

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


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute('PRAGMA table_info("%s")' % table)}
    except sqlite3.DatabaseError:
        return set()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def ensure_fb_schema(conn: sqlite3.Connection, *, commit: bool = True) -> list[str]:
    """Tạo bảng/cột F&B còn thiếu. Trả list mô tả thay đổi."""
    changed: list[str] = []
    c = conn.cursor()
    for name, ddl in _FB_TABLES.items():
        existed = _table_exists(conn, name)
        c.execute(ddl)
        if not existed:
            changed.append('create:%s' % name)

    if _table_exists(conn, 'tables'):
        have = _cols(conn, 'tables')
        for col, decl in _TABLES_EXTRA_COLS:
            if col not in have:
                try:
                    c.execute('ALTER TABLE tables ADD COLUMN %s %s' % (col, decl))
                    changed.append('alter:tables.%s' % col)
                except sqlite3.OperationalError as exc:
                    print('[MIGRATE] tables.%s: %s' % (col, exc))

    if _table_exists(conn, 'menu'):
        have = _cols(conn, 'menu')
        for col, decl in _MENU_EXTRA_COLS:
            if col not in have:
                try:
                    c.execute('ALTER TABLE menu ADD COLUMN %s %s' % (col, decl))
                    changed.append('alter:menu.%s' % col)
                except sqlite3.OperationalError as exc:
                    print('[MIGRATE] menu.%s: %s' % (col, exc))

    if commit:
        conn.commit()
    return changed
