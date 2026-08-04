# -*- coding: utf-8 -*-
import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
c = sqlite3.connect(r'C:\SME\tenants\sme_demo.db')
c.execute(
    """
    UPDATE sale SET ar_status='settled'
    WHERE id=9114
      AND COALESCE(settle_amount_fc,0) >= COALESCE(amount_fc,0)
    """
)
c.commit()
for r in c.execute(
    'SELECT sale_no,ar_status,settle_amount_fc,settle_journal_id FROM sale WHERE id IN (9113,9114)'
):
    print(r)

# Verify 1122/131 for both
for sid, label in ((9113, 'XK1'), (9114, 'XK2')):
    jid = c.execute('SELECT settle_journal_id FROM sale WHERE id=?', (sid,)).fetchone()[0]
    rows = c.execute(
        'SELECT account_code,debit,credit FROM sme_journal_lines WHERE entry_id=?',
        (jid,),
    ).fetchall()
    print(label, 'jid', jid, rows)
