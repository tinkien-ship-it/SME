# -*- coding: utf-8 -*-
import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')
from Services.sme.bootstrap import ensure_sme_accounting_ready
from Services.sme.dashboard_metrics import (
    warehouse_hub_metrics, fixed_asset_hub_metrics, tools_hub_metrics, hr_hub_metrics,
)
from Services.sme.purchase_order import create_purchase_order

conn = sqlite3.connect(':memory:')
ensure_sme_accounting_ready(conn, accounting_regime='SME_TT99')
w = warehouse_hub_metrics(conn, fiscal_year=2026, period_to=7)
assert 'inventory_total' in w and 'monthly' in w
fa = fixed_asset_hub_metrics(conn, fiscal_year=2026, period_to=7)
assert 'gross_cost' in fa and 'active_count' in fa
t = tools_hub_metrics(conn, fiscal_year=2026, period_to=7)
assert 'balance' in t
h = hr_hub_metrics(conn, fiscal_year=2026, period_to=7)
assert 'salary_payable' in h
po = create_purchase_order(
    conn, po_date='2026-07-01', supplier_name='NCC',
    lines=[{'product_id': 1, 'product_code': 'SP01', 'product_name': 'Hang', 'qty': 2, 'unit_price': 500}],
    status='draft',
)
assert po['lines'][0].get('product_id') == 1
assert po['lines'][0].get('product_code') == 'SP01'
print('OK hubs + PO product fields')
