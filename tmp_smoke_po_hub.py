# -*- coding: utf-8 -*-
import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')
from Services.sme.bootstrap import ensure_sme_accounting_ready
from Services.sme.purchase_order import (
    create_purchase_order, build_import_draft_from_po, apply_po_receipt, purchasing_hub_metrics
)
from Services.sme.dashboard_metrics import debt_hub_metrics

conn = sqlite3.connect(':memory:')
ensure_sme_accounting_ready(conn, accounting_regime='SME_TT99')
po = create_purchase_order(
    conn,
    po_date='2026-07-01',
    supplier_name='NCC A',
    lines=[
        {'product_name': 'Hang A', 'unit': 'cai', 'qty': 10, 'unit_price': 1000},
        {'product_name': 'Hang B', 'unit': 'hop', 'qty': 5, 'unit_price': 2000},
    ],
    status='confirmed',
    created_by='test',
)
conn.commit()
draft = build_import_draft_from_po(conn, po['id'])
assert len(draft['items']) == 2
assert draft['items'][0]['qty'] == 10
upd = apply_po_receipt(conn, po['id'], [{'product_name': 'Hang A', 'qty': 4}], import_id=99)
assert upd['status'] == 'partial'
assert abs(upd['lines'][0]['received_qty'] - 4) < 0.01
draft2 = build_import_draft_from_po(conn, po['id'])
assert abs(draft2['items'][0]['qty'] - 6) < 0.01
upd2 = apply_po_receipt(
    conn,
    po['id'],
    [{'product_name': 'Hang A', 'qty': 6}, {'product_name': 'Hang B', 'qty': 5}],
    import_id=100,
)
assert upd2['status'] == 'received'
m = purchasing_hub_metrics(conn, fiscal_year=2026, period_to=7)
assert m['pending_orders'] == 0
d = debt_hub_metrics(conn, fiscal_year=2026, period_to=7)
assert 'receivable' in d and 'payable' in d
print('OK smoke PO receipt + hubs')
