# -*- coding: utf-8 -*-
"""Backfill + smoke: phiếu xuất kho 02-VT cho XK đã xuất kho ra cảng."""
import sqlite3
import sys
from decimal import Decimal

sys.stdout.reconfigure(encoding='utf-8')

from Services.sme.stock_vouchers import (
    ensure_phieu_xuat_kho_schema,
    get_stock_out_print_payload,
    upsert_stock_out_voucher_for_sale,
)

conn = sqlite3.connect(r'C:\SME\tenants\sme_demo.db')
conn.row_factory = sqlite3.Row
ensure_phieu_xuat_kho_schema(conn, commit=False)

sales = conn.execute(
    """
    SELECT id, sale_no, date, customer_name, address, note
    FROM sale
    WHERE UPPER(COALESCE(sale_type,'')) = 'EXPORT'
    """
).fetchall()

for s in sales:
    sid = s['id']
    existing = conn.execute(
        'SELECT id, voucher_no FROM phieu_xuat_kho WHERE sale_id = ?', (sid,)
    ).fetchone()
    if existing:
        print(f"{s['sale_no']}: already {existing['voucher_no']}")
        continue
    items = []
    total = Decimal('0')
    for it in conn.execute(
        """
        SELECT si.product_id, si.quantity, si.unit,
               COALESCE(si.cost_price, p.buyprice, 0) AS cost,
               COALESCE(si.product_name, p.name, '') AS product_name,
               COALESCE(p.product_code, p.barcode, '') AS product_code
        FROM sale_items si
        LEFT JOIN products p ON p.id = si.product_id
        WHERE si.sale_id = ?
        """,
        (sid,),
    ):
        qty = float(it['quantity'] or 0)
        cost = float(it['cost'] or 0)
        amt = qty * cost
        total += Decimal(str(amt))
        items.append({
            'product_id': it['product_id'],
            'product_name': it['product_name'],
            'product_code': it['product_code'],
            'unit': it['unit'] or 'Cái',
            'quantity': qty,
            'qty': qty,
            'price': cost,
            'amount': amt,
        })
    if not items:
        print(f"{s['sale_no']}: no items, skip")
        continue
    px = upsert_stock_out_voucher_for_sale(
        conn,
        sale_id=sid,
        sale_date=str(s['date'] or '')[:10],
        customer_name=s['customer_name'] or '',
        items=items,
        total_amount=float(total),
        note=f"Xuất kho ra cảng {s['sale_no']}",
        address=s['address'] or '',
    )
    print(f"{s['sale_no']}: created {px['voucher_no']} id={px['id']} amount={px['total_amount']}")

conn.commit()

# Verify list + print payload
from Services.sme.stock_vouchers import list_stock_out
rows = list_stock_out(conn, limit=10)
print('list_stock_out', [(r.get('voucher_no'), r.get('sale_id'), r.get('customer_name')) for r in rows[:5]])
if rows:
    payload = get_stock_out_print_payload(conn, rows[0]['id'])
    px, info = payload
    print('print hang_hoa', len(px['hang_hoa']), 'total_str', px.get('total_str'), 'note', px.get('note'))

print('OK')
