"""Báo cáo lợi nhuận điểm bán hàng — SME (B02 từ sổ nhật ký, không dùng logic HKD)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from Services.sme.bctc_lines import B02_INCOME_STATEMENT
from Services.sme.bctc_report import (
    _aggregate_leaf_amounts,
    _build_rows,
    _coa_line_map,
    _date_range_activity,
    _is_tt58_forms,
    _money,
    balance_sheet,
    income_statement,
)
from Services.sme.general_ledger import period_bounds
from Services.sme.journal_engine import ensure_sme_journal_ready

_TOLERANCE = Decimal('0.01')


def _reconcile_with_bctc(
    conn: sqlite3.Connection,
    date_from: str,
    date_to: str,
    net_profit: Decimal,
) -> dict[str, Any]:
    """So khớp LN với B02 kỳ và LN năm trên B01 khi khoảng ngày trùng kỳ."""
    checks: list[dict[str, Any]] = []
    dt_from = datetime.strptime(date_from, '%Y-%m-%d')
    dt_to = datetime.strptime(date_to, '%Y-%m-%d')
    tt58 = _is_tt58_forms(conn)

    def _add(check_id: str, label: str, expected: Decimal) -> None:
        diff = net_profit - expected
        checks.append({
            'check': check_id,
            'label': label,
            'expected': float(expected),
            'actual': float(net_profit),
            'difference': float(diff),
            'balanced': abs(diff) <= _TOLERANCE,
        })

    # Cả năm tài chính
    if (
        dt_from.month == 1 and dt_from.day == 1
        and dt_to.month == 12 and dt_to.day == 31
        and dt_from.year == dt_to.year
    ):
        fy = dt_from.year
        is_rep = income_statement(conn, fiscal_year=fy, period_from=1, period_to=12)
        b02_pat = _money(is_rep['totals']['profit_after_tax'])
        pat_code = '20' if tt58 else '60'
        _add('b02_full_year', f'B02 {fy} (LNST mã {pat_code})', b02_pat)

        bs = balance_sheet(conn, fiscal_year=fy, period_to=12)
        bs_profit = _money(bs.get('current_year_profit', 0))
        profit_line = '420' if tt58 else '421'
        _add(
            'balance_sheet_current_profit',
            f'LN năm trên B01 (chỉ tiêu {profit_line})',
            bs_profit,
        )
    elif dt_from.year == dt_to.year and dt_from.month == dt_to.month:
        # Một tháng đúng biên kỳ
        pstart, _ = period_bounds(dt_from.year, dt_from.month)
        _, pend = period_bounds(dt_to.year, dt_to.month)
        if date_from == pstart and date_to == pend:
            is_rep = income_statement(
                conn,
                fiscal_year=dt_from.year,
                period_from=dt_from.month,
                period_to=dt_to.month,
            )
            b02_pat = _money(is_rep['totals']['profit_after_tax'])
            pat_code = '20' if tt58 else '60'
            _add(
                'b02_month',
                f'B02 T{dt_from.month}/{dt_from.year} (LNST mã {pat_code})',
                b02_pat,
            )

    return {
        'checks': checks,
        'all_balanced': all(c['balanced'] for c in checks) if checks else None,
    }


def compute_sme_pos_profit_report(
    conn: sqlite3.Connection,
    date_from: str,
    date_to: str,
    *,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Tính P&L theo chỉ tiêu B02 từ phát sinh sổ nhật ký (loại KCKQ)."""
    ensure_sme_journal_ready(conn, commit=False)
    date_from = (date_from or '')[:10]
    date_to = (date_to or '')[:10]
    if not date_from or not date_to or date_from > date_to:
        raise ValueError('Khoảng ngày không hợp lệ')

    tt58 = _is_tt58_forms(conn)
    if tt58:
        from Services.sme.bctc_lines_tt58 import B02_DNSN_INCOME_STATEMENT as line_defs
        report_code = 'B02-DNSN'
        net_code, gross_code, pbt_code, pat_code = '01', '03', '03', '20'
        cogs_code = '02'
    else:
        line_defs = B02_INCOME_STATEMENT
        report_code = 'B02-DN'
        net_code, gross_code, pbt_code, pat_code = '10', '20', '50', '60'
        cogs_code = '11'

    accounts = _coa_line_map(conn)
    bal_map = _date_range_activity(
        conn, date_from, date_to,
        exclude_document_types=('KCKQ',),
        branch_code=branch_code,
    )
    leaf_vals = _aggregate_leaf_amounts(accounts, bal_map, line_defs=line_defs)
    rows = _build_rows(line_defs, leaf_vals)
    by_code = {r['code']: r['amount'] for r in rows if r['amount'] is not None}

    revenue_net = _money(by_code.get(net_code, 0))
    cogs = _money(by_code.get(cogs_code, 0))
    gross_profit = _money(by_code.get(gross_code, 0))
    profit_before_tax = _money(by_code.get(pbt_code, 0))
    net_profit = _money(by_code.get(pat_code, 0))

    if not tt58:
        op_total = (
            _money(by_code.get('22', 0))
            + _money(by_code.get('25', 0))
            + _money(by_code.get('26', 0))
            + _money(by_code.get('32', 0))
        )
        op_exp = {
            'financial': float(_money(by_code.get('22', 0))),
            'selling': float(_money(by_code.get('25', 0))),
            'admin': float(_money(by_code.get('26', 0))),
            'other_expense': float(_money(by_code.get('32', 0))),
            'tax': float(_money(by_code.get('51', 0))),
            'total': float(op_total),
        }
    else:
        op_exp = {
            'tax': float(_money(by_code.get('10', 0))),
            'total': float(_money(by_code.get('02', 0))),
        }

    reconciliation = _reconcile_with_bctc(conn, date_from, date_to, net_profit)

    b02_rows = [
        {
            'code': r['code'],
            'name': r['name'],
            'amount': float(r['amount'] or 0),
            'level': r.get('level', 0),
            'bold': bool(r.get('bold')),
            'highlight': bool(r.get('highlight')),
        }
        for r in rows
        if r.get('kind') != 'header'
    ]

    return {
        'status': 'success',
        'report_mode': 'b02',
        'form_set': 'tt58_dnsn' if tt58 else 'tt99_dn',
        'report': report_code,
        'date_from': date_from,
        'date_to': date_to,
        'revenue': float(revenue_net),
        'cogs': float(cogs),
        'gross_profit': float(gross_profit),
        'operating_expenses': op_exp,
        'profit_before_tax': float(profit_before_tax),
        'net_profit': float(net_profit),
        'b02_rows': b02_rows,
        'reconciliation': reconciliation,
        'source': 'sme_journal_b02',
    }
