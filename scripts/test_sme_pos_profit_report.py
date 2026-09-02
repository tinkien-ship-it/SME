#!/usr/bin/env python3
"""Kiểm tra báo cáo lợi nhuận SME (B02) khớp B02 kỳ và LN trên B01.

Usage:
  python scripts/test_sme_pos_profit_report.py
  python scripts/test_sme_pos_profit_report.py --db tenants/sme_demo.db --year 2026
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding='utf-8')


def run(db_path: str, year: int) -> bool:
    from Services.sme.pos_profit_report import compute_sme_pos_profit_report
    from Services.sme.bctc_report import income_statement, balance_sheet
    from Services.sme.general_ledger import period_bounds

    print(f'\n== {db_path} | year={year} ==')
    if not os.path.isfile(db_path):
        print('SKIP: DB not found')
        return True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ok = True

    date_from = f'{year}-01-01'
    date_to = f'{year}-12-31'
    rep = compute_sme_pos_profit_report(conn, date_from, date_to)
    print(
        f"POS P&L: DT={rep['revenue']:,.0f} | LNST={rep['net_profit']:,.0f} | "
        f"form={rep['report']}"
    )

    recon = rep.get('reconciliation') or {}
    checks = recon.get('checks') or []
    if not checks:
        print('WARN: no reconciliation checks (empty journal?)')
    for c in checks:
        status = 'OK' if c['balanced'] else 'FAIL'
        print(
            f"  [{status}] {c['label']}: expected={c['expected']:,.0f} "
            f"actual={c['actual']:,.0f} diff={c['difference']:,.0f}"
        )
        if not c['balanced']:
            ok = False

    tb = recon.get('trial_balance') or {}
    if tb:
        print(
            f"  BCPS: DT+TN net={tb.get('revenue_and_income', tb.get('revenue_net', 0)):,.0f} "
            f"(511 net={tb.get('revenue_net', 0):,.0f} + 711={tb.get('other_income', 0):,.0f}) | "
            f"CP={tb.get('expense_total', 0):,.0f} | "
            f"LN={tb.get('profit_before_tax', 0):,.0f}"
        )
        unmapped = tb.get('unmapped_accounts') or []
        if unmapped:
            print(f"  WARN: TK P&L chưa phân loại: {unmapped[:5]}")

    if recon.get('all_balanced') is False:
        ok = False

    # Tháng hiện tại (nếu có dữ liệu)
    import datetime as dt
    now = dt.datetime.now()
    if now.year == year:
        m = now.month
        pstart, pend = period_bounds(year, m)
        mrep = compute_sme_pos_profit_report(conn, pstart, pend)
        mrecon = mrep.get('reconciliation') or {}
        for c in mrecon.get('checks') or []:
            status = 'OK' if c['balanced'] else 'FAIL'
            print(f"  month [{status}] {c['label']}")
            if not c['balanced']:
                ok = False

    # Cross-check trực tiếp B02 vs POS
    b02 = income_statement(conn, fiscal_year=year, period_from=1, period_to=12)
    b02_pat = b02['totals']['profit_after_tax']
    diff = abs(rep['net_profit'] - b02_pat)
    if diff > 0.01:
        print(f'FAIL: POS net_profit != income_statement LNST (diff={diff:,.2f})')
        ok = False
    else:
        print(f'OK: POS net_profit == B02 LNST ({b02_pat:,.0f})')

    bs = balance_sheet(conn, fiscal_year=year, period_to=12)
    bs_pat = bs.get('current_year_profit', 0)
    diff2 = abs(rep['net_profit'] - bs_pat)
    if diff2 > 0.01:
        print(f'WARN: POS net_profit vs B01 current_year_profit diff={diff2:,.2f}')
        # Có thể lệch nếu đã KCKQ — không fail cứng
    else:
        print(f'OK: POS net_profit == B01 current_year_profit ({bs_pat:,.0f})')

    conn.close()
    return ok


def main():
    ap = argparse.ArgumentParser(description='Test SME POS profit vs B02/B01')
    ap.add_argument('--db', default='tenants/sme_demo.db')
    ap.add_argument('--year', type=int, default=None)
    args = ap.parse_args()

    db_path = args.db if os.path.isabs(args.db) else str(ROOT / args.db)
    year = args.year
    if year is None:
        import datetime as dt
        year = dt.datetime.now().year

    ok = run(db_path, year)
    if not ok:
        sys.exit(1)
    print('\nAll checks passed.')


if __name__ == '__main__':
    main()
