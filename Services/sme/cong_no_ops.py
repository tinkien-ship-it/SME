# -*- coding: utf-8 -*-
"""Cập nhật công nợ phải thu (cong_no) — giữ unpaid / paid / remaining đồng bộ."""
from __future__ import annotations

import sqlite3
from db_utils import sqlite_commit


def _cols(conn: sqlite3.Connection) -> set[str]:
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(cong_no)').fetchall()}
    except sqlite3.Error:
        cols = set()
    # Cột GENERATED có thể không hiện đủ trên một số bản SQLite
    if 'remaining_amount' not in cols:
        try:
            conn.execute('SELECT remaining_amount FROM cong_no LIMIT 0')
            cols.add('remaining_amount')
        except sqlite3.Error:
            pass
    return cols


def _remaining_is_generated(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='cong_no'"
        ).fetchone()
        ddl = (row[0] or '') if row else ''
        if 'remaining_amount' not in ddl:
            return False
        after = ddl.split('remaining_amount', 1)[1][:120].upper()
        return ' AS ' in after and 'GENERATED' not in after[:20] or 'AS (' in after.replace(' ', '')
    except sqlite3.Error:
        return False


def ensure_cong_no_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    """Bổ sung paid_amount / remaining_amount nếu thiếu (không đụng cột GENERATED)."""
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cong_no (
                debt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT,
                company_name TEXT,
                address TEXT,
                tax_code TEXT,
                debit_account TEXT,
                credit_account TEXT,
                date_of_debt TEXT,
                unpaid_amount REAL DEFAULT 0,
                paid_amount REAL DEFAULT 0,
                sale_id INTEGER,
                sale_no TEXT
            )
            """
        )
    except sqlite3.Error:
        pass

    cols = _cols(conn)
    if 'paid_amount' not in cols:
        try:
            conn.execute('ALTER TABLE cong_no ADD COLUMN paid_amount REAL DEFAULT 0')
        except sqlite3.OperationalError:
            pass
    if 'company_name' not in cols:
        try:
            conn.execute('ALTER TABLE cong_no ADD COLUMN company_name TEXT')
        except sqlite3.OperationalError:
            pass

    # Chỉ thêm remaining thường nếu chưa có (kể cả GENERATED)
    has_rem = False
    try:
        conn.execute('SELECT remaining_amount FROM cong_no LIMIT 0')
        has_rem = True
    except sqlite3.Error:
        has_rem = False
    if not has_rem and not _remaining_is_generated(conn):
        try:
            conn.execute('ALTER TABLE cong_no ADD COLUMN remaining_amount REAL DEFAULT 0')
        except sqlite3.OperationalError:
            pass

    if commit:
        sqlite_commit(conn, label='cong_no_ops')


def remaining_sql(alias: str = 'cn', conn: sqlite3.Connection | None = None) -> str:
    """Biểu thức số còn lại."""
    a = alias
    # unpaid − paid là nguồn sự thật; remaining (thường/GENERATED) chỉ là tiện ích
    return f'(COALESCE({a}.unpaid_amount, 0) - COALESCE({a}.paid_amount, 0))'


def reverse_ar_receipt(conn: sqlite3.Connection, sale_id: int, amount: float) -> None:
    """Hoàn tác khi hủy phiếu thu — chỉ giảm paid_amount (khớp apply_ar_receipt)."""
    if not sale_id or amount is None:
        return
    amt = abs(float(amount))
    if amt <= 0:
        return
    ensure_cong_no_schema(conn, commit=False)
    cols = _cols(conn)
    if not cols or 'unpaid_amount' not in cols:
        return

    if 'paid_amount' in cols:
        conn.execute(
            """
            UPDATE cong_no
            SET paid_amount = CASE
                WHEN COALESCE(paid_amount, 0) - ? < 0 THEN 0
                ELSE COALESCE(paid_amount, 0) - ?
            END
            WHERE sale_id = ?
            """,
            (amt, amt, int(sale_id)),
        )
    else:
        conn.execute(
            """
            UPDATE cong_no
            SET unpaid_amount = COALESCE(unpaid_amount, 0) + ?
            WHERE sale_id = ?
            """,
            (amt, int(sale_id)),
        )
    sync_remaining_from_unpaid(conn, sale_id=int(sale_id))


def apply_ar_receipt(conn: sqlite3.Connection, sale_id: int, amount: float) -> None:
    """Thu tiền giảm nợ (phiếu thu / settle).

    Nguồn sự thật: remaining = unpaid_amount − paid_amount.
    Chỉ tăng paid_amount (không giảm unpaid) — tránh trừ nợ 2 lần.
    """
    if not sale_id or amount is None:
        return
    amt = abs(float(amount))
    if amt <= 0:
        return
    ensure_cong_no_schema(conn, commit=False)
    cols = _cols(conn)
    if not cols or 'unpaid_amount' not in cols:
        return

    if 'paid_amount' in cols:
        conn.execute(
            """
            UPDATE cong_no
            SET paid_amount = COALESCE(paid_amount, 0) + ?
            WHERE sale_id = ?
            """,
            (amt, int(sale_id)),
        )
    else:
        conn.execute(
            """
            UPDATE cong_no
            SET unpaid_amount = CASE
                WHEN COALESCE(unpaid_amount, 0) - ? < 0 THEN 0
                ELSE COALESCE(unpaid_amount, 0) - ?
            END
            WHERE sale_id = ?
            """,
            (amt, amt, int(sale_id)),
        )
    sync_remaining_from_unpaid(conn, sale_id=int(sale_id))


def apply_ar_credit_note(
    conn: sqlite3.Connection,
    *,
    sale_id: int | None = None,
    sale_no: str | None = None,
    amount: float = 0,
) -> None:
    """Trả hàng bán: giảm gốc nợ (unpaid), không tăng paid."""
    amt = abs(float(amount or 0))
    if amt <= 0:
        return
    ensure_cong_no_schema(conn, commit=False)
    cols = _cols(conn)
    if not cols or 'unpaid_amount' not in cols:
        return

    sets = [
        """unpaid_amount = CASE
            WHEN COALESCE(unpaid_amount, 0) - ? < 0 THEN 0
            ELSE COALESCE(unpaid_amount, 0) - ?
        END"""
    ]
    params: list = [amt, amt]
    if 'remaining_amount' in cols and not _remaining_is_generated(conn):
        sets.append(
            """remaining_amount = CASE
                WHEN COALESCE(remaining_amount, unpaid_amount, 0) - ? < 0 THEN 0
                ELSE COALESCE(remaining_amount, unpaid_amount, 0) - ?
            END"""
        )
        params.extend([amt, amt])

    if sale_id:
        params.append(int(sale_id))
        conn.execute(
            f"UPDATE cong_no SET {', '.join(sets)} WHERE sale_id = ?",
            params,
        )
        sync_remaining_from_unpaid(conn, sale_id=int(sale_id))
    elif sale_no:
        params.append(str(sale_no))
        conn.execute(
            f"UPDATE cong_no SET {', '.join(sets)} WHERE sale_no = ?",
            params,
        )
        # sync theo sale_id nếu có
        row = conn.execute(
            'SELECT sale_id FROM cong_no WHERE sale_no = ? LIMIT 1', (str(sale_no),)
        ).fetchone()
        sid = None
        if row:
            sid = row['sale_id'] if hasattr(row, 'keys') else row[0]
        if sid:
            sync_remaining_from_unpaid(conn, sale_id=int(sid))
        else:
            sync_remaining_from_unpaid(conn)


def sync_remaining_from_unpaid(conn: sqlite3.Connection, sale_id: int | None = None) -> int:
    """Backfill remaining thường = unpaid − paid. Bỏ qua nếu GENERATED."""
    ensure_cong_no_schema(conn, commit=False)
    if _remaining_is_generated(conn):
        return 0
    cols = _cols(conn)
    if 'remaining_amount' not in cols:
        return 0
    paid = 'COALESCE(paid_amount, 0)' if 'paid_amount' in cols else '0'
    sql = f"""
        UPDATE cong_no
        SET remaining_amount = CASE
            WHEN COALESCE(unpaid_amount, 0) - {paid} < 0 THEN 0
            ELSE COALESCE(unpaid_amount, 0) - {paid}
        END
    """
    if sale_id:
        conn.execute(sql + ' WHERE sale_id = ?', (int(sale_id),))
    else:
        conn.execute(sql)
    return conn.total_changes
