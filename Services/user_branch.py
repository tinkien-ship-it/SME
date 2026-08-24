"""
Phân quyền User ↔ Chi nhánh.

Bảng `user_branches`: gán user vào 1+ chi nhánh.
- Admin/master: bypass — thấy tất cả chi nhánh.
- Staff/manager: chỉ thấy/bán hàng tại chi nhánh được gán.
- Kho tương ứng: user chỉ truy cập inventory thuộc warehouse có branch_code trong danh sách.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from flask import g, session
from db_utils import sqlite_commit


def ensure_user_branch_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            branch_code TEXT NOT NULL,
            is_default INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, branch_code)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_branches_user ON user_branches(user_id)"
    )
    if commit:
        sqlite_commit(conn, label='user_branch')


def assign_user_branch(
    conn: sqlite3.Connection,
    user_id: int,
    branch_code: str,
    *,
    is_default: bool = False,
    commit: bool = True,
) -> bool:
    ensure_user_branch_schema(conn, commit=False)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO user_branches (user_id, branch_code, is_default) VALUES (?, ?, ?)",
            (user_id, branch_code.strip(), 1 if is_default else 0),
        )
        if is_default:
            conn.execute(
                "UPDATE user_branches SET is_default = 0 WHERE user_id = ? AND branch_code != ?",
                (user_id, branch_code.strip()),
            )
            conn.execute(
                "UPDATE user_branches SET is_default = 1 WHERE user_id = ? AND branch_code = ?",
                (user_id, branch_code.strip()),
            )
        if commit:
            sqlite_commit(conn, label='user_branch')
        return True
    except sqlite3.Error:
        return False


def remove_user_branch(
    conn: sqlite3.Connection,
    user_id: int,
    branch_code: str,
    *,
    commit: bool = True,
) -> bool:
    ensure_user_branch_schema(conn, commit=False)
    conn.execute(
        "DELETE FROM user_branches WHERE user_id = ? AND branch_code = ?",
        (user_id, branch_code.strip()),
    )
    if commit:
        sqlite_commit(conn, label='user_branch')
    return True


def set_user_branches(
    conn: sqlite3.Connection,
    user_id: int,
    branch_codes: list[str],
    *,
    default_code: str | None = None,
    commit: bool = True,
) -> None:
    """Thay thế toàn bộ gán chi nhánh của user."""
    ensure_user_branch_schema(conn, commit=False)
    conn.execute("DELETE FROM user_branches WHERE user_id = ?", (user_id,))
    for code in branch_codes:
        code = code.strip()
        if not code:
            continue
        is_def = 1 if (default_code and code == default_code.strip()) else 0
        conn.execute(
            "INSERT OR IGNORE INTO user_branches (user_id, branch_code, is_default) VALUES (?, ?, ?)",
            (user_id, code, is_def),
        )
    if commit:
        sqlite_commit(conn, label='user_branch')


def get_user_branches(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    ensure_user_branch_schema(conn, commit=False)
    rows = conn.execute(
        "SELECT branch_code, is_default FROM user_branches WHERE user_id = ? ORDER BY is_default DESC, branch_code",
        (user_id,),
    ).fetchall()
    return [{'branch_code': r[0], 'is_default': bool(r[1])} for r in rows]


def get_user_default_branch(conn: sqlite3.Connection, user_id: int) -> str | None:
    ensure_user_branch_schema(conn, commit=False)
    row = conn.execute(
        "SELECT branch_code FROM user_branches WHERE user_id = ? AND is_default = 1 LIMIT 1",
        (user_id,),
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT branch_code FROM user_branches WHERE user_id = ? ORDER BY id LIMIT 1",
        (user_id,),
    ).fetchone()
    return row[0] if row else None


def user_allowed_branch_codes(conn: sqlite3.Connection, user_id: int) -> list[str] | None:
    """
    Trả về danh sách branch_code user được phép.
    None = không giới hạn (admin/master hoặc chưa gán).
    """
    from Services.sme_roles import PERMISSION_BYPASS_ROLES

    role = session.get('role', '')
    if role in PERMISSION_BYPASS_ROLES:
        return None

    ensure_user_branch_schema(conn, commit=False)
    rows = conn.execute(
        "SELECT branch_code FROM user_branches WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    if not rows:
        return None
    return [r[0] for r in rows]


def user_allowed_warehouse_codes(conn: sqlite3.Connection, user_id: int) -> list[str] | None:
    """
    Trả về danh sách warehouse code user được phép (dựa trên chi nhánh).
    None = không giới hạn.
    """
    branches = user_allowed_branch_codes(conn, user_id)
    if branches is None:
        return None

    placeholders = ','.join('?' * len(branches))
    rows = conn.execute(
        f"SELECT code FROM warehouses WHERE branch_code IN ({placeholders}) AND is_active = 1",
        branches,
    ).fetchall()
    return [r[0] for r in rows]


def get_current_user_branch_codes() -> list[str] | None:
    """Shortcut — lấy từ g nếu đã cache."""
    return getattr(g, '_user_branch_codes', None)


def get_current_user_warehouse_codes() -> list[str] | None:
    """Shortcut — lấy từ g nếu đã cache."""
    return getattr(g, '_user_warehouse_codes', None)


def cache_user_branch_context(conn: sqlite3.Connection) -> None:
    """Gọi 1 lần/request để cache vào g."""
    user_id = session.get('user_id')
    if not user_id:
        return
    g._user_branch_codes = user_allowed_branch_codes(conn, user_id)
    g._user_warehouse_codes = user_allowed_warehouse_codes(conn, user_id)


def stock_branch_filter_sql(alias: str = 'i') -> tuple[str, list]:
    """
    SQL filter cho inventory/stock_moves theo warehouse thuộc chi nhánh user.
    Returns (sql_fragment, params). Nếu không giới hạn → ('', []).
    """
    wh_codes = get_current_user_warehouse_codes()
    if wh_codes is None:
        return '', []
    if not wh_codes:
        return ' AND 1=0', []
    placeholders = ','.join('?' * len(wh_codes))
    return f' AND {alias}.warehouse_code IN ({placeholders})', list(wh_codes)
