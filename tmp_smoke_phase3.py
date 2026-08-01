import os
import sqlite3
import tempfile

from Services.sme.bootstrap import ensure_sme_accounting_ready
from Services.sme.production_journal import post_production_journal
from Services.sme.cit_declaration import cit_declaration_worksheet
from Services.sme.cit_xml import generate_sme_cit_xml
from Services.sme.capital import contribute_capital, declare_dividend, pay_dividend
from Services.sme.vouchers import create_receipt

path = tempfile.mktemp(suffix='.db')
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
ensure_sme_accounting_ready(conn, accounting_regime='SME_TT99', commit=True)
create_receipt(
    conn, voucher_date='2026-01-15', party_name='KH', amount=50_000_000,
        credit_account='5111', commit=True,
)

conn.execute(
    """
    CREATE TABLE IF NOT EXISTS production_orders (
        id INTEGER PRIMARY KEY, voucher_no TEXT, production_date TEXT,
        finished_product_id INTEGER, total_material_cost REAL, labor_cost REAL,
        other_cost REAL, total_cost REAL, journal_entry_id INTEGER, status TEXT,
        costing_mode TEXT
    )
    """
)
conn.execute(
    """
    INSERT INTO production_orders (
        id, voucher_no, production_date, finished_product_id,
        total_material_cost, labor_cost, other_cost, total_cost, status
    ) VALUES (1,'SX000001','2026-07-10',1, 2000000, 500000, 300000, 2800000, 'completed')
    """
)
conn.commit()
order = dict(conn.execute('SELECT * FROM production_orders WHERE id=1').fetchone())
r = post_production_journal(conn, order, created_by='test', costing_mode='full', commit=True)
print('prod_steps', r.get('steps'), 'fg', r.get('journal_entry_id'))
n = conn.execute(
    "SELECT COUNT(*) FROM sme_journal_entries WHERE document_no LIKE 'SX000001%'"
).fetchone()[0]
print('journal_count', n)

ws = cit_declaration_worksheet(conn, fiscal_year=2026, period_to=12)
print('cit_due', ws['cit_due'], 'profit', ws['accounting_profit'])
xml = generate_sme_cit_xml(conn, fiscal_year=2026, period_to=12)
print('xml_ok', len(xml['xml']) > 100, xml['filename'])

cap = contribute_capital(
    conn, doc_date='2026-02-01', amount=100_000_000, party_name='Owner', commit=True,
)
div = declare_dividend(conn, doc_date='2026-12-31', amount=5_000_000, commit=True)
pay = pay_dividend(conn, doc_date='2026-12-31', amount=5_000_000, commit=True)
print('capital', cap['doc_no'], div['doc_no'], pay['doc_no'])
print('ALL_OK')
conn.close()
os.remove(path)
