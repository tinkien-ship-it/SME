import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')
c = sqlite3.connect('tenants/sme_demo.db')
r = c.execute("""
SELECT SUM(jl.credit) AS co, SUM(jl.debit) AS no
FROM sme_journal_lines jl
JOIN sme_journal_entries je ON je.id = jl.entry_id
WHERE jl.account_code LIKE '511%'
  AND je.status IN ('posted','reversed')
  AND je.posting_date BETWEEN '2026-01-01' AND '2026-12-31'
  AND COALESCE(je.document_type,'') != 'KCKQ'
""").fetchone()
co, no = r[0] or 0, r[1] or 0
print(f'511 phát sinh 2026: Có={co:,.0f} Nợ={no:,.0f} → net={co-no:,.0f}')

from Services.sme.pl_expense_breakdown import trial_balance_pl_totals
from Services.sme.pos_profit_report import compute_sme_pos_profit_report

tb = trial_balance_pl_totals(c, '2026-01-01', '2026-12-31')
print(f'BCPS revenue_net (511/515 net): {tb["revenue_net"]:,.0f}')

# Simulate user example with a quick calc
print('\nVí dụ lý thuyết: Có 439.927.000, Nợ 100.000.000 → net = 339.927.000')
print(f'  Công thức: {439_927_000 - 100_000_000:,.0f}')
