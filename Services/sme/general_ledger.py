"""Sổ cái / bảng cân đối phát sinh SME — từ nhật ký bút toán kép."""
from __future__ import annotations

import calendar
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _period_bounds(year: int, period: int) -> tuple[str, str]:
    if period < 1 or period > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')
    last = calendar.monthrange(year, period)[1]
    return f'{year:04d}-{period:02d}-01', f'{year:04d}-{period:02d}-{last:02d}'


def _net_balance(debit: Decimal, credit: Decimal, normal: str) -> dict[str, float]:
    """Số dư theo tính chất TK — chỉ một bên có số."""
    if (normal or 'debit') == 'credit':
        net = credit - debit
        if net >= 0:
            return {'debit': 0.0, 'credit': float(net), 'net': float(net), 'side': 'credit'}
        return {'debit': float(-net), 'credit': 0.0, 'net': float(net), 'side': 'debit'}
    net = debit - credit
    if net >= 0:
        return {'debit': float(net), 'credit': 0.0, 'net': float(net), 'side': 'debit'}
    return {'debit': 0.0, 'credit': float(-net), 'net': float(net), 'side': 'credit'}


def _activity_before_period(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period: int,
) -> dict[str, dict[str, Decimal]]:
    rows = conn.execute(
        """
        SELECT jl.account_code,
               SUM(jl.debit) AS debit,
               SUM(jl.credit) AS credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND (
              je.fiscal_year < ?
              OR (je.fiscal_year = ? AND je.period < ?)
          )
        GROUP BY jl.account_code
        """,
        (fiscal_year, fiscal_year, period),
    ).fetchall()
    return {
        r[0]: {'debit': _money(r[1]), 'credit': _money(r[2])}
        for r in rows
    }


def _activity_in_periods(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period_from: int,
    period_to: int,
) -> dict[str, dict[str, Decimal]]:
    rows = conn.execute(
        """
        SELECT jl.account_code,
               SUM(jl.debit) AS debit,
               SUM(jl.credit) AS credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND je.fiscal_year = ?
          AND je.period >= ? AND je.period <= ?
        GROUP BY jl.account_code
        """,
        (fiscal_year, period_from, period_to),
    ).fetchall()
    return {
        r[0]: {'debit': _money(r[1]), 'credit': _money(r[2])}
        for r in rows
    }


def period_bounds(year: int, period: int) -> tuple[str, str]:
    """Ngày đầu/cuối tháng YYYY-MM-DD."""
    return _period_bounds(year, period)


def trial_balance(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_from: int = 1,
    period_to: int | None = None,
    postable_only: bool = True,
    include_zero: bool = False,
) -> dict[str, Any]:
    """Bảng cân đối phát sinh: đầu kỳ / trong kỳ / cuối kỳ."""
    ensure_sme_journal_ready(conn, commit=False)
    period_to = period_to or period_from
    if period_from > period_to:
        raise ValueError('period_from không được lớn hơn period_to')

    conn.row_factory = sqlite3.Row
    acc_sql = """
        SELECT code, name, normal_balance, is_postable, account_class, level
        FROM sme_chart_of_accounts
        WHERE is_active = 1
    """
    if postable_only:
        acc_sql += " AND is_postable = 1"
    acc_sql += " ORDER BY code"
    accounts = [dict(r) for r in conn.execute(acc_sql).fetchall()]

    opening_all = _activity_before_period(conn, fiscal_year, period_from)
    period_map = _activity_in_periods(conn, fiscal_year, period_from, period_to)
    codes_needed = set(opening_all) | set(period_map)

    rows_out = []
    sum_open_d = sum_open_c = Decimal('0.00')
    sum_per_d = sum_per_c = Decimal('0.00')
    sum_close_d = sum_close_c = Decimal('0.00')

    for acc in accounts:
        code = acc['code']
        if code not in codes_needed and not include_zero:
            continue
        op = opening_all.get(code, {'debit': Decimal('0.00'), 'credit': Decimal('0.00')})
        pe = period_map.get(code, {'debit': Decimal('0.00'), 'credit': Decimal('0.00')})
        if (
            not include_zero
            and op['debit'] == 0 and op['credit'] == 0
            and pe['debit'] == 0 and pe['credit'] == 0
        ):
            continue

        normal = acc.get('normal_balance') or 'debit'
        close_d = op['debit'] + pe['debit']
        close_c = op['credit'] + pe['credit']
        open_bal = _net_balance(op['debit'], op['credit'], normal)
        close_bal = _net_balance(close_d, close_c, normal)

        rows_out.append({
            'code': code,
            'name': acc['name'],
            'normal_balance': normal,
            'account_class': acc.get('account_class'),
            'level': acc.get('level'),
            'opening_debit': open_bal['debit'],
            'opening_credit': open_bal['credit'],
            'period_debit': float(pe['debit']),
            'period_credit': float(pe['credit']),
            'closing_debit': close_bal['debit'],
            'closing_credit': close_bal['credit'],
        })
        sum_open_d += _money(open_bal['debit'])
        sum_open_c += _money(open_bal['credit'])
        sum_per_d += pe['debit']
        sum_per_c += pe['credit']
        sum_close_d += _money(close_bal['debit'])
        sum_close_c += _money(close_bal['credit'])

    date_from, _ = _period_bounds(fiscal_year, period_from)
    _, date_to = _period_bounds(fiscal_year, period_to)
    return {
        'fiscal_year': fiscal_year,
        'period_from': period_from,
        'period_to': period_to,
        'date_from': date_from,
        'date_to': date_to,
        'rows': rows_out,
        'totals': {
            'opening_debit': float(sum_open_d),
            'opening_credit': float(sum_open_c),
            'period_debit': float(sum_per_d),
            'period_credit': float(sum_per_c),
            'closing_debit': float(sum_close_d),
            'closing_credit': float(sum_close_c),
            'period_balanced': sum_per_d == sum_per_c,
            'opening_balanced': sum_open_d == sum_open_c,
            'closing_balanced': sum_close_d == sum_close_c,
        },
    }


