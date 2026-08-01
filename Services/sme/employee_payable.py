"""Phải trả nhân viên — số dư Có TK 334* từ sổ kép SME."""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.bctc_report import _closing_balances
from Services.sme.journal_engine import ensure_sme_journal_ready

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def employee_payable_summary(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Số dư Có TK 334 / 334x (lương và phải trả nhân viên)."""
    ensure_sme_journal_ready(conn, commit=False)
    bals = _closing_balances(conn, fiscal_year, period, branch_code=branch_code)
    coa = {}
    try:
        rows = conn.execute(
            "SELECT code, name FROM sme_chart_of_accounts WHERE is_active = 1"
        ).fetchall()
        coa = {r[0]: r[1] for r in rows}
    except sqlite3.Error:
        pass

    lines = []
    total = Decimal('0.00')
    for code in sorted(bals.keys()):
        if not (code == '334' or code.startswith('334')):
            continue
        bal = bals[code]
        net = _money(bal.get('credit')) - _money(bal.get('debit'))
        if net == 0:
            continue
        lines.append({
            'account_code': code,
            'name': coa.get(code) or code,
            'debit': _f(bal.get('debit')),
            'credit': _f(bal.get('credit')),
            'balance': _f(net),
        })
        total += net

    return {
        'fiscal_year': fiscal_year,
        'period': period,
        'account_prefix': '334',
        'lines': lines,
        'total': _f(total),
        'hint': 'Số dư Có TK 334* = lương / phải trả nhân viên trên sổ kép SME.',
        'branch_code': branch_code or 'ALL',
    }
