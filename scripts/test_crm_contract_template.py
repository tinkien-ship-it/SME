# -*- coding: utf-8 -*-
import sqlite3

from Services import crm_contract_template as tpl
from Services import crm_ops
from Services.crm_schema import ensure_crm_schema

assert not tpl.validate_template(tpl.DEFAULT_TEMPLATE_HTML)

ctx = {
    'CONTRACT_NO': 'HD20260001',
    'DAY': '29',
    'MONTH': '08',
    'YEAR': '2026',
    'PLACE': 'HCM',
    'SELLER_NAME': 'Cong ty A',
    'SELLER_TAX': '01',
    'SELLER_ADDRESS': 'x',
    'SELLER_PHONE': '1',
    'SELLER_EMAIL': 'a@b.c',
    'SELLER_BANK': '123',
    'SELLER_BANK_NAME': 'VCB',
    'SELLER_REP': 'Nguyen Van A',
    'SELLER_TITLE': 'GD',
    'BUYER_NAME': 'KH B',
    'BUYER_TAX': '02',
    'BUYER_ADDRESS': 'y',
    'BUYER_PHONE': '2',
    'BUYER_EMAIL': '',
    'BUYER_REP': 'B',
    'BUYER_TITLE': 'GD',
    'SUBTOTAL': '1.000',
    'VAT_AMOUNT': '100',
    'TOTAL': '1.100',
    'TOTAL_WORDS': 'mot nghin',
    'PAYMENT_METHOD': 'CK',
    'PAYMENT_TERM': '7 ngay',
    'DELIVERY_SCHEDULE': '1 tuan',
    'DELIVERY_PLACE': 'kho',
    'SHIPPING_PARTY': 'A',
    'WARRANTY_MONTHS': '12',
    'QUALITY_NOTES': 'ok',
    'PACKAGING_NOTES': 'ok',
    'NOTES': '',
    '_ITEMS': [{
        'product_name': 'Hang 1',
        'unit': 'cai',
        'qty': 2,
        'unit_price': 500,
        'tax_rate': 10,
        'line_subtotal': 1000,
        'vat_amount': 100,
        'line_total': 1100,
    }],
}
out = tpl.fill_template(tpl.DEFAULT_TEMPLATE_HTML, ctx)
assert 'Hang 1' in out and 'HD20260001' in out and '[[CONTRACT_NO]]' not in out
print('fill ok', len(tpl.extract_placeholders(tpl.DEFAULT_TEMPLATE_HTML)))

conn = sqlite3.connect(':memory:')
conn.row_factory = sqlite3.Row
conn.execute(
    'CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, company_name TEXT, '
    'tax_code TEXT, address TEXT, phone TEXT, email TEXT)'
)
conn.execute(
    "INSERT INTO customers VALUES (1,'KH','Cong ty KH','010','addr','090','a@a.a')"
)
ensure_crm_schema(conn)
cid = crm_ops.upsert_contract(conn, {
    'customer_id': 1,
    'title': 'Test',
    'items': [{'product_name': 'SP1', 'unit': 'cai', 'qty': 2, 'unit_price': 100000, 'tax_rate': 10}],
})
row = crm_ops.get_contract(conn, cid)
assert row and len(row['items']) == 1
assert abs(row['amount'] - 220000) < 1
print('contract', row['contract_no'], row['amount'], row['tax_amount'])
html = tpl.render_contract_html(conn, row)
assert 'SP1' in html and '10%' in html
print('render ok', len(html))

# import roundtrip
tpl.set_template_html(conn, tpl.DEFAULT_TEMPLATE_HTML.replace('Điều 7', 'Điều 7 (tuỳ chỉnh)'))
assert 'tuỳ chỉnh' in tpl.get_template_html(conn)
print('template save ok')