def account_ledger(
    conn: sqlite3.Connection,
    account_code: str,
    *,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    """Sổ cái chi tiết một tài khoản theo khoảng ngày."""
    ensure_sme_journal_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    code = (account_code or '').strip()
    if not code:
        raise ValueError('Thiếu mã tài khoản')

    acc = conn.execute(
        """
        SELECT code, name, normal_balance, is_postable
        FROM sme_chart_of_accounts WHERE code = ?
        """,
        (code,),
    ).fetchone()
    if not acc:
        raise ValueError(f'Không tìm thấy tài khoản {code}')

    d_from = date_from[:10]
    d_to = date_to[:10]
    normal = acc['normal_balance'] or 'debit'

    op = conn.execute(
        """
        SELECT COALESCE(SUM(jl.debit), 0), COALESCE(SUM(jl.credit), 0)
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE jl.account_code = ?
          AND je.status IN ('posted', 'reversed')
          AND je.posting_date < ?
        """,
        (code, d_from),
    ).fetchone()
    open_d, open_c = _money(op[0]), _money(op[1])
    open_bal = _net_balance(open_d, open_c, normal)

    lines = conn.execute(
        """
        SELECT jl.id AS line_id, jl.sequence, jl.debit, jl.credit, jl.description,
               jl.partner_id, jl.partner_type, jl.product_id, jl.warehouse_code,
               je.id AS entry_id, je.entry_no, je.posting_date, je.document_type,
               je.document_no, je.document_id, je.business_type, je.status,
               je.description AS entry_description
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE jl.account_code = ?
          AND je.status IN ('posted', 'reversed')
          AND je.posting_date >= ? AND je.posting_date <= ?
        ORDER BY je.posting_date, je.id, jl.sequence, jl.id
        """,
        (code, d_from, d_to),
    ).fetchall()

    run_d, run_c = open_d, open_c
    detail = []
    period_d = Decimal('0.00')
    period_c = Decimal('0.00')
    for ln in lines:
        d = _money(ln['debit'])
        c = _money(ln['credit'])
        period_d += d
        period_c += c
        run_d += d
        run_c += c
        run_bal = _net_balance(run_d, run_c, normal)
        detail.append({
            'line_id': ln['line_id'],
            'entry_id': ln['entry_id'],
            'entry_no': ln['entry_no'],
            'posting_date': ln['posting_date'],
            'document_type': ln['document_type'],
            'document_no': ln['document_no'],
            'document_id': ln['document_id'],
            'business_type': ln['business_type'],
            'status': ln['status'],
            'description': ln['description'] or ln['entry_description'] or '',
            'debit': float(d),
            'credit': float(c),
            'balance_debit': run_bal['debit'],
            'balance_credit': run_bal['credit'],
            'partner_type': ln['partner_type'],
            'partner_id': ln['partner_id'],
        })

    close_bal = _net_balance(open_d + period_d, open_c + period_c, normal)
    return {
        'account': {
            'code': acc['code'],
            'name': acc['name'],
            'normal_balance': normal,
            'is_postable': acc['is_postable'],
        },
        'date_from': d_from,
        'date_to': d_to,
        'opening': open_bal,
        'period_debit': float(period_d),
        'period_credit': float(period_c),
        'closing': close_bal,
        'lines': detail,
        'line_count': len(detail),
    }
