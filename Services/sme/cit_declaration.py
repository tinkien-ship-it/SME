"""Tờ khai / quyết toán TNDN SME — worksheet từ sổ kép + tạm nộp."""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.bctc_report import _closing_balances, _period_activity, income_statement
from Services.sme.cit import ensure_sme_cit_schema, list_cit_provisions
from Services.sme.journal_engine import ensure_sme_journal_ready

MONEY_Q = Decimal('0.01')
DEFAULT_RATE = Decimal('0.20')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def cit_declaration_worksheet(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int = 12,
    tax_rate: float | None = None,
    adjustments: dict | None = None,
) -> dict[str, Any]:
    """
    Lập chỉ tiêu quyết toán TNDN rút gọn từ B02 + điều chỉnh thủ công.

    adjustments (tuỳ chọn):
      - non_deductible: chi phí không được trừ
      - exempt_income: thu nhập miễn thuế
      - other_increase / other_decrease
    """
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_cit_schema(conn, commit=False)
    year = int(fiscal_year)
    p_to = max(1, min(12, int(period_to)))
    rate = _money(tax_rate if tax_rate is not None else DEFAULT_RATE)

    # Lợi nhuận kế toán trước thuế ≈ từ B02 đến hết kỳ
    try:
        b02 = income_statement(conn, fiscal_year=year, period_from=1, period_to=p_to)
        totals = b02.get('totals') or {}
        accounting_profit = _money(totals.get('profit_before_tax') or 0)
    except Exception:
        accounting_profit = Decimal('0.00')
        totals = {}
        b02 = {}

    revenue_for_rate = _money(totals.get('revenue_net') or 0)

    # Nếu B02 chưa có profit_before_tax rõ — ước từ phát sinh doanh thu - chi phí
    if accounting_profit == 0:
        act = _period_activity(conn, year, 1, p_to)
        rev = Decimal('0')
        exp = Decimal('0')
        for code, bal in act.items():
            if code.startswith(('511', '515', '711')):
                rev += _money(bal.get('credit')) - _money(bal.get('debit'))
            if code.startswith(('632', '641', '642', '635', '811', '621', '622', '627')):
                exp += _money(bal.get('debit')) - _money(bal.get('credit'))
        accounting_profit = rev - exp
        if revenue_for_rate <= 0:
            revenue_for_rate = rev

    if tax_rate is None:
        try:
            from Services.sme.regime_profile import get_ledger_profile
            from Services.sme.tt58_tax_rates import get_cit_income_rate_pct
            if get_ledger_profile(conn).get('is_tt58_micro'):
                rate = _money(
                    get_cit_income_rate_pct(
                        conn,
                        as_of=f'{year:04d}-12-31',
                        revenue=float(revenue_for_rate),
                    ) / 100.0
                )
        except Exception:
            pass

    adj = adjustments or {}
    non_deduct = _money(adj.get('non_deductible'))
    exempt = _money(adj.get('exempt_income'))
    other_inc = _money(adj.get('other_increase'))
    other_dec = _money(adj.get('other_decrease'))
    # Lỗ các năm trước được kết chuyển sang năm nay (người dùng nhập)
    loss_carry = _money(adj.get('loss_carry_forward'))
    if loss_carry < 0:
        loss_carry = abs(loss_carry)

    taxable_before_loss = accounting_profit + non_deduct + other_inc - exempt - other_dec
    current_year_loss = abs(taxable_before_loss) if taxable_before_loss < 0 else Decimal('0.00')
    # Chỉ được KC lỗ tối đa bằng thu nhập dương trước lỗ
    loss_applied = min(loss_carry, max(Decimal('0.00'), taxable_before_loss))
    taxable = taxable_before_loss - loss_applied
    taxable_pos = max(Decimal('0.00'), taxable)

    cit_due = (taxable_pos * rate).quantize(MONEY_Q)

    provisions = list_cit_provisions(conn, fiscal_year=year)
    prepaid = sum((_money(p.get('tax_amount')) for p in provisions if p.get('status') in ('accrued', 'paid')), Decimal('0'))
    paid_cash = sum((_money(p.get('tax_amount')) for p in provisions if p.get('status') == 'paid'), Decimal('0'))

    remaining = cit_due - prepaid
    bals = _closing_balances(conn, year, p_to)
    bal_3334 = Decimal('0')
    for code, bal in bals.items():
        if code == '3334' or code.startswith('3334'):
            bal_3334 += _money(bal.get('credit')) - _money(bal.get('debit'))

    lines = [
        {'code': 'A1', 'name': 'Tổng lợi nhuận kế toán trước thuế TNDN', 'amount': _f(accounting_profit)},
        {'code': 'B1', 'name': 'Các khoản điều chỉnh tăng', 'amount': _f(non_deduct + other_inc)},
        {'code': 'B1a', 'name': '  — Chi phí không được trừ', 'amount': _f(non_deduct)},
        {'code': 'B1b', 'name': '  — Điều chỉnh tăng khác', 'amount': _f(other_inc)},
        {'code': 'B2', 'name': 'Các khoản điều chỉnh giảm', 'amount': _f(exempt + other_dec)},
        {'code': 'B2a', 'name': '  — Thu nhập miễn thuế', 'amount': _f(exempt)},
        {'code': 'B2b', 'name': '  — Điều chỉnh giảm khác', 'amount': _f(other_dec)},
        {'code': 'C', 'name': 'Thu nhập tính thuế trước KC lỗ', 'amount': _f(taxable_before_loss)},
        {'code': 'C1', 'name': 'Thu nhập tính thuế dương (sau KC lỗ)', 'amount': _f(taxable_pos)},
        {'code': 'C2', 'name': 'Lỗ các năm trước được kết chuyển', 'amount': _f(loss_carry)},
        {'code': 'C2a', 'name': '  — Lỗ đã trừ trong năm', 'amount': _f(loss_applied)},
        {'code': 'C3', 'name': 'Lỗ phát sinh năm nay (chuyển kỳ sau)', 'amount': _f(current_year_loss)},
        {'code': 'D', 'name': f'Thuế TNDN phải nộp (thuế suất {float(rate)*100:.0f}%)', 'amount': _f(cit_due)},
        {'code': 'E', 'name': 'Thuế TNDN đã tạm nộp trong năm', 'amount': _f(prepaid)},
        {'code': 'E1', 'name': '  — Trong đó đã nộp NSNN', 'amount': _f(paid_cash)},
        {'code': 'F', 'name': 'Thuế TNDN còn phải nộp / (nộp thừa)', 'amount': _f(remaining)},
        {'code': 'G', 'name': 'Số dư TK 3334 cuối kỳ', 'amount': _f(bal_3334)},
    ]

    return {
        'fiscal_year': year,
        'period_to': p_to,
        'tax_rate': float(rate),
        'accounting_profit': _f(accounting_profit),
        'taxable_income': _f(taxable),
        'taxable_before_loss': _f(taxable_before_loss),
        'loss_applied': _f(loss_applied),
        'current_year_loss': _f(current_year_loss),
        'cit_due': _f(cit_due),
        'cit_prepaid': _f(prepaid),
        'cit_remaining': _f(remaining),
        'balance_3334': _f(bal_3334),
        'adjustments': {
            'non_deductible': _f(non_deduct),
            'exempt_income': _f(exempt),
            'other_increase': _f(other_inc),
            'other_decrease': _f(other_dec),
            'loss_carry_forward': _f(loss_carry),
        },
        'provisions': provisions,
        'lines': lines,
        'form_hint': '03/TNDN (rút gọn) — đối chiếu sổ kép SME; nhập lỗ KC tại C2',
    }
