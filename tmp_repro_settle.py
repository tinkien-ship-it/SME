# -*- coding: utf-8 -*-
"""Reproduce settle for XK000002 / XK000001."""
import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')

from Services.sme.export_settle import settle_export_ar
from Services.sme.export_payment import ensure_export_sale_schema

conn = sqlite3.connect(r'C:\SME\tenants\sme_demo.db')
conn.row_factory = sqlite3.Row
ensure_export_sale_schema(conn)

# Reset XK000002 to allow re-settle for test (don't commit reset if settle fails)
sale = dict(conn.execute('SELECT * FROM sale WHERE id=9114').fetchone())
print('before', sale['ar_status'], sale['settle_journal_id'], sale['settle_amount_fc'])

# Check if revenue exists
revs = conn.execute(
    "SELECT id FROM sme_journal_entries WHERE document_id=9114 AND document_type='EXPORT_REVENUE' AND status='posted'"
).fetchall()
print('revenue entries', [r[0] for r in revs])

# Try settle on 9113 XK000001 which has revenue and ar open-ish
s1 = dict(conn.execute('SELECT * FROM sale WHERE id=9113').fetchone())
print('XK000001', s1['ar_status'], s1['settle_amount_fc'], s1['amount_fc'], s1['settle_journal_id'])

try:
    # Force allow settle on 9113: clear settle flags if journal missing
    if s1.get('settle_journal_id') and not conn.execute(
        'SELECT id FROM sme_journal_entries WHERE id=?', (s1['settle_journal_id'],)
    ).fetchone():
        print('clearing stale settle_journal_id on 9113')
        conn.execute(
            "UPDATE sale SET settle_journal_id=NULL, settle_amount_fc=0, ar_status='open' WHERE id=9113"
        )
        conn.commit()

    if sale.get('settle_journal_id') and not conn.execute(
        'SELECT id FROM sme_journal_entries WHERE id=?', (sale['settle_journal_id'],)
    ).fetchone():
        print('clearing stale settle on 9114')
        conn.execute(
            "UPDATE sale SET settle_journal_id=NULL, settle_amount_fc=0, ar_status='open' WHERE id=9114"
        )
        conn.commit()

    result = settle_export_ar(
        conn, 9114,
        settle_date='2026-08-04',
        exchange_rate=25000,
        payment_method='bank',
        created_by='test',
        commit=True,
    )
    print('settle result', result)
    eid = result['journal_entry_id']
    for ln in conn.execute(
        'SELECT account_code,debit,credit,description FROM sme_journal_lines WHERE entry_id=? ORDER BY sequence',
        (eid,),
    ):
        print(' line', dict(ln))
except Exception as e:
    conn.rollback()
    print('SETTLE ERROR:', type(e).__name__, e)
    import traceback
    traceback.print_exc()
