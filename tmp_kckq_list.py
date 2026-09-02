import sqlite3, sys
sys.stdout.reconfigure(encoding='utf-8')
c = sqlite3.connect('tenants/sme_demo.db')
rows = c.execute("""
SELECT entry_no, posting_date, fiscal_year, period, description
FROM sme_journal_entries
WHERE document_type='KCKQ' AND status='posted'
ORDER BY fiscal_year, period
""").fetchall()
print(f'KCKQ entries: {len(rows)}')
for r in rows:
    print(r)
if rows:
    eid = c.execute("SELECT id FROM sme_journal_entries WHERE entry_no=?", (rows[0][0],)).fetchone()[0]
    lines = c.execute("""
    SELECT account_code, debit, credit, description FROM sme_journal_lines
    WHERE entry_id=? ORDER BY sequence LIMIT 15
    """, (eid,)).fetchall()
    print('\nSample lines entry', rows[0][0])
    for ln in lines:
        print(' ', ln)
