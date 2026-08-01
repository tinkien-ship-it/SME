"""Chứng từ thu/chi SME (mẫu 01-TT / 02-TT) — journal-first, tách HKD phieu_thu/chi."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry

MONEY_Q = Decimal('0.01')

VOUCHER_FORM_RECEIPT = '01-TT'
VOUCHER_FORM_PAYMENT = '02-TT'


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_voucher_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_vouchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_type TEXT NOT NULL,
            form_code TEXT NOT NULL,
            voucher_no TEXT NOT NULL,
            voucher_date TEXT NOT NULL,
            party_name TEXT,
            party_address TEXT,
            party_tax_code TEXT,
            amount REAL NOT NULL DEFAULT 0,
            debit_account TEXT NOT NULL,
            credit_account TEXT NOT NULL,
            reason TEXT,
            attached_docs INTEGER DEFAULT 0,
            reference_document TEXT,
            source_type TEXT,
            source_id INTEGER,
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'posted',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(voucher_type, voucher_no)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sme_vouchers_date
        ON sme_vouchers(voucher_type, voucher_date)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sme_vouchers_journal
        ON sme_vouchers(journal_entry_id)
        """
    )
    if commit:
        conn.commit()


def _next_voucher_no(conn: sqlite3.Connection, voucher_type: str) -> str:
    prefix = 'PT' if voucher_type == 'receipt' else 'PC'
    row = conn.execute(
        """
        SELECT voucher_no FROM sme_vouchers
        WHERE voucher_type = ? AND voucher_no LIKE ?
        ORDER BY id DESC LIMIT 1
        """,
        (voucher_type, f'{prefix}%'),
    ).fetchone()
    if not row:
        return f'{prefix}000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['voucher_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'{prefix}{int(digits) + 1:06d}'


def _cash_account(payment_method: str) -> str:
    method = (payment_method or 'cash').strip().lower()
    if method in ('112', 'bank', 'bank_transfer', 'ck', 'transfer'):
        return '1121'
    return '1111'


