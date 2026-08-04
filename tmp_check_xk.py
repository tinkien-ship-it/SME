# -*- coding: utf-8 -*-
import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
from Services.sme.export_settle import settle_export_ar

conn = sqlite3.connect(r'C:\SME\tenants\sme_demo.db')
conn.row_factory = sqlite3.Row

for sid in (9113, 9114):
    s = dict(conn.execute(
        'SELECT id,sale_no,ar_status,settle_journal_id,settle_amount_fc,amount_fc FROM sale WHERE id=?',
        (sid,),
    ).fetchone())
    print('SALE', s)
    jid = s.get('settle_journal_id')
    if jid:
        je = conn.execute(
            'SELECT id,document_no,document_type,status FROM sme_journal_entries WHERE id=?',
            (jid,),
        ).fetchone()
        print(' JE', dict(je) if je else None)
        if je:
            for ln in conn.execute(
                'SELECT account_code,debit,credit FROM sme_journal_lines WHERE entry_id=?',
                (jid,),
            ):
                print('  ', dict(ln))
    else:
        rows = conn.execute(
            """
            SELECT id,document_no,document_type,status FROM sme_journal_entries
            WHERE document_id=? AND document_type IN ('PT','EXPORT_SETTLE')
            ORDER BY id
            """,
            (sid,),
        ).fetchall()
        for je in rows:
            print(' PT-like', dict(je))
            for ln in conn.execute(
                'SELECT account_code,debit,credit FROM sme_journal_lines WHERE entry_id=?',
                (je['id'],),
            ):
                print('  ', dict(ln))

# XK000002: settle if open after revenue repair
s2 = dict(conn.execute('SELECT * FROM sale WHERE id=9114').fetchone())
print('XK2 ar', s2.get('ar_status'), 'settle_fc', s2.get('settle_amount_fc'), 'jid', s2.get('settle_journal_id'))
need = float(s2.get('amount_fc') or 0) - float(s2.get('settle_amount_fc') or 0) - float(s2.get('advance_fc') or 0)
print('need_fc', need)
if need > 0.0001:
    # clear stale if any
    jid = s2.get('settle_journal_id')
    ok = False
    if jid:
        ok = bool(conn.execute(
            "SELECT id FROM sme_journal_entries WHERE id=? AND status='posted'", (jid,)
        ).fetchone())
    if not ok and jid:
        conn.execute(
            "UPDATE sale SET settle_journal_id=NULL, settle_amount_fc=0, ar_status='open' WHERE id=9114"
        )
        conn.commit()
    r = settle_export_ar(
        conn, 9114, settle_date='2026-08-04', exchange_rate=25000,
        payment_method='bank', commit=True,
    )
    print('settle XK2', r['message'])
    for ln in conn.execute(
        'SELECT account_code,debit,credit FROM sme_journal_lines WHERE entry_id=?',
        (r['journal_entry_id'],),
    ):
        print(dict(ln))
