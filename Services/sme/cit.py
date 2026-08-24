"""Thuế TNDN SME — tạm nộp (8211 / 3334) và thanh toán."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry, reverse_journal_entry
from Services.sme.vouchers import create_payment
from db_utils import sqlite_commit

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_cit_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_cit_provisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fiscal_year INTEGER NOT NULL,
            period INTEGER NOT NULL,
            provision_date TEXT NOT NULL,
            taxable_income REAL NOT NULL DEFAULT 0,
            tax_rate REAL NOT NULL DEFAULT 0.20,
            tax_amount REAL NOT NULL DEFAULT 0,
            journal_entry_id INTEGER,
            payment_voucher_id INTEGER,
            status TEXT NOT NULL DEFAULT 'accrued',
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(fiscal_year, period)
        )
        """
    )
    if commit:
        sqlite_commit(conn, label='cit')


def accrue_cit_provisional(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    tax_amount=None,
    taxable_income=0,
    tax_rate=0.20,
    provision_date: str | None = None,
    notes: str = '',
    created_by: str | None = None,
    replace_existing: bool = False,
    commit: bool = False,
) -> dict[str, Any]:
    """Tạm nộp TNDN: Nợ 8211 / Có 3334."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_cit_schema(conn, commit=False)
    year, per = int(fiscal_year), int(period)
    if per < 1 or per > 12:
        raise ValueError('Kỳ phải từ 1–12')

    existing = conn.execute(
        'SELECT * FROM sme_cit_provisions WHERE fiscal_year = ? AND period = ?',
        (year, per),
    ).fetchone()
    if existing:
        ex = dict(existing)
        if not replace_existing:
            raise ValueError(f'Đã có tạm nộp TNDN {per}/{year} — dùng replace_existing')
        if ex.get('journal_entry_id'):
            reverse_journal_entry(
                conn, int(ex['journal_entry_id']),
                created_by=created_by, reason='Thay tạm nộp TNDN',
            )
        conn.execute('DELETE FROM sme_cit_provisions WHERE id = ?', (ex['id'],))

    income = _money(taxable_income)
    rate = _money(tax_rate)
    if tax_amount is not None:
        tax = _money(tax_amount)
    else:
        tax = (income * rate).quantize(MONEY_Q)
    if tax <= 0:
        raise ValueError('Số thuế tạm nộp phải > 0')

    date_s = (provision_date or f'{year:04d}-{per:02d}-{28 if per == 2 else 30}')[:10]
    # Chuẩn hóa ngày cuối tháng đơn giản
    if per in (1, 3, 5, 7, 8, 10, 12):
        date_s = f'{year:04d}-{per:02d}-31' if not provision_date else date_s
    elif per != 2:
        date_s = f'{year:04d}-{per:02d}-30' if not provision_date else date_s

    doc_no = f'TNDN{year}{per:02d}'
    desc = notes or f'Tạm nộp TNDN kỳ {per}/{year}'
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='TNDN',
        document_no=doc_no,
        document_id=per,
        business_type='TAM_NOP_TNDN',
        description=desc,
        created_by=created_by,
        lines=[
            {'sequence': 1, 'account_code': '8211', 'debit': float(tax), 'credit': 0, 'description': desc},
            {'sequence': 2, 'account_code': '3334', 'debit': 0, 'credit': float(tax), 'description': desc},
        ],
    )
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_cit_provisions (
            fiscal_year, period, provision_date, taxable_income, tax_rate, tax_amount,
            journal_entry_id, status, notes, created_by, created_at
        ) VALUES (?,?,?,?,?,?,?,'accrued',?,?,?)
        """,
        (year, per, date_s, float(income), float(rate), float(tax),
         entry['id'], notes or '', created_by, _now()),
    )
    if commit:
        sqlite_commit(conn, label='cit')
    return get_cit_provision(conn, year, per)


