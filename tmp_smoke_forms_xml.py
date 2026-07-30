# -*- coding: utf-8 -*-
"""Smoke: VAT XML + 01-BH / 02-BH sale forms."""
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Services.sme.bootstrap import ensure_sme_accounting_ready
from Services.sme.journal_engine import post_journal_entry
from Services.sme.vat_xml import generate_sme_vat_xml
from Services.sme.sale_forms import form_01_bh, form_02_bh, list_sale_customers, list_products_brief


def _post(conn, *, date, lines, doc_type='TEST', doc_no='T1', doc_id=1):
    return post_journal_entry(
        conn,
        posting_date=date,
        document_date=date,
        document_type=doc_type,
        document_no=doc_no,
        document_id=doc_id,
        business_type=doc_type,
        description='smoke forms',
        created_by='smoke',
        lines=lines,
    )


conn = sqlite3.connect(':memory:')
conn.row_factory = sqlite3.Row
ensure_sme_accounting_ready(conn, commit=True)

# business_info for XML header
conn.execute(
    """CREATE TABLE IF NOT EXISTS business_info (
        id INTEGER PRIMARY KEY, business_name TEXT, tax_code TEXT,
        address TEXT, phone TEXT, email TEXT, representative_name TEXT
    )"""
)
conn.execute(
    "INSERT INTO business_info (business_name, tax_code, address, representative_name) VALUES (?,?,?,?)",
    ('DN Smoke', '0312345678', 'HCM', 'Nguyen Van A'),
)

_post(conn, date='2026-07-15', doc_type='BAN', doc_no='B1', doc_id=1, lines=[
    {'sequence': 1, 'account_code': '131', 'debit': 1100000, 'credit': 0, 'description': 'PT'},
    {'sequence': 2, 'account_code': '5111', 'debit': 0, 'credit': 1000000, 'description': 'DT'},
    {'sequence': 3, 'account_code': '33311', 'debit': 0, 'credit': 100000, 'description': 'VAT out'},
])
_post(conn, date='2026-07-16', doc_type='MUA', doc_no='M1', doc_id=2, lines=[
    {'sequence': 1, 'account_code': '156', 'debit': 500000, 'credit': 0, 'description': 'HH'},
    {'sequence': 2, 'account_code': '13311', 'debit': 50000, 'credit': 0, 'description': 'VAT in'},
    {'sequence': 3, 'account_code': '331', 'debit': 0, 'credit': 550000, 'description': 'NCC'},
])
conn.commit()

xml_res = generate_sme_vat_xml(conn, fiscal_year=2026, period=7, filing_mode='monthly')
assert 'HSoThueDTu' in xml_res['xml']
assert '<ct21>' in xml_res['xml']
assert '<ct25>' in xml_res['xml']
assert '0312345678' in xml_res['xml']
assert xml_res['filename'].endswith('.xml')
print('OK xml', xml_res['filename'])

# Sale forms tables
conn.execute("CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, product_code TEXT, name TEXT, unit TEXT, price REAL)")
conn.execute("CREATE TABLE IF NOT EXISTS inventory (product_id INTEGER PRIMARY KEY, quantity REAL)")
conn.execute("""CREATE TABLE IF NOT EXISTS sale (
    id INTEGER PRIMARY KEY, date TEXT, total_amount REAL, customer_name TEXT, status TEXT
)""")
conn.execute("""CREATE TABLE IF NOT EXISTS sale_items (
    id INTEGER PRIMARY KEY, sale_id INTEGER, product_id INTEGER, quantity REAL, price REAL
)""")
conn.execute("""CREATE TABLE IF NOT EXISTS stock_moves (
    id INTEGER PRIMARY KEY, product_id INTEGER, date TEXT, type TEXT,
    ref_document TEXT DEFAULT '', in_quantity REAL, out_quantity REAL, quantity REAL, total_value REAL DEFAULT 0
)""")
conn.execute("INSERT INTO products (id, product_code, name, unit, price) VALUES (1,'SP01','Hang A','Cai',10000)")
conn.execute("INSERT INTO inventory (product_id, quantity) VALUES (1, 40)")
conn.execute("INSERT INTO sale (id, date, total_amount, customer_name, status) VALUES (1,'2026-07-10',50000,'Dai ly ABC','completed')")
conn.execute("INSERT INTO sale_items (sale_id, product_id, quantity, price) VALUES (1,1,5,10000)")
conn.execute("INSERT INTO stock_moves (product_id, date, type, in_quantity, out_quantity, quantity, total_value) VALUES (1,'2026-07-01','IMPORT',50,0,50,0)")
conn.execute("INSERT INTO stock_moves (product_id, date, type, in_quantity, out_quantity, quantity, total_value) VALUES (1,'2026-07-10','SALE',0,5,5,0)")
conn.commit()

custs = list_sale_customers(conn)
assert any(c['name'] == 'Dai ly ABC' for c in custs), custs
prods = list_products_brief(conn)
assert prods and prods[0]['name'] == 'Hang A'

f01 = form_01_bh(conn, agent_name='Dai ly ABC', date_from='2026-07-01', date_to='2026-07-31')
assert f01['lines'] and f01['lines'][0]['qty_sold'] == 5
assert f01['totals']['sold_amount'] == 50000

f02 = form_02_bh(conn, product_id=1, date_from='2026-07-01', date_to='2026-07-31')
assert f02['days'] and f02['closing_qty'] == 45  # 50 in - 5 out
print('OK 01-BH lines', len(f01['lines']), '02-BH days', len(f02['days']))

# Templates exist
root = Path(r'C:\SME\templates\KeToanSME')
assert (root / 'form_01_bh.html').exists()
assert (root / 'form_02_bh.html').exists()
assert 'btnXml' in (root / 'vat_declaration.html').read_text(encoding='utf-8')
assert 'SME_form_01_bh' in (root / '_sidebar.html').read_text(encoding='utf-8')

print('ALL PASSED forms + xml')
