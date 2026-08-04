# -*- coding: utf-8 -*-
import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
from Services.sme.export_clearance import sync_export_clearance_journals
from Services.sme.export_settle import settle_export_ar

conn = sqlite3.connect(r'C:\SME\tenants\sme_demo.db')
conn.row_factory = sqlite3.Row

# XK000001: ensure revenue exists, then settle if needed
s = dict(conn.execute('SELECT * FROM sale WHERE id=9113').fetchone())
print('XK000001 before', s['ar_status'], s['settle_amount_fc'], s['settle_journal_id'])
rev = conn.execute(
    "SELECT id FROM sme_journal_entries WHERE document_id=9113 AND document_type='EXPORT_REVENUE' AND status='posted'"
).fetchall()
print('rev', [r[0] for r in rev])

# If already fully settled with real journal, skip
jid = s.get('settle_journal_id')
ok = jid and conn.execute(
    "SELECT id FROM sme_journal_entries WHERE id=? AND status='posted'", (jid,)
).fetchone()
if ok and float(s.get('settle_amount_fc') or 0) >= float(s.get('amount_fc') or 0):
    print('already settled ok')
else:
    # reset stale settle amount if needed
    if float(s.get('settle_amount_fc') or 0) > 0 and not ok:
        conn.execute("UPDATE sale SET settle_amount_fc=0, settle_journal_id=NULL, ar_status='open' WHERE id=9113")
        conn.commit()
    r = settle_export_ar(conn, 9113, settle_date='2026-08-04', exchange_rate=25000, payment_method='bank', commit=True)
    print('settle', r['message'], r['debit_account'], r['credit_account'])
    for ln in conn.execute(
        'SELECT account_code,debit,credit FROM sme_journal_lines WHERE entry_id=?',
        (r['journal_entry_id'],),
    ):
        print(dict(ln))

# XK000002 revenue repair check
rev2 = conn.execute(
    "SELECT id FROM sme_journal_entries WHERE document_id=9114 AND document_type='EXPORT_REVENUE' AND status='posted'"
).fetchall()
print('XK000002 rev', [r[0] for r in rev2])
if not rev2:
    jr = sync_export_clearance_journals(conn, 9114, created_by='repair')
    conn.commit()
    print('repaired', jr)
    rev2 = conn.execute(
        "SELECT id FROM sme_journal_entries WHERE document_id=9114 AND document_type='EXPORT_REVENUE' AND status='posted'"
    ).fetchall()
    print('XK000002 rev after', [r[0] for r in rev2])
    for ln in conn.execute(
        "SELECT jl.account_code,jl.debit,jl.credit FROM sme_journal_lines jl JOIN sme_journal_entries je ON je.id=jl.entry_id WHERE je.document_id=9114 AND je.document_type='EXPORT_REVENUE'"
    ):
        print(dict(ln))

print('OK')