def pay_cit(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    amount=None,
    pay_date: str | None = None,
    payment_method: str = 'bank',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Nộp TNDN: phiếu chi Nợ 3334 / Có 112|111."""
    ensure_sme_cit_schema(conn, commit=False)
    prov = get_cit_provision(conn, int(fiscal_year), int(period))
    if not prov:
        raise ValueError('Chưa có bút toán tạm nộp TNDN kỳ này')
    if prov.get('payment_voucher_id'):
        raise ValueError('Đã nộp TNDN kỳ này')
    pay_amt = _money(amount if amount is not None else prov['tax_amount'])
    if pay_amt <= 0:
        raise ValueError('Số tiền nộp phải > 0')
    date_s = (pay_date or datetime.now().strftime('%Y-%m-%d'))[:10]
    voucher = create_payment(
        conn,
        voucher_date=date_s,
        party_name='Cơ quan thuế',
        amount=float(pay_amt),
        payment_method=payment_method or 'bank',
        debit_account='3334',
        reason=f'Nộp TNDN kỳ {period}/{fiscal_year}',
        reference_document=f'TNDN|{fiscal_year}|{period}',
        source_type='cit',
        source_id=prov['id'],
        created_by=created_by,
        commit=False,
    )
    conn.execute(
        """
        UPDATE sme_cit_provisions
        SET payment_voucher_id = ?, status = 'paid'
        WHERE id = ?
        """,
        (voucher['id'], prov['id']),
    )
    if commit:
        sqlite_commit(conn, label='cit')
    out = get_cit_provision(conn, int(fiscal_year), int(period))
    out['voucher'] = voucher
    return out


def get_cit_provision(conn: sqlite3.Connection, fiscal_year: int, period: int) -> dict[str, Any] | None:
    ensure_sme_cit_schema(conn, commit=False)
    row = conn.execute(
        'SELECT * FROM sme_cit_provisions WHERE fiscal_year = ? AND period = ?',
        (int(fiscal_year), int(period)),
    ).fetchone()
    return dict(row) if row else None


def list_cit_provisions(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int | None = None,
) -> list[dict[str, Any]]:
    ensure_sme_cit_schema(conn, commit=False)
    if fiscal_year:
        rows = conn.execute(
            'SELECT * FROM sme_cit_provisions WHERE fiscal_year = ? ORDER BY period',
            (int(fiscal_year),),
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM sme_cit_provisions ORDER BY fiscal_year DESC, period DESC LIMIT 48'
        ).fetchall()
    return [dict(r) for r in rows]


def estimate_ytd_profit_before_cit(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
):
    """LN kế toán lũy kế từ đầu năm đến kỳ, trước thuế TNDN (bỏ TK 821)."""
    from Services.sme.period_close import (
        EXPENSE_PREFIXES,
        REVENUE_PREFIXES,
        SKIP_CODES,
        _matches_prefix,
        _period_pl_activity,
    )

    rev = Decimal('0.00')
    exp = Decimal('0.00')
    year, per = int(fiscal_year), int(period)
    for month in range(1, per + 1):
        activity = _period_pl_activity(conn, year, month)
        for code, bal in activity.items():
            if code in SKIP_CODES or str(code).startswith('421') or str(code).startswith('821'):
                continue
            if _matches_prefix(code, REVENUE_PREFIXES):
                net = _money(bal['credit']) - _money(bal['debit'])
                if net > 0:
                    rev += net
            elif _matches_prefix(code, EXPENSE_PREFIXES):
                net = _money(bal['debit']) - _money(bal['credit'])
                if net > 0:
                    exp += net
    return (rev - exp).quantize(MONEY_Q)


def accrue_period_cit(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    provision_date: str | None = None,
    tax_rate=0.20,
    replace_existing: bool = False,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Tạm nộp TNDN cuối quý (T3/T6/T9/T12): Nợ 8211 / Có 3334.

    Số thuế = max(0, LNKT lũy kế × suất − đã trích các kỳ trước trong năm).
    """
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_cit_schema(conn, commit=False)
    year, per = int(fiscal_year), int(period)
    if per not in (3, 6, 9, 12):
        return {
            'posted': False, 'reason': 'not_quarter_end',
            'period': per, 'fiscal_year': year, 'amount': 0.0,
        }

    existing = get_cit_provision(conn, year, per)
    if existing and not replace_existing:
        return {
            'posted': False,
            'reason': 'already_posted',
            'period': per,
            'fiscal_year': year,
            'amount': float(existing.get('tax_amount') or 0),
            'journal_entry_id': existing.get('journal_entry_id'),
        }

    pbt = estimate_ytd_profit_before_cit(conn, fiscal_year=year, period=per)
    rate = _money(tax_rate if tax_rate is not None else 0.20)
    if rate <= 0:
        rate = _money('0.20')
    ytd_tax = (pbt * rate).quantize(MONEY_Q) if pbt > 0 else Decimal('0.00')
    prior = Decimal('0.00')
    for row in list_cit_provisions(conn, fiscal_year=year):
        if int(row.get('period') or 0) >= per:
            continue
        if str(row.get('status') or '') == 'void':
            continue
        prior += _money(row.get('tax_amount'))
    tax = ytd_tax - prior
    if tax <= 0:
        return {
            'posted': False,
            'reason': 'no_taxable_profit',
            'period': per,
            'fiscal_year': year,
            'taxable_income': float(pbt),
            'ytd_tax': float(ytd_tax),
            'prior_tax': float(prior),
            'amount': 0.0,
        }

    rec = accrue_cit_provisional(
        conn,
        fiscal_year=year,
        period=per,
        tax_amount=tax,
        taxable_income=pbt,
        tax_rate=rate,
        provision_date=provision_date,
        notes=f'Tạm nộp TNDN quý (LNKT {float(pbt):,.0f} × {float(rate):g} − đã trích {float(prior):,.0f})',
        created_by=created_by,
        replace_existing=replace_existing,
        commit=False,
    )
    if commit:
        sqlite_commit(conn, label='cit')
    return {
        'posted': True,
        'period': per,
        'fiscal_year': year,
        'amount': float(rec.get('tax_amount') or tax),
        'taxable_income': float(pbt),
        'tax_rate': float(rate),
        'journal_entry_id': rec.get('journal_entry_id'),
        'ytd_tax': float(ytd_tax),
        'prior_tax': float(prior),
    }
