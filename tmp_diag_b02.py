# -*- coding: utf-8 -*-
import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')
c = sqlite3.connect(r'C:/SME/tenants/sme_demo.db')
c.row_factory = sqlite3.Row
print('511 by doc')
for r in c.execute(
    """
    SELECT je.document_type, sum(jl.debit) d, sum(jl.credit) cr
    FROM sme_journal_lines jl
    JOIN sme_journal_entries je ON je.id=jl.entry_id
    WHERE jl.account_code LIKE '511%' AND je.fiscal_year=2026 AND je.period<=6
      AND je.status IN ('posted','reversed')
    GROUP BY je.document_type
    """
):
    print(dict(r))
from Services.sme.bctc_report import income_statement, _period_activity
act = _period_activity(c, 2026, 1, 6)
print('5111', act.get('5111'))
print('totals', income_statement(c, fiscal_year=2026, period_from=1, period_to=6)['totals'])
