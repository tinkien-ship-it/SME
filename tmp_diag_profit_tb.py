#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Đối chiếu chi tiết B02 vs BCPS — tenant sme_demo."""
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8')

DB = ROOT / 'tenants' / 'sme_demo.db'
YEAR = 2026
date_from = f'{YEAR}-01-01'
date_to = f'{YEAR}-12-31'

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

from Services.sme.pos_profit_report import compute_sme_pos_profit_report
from Services.sme.pl_expense_breakdown import trial_balance_pl_totals
from Services.sme.bctc_report import _date_range_activity, _coa_line_map, _aggregate_leaf_amounts
from Services.sme.bctc_lines import B02_INCOME_STATEMENT
from Services.sme.general_ledger import trial_balance

rep = compute_sme_pos_profit_report(conn, date_from, date_to)
recon = rep.get('reconciliation') or {}
print('=== BÁO CÁO LỢI NHUẬN (B02) ===')
print(f"DT thuần (10): {rep['revenue']:,.0f}")
print(f"Tổng CP B02:    {rep['total_expenses']:,.0f}")
print(f"LN trước thuế:  {rep['profit_before_tax']:,.0f}")
print(f"LNST (60):      {rep['net_profit']:,.0f}")
print()
print('=== ĐỐI CHIẾU ===')
for c in recon.get('checks') or []:
    st = 'OK' if c['balanced'] else 'FAIL'
    print(f"[{st}] {c['label']}")
    print(f"     BCPS/B02 kỳ vọng={c['expected']:,.0f} | Báo cáo={c['actual']:,.0f} | lệch={c['difference']:,.0f}")

tb = trial_balance_pl_totals(conn, date_from, date_to)
print()
print('=== BCPS (trial_balance_pl_totals) ===')
for k, v in tb.items():
    if k != 'unmapped_accounts':
        print(f"  {k}: {v}")

# Official trial balance by fiscal period
tb_off = trial_balance(conn, fiscal_year=YEAR, period_from=1, period_to=12, include_zero=False)
print()
print('=== BCPS chính thức (trial_balance kỳ 1-12/2026) ===')
rev_tb = exp_tb = Decimal('0')
for row in tb_off.get('rows') or []:
    code = str(row.get('code') or '')
    pd = Decimal(str(row.get('period_debit') or 0))
    pc = Decimal(str(row.get('period_credit') or 0))
    if code.startswith(('511', '515')):
        rev_tb += pc - pd
    elif code.startswith('521'):
        rev_tb -= pd - pc
    elif code.startswith('711'):
        pass  # other income
    elif row.get('account_class') == 'expense' or code.startswith(('632','635','641','642','811','821','621','622','623','627','631')):
        exp_tb += pd - pc
print(f"  DT thuần (511/515-521) PS kỳ: {float(rev_tb):,.0f}")
print(f"  CP PS kỳ (6x/8x):            {float(exp_tb):,.0f}")

# Unmapped B02 accounts - activity without bctc_line_code
bal_map = _date_range_activity(conn, date_from, date_to, exclude_document_types=('KCKQ',))
accounts = _coa_line_map(conn)
mapped_codes = {a['code'] for a in accounts if a.get('is_postable')}
print()
print('=== TK có PS nhưng KHÔNG map B02 (bctc_line_code) ===')
unmapped_activity = []
for code, bal in sorted(bal_map.items()):
    if (bal['debit'] or bal['credit']) and code not in mapped_codes:
        net = bal['debit'] - bal['credit']
        if abs(net) > 0.01:
            unmapped_activity.append((code, float(bal['debit']), float(bal['credit']), float(net)))
for row in unmapped_activity[:30]:
    print(f"  {row[0]}: N={row[1]:,.0f} C={row[2]:,.0f} net={row[3]:,.0f}")

# Accounts with activity but no bctc in COA table at all
print()
print('=== TK postable có PS, bctc_line_code rỗng ===')
coa_rows = conn.execute(
    "SELECT code, name, account_class, bctc_line_code FROM sme_chart_of_accounts WHERE is_active=1 AND is_postable=1"
).fetchall()
for r in coa_rows:
    code = r['code']
    if code not in bal_map:
        continue
    b = bal_map[code]
    if not (b['debit'] or b['credit']):
        continue
    line = (r['bctc_line_code'] or '').strip()
    if not line:
        # check inherit
        resolved = any(a['code'] == code for a in accounts)
        if not resolved:
            net = float(b['debit'] - b['credit'])
            if abs(net) > 0.01:
                print(f"  {code} {r['name'][:40]} class={r['account_class']} net={net:,.0f}")

# posting_date vs fiscal period mismatch
print()
print('=== Bút toán PS ngoài năm 2026 theo posting_date nhưng fiscal_year=2026 ===')
rows = conn.execute(
    """
    SELECT je.id, je.entry_no, je.posting_date, je.fiscal_year, je.period, je.document_type,
           SUM(jl.debit) d, SUM(jl.credit) c
    FROM sme_journal_entries je
    JOIN sme_journal_lines jl ON jl.entry_id = je.id
    WHERE je.status IN ('posted','reversed') AND je.fiscal_year = ?
      AND (je.posting_date < ? OR je.posting_date > ?)
    GROUP BY je.id
    LIMIT 15
    """,
    (YEAR, date_from, date_to),
).fetchall()
for r in rows:
    print(f"  {r['entry_no']} post={r['posting_date']} fy={r['fiscal_year']} T{r['period']} {r['document_type']} D={r['d']} C={r['c']}")

print()
print('=== PS trong 2026 posting_date nhưng fiscal_year != 2026 ===')
rows2 = conn.execute(
    """
    SELECT je.id, je.entry_no, je.posting_date, je.fiscal_year, je.period, je.document_type,
           SUM(jl.debit) d, SUM(jl.credit) c
    FROM sme_journal_entries je
    JOIN sme_journal_lines jl ON jl.entry_id = je.id
    WHERE je.status IN ('posted','reversed')
      AND je.posting_date >= ? AND je.posting_date <= ?
      AND je.fiscal_year != ?
    GROUP BY je.id
    LIMIT 15
    """,
    (date_from, date_to, YEAR),
).fetchall()
for r in rows2:
    print(f"  {r['entry_no']} post={r['posting_date']} fy={r['fiscal_year']} T{r['period']} {r['document_type']}")

# Compare B02 leaf aggregation vs raw 511
leaf = _aggregate_leaf_amounts(accounts, bal_map, line_defs=B02_INCOME_STATEMENT)
print()
print('=== B02 leaf amounts (date range) ===')
for code in ('01','02','10','11','22','25','26','32','50','51','60'):
    print(f"  {code}: {float(leaf.get(code, 0)):,.0f}")

conn.close()
