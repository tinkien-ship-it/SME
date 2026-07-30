"""Giá thành SME — tổng hợp 154/632 + liên kết lệnh sản xuất."""
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


def costing_summary(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
) -> dict[str, Any]:
    ensure_sme_journal_ready(conn, commit=False)
    bals = _closing_balances(conn, fiscal_year, period)
    act = _period_activity(conn, fiscal_year, period, period)
    ytd = _period_activity(conn, fiscal_year, 1, period)

    def bal_prefix(prefix: str) -> float:
        total = Decimal('0')
        for code, bal in bals.items():
            if code == prefix or code.startswith(prefix):
                total += _money(bal.get('debit')) - _money(bal.get('credit'))
        return _f(total)

    def act_debit(activity, prefix: str) -> float:
        total = Decimal('0')
        for code, bal in activity.items():
            if code == prefix or code.startswith(prefix):
                total += _money(bal.get('debit')) - _money(bal.get('credit'))
        return _f(total)

    wip = bal_prefix('154')
    inventory = sum(bal_prefix(p) for p in ('152', '155', '156'))
    cogs_period = act_debit(act, '632')
    cogs_ytd = act_debit(ytd, '632')
    materials = act_debit(act, '152')  # xuất NVL gần đúng nếu có

    prod_orders = 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='production_orders'"
        ).fetchone()
        if row and row[0]:
            prod_orders = int(conn.execute(
                "SELECT COUNT(*) FROM production_orders WHERE status != 'cancelled'"
            ).fetchone()[0] or 0)
    except sqlite3.Error:
        prod_orders = 0

    return {
        'fiscal_year': fiscal_year,
        'period': period,
        'wip_154': wip,
        'inventory_raw_fg': inventory,
        'cogs_period': cogs_period,
        'cogs_ytd': cogs_ytd,
        'materials_movement': materials,
        'production_orders_active': prod_orders,
    }
