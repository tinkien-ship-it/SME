# -*- coding: utf-8 -*-
from Services.tenant_profile import (
    build_tenant_settings,
    normalize_vat_filing_period,
    default_vat_filing_period_for_regime,
)
from Services.sme.tax_nsnn import resolve_filing_window, quarter_bounds, tax_nsnn_summary
from Services.sme.bootstrap import ensure_sme_accounting_ready
import sqlite3

assert normalize_vat_filing_period('thang') == 'monthly'
assert normalize_vat_filing_period('quy') == 'quarterly'
assert default_vat_filing_period_for_regime('SME_MICRO_TT58') == 'quarterly'
assert default_vat_filing_period_for_regime('SME_TT99') == 'monthly'

s58 = build_tenant_settings(accounting_regime='SME_MICRO_TT58')
assert s58['vat_filing_period'] == 'quarterly'
assert s58['features']['monthly_vat_filing'] is False

s99 = build_tenant_settings(accounting_regime='SME_TT99')
assert s99['vat_filing_period'] == 'monthly'
assert s99['features']['monthly_vat_filing'] is True

s_q = build_tenant_settings(accounting_regime='SME_TT99', extra={'vat_filing_period': 'quarterly'})
assert s_q['vat_filing_period'] == 'quarterly'
assert s_q['features']['monthly_vat_filing'] is False

s_m = build_tenant_settings(accounting_regime='SME_MICRO_TT58', extra={'vat_filing_period': 'monthly'})
assert s_m['vat_filing_period'] == 'monthly'
assert s_m['features']['monthly_vat_filing'] is True

w = resolve_filing_window(filing_mode='quarterly', quarter=2)
assert w['period_from'] == 4 and w['period_to'] == 6
w2 = resolve_filing_window(filing_mode='monthly', period=5)
assert w2['period_from'] == 5 and w2['period_to'] == 5
assert quarter_bounds(1) == (1, 3)

conn = sqlite3.connect(':memory:')
ensure_sme_accounting_ready(conn, accounting_regime='SME_TT99')
d = tax_nsnn_summary(conn, fiscal_year=2026, quarter=1, filing_mode='quarterly')
assert d['filing_mode'] == 'quarterly'
assert d['period_from'] == 1 and d['period_to'] == 3
d2 = tax_nsnn_summary(conn, fiscal_year=2026, period=7, filing_mode='monthly')
assert d2['filing_mode'] == 'monthly' and d2['period'] == 7
print('OK smoke vat filing period')
