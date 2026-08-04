# -*- coding: utf-8 -*-
import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')

from Services.sme.coa_service import ensure_sme_coa_ready
from Services.sme.export_payment import ensure_export_sale_schema
from Services.sme.export_clearance import (
    sync_export_ship_journals, sync_export_clearance_journals, EXPORT_STATUS_SHIPPED,
)
from Services.sme.journal_engine import ensure_sme_journal_ready, resolve_postable_account
from Services.sme.account_roles import ensure_account_roles_ready

conn = sqlite3.connect(':memory:')
ensure_sme_coa_ready(conn)
ensure_account_roles_ready(conn)
ensure_sme_journal_ready(conn)

# Minimal sale + stock for export
conn.execute("""
CREATE TABLE IF NOT EXISTS sale (
  id INTEGER PRIMARY KEY, sale_no TEXT, date TEXT, sale_type TEXT,
  customer_name TEXT, currency TEXT, exchange_rate REAL, customs_fx_rate REAL,
  amount_fc REAL, payment_mode TEXT, export_tax_vnd REAL DEFAULT 0,
  customs_decl_no TEXT, bl_no TEXT, risk_transfer_date TEXT,
  export_status TEXT, warehouse_code TEXT, total_amount REAL, status TEXT,
  advance_fc REAL DEFAULT 0, advance_vnd REAL DEFAULT 0
)
""")
from Services.sme.export_payment import _export_schema_ready
_export_schema_ready.clear()
ensure_export_sale_schema(conn)
conn.execute("""
CREATE TABLE IF NOT EXISTS stock_moves (
  id INTEGER PRIMARY KEY, product_id INTEGER, date TEXT, type TEXT,
  ref_id INTEGER, quantity REAL, cost_price REAL
)
""")
conn.execute("""
CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, product_type TEXT)
""")
conn.execute("INSERT INTO products VALUES (1, 'goods')")
conn.execute("""
INSERT INTO sale (sale_no, date, sale_type, customer_name, currency, exchange_rate,
  customs_fx_rate, amount_fc, payment_mode, export_status, total_amount, status)
VALUES ('XK000001','2026-08-01','EXPORT','Buyer','USD',25000,25000,100,'unpaid','shipped',2500000,'completed')
""")
sale_id = 1
conn.execute(
    "INSERT INTO stock_moves (product_id,date,type,ref_id,quantity,cost_price) VALUES (1,'2026-08-01','EXPORT_SHIP',1,-10,50000)"
)
conn.commit()

print('157', resolve_postable_account(conn, 'inv.consignment'))
print('6321', resolve_postable_account(conn, 'cogs.goods.export'))

ship = sync_export_ship_journals(conn, sale_id, created_by='t')
print('ship', ship)

conn.execute("UPDATE sale SET customs_decl_no='TK123', bl_no='BL999', risk_transfer_date='2026-08-05' WHERE id=1")
clr = sync_export_clearance_journals(conn, sale_id, created_by='t')
print('clearance', {k: clr[k] for k in ('posted','phase','export_status','revenue_vnd','entry_ids')})

# Verify journal accounts
rows = conn.execute("""
  SELECT je.document_type, jl.account_code, jl.debit, jl.credit
  FROM sme_journal_entries je
  JOIN sme_journal_lines jl ON jl.entry_id = je.id
  WHERE je.document_id=1 AND je.status='posted' AND je.reverses_id IS NULL
  ORDER BY je.id, jl.sequence
""").fetchall()
for r in rows:
    print(tuple(r))
print('OK')
