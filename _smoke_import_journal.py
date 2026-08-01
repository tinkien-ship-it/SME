"""Smoke-test DOMESTIC vs IMPORT journal line builder."""
import sqlite3
from decimal import Decimal
from Services.sme.bootstrap import ensure_sme_accounting_ready
from Services.sme.journal_engine import build_import_stock_lines

conn = sqlite3.connect(':memory:')
conn.row_factory = sqlite3.Row
ensure_sme_accounting_ready(conn, commit=True)

inv = [{
    'product_id': 1,
    'product_name': 'Hang A',
    'amount': Decimal('1000000'),
    'tax_pct': 10,
    'warehouse_code': 'KHO_001',
}]

_, lines_d = build_import_stock_lines(
    conn, business_type='NHAP_KHO_HANG_HOA', payment_method='CREDIT',
    inventory_lines=inv, vat_amount=100000, import_tax_amount=0,
    payable_amount=1100000, import_type='DOMESTIC', bill_no='HD1',
)
print('DOMESTIC:')
for L in lines_d:
    print(f"  {L['account_code']}: Dr={L['debit']} Cr={L['credit']}")
dr = sum(Decimal(str(L['debit'])) for L in lines_d)
cr = sum(Decimal(str(L['credit'])) for L in lines_d)
assert dr == cr, (dr, cr)

inv_i = [{**inv[0], 'amount': Decimal('1100000')}]
_, lines_i = build_import_stock_lines(
    conn, business_type='NHAP_KHO_HANG_HOA', payment_method='CREDIT',
    inventory_lines=inv_i, vat_amount=110000, import_tax_amount=100000,
    payable_amount=1110000, import_type='IMPORT', bill_no='HD2',
)
print('IMPORT:')
for L in lines_i:
    print(f"  {L['account_code']}: Dr={L['debit']} Cr={L['credit']}")
dr = sum(Decimal(str(L['debit'])) for L in lines_i)
cr = sum(Decimal(str(L['credit'])) for L in lines_i)
assert dr == cr, (dr, cr)
codes = {L['account_code'] for L in lines_i}
assert any(c.startswith('333') for c in codes), codes
assert any(c.startswith('133') for c in codes), codes
print('BALANCE OK')
