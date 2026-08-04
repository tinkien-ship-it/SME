# -*- coding: utf-8 -*-
import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
from Services.sme.journal_engine import (
    post_journal_entry, delete_journal_entry, ensure_sme_journal_ready,
    resolve_postable_account,
)
from Services.sme.journal_cascade import delete_stock_out_voucher
from Services.sme.stock_vouchers import upsert_stock_out_voucher_for_sale

conn = sqlite3.connect(r'C:\SME\tenants\sme_demo.db')
conn.row_factory = sqlite3.Row
ensure_sme_journal_ready(conn, commit=False)

# Fake sale id unlikely to collide
sale_id = 980001
conn.execute('DELETE FROM phieu_xuat_kho WHERE sale_id=?', (sale_id,))
conn.execute(
    "DELETE FROM sme_journal_lines WHERE entry_id IN (SELECT id FROM sme_journal_entries WHERE document_id=?)",
    (sale_id,),
)
conn.execute('DELETE FROM sme_journal_entries WHERE document_id=?', (sale_id,))

a157 = resolve_postable_account(conn, '157')
a156 = resolve_postable_account(conn, '156')
entry = post_journal_entry(
    conn,
    posting_date='2026-08-04',
    document_date='2026-08-04',
    document_type='EXPORT_SHIP',
    document_no='XKTEST',
    document_id=sale_id,
    business_type='XUAT_KHO_CANG',
    currency='VND',
    exchange_rate=1,
    description='Test ship',
    created_by='test',
    lines=[
        {'sequence': 1, 'account_code': a157, 'debit': 1000, 'credit': 0, 'description': '157'},
        {'sequence': 2, 'account_code': a156, 'debit': 0, 'credit': 1000, 'description': '156'},
    ],
)
px = upsert_stock_out_voucher_for_sale(
    conn, sale_id=sale_id, sale_date='2026-08-04', customer_name='T',
    items=[{'product_id': 1, 'product_name': 'A', 'product_code': 'A', 'unit': 'Cái',
            'quantity': 1, 'qty': 1, 'price': 1000, 'amount': 1000}],
    total_amount=1000, note='test',
)
print('created', entry['entry_no'], px['voucher_no'])

# Direction A: delete journal → PX gone
r = delete_journal_entry(conn, entry['id'], reason='test A', deleted_by='test')
print('A cascade', r['cascade']['stock_out_deleted'], r['cascade']['vouchers_deleted'])
px_left = conn.execute('SELECT id FROM phieu_xuat_kho WHERE sale_id=?', (sale_id,)).fetchall()
je_left = conn.execute('SELECT id FROM sme_journal_entries WHERE id=?', (entry['id'],)).fetchall()
print('A px_left', px_left, 'je_left', je_left)
assert not px_left and not je_left

# Recreate for direction B
entry2 = post_journal_entry(
    conn,
    posting_date='2026-08-04',
    document_date='2026-08-04',
    document_type='EXPORT_SHIP',
    document_no='XKTEST2',
    document_id=sale_id,
    business_type='XUAT_KHO_CANG',
    currency='VND',
    exchange_rate=1,
    description='Test ship 2',
    created_by='test',
    lines=[
        {'sequence': 1, 'account_code': a157, 'debit': 2000, 'credit': 0},
        {'sequence': 2, 'account_code': a156, 'debit': 0, 'credit': 2000},
    ],
)
px2 = upsert_stock_out_voucher_for_sale(
    conn, sale_id=sale_id, sale_date='2026-08-04', customer_name='T',
    items=[{'product_id': 1, 'product_name': 'A', 'product_code': 'A', 'unit': 'Cái',
            'quantity': 1, 'qty': 1, 'price': 2000, 'amount': 2000}],
    total_amount=2000, note='test2',
)
print('created B', entry2['entry_no'], px2['voucher_no'])
r2 = delete_stock_out_voucher(conn, px2['id'], reason='test B', deleted_by='test')
print('B', r2['message'], 'journals', r2['journals_deleted'])
px_left2 = conn.execute('SELECT id FROM phieu_xuat_kho WHERE sale_id=?', (sale_id,)).fetchall()
je_left2 = conn.execute(
    'SELECT id FROM sme_journal_entries WHERE document_id=? AND status="posted"', (sale_id,)
).fetchall()
print('B left', px_left2, je_left2)
assert not px_left2 and not je_left2

# Block: clearance present
entry3 = post_journal_entry(
    conn, posting_date='2026-08-04', document_date='2026-08-04',
    document_type='EXPORT_SHIP', document_no='XKTEST3', document_id=sale_id,
    business_type='XUAT_KHO_CANG', currency='VND', exchange_rate=1, description='t3',
    created_by='test',
    lines=[
        {'sequence': 1, 'account_code': a157, 'debit': 500, 'credit': 0},
        {'sequence': 2, 'account_code': a156, 'debit': 0, 'credit': 500},
    ],
)
a131 = resolve_postable_account(conn, '131')
a511 = resolve_postable_account(conn, '5111')
post_journal_entry(
    conn, posting_date='2026-08-04', document_date='2026-08-04',
    document_type='EXPORT_REVENUE', document_no='XKTEST3', document_id=sale_id,
    business_type='XUAT_KHAU_DT', currency='VND', exchange_rate=1, description='rev',
    created_by='test',
    lines=[
        {'sequence': 1, 'account_code': a131, 'debit': 500, 'credit': 0},
        {'sequence': 2, 'account_code': a511, 'debit': 0, 'credit': 500},
    ],
)
px3 = upsert_stock_out_voucher_for_sale(
    conn, sale_id=sale_id, sale_date='2026-08-04', customer_name='T',
    items=[{'product_id': 1, 'product_name': 'A', 'product_code': 'A', 'unit': 'Cái',
            'quantity': 1, 'qty': 1, 'price': 500, 'amount': 500}],
    total_amount=500, note='t3',
)
try:
    delete_journal_entry(conn, entry3['id'], reason='should fail')
    print('FAIL: should have blocked')
except ValueError as e:
    print('blocked ship delete OK:', e)
try:
    delete_stock_out_voucher(conn, px3['id'], reason='should fail')
    print('FAIL: should have blocked PX')
except ValueError as e:
    print('blocked PX delete OK:', e)

conn.rollback()
print('ALL OK')
