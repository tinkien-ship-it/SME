# -*- coding: utf-8 -*-
import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')

db = r'C:\SME\tenants\sme_demo.db'
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

print('=== EXPORT SALES ===')
for r in conn.execute(
    """
    SELECT id, sale_no, ar_status, settle_journal_id, settle_amount_fc, amount_fc,
           advance_fc, export_status, currency, exchange_rate, payment_mode
    FROM sale
    WHERE UPPER(COALESCE(sale_type,'')) = 'EXPORT'
    ORDER BY id DESC LIMIT 8
    """
):
    print(dict(r))

print('=== RECENT JOURNALS PT / THU ===')
for r in conn.execute(
    """
    SELECT id, entry_no, document_type, document_no, business_type, description, status, document_id
    FROM sme_journal_entries
    WHERE document_type IN ('PT','EXPORT_AR_SETTLE')
       OR business_type IN ('THU_TIEN','THU_XK')
       OR description LIKE '%Thu công nợ%'
       OR description LIKE '%Thu hồi%'
    ORDER BY id DESC LIMIT 15
    """
):
    print(dict(r))

print('=== LINES for those entries ===')
for r in conn.execute(
    """
    SELECT je.id, je.document_no, jl.account_code, jl.debit, jl.credit, jl.description
    FROM sme_journal_entries je
    JOIN sme_journal_lines jl ON jl.entry_id = je.id
    WHERE je.document_type IN ('PT','EXPORT_AR_SETTLE')
       OR je.business_type IN ('THU_TIEN','THU_XK')
    ORDER BY je.id DESC, jl.sequence
    LIMIT 40
    """
):
    print(tuple(r))

print('=== VOUCHERS ===')
try:
    for r in conn.execute(
        """
        SELECT id, voucher_no, source_type, journal_entry_id, debit_account, credit_account, amount, reason, status
        FROM sme_vouchers ORDER BY id DESC LIMIT 10
        """
    ):
        print(dict(r))
except Exception as e:
    print('no vouchers', e)

print('=== 1122 / 131 balances recent ===')
for r in conn.execute(
    """
    SELECT je.id, je.document_no, je.document_type, jl.account_code, jl.debit, jl.credit
    FROM sme_journal_lines jl
    JOIN sme_journal_entries je ON je.id = jl.entry_id
    WHERE je.status='posted' AND je.reverses_id IS NULL
      AND (jl.account_code LIKE '1122%' OR jl.account_code LIKE '131%')
    ORDER BY je.id DESC LIMIT 30
    """
):
    print(tuple(r))
