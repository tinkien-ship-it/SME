"""Tờ khai khấu trừ TNCN DN rút gọn — từ bảng lương SME (3335 / salary_detail)."""
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


def pit_withholding_worksheet(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_from: int = 1,
    period_to: int | None = None,
) -> dict[str, Any]:
    """
    Bảng kê TNCN khấu trừ từ tiền lương (khung 05/KK-TNCN rút gọn).
    Nguồn: salary_detail + đối chiếu phát sinh Có TK 3335.
    """
    ensure_sme_journal_ready(conn, commit=False)
    year = int(fiscal_year)
    p_from = max(1, min(12, int(period_from)))
    p_to = max(p_from, min(12, int(period_to or 12)))

    lines: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            """
            SELECT COALESCE(e.fullname, s.fullname) AS fullname,
                   COALESCE(e.id_card, '') AS id_card,
                   s.month, s.year,
                   COALESCE(s.total_income, 0) AS taxable_income,
                   COALESCE(s.tncn_tax, 0) AS pit_amount,
                   COALESCE(s.final_amount, 0) AS net_pay
            FROM salary_detail s
            LEFT JOIN employees e ON e.id = s.employee_id
            WHERE s.year = ? AND s.month BETWEEN ? AND ?
            ORDER BY s.month, fullname
            """,
            (year, p_from, p_to),
        ).fetchall()
        for r in rows:
            d = dict(r)
            lines.append({
                'fullname': d.get('fullname') or '',
                'tax_code': d.get('id_card') or '',
                'month': int(d.get('month') or 0),
                'year': int(d.get('year') or year),
                'taxable_income': _f(d.get('taxable_income')),
                'pit_amount': _f(d.get('pit_amount')),
                'net_pay': _f(d.get('net_pay')),
            })
    except sqlite3.Error:
        lines = []

    total_income = sum((_money(x['taxable_income']) for x in lines), Decimal('0'))
    total_pit = sum((_money(x['pit_amount']) for x in lines), Decimal('0'))

    # Đối chiếu sổ: phát sinh Có 3335 trong kỳ
    act = _period_activity(conn, year, p_from, p_to)
    credit_3335 = Decimal('0')
    for code, bal in act.items():
        if code == '3335' or str(code).startswith('3335'):
            credit_3335 += _money(bal.get('credit')) - _money(bal.get('debit'))
    bals = _closing_balances(conn, year, p_to)
    bal_3335 = Decimal('0')
    for code, bal in bals.items():
        if code == '3335' or str(code).startswith('3335'):
            bal_3335 += _money(bal.get('credit')) - _money(bal.get('debit'))

    by_month: dict[int, dict[str, float]] = {}
    for x in lines:
        m = int(x['month'])
        slot = by_month.setdefault(m, {'taxable_income': 0.0, 'pit_amount': 0.0, 'count': 0})
        slot['taxable_income'] += x['taxable_income']
        slot['pit_amount'] += x['pit_amount']
        slot['count'] += 1

    return {
        'form_hint': '05/KK-TNCN (rút gọn) — khấu trừ TNCN từ tiền lương theo bảng lương SME',
        'fiscal_year': year,
        'period_from': p_from,
        'period_to': p_to,
        'lines': lines,
        'monthly': [
            {'month': m, **by_month[m]} for m in sorted(by_month)
        ],
        'totals': {
            'employee_rows': len(lines),
            'taxable_income': _f(total_income),
            'pit_withheld': _f(total_pit),
            'journal_3335_net_credit': _f(credit_3335),
            'balance_3335': _f(bal_3335),
            'difference_vs_journal': _f(total_pit - credit_3335),
        },
    }
