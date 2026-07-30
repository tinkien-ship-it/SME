# -*- coding: utf-8 -*-
"""CLI: tạo tenant demo SME + dữ liệu mẫu sổ/báo cáo.

Usage:
  python scripts/seed_sme_sample.py
  python scripts/seed_sme_sample.py --year 2026 --close-through 6
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description='Seed dữ liệu mẫu kế toán SME')
    ap.add_argument('--tenant-id', default='sme_demo')
    ap.add_argument('--username', default='0909000111', help='SĐT đăng nhập (= username tenant)')
    ap.add_argument('--password', default='admin123')
    ap.add_argument('--year', type=int, default=None)
    ap.add_argument('--close-through', type=int, default=None, help='Kỳ cuối chạy QTGT+KCKQ')
    ap.add_argument('--no-force', action='store_true', help='Không xóa DB demo cũ')
    ap.add_argument('--db-only', action='store_true', help='Chỉ seed vào --db, không init tenant')
    ap.add_argument('--db', default='', help='Đường dẫn DB khi --db-only')
    args = ap.parse_args()

    from Services.sme.sample_data import register_demo_tenant, seed_sample_journals
    from Services.sme.bctc_report import balance_sheet, income_statement, cash_flow_statement
    from Services.sme.dashboard_metrics import dashboard_metrics, sales_hub_metrics, books_hub_metrics
    from Services.sme.vat_declaration import vat_declaration_worksheet
    from Services.sme.general_ledger import trial_balance

    year = args.year or datetime.now().year

    if args.db_only:
        abs_db = args.db if os.path.isabs(args.db) else str(ROOT / (args.db or f'tenants/{args.tenant_id}.db'))
        os.makedirs(os.path.dirname(abs_db) or '.', exist_ok=True)
        conn = sqlite3.connect(abs_db)
        conn.row_factory = sqlite3.Row
        summary = seed_sample_journals(
            conn, fiscal_year=year, close_through=args.close_through, commit=True,
        )
        conn.close()
        result = {
            'tenant_id': args.tenant_id,
            'db_path': abs_db,
            'username': args.username,
            'password': args.password,
            'seed': summary,
        }
    else:
        result = register_demo_tenant(
            tenant_id=args.tenant_id,
            username=args.username,
            password=args.password,
            force=not args.no_force,
            fiscal_year=year,
            close_through=args.close_through,
        )

    seed = result['seed']
    print('=== SEED SME SAMPLE ===')
    print('DB:', result['db_path'])
    print('Tenant:', result.get('tenant_id') or args.tenant_id)
    print('Login:', result['username'], '/', result['password'])
    print(
        'Year:', seed['fiscal_year'],
        '| sample entries:', seed['posted_sample_entries'],
        '| close through:', seed['close_through'],
    )

    conn = sqlite3.connect(result['db_path'])
    conn.row_factory = sqlite3.Row
    y = seed['fiscal_year']
    pt = max(1, seed['close_through'] or datetime.now().month)
    b01 = balance_sheet(conn, fiscal_year=y, period_to=pt)
    b02 = income_statement(conn, fiscal_year=y, period_from=1, period_to=pt)
    b03 = cash_flow_statement(conn, fiscal_year=y, period_from=1, period_to=pt)
    dash = dashboard_metrics(conn, fiscal_year=y, period_to=pt)
    sales = sales_hub_metrics(conn, fiscal_year=y, period_to=pt)
    books = books_hub_metrics(conn, fiscal_year=y, period_to=pt)
    vat = vat_declaration_worksheet(conn, fiscal_year=y, period=pt, filing_mode='monthly')
    tb = trial_balance(conn, fiscal_year=y, period_from=1, period_to=pt)

    print('\n--- VERIFY REPORTS ---')
    print(
        f"B01 assets={b01['totals']['total_assets']:,.0f} | "
        f"equity+liab={b01['totals']['total_equity_and_liabilities']:,.0f} | "
        f"balanced={b01['totals']['balanced']} | diff={b01['totals']['difference']:,.0f}"
    )
    print(
        f"B02 DT={b02['totals']['revenue_net']:,.0f} | "
        f"LG={b02['totals']['gross_profit']:,.0f} | "
        f"LNST={b02['totals']['profit_after_tax']:,.0f}"
    )
    totals3 = b03.get('totals') or {}
    print(
        f"B03 open={totals3.get('cash_opening', 0):,.0f} | "
        f"close={totals3.get('cash_closing', 0):,.0f} | "
        f"Δ={totals3.get('net_change', 0):,.0f} | balanced={totals3.get('balanced')}"
    )
    print(f"Dashboard revenue={dash.get('revenue')} cash={dash.get('cash')} profit={dash.get('profit')}")
    print(f"Sales hub revenue={sales['revenue']} AR={sales['receivable']} orders_ops={seed['ops']}")
    print(f"Books entries={books['entry_count']} debit={books['period_debit']} balanced={books['balanced']}")
    print(f"VAT T{pt}: out={vat['summary']['vat_output']} in={vat['summary']['vat_input']} pay={vat['summary']['vat_payable']}")
    print(f"CĐPS rows={len(tb.get('rows') or [])}")
    conn.close()
    print('\nXong. Đăng nhập SĐT', result['username'], '/ MK', result['password'],
          '→ mở SME: Dashboard, Nhật ký, Sổ cái, BCTC B01–B03, Tờ khai GTGT, 01-BH/02-BH.')


if __name__ == '__main__':
    main()
