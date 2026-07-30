"""Chỉ số dashboard SME — từ nhật ký bút toán."""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.bctc_report import _closing_balances, _period_activity
from Services.sme.journal_engine import ensure_sme_journal_ready

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _sum_activity(
    activity: dict[str, dict[str, Decimal]],
    prefixes: tuple[str, ...],
    *,
    side: str,
) -> Decimal:
    total = Decimal('0.00')
    for code, bal in activity.items():
        if not any(code == p or code.startswith(p) for p in prefixes):
            continue
        if side == 'credit':
            total += _money(bal.get('credit')) - _money(bal.get('debit'))
        else:
            total += _money(bal.get('debit')) - _money(bal.get('credit'))
    return _money(total)


def _sum_balance(
    bals: dict[str, dict[str, Decimal]],
    prefixes: tuple[str, ...],
    *,
    normal: str,
) -> Decimal:
    total = Decimal('0.00')
    for code, bal in bals.items():
        if not any(code == p or code.startswith(p) for p in prefixes):
            continue
        d, c = _money(bal.get('debit')), _money(bal.get('credit'))
        if normal == 'credit':
            total += c - d
        else:
            total += d - c
    return _money(total)


def dashboard_metrics(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int | None = None,
) -> dict[str, Any]:
    """Doanh thu, LN gộp, phải thu/trả, cơ cấu thuế theo kỳ YTD."""
    ensure_sme_journal_ready(conn, commit=False)
    from datetime import datetime
    period_to = period_to or datetime.now().month
    if period_to < 1 or period_to > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    activity = _period_activity(conn, fiscal_year, 1, period_to)
    bals = _closing_balances(conn, fiscal_year, period_to)

    revenue = _sum_activity(activity, ('511', '515', '711'), side='credit')
    cogs = _sum_activity(activity, ('632',), side='debit')
    selling = _sum_activity(activity, ('641',), side='debit')
    admin = _sum_activity(activity, ('642',), side='debit')
    other_exp = _sum_activity(activity, ('635', '811', '821'), side='debit')
    gross = revenue - cogs
    operating = gross - selling - admin
    profit = operating - other_exp

    receivable = _sum_balance(bals, ('131',), normal='debit')
    payable = _sum_balance(bals, ('331',), normal='credit')
    cash = _sum_balance(bals, ('111', '112'), normal='debit')
    vat_in = _sum_balance(bals, ('133',), normal='debit')
    vat_out = _sum_balance(bals, ('33311', '3331'), normal='credit')
    # Tránh đếm cha+con: ưu tiên lá 33311 nếu có
    vat_out_leaf = _sum_balance(bals, ('33311',), normal='credit')
    if vat_out_leaf != 0:
        vat_out = vat_out_leaf
    cit = _sum_balance(bals, ('3334',), normal='credit')
    pit = _sum_balance(bals, ('3335',), normal='credit')
    other_tax = _sum_balance(bals, ('3332', '3333', '3336', '3337', '3338', '3339'), normal='credit')

    # P&L theo tháng (1..period_to)
    monthly = []
    for m in range(1, period_to + 1):
        act = _period_activity(conn, fiscal_year, m, m)
        rev_m = _sum_activity(act, ('511', '515', '711'), side='credit')
        cogs_m = _sum_activity(act, ('632',), side='debit')
        exp_m = _sum_activity(act, ('641', '642', '635', '811'), side='debit')
        monthly.append({
            'period': m,
            'label': f'T{m:02d}',
            'revenue': _f(rev_m),
            'cogs': _f(cogs_m),
            'expenses': _f(exp_m),
            'profit': _f(rev_m - cogs_m - exp_m),
        })

    return {
        'fiscal_year': fiscal_year,
        'period_to': period_to,
        'revenue': _f(revenue),
        'cogs': _f(cogs),
        'gross_profit': _f(gross),
        'selling_expense': _f(selling),
        'admin_expense': _f(admin),
        'operating_profit': _f(operating),
        'profit': _f(profit),
        'receivable': _f(receivable),
        'payable': _f(payable),
        'cash': _f(cash),
        'vat_input': _f(vat_in),
        'vat_output': _f(vat_out),
        'vat_payable': _f(max(Decimal('0'), vat_out - vat_in)),
        'vat_credit': _f(max(Decimal('0'), vat_in - vat_out)),
        'tax_breakdown': {
            'gtgt': _f(vat_out),
            'tndn': _f(cit),
            'tncn': _f(pit),
            'other': _f(other_tax),
        },
        'monthly': monthly,
    }
