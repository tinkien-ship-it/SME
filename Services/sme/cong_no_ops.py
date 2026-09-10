# -*- coding: utf-8 -*-
"""Cập nhật công nợ phải thu HKD/open-item (cong_no).

Nguồn sự thật open-item: remaining = unpaid_amount - paid_amount.
Tương thích SQLite và PostgreSQL/multi-tenant schema.
"""
from __future__ import annotations

import sqlite3

from db_utils import is_postgres, sqlite_commit


def _row_value(row, key: str, index: int = 0):
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    if hasattr(row, 'keys'):
        try:
            return row[key]
        except Exception:
            pass
    try:
        return row[index]
    except Exception:
        return None


def _cols(conn: sqlite3.Connection) -> set[str]:
    """Danh sách cột cong_no trên đúng backend/schema tenant."""
    try:
        if is_postgres():
            rows = conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'cong_no'
                ORDER BY ordinal_position
                """
            ).fetchall()
            return {
                str(_row_value(r, 'column_name', 0))
                for r in rows
                if _row_value(r, 'column_name', 0)
            }

        cols = {str(r[1]) for r in conn.execute('PRAGMA table_info(cong_no)').fetchall()}
        if 'remaining_amount' not in cols:
            try:
                conn.execute('SELECT remaining_amount FROM cong_no LIMIT 0')
                cols.add('remaining_amount')
            except Exception:
                pass
        return cols
    except Exception:
        return set()


def _remaining_is_generated(conn: sqlite3.Connection) -> bool:
    """True nếu remaining_amount là generated column."""
    try:
        if is_postgres():
            row = conn.execute(
                """
                SELECT is_generated
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'cong_no'
                  AND column_name = 'remaining_amount'
                LIMIT 1
                """
            ).fetchone()
            return str(_row_value(row, 'is_generated', 0) or '').upper() == 'ALWAYS'

        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='cong_no'"
        ).fetchone()
        ddl = str(_row_value(row, 'sql', 0) or '')
        if 'remaining_amount' not in ddl:
            return False
        after = ddl.split('remaining_amount', 1)[1][:160].upper()
        compact = after.replace(' ', '')
        return ('GENERATED' in after and ' AS ' in after) or 'AS(' in compact
    except Exception:
        return False


def ensure_cong_no_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    """Đảm bảo schema open-item cong_no cho HKD trên SQLite/PostgreSQL."""
    if is_postgres():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cong_no (
                debt_id BIGSERIAL PRIMARY KEY,
                customer_name TEXT,
                company_name TEXT,
                address TEXT,
                tax_code TEXT,
                debit_account TEXT,
                credit_account TEXT,
                date_of_debt TEXT,
                unpaid_amount NUMERIC DEFAULT 0,
                paid_amount NUMERIC DEFAULT 0,
                remaining_amount NUMERIC DEFAULT 0,
                sale_id BIGINT,
                sale_no TEXT
            )
            """
        )
        conn.execute("ALTER TABLE cong_no ADD COLUMN IF NOT EXISTS company_name TEXT")
        conn.execute("ALTER TABLE cong_no ADD COLUMN IF NOT EXISTS paid_amount NUMERIC DEFAULT 0")
        conn.execute("ALTER TABLE cong_no ADD COLUMN IF NOT EXISTS remaining_amount NUMERIC DEFAULT 0")
    else:
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
                remaining_amount REAL DEFAULT 0,
                sale_id INTEGER,
                sale_no TEXT
            )
            """
        )
        cols = _cols(conn)
        if 'paid_amount' not in cols:
            try:
                conn.execute('ALTER TABLE cong_no ADD COLUMN paid_amount REAL DEFAULT 0')
            except Exception:
                pass
        if 'company_name' not in cols:
            try:
                conn.execute('ALTER TABLE cong_no ADD COLUMN company_name TEXT')
            except Exception:
                pass
        if 'remaining_amount' not in cols and not _remaining_is_generated(conn):
            try:
                conn.execute('ALTER TABLE cong_no ADD COLUMN remaining_amount REAL DEFAULT 0')
            except Exception:
                pass

    if commit:
        sqlite_commit(conn, label='cong_no_ops')


def remaining_sql(alias: str = 'cn', conn: sqlite3.Connection | None = None) -> str:
    """Biểu thức số còn lại; không phụ thuộc remaining_amount vật lý."""
    a = alias
    return f'(COALESCE({a}.unpaid_amount, 0) - COALESCE({a}.paid_amount, 0))'


def reverse_ar_receipt(conn: sqlite3.Connection, sale_id: int, amount: float) -> None:
    """Hoàn tác thu nợ: giảm paid_amount."""
    if not sale_id or amount is None:
        return
    amt = abs(float(amount))
    if amt <= 0:
        return
    ensure_cong_no_schema(conn, commit=False)
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
    sync_remaining_from_unpaid(conn, sale_id=int(sale_id))


def apply_ar_receipt(conn: sqlite3.Connection, sale_id: int, amount: float) -> None:
    """Thu tiền giảm nợ: tăng paid_amount, không giảm unpaid_amount."""
    if not sale_id or amount is None:
        return
    amt = abs(float(amount))
    if amt <= 0:
        return
    ensure_cong_no_schema(conn, commit=False)
    conn.execute(
        """
        UPDATE cong_no
        SET paid_amount = COALESCE(paid_amount, 0) + ?
        WHERE sale_id = ?
        """,
        (amt, int(sale_id)),
    )
    sync_remaining_from_unpaid(conn, sale_id=int(sale_id))


def apply_ar_credit_note(
    conn: sqlite3.Connection,
    *,
    sale_id: int | None = None,
    sale_no: str | None = None,
    amount: float = 0,
) -> None:
    """Trả hàng bán: giảm gốc nợ unpaid_amount, không tăng paid_amount."""
    amt = abs(float(amount or 0))
    if amt <= 0:
        return

    ensure_cong_no_schema(conn, commit=False)
    sets = [
        """unpaid_amount = CASE
            WHEN COALESCE(unpaid_amount, 0) - ? < 0 THEN 0
            ELSE COALESCE(unpaid_amount, 0) - ?
        END"""
    ]
    params: list = [amt, amt]

    cols = _cols(conn)
    if 'remaining_amount' in cols and not _remaining_is_generated(conn):
        sets.append(
            """remaining_amount = CASE
                WHEN COALESCE(unpaid_amount, 0) - COALESCE(paid_amount, 0) - ? < 0 THEN 0
                ELSE COALESCE(unpaid_amount, 0) - COALESCE(paid_amount, 0) - ?
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
        sync_remaining_from_unpaid(conn)


def sync_remaining_from_unpaid(conn: sqlite3.Connection, sale_id: int | None = None) -> int:
    """Backfill remaining_amount = max(unpaid_amount - paid_amount, 0)."""
    ensure_cong_no_schema(conn, commit=False)
    if _remaining_is_generated(conn):
        return 0
    cols = _cols(conn)
    if 'remaining_amount' not in cols:
        return 0

    sql = """
        UPDATE cong_no
        SET remaining_amount = CASE
            WHEN COALESCE(unpaid_amount, 0) - COALESCE(paid_amount, 0) < 0 THEN 0
            ELSE COALESCE(unpaid_amount, 0) - COALESCE(paid_amount, 0)
        END
    """
    cur = conn.execute(
        sql + (' WHERE sale_id = ?' if sale_id else ''),
        (int(sale_id),) if sale_id else (),
    )
    try:
        return max(int(cur.rowcount or 0), 0)
    except Exception:
        return 0
