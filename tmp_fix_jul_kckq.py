import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')

from Services.sme.period_close import catch_up_missing_period_closes, _active_close_entry
from Services.tenant_profile import resolve_features, normalize_accounting_regime

c = sqlite3.connect('tenants/sme_demo.db')
regime = normalize_accounting_regime('SME_MICRO_TT58')
features = resolve_features(regime, 'DT1', {})

print('T7 KCKQ before:', _active_close_entry(c, 202607))
r = catch_up_missing_period_closes(
    c, fiscal_year=2026, accounting_regime=regime, features=features, created_by='fix',
)
c.commit()
print('catch_up:', r)

rows = c.execute("""
SELECT entry_no, posting_date, period, description
FROM sme_journal_entries
WHERE document_type='KCKQ' AND fiscal_year=2026 AND period=7
""").fetchall()
print('T7 KCKQ after:', rows)
