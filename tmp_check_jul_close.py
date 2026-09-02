import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')

c = sqlite3.connect('tenants/sme_demo.db')
c.row_factory = sqlite3.Row

print('=== KCKQ theo kỳ 2026 ===')
for r in c.execute("""
SELECT period, entry_no, posting_date, status, created_by, description
FROM sme_journal_entries
WHERE fiscal_year=2026 AND document_type='KCKQ'
ORDER BY period
"""):
    print(dict(r))

print('\n=== Phát sinh T7/2026 (period=7) ===')
for r in c.execute("""
SELECT je.entry_no, je.posting_date, je.period, je.document_type, je.status, je.created_by,
       SUM(jl.debit) d, SUM(jl.credit) cr
FROM sme_journal_entries je
JOIN sme_journal_lines jl ON jl.entry_id=je.id
WHERE je.fiscal_year=2026 AND je.period=7 AND je.status IN ('posted','reversed')
GROUP BY je.id
ORDER BY je.posting_date
"""):
    print(dict(r))

print('\n=== Bút toán quanh 15/7/2026 ===')
for r in c.execute("""
SELECT je.entry_no, je.posting_date, je.period, je.document_type, je.business_type,
       je.status, je.created_by, je.description
FROM sme_journal_entries je
WHERE je.posting_date BETWEEN '2026-07-01' AND '2026-07-31'
ORDER BY je.posting_date, je.entry_no
"""):
    print(dict(r))

print('\n=== P&L net T7 (trước KCKQ) ===')
from Services.sme.period_close import build_period_close_lines, _period_pl_activity
act = _period_pl_activity(c, 2026, 7)
for code in sorted(act.keys()):
    if code[0] in '5678' or code.startswith('711'):
        b = act[code]
        print(f"  {code}: N={b['debit']} C={b['credit']}")

lines, meta = build_period_close_lines(c, fiscal_year=2026, period=7)
print('build_period_close_lines:', meta)
print('lines count:', len(lines))

print('\n=== Thử run_period_close T7 ===')
from Services.sme.period_close import run_period_close
from Services.tenant_profile import resolve_features, normalize_accounting_regime
regime = normalize_accounting_regime('SME_MICRO_TT58')
features = resolve_features(regime, 'DT1', {})
r = run_period_close(c, fiscal_year=2026, period=7, accounting_regime=regime, features=features, created_by='diag')
print(r)
