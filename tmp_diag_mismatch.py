# -*- coding: utf-8 -*-
import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')
c = sqlite3.connect('tenants/sme_demo.db')
c.row_factory = sqlite3.Row

r1 = c.execute("""
SELECT SUM(jl.credit-jl.debit) FROM sme_journal_lines jl
JOIN sme_journal_entries je ON je.id=jl.entry_id
WHERE jl.account_code LIKE '511%' AND je.status IN ('posted','reversed')
  AND je.posting_date >= '2026-01-01' AND je.posting_date <= '2026-12-31'
  AND COALESCE(je.document_type,'') != 'KCKQ'
""").fetchone()[0]

r2 = c.execute("""
SELECT SUM(jl.credit-jl.debit) FROM sme_journal_lines jl
JOIN sme_journal_entries je ON je.id=jl.entry_id
WHERE jl.account_code LIKE '511%' AND je.status IN ('posted','reversed')
  AND je.fiscal_year=2026 AND je.period BETWEEN 1 AND 12
  AND COALESCE(je.document_type,'') != 'KCKQ'
""").fetchone()[0]

r3 = c.execute("""
SELECT SUM(jl.credit-jl.debit) FROM sme_journal_lines jl
JOIN sme_journal_entries je ON je.id=jl.entry_id
WHERE jl.account_code LIKE '711%' AND je.status IN ('posted','reversed')
  AND je.posting_date >= '2026-01-01' AND je.posting_date <= '2026-12-31'
  AND COALESCE(je.document_type,'') != 'KCKQ'
""").fetchone()[0]

r4 = c.execute("""
SELECT SUM(jl.credit-jl.debit) FROM sme_journal_lines jl
JOIN sme_journal_entries je ON je.id=jl.entry_id
WHERE jl.account_code LIKE '711%' AND je.status IN ('posted','reversed')
  AND je.fiscal_year=2026 AND je.period BETWEEN 1 AND 12
  AND COALESCE(je.document_type,'') != 'KCKQ'
""").fetchone()[0]

print('511 by posting_date 2026:', r1)
print('511 by fiscal 2026 T1-12:', r2)
print('711 by posting_date 2026:', r3)
print('711 by fiscal 2026 T1-12:', r4)
print('B02 TT58 line 01 expected (511+711):', (r1 or 0) + (r3 or 0))

print('\n=== TK thu nhập khác (711) chi tiết ===')
for row in c.execute("""
SELECT je.entry_no, je.posting_date, je.fiscal_year, je.period, je.document_type,
       jl.account_code, jl.debit, jl.credit, jl.description
FROM sme_journal_lines jl
JOIN sme_journal_entries je ON je.id=jl.entry_id
WHERE jl.account_code LIKE '711%'
  AND je.posting_date >= '2026-01-01' AND je.posting_date <= '2026-12-31'
  AND je.status IN ('posted','reversed')
"""):
    print(dict(row))

print('\n=== Mismatch fiscal_year/period vs posting_date (511 only) ===')
for row in c.execute("""
SELECT je.entry_no, je.posting_date, je.fiscal_year, je.period,
       SUM(jl.credit-jl.debit) AS rev
FROM sme_journal_lines jl
JOIN sme_journal_entries je ON je.id=jl.entry_id
WHERE jl.account_code LIKE '511%' AND je.status IN ('posted','reversed')
  AND (
    (je.posting_date >= '2026-01-01' AND je.posting_date <= '2026-12-31'
     AND NOT (je.fiscal_year=2026 AND je.period BETWEEN 1 AND 12))
    OR
    (je.fiscal_year=2026 AND je.period BETWEEN 1 AND 12
     AND (je.posting_date < '2026-01-01' OR je.posting_date > '2026-12-31'))
  )
GROUP BY je.id
ORDER BY rev DESC
LIMIT 20
"""):
    print(dict(row))

cnt = c.execute("""
SELECT COUNT(DISTINCT je.id) FROM sme_journal_entries je
JOIN sme_journal_lines jl ON jl.entry_id=je.id
WHERE jl.account_code LIKE '511%' AND je.status IN ('posted','reversed')
  AND je.fiscal_year=2026 AND je.period BETWEEN 1 AND 12
  AND (je.posting_date < '2026-01-01' OR je.posting_date > '2026-12-31')
""").fetchone()[0]
print('511 entries in fiscal 2026 but posting_date outside 2026:', cnt)

cnt2 = c.execute("""
SELECT COUNT(DISTINCT je.id) FROM sme_journal_entries je
JOIN sme_journal_lines jl ON jl.entry_id=je.id
WHERE jl.account_code LIKE '511%' AND je.status IN ('posted','reversed')
  AND je.posting_date >= '2026-01-01' AND je.posting_date <= '2026-12-31'
  AND NOT (je.fiscal_year=2026 AND je.period BETWEEN 1 AND 12)
""").fetchone()[0]
print('511 entries posting_date in 2026 but fiscal_year not 2026:', cnt2)

# Regime
from Services.sme.regime_profile import get_ledger_profile
print('\nRegime:', get_ledger_profile(c))