def create_receipt(
    conn: sqlite3.Connection,
    *,
    voucher_date: str,
    party_name: str,
    amount,
    payment_method: str = 'cash',
    credit_account: str = '131',
    reason: str = '',
    party_address: str = '',
    party_tax_code: str = '',
    reference_document: str = '',
    source_type: str | None = None,
    source_id: int | None = None,
    sale_id: int | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Lập phiếu thu 01-TT + bút toán Nợ 1111/1121 · Có credit_account."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_voucher_schema(conn, commit=False)

    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số tiền phiếu thu phải > 0')
    date_s = str(voucher_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày phiếu thu')

    debit = _cash_account(payment_method)
    credit = str(credit_account or '131').strip() or '131'
    vno = _next_voucher_no(conn, 'receipt')
    desc = reason or f'Thu tiền {party_name or ""}'.strip()

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='PT',
        document_no=vno,
        document_id=source_id or sale_id,
        business_type='THU_TIEN',
        description=desc,
        reference_document=reference_document or None,
        created_by=created_by,
        lines=[
            {
                'sequence': 1,
                'account_code': debit,
                'debit': float(amt),
                'credit': 0,
                'description': desc,
            },
            {
                'sequence': 2,
                'account_code': credit,
                'debit': 0,
                'credit': float(amt),
                'description': desc,
            },
        ],
    )

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_vouchers (
            voucher_type, form_code, voucher_no, voucher_date,
            party_name, party_address, party_tax_code, amount,
            debit_account, credit_account, reason, reference_document,
            source_type, source_id, journal_entry_id, status, created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'posted',?,?,?)
        """,
        (
            'receipt', VOUCHER_FORM_RECEIPT, vno, date_s,
            party_name, party_address, party_tax_code, float(amt),
            debit, credit, desc, reference_document or None,
            source_type or ('sale' if sale_id else None),
            source_id or sale_id,
            entry['id'], created_by, _now(), _now(),
        ),
    )
    voucher_id = cur.lastrowid

    # Cập nhật công nợ bán (bảng cong_no dùng chung vận hành) nếu thu theo đơn
    if sale_id and credit.startswith('131'):
        try:
            cur.execute(
                """
                UPDATE cong_no
                SET unpaid_amount = CASE
                    WHEN COALESCE(unpaid_amount, 0) - ? < 0 THEN 0
                    ELSE COALESCE(unpaid_amount, 0) - ?
                END
                WHERE sale_id = ?
                """,
                (float(amt), float(amt), sale_id),
            )
        except sqlite3.OperationalError:
            pass

    if commit:
        conn.commit()

    return {
        'id': voucher_id,
        'voucher_no': vno,
        'form_code': VOUCHER_FORM_RECEIPT,
        'journal_entry_id': entry['id'],
        'amount': float(amt),
        'debit_account': debit,
        'credit_account': credit,
    }


def create_payment(
    conn: sqlite3.Connection,
    *,
    voucher_date: str,
    party_name: str,
    amount,
    payment_method: str = 'cash',
    debit_account: str = '331',
    reason: str = '',
    party_address: str = '',
    party_tax_code: str = '',
    reference_document: str = '',
    source_type: str | None = None,
    source_id: int | None = None,
    import_id: int | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Lập phiếu chi 02-TT + bút toán Nợ debit_account · Có 1111/1121."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_voucher_schema(conn, commit=False)

    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số tiền phiếu chi phải > 0')
    date_s = str(voucher_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày phiếu chi')

    credit = _cash_account(payment_method)
    debit = str(debit_account or '331').strip() or '331'
    vno = _next_voucher_no(conn, 'payment')
    desc = reason or f'Chi tiền {party_name or ""}'.strip()

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='PC',
        document_no=vno,
        document_id=source_id or import_id,
        business_type='CHI_TIEN',
        description=desc,
        reference_document=reference_document or None,
        created_by=created_by,
        lines=[
            {
                'sequence': 1,
                'account_code': debit,
                'debit': float(amt),
                'credit': 0,
                'description': desc,
            },
            {
                'sequence': 2,
                'account_code': credit,
                'debit': 0,
                'credit': float(amt),
                'description': desc,
            },
        ],
    )

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_vouchers (
            voucher_type, form_code, voucher_no, voucher_date,
            party_name, party_address, party_tax_code, amount,
            debit_account, credit_account, reason, reference_document,
            source_type, source_id, journal_entry_id, status, created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'posted',?,?,?)
        """,
        (
            'payment', VOUCHER_FORM_PAYMENT, vno, date_s,
            party_name, party_address, party_tax_code, float(amt),
            debit, credit, desc, reference_document or None,
            source_type or ('import' if import_id else None),
            source_id or import_id,
            entry['id'], created_by, _now(), _now(),
        ),
    )
    voucher_id = cur.lastrowid

    if import_id and debit.startswith('331'):
        try:
            cur.execute(
                """
                UPDATE import
                SET paid_amount = COALESCE(paid_amount, 0) + ?
                WHERE id = ?
                """,
                (float(amt), import_id),
            )
        except sqlite3.OperationalError:
            pass

    if commit:
        conn.commit()

    return {
        'id': voucher_id,
        'voucher_no': vno,
        'form_code': VOUCHER_FORM_PAYMENT,
        'journal_entry_id': entry['id'],
        'amount': float(amt),
        'debit_account': debit,
        'credit_account': credit,
    }


def list_vouchers(
    conn: sqlite3.Connection,
    *,
    voucher_type: str,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    ensure_sme_voucher_schema(conn, commit=False)
    sql = """
        SELECT * FROM sme_vouchers
        WHERE voucher_type = ? AND status != 'void'
    """
    params: list[Any] = [voucher_type]
    if date_from:
        sql += ' AND date(voucher_date) >= date(?)'
        params.append(date_from[:10])
    if date_to:
        sql += ' AND date(voucher_date) <= date(?)'
        params.append(date_to[:10])
    sql += ' ORDER BY voucher_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_voucher(conn: sqlite3.Connection, voucher_id: int) -> dict[str, Any] | None:
    ensure_sme_voucher_schema(conn, commit=False)
    row = conn.execute(
        'SELECT * FROM sme_vouchers WHERE id = ?', (voucher_id,)
    ).fetchone()
    return dict(row) if row else None
