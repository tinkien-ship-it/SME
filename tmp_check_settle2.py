# -*- coding: utf-8 -*-
import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
conn = sqlite3.connect(r'C:\SME\tenants\sme_demo.db')
conn.row_factory = sqlite3.Row

for eid in (150, 149, 148, 151, 152, 153):
    je = conn.execute('SELECT * FROM sme_journal_entries WHERE id=?', (eid,)).fetchone()
    print('ENTRY', eid, dict(je) if je else None)
    if je:
        for ln in conn.execute('SELECT account_code,debit,credit,description FROM sme_journal_lines WHERE entry_id=? ORDER BY sequence', (eid,)):
            print(' ', dict(ln))

print('--- all journals document_id 9114 ---')
for r in conn.execute(
    "SELECT id,entry_no,document_type,document_no,business_type,description,status,reverses_id FROM sme_journal_entries WHERE document_id=9114 ORDER BY id"
):
    print(dict(r))
    for ln in conn.execute('SELECT account_code,debit,credit FROM sme_journal_lines WHERE entry_id=?', (r['id'],)):
        print(' ', dict(ln))

print('--- all journals document_id 9113 ---')
for r in conn.execute(
    "SELECT id,entry_no,document_type,document_no,business_type,description,status,reverses_id FROM sme_journal_entries WHERE document_id=9113 ORDER BY id"
):
    print(dict(r))
    for ln in conn.execute('SELECT account_code,debit,credit FROM sme_journal_lines WHERE entry_id=?', (r['id'],)):
        print(' ', dict(ln))
