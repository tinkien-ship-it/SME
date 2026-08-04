# -*- coding: utf-8 -*-
import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
c = sqlite3.connect(r'C:\SME\tenants\sme_demo.db')
c.row_factory = sqlite3.Row
c.execute('PRAGMA foreign_keys=ON')
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
for t in tables:
    for fk in c.execute(f'PRAGMA foreign_key_list("{t}")'):
        if fk[2] == 'sale':
            print(f'{t}.{fk[3]} -> sale.{fk[4]}')
s = c.execute('SELECT id,status,sale_no,sale_type FROM sale WHERE id=9115').fetchone()
print('sale9115', dict(s) if s else None)
for tbl, col in [
    ('sale_items', 'sale_id'),
    ('phieu_xuat_kho', 'sale_id'),
    ('cong_no', 'sale_id'),
    ('phieu_thu', 'sale_id'),
    ('stock_moves', 'ref_id'),
    ('sme_journal_entries', 'document_id'),
    ('sme_vouchers', 'source_id'),
    ('sme_sale_advances', 'sale_id'),
]:
    try:
        n = c.execute(f'SELECT COUNT(*) FROM "{tbl}" WHERE {col}=?', (9115,)).fetchone()[0]
        if n:
            print(tbl, n)
            if tbl == 'sale_items':
                print('  sample', c.execute('SELECT * FROM sale_items WHERE sale_id=9115 LIMIT 2').fetchall())
    except Exception as e:
        print(tbl, 'err', e)

# Simulate delete with FK on
try:
    c.execute('DELETE FROM sale WHERE id=9115')
    print('delete ok? unexpected')
    c.rollback()
except Exception as e:
    print('delete fail:', e)
    # find which table blocks via orphan check - try deleting children first mentally
    c.rollback()
