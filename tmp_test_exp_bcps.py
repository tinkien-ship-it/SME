import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')
from Services.sme.pos_profit_report import compute_sme_pos_profit_report

c = sqlite3.connect('tenants/sme_demo.db')
r = compute_sme_pos_profit_report(c, '2026-01-01', '2026-12-31')
ed = r['expense_detail']
print('source:', ed['source'], 'total:', ed['total'], 'count:', ed['account_count'])
for row in ed['rows']:
    print(
        f"  {row['account_code']:6} {row['name'][:32]:32} "
        f"N={row['period_debit']:>12,.0f} C={row['period_credit']:>12,.0f} net={row['net']:>12,.0f}"
    )
