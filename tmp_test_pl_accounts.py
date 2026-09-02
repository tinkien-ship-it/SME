import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')

from Services.sme.pos_profit_report import compute_sme_pos_profit_report

c = sqlite3.connect('tenants/sme_demo.db')
rep = compute_sme_pos_profit_report(c, '2026-01-01', '2026-12-31')
ad = rep.get('account_detail') or {}
print('sections:', ad.get('sections'))
print('sample accounts:')
for r in (ad.get('rows') or [])[:12]:
    if r.get('kind') == 'account':
        print(f"  {r['account_code']:8} {r['name'][:35]:35} N={r['period_debit']:>12,.0f} C={r['period_credit']:>12,.0f} net={r['net']:>12,.0f}")
print('totals:', ad.get('totals'))
print('B02 revenue:', rep['revenue'], 'net_profit:', rep['net_profit'])
