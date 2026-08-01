"""Helpers gắn/lọc branch_code trên chứng từ SME."""
from __future__ import annotations

import sqlite3
from typing import Any

from Services.sme.branches import DEFAULT_BRANCH_CODE, resolve_posting_branch


def ensure_branch_column(conn: sqlite3.Connection, table: str) -> None:
    try:
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    except sqlite3.Error:
        return
    if 'branch_code' not in cols:
        try:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN branch_code TEXT')
        except sqlite3.OperationalError:
            pass


def branch_where(
    branch_code: str | None,
    *,
    column: str = 'branch_code',
) -> tuple[str, list[Any]]:
    code = (branch_code or '').strip().upper()
    if not code or code == 'ALL':
        return '', []
    if code == DEFAULT_BRANCH_CODE:
        return f" AND ({column} IS NULL OR {column} = '' OR {column} = ?)", [DEFAULT_BRANCH_CODE]
    return f' AND {column} = ?', [code]


def stamp_row_branch(
    conn: sqlite3.Connection,
    table: str,
    row_id: int,
    branch_code: str | None = None,
) -> str:
    ensure_branch_column(conn, table)
    br = resolve_posting_branch(conn, branch_code)
    try:
        conn.execute(
            f'UPDATE {table} SET branch_code = ? WHERE id = ?',
            (br, row_id),
        )
    except sqlite3.Error:
        pass
    return br


def assert_row_in_branch(
    conn: sqlite3.Connection,
    table: str,
    row_id: int,
    *,
    branch_code: str | None = None,
    label: str = 'Chứng từ',
    column: str = 'branch_code',
) -> None:
    """Chặn thao tác (void/in) trên dòng không thuộc CN đang chọn."""
    from Services.sme.branches import request_branch_filter

    code = branch_code
    if code is None:
        try:
            code = request_branch_filter()
        except Exception:
            return
    ensure_branch_column(conn, table)
    bf, bp = branch_where(code, column=column)
    if not bf:
        return
    try:
        ok = conn.execute(
            f'SELECT 1 FROM "{table}" WHERE id = ? {bf}',
            [int(row_id), *bp],
        ).fetchone()
    except sqlite3.Error:
        return
    if ok:
        return
    exists = conn.execute(
        f'SELECT 1 FROM "{table}" WHERE id = ?', (int(row_id),)
    ).fetchone()
    if exists:
        raise ValueError(f'{label} không thuộc chi nhánh đang chọn')


def assert_stock_transfer_in_branch(
    conn: sqlite3.Connection,
    transfer_id: int,
    *,
    branch_code: str | None = None,
) -> None:
    """Chuyển kho dùng from_branch_code / to_branch_code (không có branch_code đơn)."""
    from Services.sme.branches import request_branch_filter

    code = branch_code
    if code is None:
        try:
            code = request_branch_filter()
        except Exception:
            return
    code = (code or '').strip().upper()
    if not code or code == 'ALL':
        return
    if code == DEFAULT_BRANCH_CODE:
        sql = """
            SELECT 1 FROM sme_stock_transfers WHERE id = ?
              AND (
                from_branch_code IS NULL OR from_branch_code = '' OR from_branch_code = ?
                OR to_branch_code IS NULL OR to_branch_code = '' OR to_branch_code = ?
              )
        """
        params: list[Any] = [int(transfer_id), DEFAULT_BRANCH_CODE, DEFAULT_BRANCH_CODE]
    else:
        sql = """
            SELECT 1 FROM sme_stock_transfers WHERE id = ?
              AND (from_branch_code = ? OR to_branch_code = ?)
        """
        params = [int(transfer_id), code, code]
    try:
        ok = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return
    if ok:
        return
    exists = conn.execute(
        'SELECT 1 FROM sme_stock_transfers WHERE id = ?', (int(transfer_id),)
    ).fetchone()
    if exists:
        raise ValueError('Phiếu chuyển kho không thuộc chi nhánh đang chọn')


def warehouse_branch_or_session(
    conn: sqlite3.Connection,
    warehouse_code: str | None = None,
    branch_code: str | None = None,
) -> str:
    if branch_code:
        return resolve_posting_branch(conn, branch_code)
    if warehouse_code:
        from Services.sme.branches import get_warehouse_branch_code
        return resolve_posting_branch(
            conn, get_warehouse_branch_code(conn, warehouse_code),
        )
    return resolve_posting_branch(conn, None)


def assert_warehouse_in_session_branch(
    conn: sqlite3.Connection,
    warehouse_code: str,
    *,
    allow_all: bool = True,
) -> None:
    """Chặn nhập vào kho không thuộc CN đang làm việc (trừ filter ALL)."""
    from Services.sme.branches import get_warehouse_branch_code, request_branch_filter

    wh = (warehouse_code or '').strip()
    if not wh:
        raise ValueError('Thiếu mã kho')
    filt = request_branch_filter()
    if allow_all and filt in ('', 'ALL'):
        return
    wh_br = get_warehouse_branch_code(conn, wh)
    if filt == DEFAULT_BRANCH_CODE:
        if wh_br not in (DEFAULT_BRANCH_CODE, '', None):
            # HQ filter allows HQ warehouses only
            if wh_br != DEFAULT_BRANCH_CODE:
                raise ValueError(
                    f'Kho {wh} thuộc chi nhánh {wh_br}, không khớp CN đang chọn ({filt})'
                )
        return
    if wh_br != filt:
        raise ValueError(
            f'Kho {wh} thuộc chi nhánh {wh_br}, không khớp CN đang chọn ({filt})'
        )
