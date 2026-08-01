"""Danh mục CCDC SME — kích hoạt / thanh lý từ bảng tools_supplies."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from Services.fixed_assets_helpers import (
    STATUS_ACTIVE,
    STATUS_DISPOSED,
    STATUS_IN_STOCK,
    TOOLS_TABLE,
    ensure_fixed_assets_schema,
)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _table_ok(conn: sqlite3.Connection) -> bool:
    ensure_fixed_assets_schema(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (TOOLS_TABLE,),
    ).fetchone()
    return bool(row)


def list_tools(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    branch_code: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    if not _table_ok(conn):
        return []
    from Services.sme.branches import DEFAULT_BRANCH_CODE

    sql = f'SELECT * FROM {TOOLS_TABLE} WHERE 1=1'
    params: list[Any] = []
    if status:
        sql += ' AND tinh_trang = ?'
        params.append(status)
    code = (branch_code or '').strip().upper()
    if code and code != 'ALL':
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info({TOOLS_TABLE})').fetchall()}
        if 'branch_code' in cols:
            if code == DEFAULT_BRANCH_CODE:
                sql += " AND (branch_code IS NULL OR branch_code = '' OR branch_code = ?)"
            else:
                sql += ' AND branch_code = ?'
            params.append(code)
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def activate_tool(
    conn: sqlite3.Connection,
    tool_id: int,
    *,
    start_date: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    if not _table_ok(conn):
        raise ValueError('Chưa có bảng CCDC')
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, TOOLS_TABLE, tool_id, label='CCDC')
    row = conn.execute(f'SELECT * FROM {TOOLS_TABLE} WHERE id = ?', (tool_id,)).fetchone()
    if not row:
        raise ValueError('Không tìm thấy CCDC')
    d = dict(row)
    if d.get('tinh_trang') == STATUS_DISPOSED:
        raise ValueError('CCDC đã thanh lý')
    if d.get('tinh_trang') == STATUS_ACTIVE:
        return d
    date_s = str(start_date or datetime.now().strftime('%Y-%m-%d'))[:10]
    conn.execute(
        f"""
        UPDATE {TOOLS_TABLE}
        SET tinh_trang = ?, ngay_bat_dau_su_dung = COALESCE(ngay_bat_dau_su_dung, ?)
        WHERE id = ?
        """,
        (STATUS_ACTIVE, date_s, tool_id),
    )
    if commit:
        conn.commit()
    return dict(conn.execute(f'SELECT * FROM {TOOLS_TABLE} WHERE id = ?', (tool_id,)).fetchone())


def scrap_tool(
    conn: sqlite3.Connection,
    tool_id: int,
    *,
    reason: str = 'Thanh lý CCDC',
    commit: bool = False,
) -> dict[str, Any]:
    if not _table_ok(conn):
        raise ValueError('Chưa có bảng CCDC')
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, TOOLS_TABLE, tool_id, label='CCDC')
    row = conn.execute(f'SELECT * FROM {TOOLS_TABLE} WHERE id = ?', (tool_id,)).fetchone()
    if not row:
        raise ValueError('Không tìm thấy CCDC')
    d = dict(row)
    if d.get('tinh_trang') == STATUS_DISPOSED:
        raise ValueError('CCDC đã thanh lý')
    note = ((d.get('ghi_chu') or d.get('note') or '') + f' | {reason} | {_now()}').strip(' |')
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info({TOOLS_TABLE})').fetchall()}
    if 'ghi_chu' in cols:
        conn.execute(
            f"UPDATE {TOOLS_TABLE} SET tinh_trang = ?, ghi_chu = ? WHERE id = ?",
            (STATUS_DISPOSED, note, tool_id),
        )
    elif 'note' in cols:
        conn.execute(
            f"UPDATE {TOOLS_TABLE} SET tinh_trang = ?, note = ? WHERE id = ?",
            (STATUS_DISPOSED, note, tool_id),
        )
    else:
        conn.execute(
            f"UPDATE {TOOLS_TABLE} SET tinh_trang = ? WHERE id = ?",
            (STATUS_DISPOSED, tool_id),
        )
    if commit:
        conn.commit()
    return dict(conn.execute(f'SELECT * FROM {TOOLS_TABLE} WHERE id = ?', (tool_id,)).fetchone())
