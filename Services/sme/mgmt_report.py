"""Báo cáo quản trị SME — P&L theo kỳ từ nhật ký."""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.dashboard_metrics import dashboard_metrics
from Services.sme.journal_engine import ensure_sme_journal_ready

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def management_report(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_from: int = 1,
    period_to: int | None = None,
) -> dict[str, Any]:
    ensure_sme_journal_ready(conn, commit=False)
    from datetime import datetime
    period_to = period_to or datetime.now().month
    if not (1 <= period_from <= 12 and 1 <= period_to <= 12) or period_from > period_to:
        raise ValueError('Kỳ không hợp lệ')

    # Dùng metrics YTD đến period_to rồi cắt monthly
    full = dashboard_metrics(conn, fiscal_year=fiscal_year, period_to=period_to)
    monthly = [m for m in full['monthly'] if period_from <= m['period'] <= period_to]

    # Tính lại tổng khoảng chọn
    revenue = sum(m['revenue'] for m in monthly)
    cogs = sum(m['cogs'] for m in monthly)
    expenses = sum(m['expenses'] for m in monthly)
    profit = sum(m['profit'] for m in monthly)

    return {
        'fiscal_year': fiscal_year,
        'period_from': period_from,
        'period_to': period_to,
        'revenue': revenue,
        'cogs': cogs,
        'gross_profit': revenue - cogs,
        'expenses': expenses,
        'profit': profit,
        'margin_pct': round((profit / revenue * 100), 2) if revenue else 0.0,
        'gross_margin_pct': round(((revenue - cogs) / revenue * 100), 2) if revenue else 0.0,
        'monthly': monthly,
        'cash': full['cash'],
        'receivable': full['receivable'],
        'payable': full['payable'],
        'vat_payable': full['vat_payable'],
    }
