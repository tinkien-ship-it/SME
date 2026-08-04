# -*- coding: utf-8 -*-
import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
c = sqlite3.connect(r'C:\SME\tenants\sme_demo.db')
print('cols', [r[1] for r in c.execute('PRAGMA table_info(phieu_xuat_kho)')])
print('px', c.execute(
    'SELECT id,voucher_no,sale_id,substr(customer_name,1,40) FROM phieu_xuat_kho ORDER BY id DESC LIMIT 8'
).fetchall())
print('xk', c.execute(
    "SELECT id,sale_no FROM sale WHERE UPPER(COALESCE(sale_type,''))='EXPORT'"
).fetchall())
for sid in c.execute(
    "SELECT id FROM sale WHERE UPPER(COALESCE(sale_type,''))='EXPORT'"
):
    px = c.execute('SELECT id,voucher_no FROM phieu_xuat_kho WHERE sale_id=?', (sid[0],)).fetchall()
    print(' sale', sid[0], 'px', px)
