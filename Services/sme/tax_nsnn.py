"""Thuế & NSNN SME — số dư TK 133 / 333 từ sổ kép."""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.bctc_report import _closing_balances, _period_activity
from Services.sme.journal_engine import ensure_sme_journal_ready
from Services.sme.filing_period import quarter_bounds, resolve_filing_window
from Services.sme.period_lock import is_period_locked, list_locked_periods
from Services.tenant_profile import normalize_vat_filing_period

MONEY_Q = Decimal('0.01')

TAX_GROUPS = (
    ('133', 'GTGT được khấu trừ', 'debit', 'asset'),
    ('33311', 'GTGT đầu ra', 'credit', 'liability'),
    ('33312', 'GTGT hàng nhập khẩu', 'credit', 'liability'),
    ('3332', 'Thuế TTĐB', 'credit', 'liability'),
    ('3333', 'Thuế XNK', 'credit', 'liability'),
    ('3334', 'Thuế TNDN', 'credit', 'liability'),
    ('3335', 'Thuế TNCN', 'credit', 'liability'),
    ('3336', 'Thuế tài nguyên', 'credit', 'liability'),
    ('3337', 'Thuế nhà đất / thuê đất', 'credit', 'liability'),
    ('3338', 'Thuế BVMT & thuế khác', 'credit', 'liability'),
    ('3339', 'Phí, lệ phí khác', 'credit', 'liability'),
)

def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _net(bal: dict | None, normal: str) -> Decimal:
    if not bal:
        return Decimal('0.00')
    d, c = _money(bal.get('debit')), _money(bal.get('credit'))
    return (c - d) if normal == 'credit' else (d - c)


def tax_nsnn_summary(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int | None = None,
    quarter: int | None = None,
    filing_mode: str | None = None,
) -> dict[str, Any]:
    ensure_sme_journal_ready(conn, commit=False)
    window = resolve_filing_window(
        filing_mode=filing_mode,
        period=period,
        quarter=quarter,
    )
    period_to = int(window['period_to'])
    period_from = int(window['period_from'])

    bals = _closing_balances(conn, fiscal_year, period_to)
    activity = _period_activity(conn, fiscal_year, period_from, period_to)
    coa_names = {}
    try:
        rows = conn.execute(
            "SELECT code, name FROM sme_chart_of_accounts WHERE is_active = 1"
        ).fetchall()
        coa_names = {r[0]: r[1] for r in rows}
    except sqlite3.Error:
        pass

    lines = []
    payable_total = Decimal('0.00')
    credit_total = Decimal('0.00')
    for code, label, normal, kind in TAX_GROUPS:
        amount = Decimal('0.00')
        period_net = Decimal('0.00')
        detail_rows = []
        for acc, bal in sorted(bals.items()):
            if acc == code or acc.startswith(code):
                if code == '33311' and acc.startswith('33312'):
                    continue
                net = _net(bal, normal)
                act_net = _net(activity.get(acc), normal)
                if net == 0 and act_net == 0:
                    continue
                if code.startswith('333') and len(code) >= 5:
                    if not (acc == code or acc.startswith(code)):
                        continue
                amount += net
                period_net += act_net
                detail_rows.append({
                    'account_code': acc,
                    'name': coa_names.get(acc) or acc,
                    'amount': _f(net),
                    'period_amount': _f(act_net),
                })
        lines.append({
            'code': code,
            'label': label,
            'kind': kind,
            'normal': normal,
            'amount': _f(amount),
            'period_amount': _f(period_net),
            'details': detail_rows,
        })
        if kind == 'liability' and amount > 0:
            payable_total += amount
        if kind == 'asset' and amount > 0:
            credit_total += amount

    vat_in = next((x['amount'] for x in lines if x['code'] == '133'), 0.0)
    vat_out = next((x['amount'] for x in lines if x['code'] == '33311'), 0.0)
    vat_in_period = next((x['period_amount'] for x in lines if x['code'] == '133'), 0.0)
    vat_out_period = next((x['period_amount'] for x in lines if x['code'] == '33311'), 0.0)
    vat_payable = max(0.0, float(vat_out) - float(vat_in))
    vat_credit = max(0.0, float(vat_in) - float(vat_out))
    vat_payable_period = max(0.0, float(vat_out_period) - float(vat_in_period))
    vat_credit_period = max(0.0, float(vat_in_period) - float(vat_out_period))

    locked = is_period_locked(conn, fiscal_year, period_to)
    locks = list_locked_periods(conn, fiscal_year=fiscal_year)
    months_locked = [
        m for m in range(period_from, period_to + 1)
        if is_period_locked(conn, fiscal_year, m)
    ]

    return {
        'fiscal_year': fiscal_year,
        'period': period_to,
        'period_from': period_from,
        'period_to': period_to,
        'quarter': window['quarter'],
        'filing_mode': window['filing_mode'],
        'filing_label': window['label'],
        'lines': lines,
        'summary': {
            'tax_payable_total': _f(payable_total),
            'vat_input_credit': float(vat_in),
            'vat_output': float(vat_out),
            'vat_payable': vat_payable,
            'vat_credit_carry': vat_credit,
            'input_credit_total': _f(credit_total),
            'vat_input_period': float(vat_in_period),
            'vat_output_period': float(vat_out_period),
            'vat_payable_period': vat_payable_period,
            'vat_credit_period': vat_credit_period,
        },
        'period_locked': locked,
        'months_locked': months_locked,
        'locked_periods': locks,
    }
