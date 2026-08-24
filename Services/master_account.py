"""Tạo / bảo trì tài khoản master trên main database.db."""
from __future__ import annotations

import os
import sqlite3

USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    role TEXT DEFAULT 'user',
    full_name TEXT,
    permissions TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    email TEXT,
    phone TEXT,
    reset_token TEXT,
    reset_token_expiry DATETIME,
    last_session_id TEXT,
    is_2fa_enabled INTEGER DEFAULT 0,
    google_login_allowed INTEGER DEFAULT 1,
    must_change_password INTEGER DEFAULT 0,
    is_support_account INTEGER DEFAULT 0,
    totp_secret TEXT,
    totp_confirmed_at TEXT
)
"""

EXTRA_COLUMNS = {
    'email': 'TEXT',
    'phone': 'TEXT',
    'reset_token': 'TEXT',
    'reset_token_expiry': 'DATETIME',
    'last_session_id': 'TEXT',
    'is_2fa_enabled': 'INTEGER DEFAULT 0',
    'google_login_allowed': 'INTEGER DEFAULT 1',
    'must_change_password': 'INTEGER DEFAULT 0',
    'is_support_account': 'INTEGER DEFAULT 0',
    'permissions': "TEXT DEFAULT ''",
    'full_name': 'TEXT',
    'created_at': "TEXT DEFAULT (datetime('now'))",
    'totp_secret': 'TEXT',
    'totp_confirmed_at': 'TEXT',
}


def hash_password(password: str) -> str:
    try:
        from flask_bcrypt import generate_password_hash
        hashed = generate_password_hash(password)
        return hashed.decode('utf-8') if isinstance(hashed, bytes) else str(hashed)
    except Exception:
        import bcrypt
        return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def ensure_users_table(conn: sqlite3.Connection) -> list[str]:
    from db.dialect import column_names, table_exists
    from db.schema_helpers import add_column_if_missing, execute_ddl

    changed = []
    existed = table_exists(conn, 'users')
    execute_ddl(conn, USERS_DDL)
    if not existed:
        changed.append('create:users')
    cols = {c.lower() for c in column_names(conn, 'users')}
    for name, decl in EXTRA_COLUMNS.items():
        if name.lower() in cols:
            continue
        if add_column_if_missing(conn, 'users', name, decl):
            changed.append('alter:users.%s' % name)
            cols.add(name.lower())
    return changed


def list_masters(conn: sqlite3.Connection):
    try:
        rows = conn.execute(
            "SELECT id, username, role, email, full_name, "
            "COALESCE(is_2fa_enabled, 0) AS is_2fa_enabled "
            "FROM users WHERE role = 'master' ORDER BY id"
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [dict(r) for r in rows]


def count_masters(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='master'"
        ).fetchone()[0])
    except sqlite3.DatabaseError:
        return 0


def ensure_master(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: str,
    email: str = '',
    full_name: str = 'Master',
    disable_2fa: bool = True,
    force_password: bool = True,
) -> str:
    """Trả 'created' | 'updated' | 'password_reset'."""
    ensure_users_table(conn)
    row = conn.execute(
        "SELECT id, role FROM users WHERE username = ?", (username,)
    ).fetchone()
    pwd_hash = hash_password(password)
    tfa = 0 if disable_2fa else 1
    if row is None:
        conn.execute(
            """
            INSERT INTO users (
                username, password, role, full_name, email, permissions,
                is_2fa_enabled, google_login_allowed, must_change_password
            ) VALUES (?, ?, 'master', ?, ?, '', ?, 1, 0)
            """,
            (username, pwd_hash, full_name, email or None, tfa),
        )
        return 'created'

    if force_password:
        conn.execute(
            """
            UPDATE users SET password = ?, role = 'master',
                full_name = COALESCE(NULLIF(?, ''), full_name),
                email = CASE WHEN ? != '' THEN ? ELSE email END,
                is_2fa_enabled = ?, must_change_password = 0, last_session_id = NULL
            WHERE username = ?
            """,
            (pwd_hash, full_name, email, email or None, tfa, username),
        )
        return 'password_reset'

    conn.execute(
        """
        UPDATE users SET role = 'master',
            full_name = COALESCE(NULLIF(?, ''), full_name),
            email = CASE WHEN ? != '' THEN ? ELSE email END,
            is_2fa_enabled = CASE WHEN ? THEN 0 ELSE is_2fa_enabled END
        WHERE username = ?
        """,
        (full_name, email, email or None, 1 if disable_2fa else 0, username),
    )
    return 'updated'


def ensure_master_from_env(conn: sqlite3.Connection | None = None) -> str | None:
    """Nếu thiếu master và có MASTER_PASSWORD trong env → tạo. Trả action hoặc None."""
    from db_utils import get_main_db_connection, sqlite_commit

    own = conn is None
    conn = conn or get_main_db_connection()
    try:
        ensure_users_table(conn)
        if count_masters(conn) > 0:
            return None
        password = (os.environ.get('MASTER_PASSWORD') or '').strip()
        if not password:
            return None
        action = ensure_master(
            conn,
            username=(os.environ.get('MASTER_USERNAME') or 'master').strip(),
            password=password,
            email=(os.environ.get('MASTER_EMAIL') or '').strip(),
            full_name=(os.environ.get('MASTER_FULL_NAME') or 'Master').strip(),
            disable_2fa=True,
            force_password=True,
        )
        sqlite_commit(conn, label='master_account')
        return action
    finally:
        if own:
            conn.close()
