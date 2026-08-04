# -*- coding: utf-8 -*-
import sqlite3, sys, time
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'C:\SME')

# Simulate delete_pending cascade without Flask
from routes.sale import _delete_sale_child_rows

path = r'C:\SME\tenants\sme_demo.db'
for attempt in range(6):
    try:
        c = sqlite3.connect(path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute(f'PRAGMA busy_timeout={30000}')
        s = c.execute('SELECT id,status FROM sale WHERE id=9115').fetchone()
        print('sale', dict(s) if s else None)
        if not s:
            print('already gone')
            break
        if s['status'] != 'pending':
            print('not pending, skip delete')
            break
        cur = c.cursor()
        cur.execute('BEGIN IMMEDIATE')
        _delete_sale_child_rows(cur, 9115)
        cur.execute('DELETE FROM sale WHERE id=?', (9115,))
        c.commit()
        print('DELETED 9115 OK')
        break
    except sqlite3.OperationalError as e:
        print(f'attempt {attempt+1}: {e}')
        time.sleep(1.5)
    finally:
        try:
            c.close()
        except Exception:
            pass
