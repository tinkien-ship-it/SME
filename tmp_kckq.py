import sqlite3, sys
sys.path.insert(0, '.')
sys.stdout.reconfigure(encoding='utf-8')
c = sqlite3.connect('tenants/sme_demo.db')
from Services.sme.bctc_report import income_statement, balance_sheet
for pt in (6, 12):
    b02 = income_statement(c, fiscal_year=2026, period_from=1, period_to=pt)
    bs = balance_sheet(c, fiscal_year=2026, period_to=pt)
    print(f"period_to={pt}: B02 LNST={b02['totals']['profit_after_tax']:,.0f} B01 current_profit={bs.get('current_year_profit',0):,.0f}")

closed = c.execute(
    "SELECT DISTINCT period FROM sme_journal_entries WHERE fiscal_year=2026 AND document_type='KCKQ' AND status='posted'"
).fetchall()
print('KCKQ periods:', [r[0] for r in closed])
