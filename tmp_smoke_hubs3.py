# -*- coding: utf-8 -*-
"""Smoke: sales/books/bctc hubs + VAT declaration worksheet."""
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Services.sme.bootstrap import ensure_sme_accounting_ready
from Services.sme.dashboard_metrics import (
    sales_hub_metrics, books_hub_metrics, bctc_hub_metrics,
)
from Services.sme.vat_declaration import vat_declaration_worksheet
from Services.sme.journal_engine import post_journal_entry


def _post(conn, *, date, lines, doc_type='TEST', doc_no='T1', doc_id=1):
    return post_journal_entry(
        conn,
        posting_date=date,
        document_date=date,
        document_type=doc_type,
        document_no=doc_no,
        document_id=doc_id,
        business_type=doc_type,
        description='smoke hubs3',
        created_by='smoke',
        lines=lines,
    )


conn = sqlite3.connect(':memory:')
conn.row_factory = sqlite3.Row
ensure_sme_accounting_ready(conn, commit=True)

date = '2026-07-15'
_post(conn, date=date, doc_type='BAN', doc_no='B1', doc_id=1, lines=[
    {'sequence': 1, 'account_code': '131', 'debit': 1100000, 'credit': 0, 'description': 'PT'},
    {'sequence': 2, 'account_code': '5111', 'debit': 0, 'credit': 1000000, 'description': 'DT'},
    {'sequence': 3, 'account_code': '33311', 'debit': 0, 'credit': 100000, 'description': 'VAT out'},
])
_post(conn, date=date, doc_type='MUA', doc_no='M1', doc_id=2, lines=[
    {'sequence': 1, 'account_code': '156', 'debit': 500000, 'credit': 0, 'description': 'HH'},
    {'sequence': 2, 'account_code': '13311', 'debit': 50000, 'credit': 0, 'description': 'VAT in'},
    {'sequence': 3, 'account_code': '331', 'debit': 0, 'credit': 550000, 'description': 'NCC'},
])
conn.commit()

s = sales_hub_metrics(conn, fiscal_year=2026, period_to=7)
assert s['revenue'] >= 1000000, s
assert s['receivable'] >= 1100000, s
assert isinstance(s['monthly'], list) and len(s['monthly']) == 7

b = books_hub_metrics(conn, fiscal_year=2026, period_to=7)
assert b['entry_count'] >= 2, b
assert b['balanced'] is True, b
assert b['period_debit'] > 0

c = bctc_hub_metrics(conn, fiscal_year=2026, period_to=7)
assert 'total_assets_approx' in c and 'tax_breakdown' in c
assert c['revenue'] >= 1000000

v = vat_declaration_worksheet(conn, fiscal_year=2026, period=7, filing_mode='monthly')
assert v['filing_mode'] == 'monthly'
inds = {i['code']: i['amount'] for i in v['indicators']}
assert inds['21'] >= 1000000, inds
assert inds['22'] >= 100000, inds
assert inds['23'] >= 50000, inds
assert inds['25'] >= 50000, inds

vq = vat_declaration_worksheet(conn, fiscal_year=2026, quarter=3, filing_mode='quarterly')
assert vq['period_from'] == 7 and vq['period_to'] == 9
assert len(vq['monthly_break']) == 3

root = Path(r'C:\SME\templates\KeToanSME')
for name in ('dashboard_sale.html', 'dashboard_sosachketoan.html', 'dashboard_BCTC.html'):
    text = (root / name).read_text(encoding='utf-8')
    assert '2450000000' not in text, name
    assert '/api/sme/' in text, name

assert 'SME_BCTC_reports' in (root / 'dashboard_BCTC.html').read_text(encoding='utf-8')
assert (root / 'vat_declaration.html').exists()
sb = (root / '_sidebar.html').read_text(encoding='utf-8')
assert 'SME_vat_declaration' in sb

print('OK hubs3 sales/books/bctc + vat declaration')
