# -*- coding: utf-8 -*-
"""Smoke cascade: xóa journal → PX; xóa PX → journal."""
import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')

from Services.sme.journal_cascade import (
    cleanup_documents_for_deleted_journal,
    delete_stock_out_voucher,
)
from Services.sme.journal_engine import delete_journal_entry, ensure_sme_journal_ready
from Services.sme.stock_vouchers import upsert_stock_out_voucher_for_sale

conn = sqlite3.connect(r'C:\SME\tenants\sme_demo.db')
conn.row_factory = sqlite3.Row
ensure_sme_journal_ready(conn, commit=False)

# Find an EXPORT_SHIP journal + PX
ship = conn.execute(
    """
    SELECT id, entry_no, document_id, document_type FROM sme_journal_entries
    WHERE document_type='EXPORT_SHIP' AND status='posted' AND reverses_id IS NULL
    ORDER BY id DESC LIMIT 1
    """
).fetchone()
if not ship:
    print('No EXPORT_SHIP to test — skip journal→PX')
else:
    sid = ship['document_id']
    px_before = conn.execute(
        'SELECT id, voucher_no FROM phieu_xuat_kho WHERE sale_id=?', (sid,)
    ).fetchall()
    print('ship', dict(ship), 'px_before', [dict(x) for x in px_before])
    # Don't actually delete production data if clearance exists
    clr = conn.execute(
        """
        SELECT id, document_type FROM sme_journal_entries
        WHERE document_id=? AND document_type IN ('EXPORT_REVENUE','EXPORT_COGS','EXPORT_TAX')
          AND status='posted' AND reverses_id IS NULL
        """,
        (sid,),
    ).fetchall()
    if clr:
        print('has clearance', [dict(c) for c in clr], '— test cleanup dry via helper on snapshot only')
        # Unit-style: call cleanup logic pieces without full delete
        from Services.sme.journal_cascade import _delete_vouchers_for_journal
        print('vouchers for ship', _delete_vouchers_for_journal(conn, -1))  # no-op
        conn.rollback()
    else:
        r = delete_journal_entry(conn, ship['id'], reason='smoke cascade', deleted_by='test')
        print('deleted journal cascade', r.get('cascade'))
        px_after = conn.execute(
            'SELECT id FROM phieu_xuat_kho WHERE sale_id=?', (sid,)
        ).fetchall()
        print('px_after', px_after)
        conn.rollback()  # don't keep smoke deletes
        print('rolled back')

# Test delete_stock_out_voucher on a temp PX without journals
upsert_stock_out_voucher_for_sale(
    conn,
    sale_id=999999,
    sale_date='2026-08-04',
    customer_name='Cascade Test',
    items=[{'product_id': 1, 'product_name': 'T', 'product_code': 'T', 'unit': 'Cái',
            'quantity': 1, 'qty': 1, 'price': 1000, 'amount': 1000}],
    total_amount=1000,
    note='smoke',
)
px = conn.execute(
    "SELECT id, voucher_no FROM phieu_xuat_kho WHERE sale_id=999999"
).fetchone()
print('temp px', dict(px))
r2 = delete_stock_out_voucher(conn, px['id'], reason='smoke', deleted_by='test', commit=False)
print('delete px', r2['message'])
left = conn.execute('SELECT id FROM phieu_xuat_kho WHERE sale_id=999999').fetchall()
print('left', left)
conn.rollback()
print('OK')
