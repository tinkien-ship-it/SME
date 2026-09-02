"""Giá thành SME — tổng hợp 621/622/627/154/155/632 + lệnh sản xuất."""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.bctc_report import _closing_balances, _period_activity
from Services.sme.journal_engine import ensure_sme_journal_ready
from db.dialect import is_postgres
from db.schema_helpers import table_cols, table_exists

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _prefix_net(bals: dict, prefix: str) -> Decimal:
    total = Decimal('0.00')
    for code, bal in bals.items():
        if code == prefix or code.startswith(prefix):
            total += _money(bal.get('debit')) - _money(bal.get('credit'))
    return total


def _prefix_debit(activity: dict, prefix: str) -> Decimal:
    total = Decimal('0.00')
    for code, bal in activity.items():
        if code == prefix or code.startswith(prefix):
            total += _money(bal.get('debit'))
    return total


def costing_summary(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    branch_code: str | None = None,
) -> dict[str, Any]:
    ensure_sme_journal_ready(conn, commit=False)
    bals = _closing_balances(conn, fiscal_year, period, branch_code=branch_code)
    act = _period_activity(conn, fiscal_year, period, period, branch_code=branch_code)
    ytd = _period_activity(conn, fiscal_year, 1, period, branch_code=branch_code)

    cp621 = _f(_prefix_debit(act, '621'))
    cp622 = _f(_prefix_debit(act, '622'))
    cp627 = _f(_prefix_debit(act, '627'))
    collected = cp621 + cp622 + cp627

    wip = _f(_prefix_net(bals, '154'))
    fg = _f(_prefix_net(bals, '155'))
    materials = _f(_prefix_net(bals, '152'))
    goods = _f(_prefix_net(bals, '156'))
    cogs_period = _f(_prefix_debit(act, '632'))
    cogs_ytd = _f(_prefix_debit(ytd, '632'))

    # Phát sinh Nợ 155 trong kỳ ≈ nhập TP
    fg_in = _f(_prefix_debit(act, '155'))

    prod_orders = 0
    prod_cost = 0.0
    prod_rows = []
    try:
        if table_exists(conn, 'production_orders'):
            prod_orders = int(conn.execute(
                "SELECT COUNT(*) FROM production_orders WHERE COALESCE(status,'') NOT IN ('cancelled','draft')"
            ).fetchone()[0] or 0)
            if is_postgres():
                cost_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(COALESCE(total_cost, total_material_cost, 0)), 0)
                    FROM production_orders
                    WHERE COALESCE(status,'') NOT IN ('cancelled','draft')
                      AND EXTRACT(YEAR FROM production_date::timestamp) = ?
                      AND EXTRACT(MONTH FROM production_date::timestamp) = ?
                    """,
                    (int(fiscal_year), int(period)),
                ).fetchone()
            else:
                cost_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(COALESCE(total_cost, total_material_cost, 0)), 0)
                    FROM production_orders
                    WHERE COALESCE(status,'') NOT IN ('cancelled','draft')
                      AND strftime('%Y', production_date) = ?
                      AND CAST(strftime('%m', production_date) AS INTEGER) = ?
                    """,
                    (str(fiscal_year), int(period)),
                ).fetchone()
            prod_cost = float(cost_row[0] or 0)
            try:
                from Services.sme.production_journal import ensure_production_journal_column
                ensure_production_journal_column(conn, commit=False)
            except Exception:
                pass
            cols = table_cols(conn, 'production_orders')
            mode_expr = "COALESCE(costing_mode,'full')" if 'costing_mode' in cols else "'full'"
            jid_expr = 'journal_entry_id' if 'journal_entry_id' in cols else 'NULL'
            prod_rows = [dict(r) for r in conn.execute(
                f"""
                SELECT id, voucher_no, production_date, qty_completed,
                       COALESCE(total_material_cost,0) AS material,
                       COALESCE(labor_cost,0) AS labor,
                       COALESCE(other_cost,0) AS other,
                       COALESCE(total_cost,0) AS total_cost,
                       {mode_expr} AS costing_mode,
                       {jid_expr} AS journal_entry_id, status
                FROM production_orders
                WHERE COALESCE(status,'') NOT IN ('cancelled')
                ORDER BY production_date DESC, id DESC
                LIMIT 30
                """
            ).fetchall()]
    except sqlite3.Error:
        pass

    svc = {
        'service_wip_open': 0.0,
        'service_cogs_period': 0.0,
        'service_delivered_count': 0,
        'recent_service_jobs': [],
    }
    try:
        from Services.sme.service_costing import service_costing_period_summary
        svc = service_costing_period_summary(
            conn, fiscal_year=fiscal_year, period=period,
        )
    except Exception:
        pass

    cogs_6323 = _f(_prefix_debit(act, '6323'))

    return {
        'fiscal_year': fiscal_year,
        'period': period,
        'wip_154': wip,
        'finished_goods_155': fg,
        'materials_152': materials,
        'goods_156': goods,
        'inventory_raw_fg': _f(_money(materials) + _money(fg) + _money(goods)),
        'cp_621': cp621,
        'cp_622': cp622,
        'cp_627': cp627,
        'collected_period': collected,
        'fg_receipts_period': fg_in,
        'cogs_period': cogs_period,
        'cogs_ytd': cogs_ytd,
        'cogs_6323_period': cogs_6323,
        'materials_movement': _f(_prefix_debit(act, '152')),
        'production_orders_active': prod_orders,
        'production_cost_period': prod_cost,
        'recent_orders': prod_rows,
        'service_wip_open': svc.get('service_wip_open', 0),
        'service_cogs_period': svc.get('service_cogs_period', 0),
        'service_delivered_count': svc.get('service_delivered_count', 0),
        'recent_service_jobs': svc.get('recent_service_jobs') or [],
        'hint': 'TP: 621/622/627 → 154 → 155 → 6322. DV: 621/622/627 → 154 → (nghiệm thu) 6323',
    }
