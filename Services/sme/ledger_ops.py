"""Nghiệp vụ kế toán còn thiếu: 113, 521, dự phòng, thuế khác, phân phối LN, thuê TC."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    post_journal_entry,
    resolve_postable_account,
)

MONEY_Q = Decimal('0.01')

# DN trích KPCĐ 2% quỹ lương (Nghị định 191/2013/NĐ-CP, thực tiễn phổ biến)
KPCD_EMPLOYER_RATE = Decimal('0.02')

OTHER_TAX_DEFS = (
    ('3339', 'Lệ phí môn bài / phí lệ phí', '642'),
    ('3337', 'Thuế nhà đất / thuê đất', '642'),
    ('3338', 'Thuế bảo vệ môi trường', '642'),
    ('3332', 'Thuế tiêu thụ đặc biệt', '632'),
    ('3336', 'Thuế tài nguyên', '642'),
)

PROVISION_KINDS = {
    'ar': {
        'label': 'Dự phòng phải thu khó đòi',
        'debit': '642',
        'credit': '2293',
        'writeoff_target': '131',
    },
    'inventory': {
        'label': 'Dự phòng giảm giá hàng tồn kho',
        'debit': '632',
        'credit': '2294',
        'writeoff_target': '156',
    },
}


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_ledger_ops_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_ledger_ops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            op_type TEXT NOT NULL,
            doc_no TEXT NOT NULL,
            doc_date TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            debit_account TEXT,
            credit_account TEXT,
            party_name TEXT,
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'posted',
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            branch_code TEXT
        )
        """
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_ledger_ops_type ON sme_ledger_ops(op_type, doc_date)'
    )
    if commit:
        conn.commit()


def _next_no(conn: sqlite3.Connection, prefix: str) -> str:
    row = conn.execute(
        "SELECT doc_no FROM sme_ledger_ops WHERE doc_no LIKE ? ORDER BY id DESC LIMIT 1",
        (f'{prefix}%',),
    ).fetchone()
    if not row:
        return f'{prefix}000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'{prefix}{int(digits) + 1:06d}'


def _save_op(conn, *, op_type, doc_no, doc_date, amount, debit, credit, party, entry_id, notes, created_by, branch):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_ledger_ops (
            op_type, doc_no, doc_date, amount, debit_account, credit_account,
            party_name, journal_entry_id, notes, created_by, created_at, branch_code
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            op_type, doc_no, doc_date, float(amount), debit, credit,
            party or '', entry_id, notes or '', created_by, _now(), branch or 'HQ',
        ),
    )
    return int(cur.lastrowid)


def post_sales_allowance(
    conn: sqlite3.Connection,
    *,
    doc_date: str,
    amount,
    vat_amount=0,
    kind: str = 'discount',
    customer_name: str = '',
    settle_account: str = '131',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Chiết khấu thương mại (5211) hoặc giảm giá hàng bán (5213).

    Nợ 521x (+ Nợ 33311 nếu có VAT) / Có 131|1111.
    """
    ensure_sme_journal_ready(conn, commit=False)
    ensure_ledger_ops_schema(conn, commit=False)
    amt = _money(amount)
    vat = _money(vat_amount)
    if amt <= 0:
        raise ValueError('Số tiền giảm trừ phải > 0')
    date_s = str(doc_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày')
    acc_521 = '5211' if str(kind or '').lower() in ('ck', 'chiet_khau', '5211', 'trade') else '5213'
    debit_521 = resolve_postable_account(conn, acc_521)
    vat_acc = resolve_postable_account(conn, '33311')
    credit = resolve_postable_account(conn, settle_account or '131')
    desc = notes or (
        f'Chiết khấu thương mại {customer_name}'.strip()
        if acc_521 == '5211' else
        f'Giảm giá hàng bán {customer_name}'.strip()
    )
    lines = [
        {'sequence': 1, 'account_code': debit_521, 'debit': float(amt), 'credit': 0, 'description': desc},
    ]
    seq = 2
    if vat > 0:
        lines.append({
            'sequence': seq, 'account_code': vat_acc,
            'debit': float(vat), 'credit': 0, 'description': desc + ' — VAT',
        })
        seq += 1
    lines.append({
        'sequence': seq, 'account_code': credit,
        'debit': 0, 'credit': float(amt + vat), 'description': desc,
    })
    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, None)
    doc_no = _next_no(conn, 'GG')
    entry = post_journal_entry(
        conn,
        posting_date=date_s, document_date=date_s,
        document_type='GGHB', document_no=doc_no,
        business_type='GIAM_TRU_DOANH_THU',
        description=desc, created_by=created_by, branch_code=branch,
        lines=lines,
    )
    oid = _save_op(
        conn, op_type='sales_allowance', doc_no=doc_no, doc_date=date_s,
        amount=amt + vat, debit=debit_521, credit=credit, party=customer_name,
        entry_id=entry['id'], notes=desc, created_by=created_by, branch=branch,
    )
    if commit:
        conn.commit()
    return {'id': oid, 'doc_no': doc_no, 'journal_entry_id': entry['id'], 'account_521': debit_521}


def post_provision(
    conn: sqlite3.Connection,
    *,
    doc_date: str,
    amount,
    kind: str = 'ar',
    action: str = 'accrue',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Trích / hoàn / xóa nợ: 2293 phải thu hoặc 2294 HTK."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_ledger_ops_schema(conn, commit=False)
    meta = PROVISION_KINDS.get(str(kind or 'ar').lower()) or PROVISION_KINDS['ar']
    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số tiền dự phòng phải > 0')
    date_s = str(doc_date or '')[:10]
    act = str(action or 'accrue').lower()
    debit, credit = meta['debit'], meta['credit']
    biz = 'TRICH_DU_PHONG'
    prefix = 'DP'
    if act in ('reverse', 'hoan'):
        debit, credit = meta['credit'], meta['debit']
        biz = 'HOAN_DU_PHONG'
        prefix = 'HDP'
    elif act in ('writeoff', 'xoa_no'):
        debit, credit = meta['credit'], meta['writeoff_target']
        biz = 'XOA_NO_DU_PHONG'
        prefix = 'XN'
    desc = notes or f"{meta['label']} — {act}"
    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, None)
    doc_no = _next_no(conn, prefix)
    d_acc = resolve_postable_account(conn, debit)
    c_acc = resolve_postable_account(conn, credit)
    entry = post_journal_entry(
        conn,
        posting_date=date_s, document_date=date_s,
        document_type='DUPHONG', document_no=doc_no,
        business_type=biz, description=desc,
        created_by=created_by, branch_code=branch,
        lines=[
            {'sequence': 1, 'account_code': d_acc, 'debit': float(amt), 'credit': 0, 'description': desc},
            {'sequence': 2, 'account_code': c_acc, 'debit': 0, 'credit': float(amt), 'description': desc},
        ],
    )
    oid = _save_op(
        conn, op_type=f'provision_{kind}_{act}', doc_no=doc_no, doc_date=date_s,
        amount=amt, debit=d_acc, credit=c_acc, party='',
        entry_id=entry['id'], notes=desc, created_by=created_by, branch=branch,
    )
    if commit:
        conn.commit()
    return {'id': oid, 'doc_no': doc_no, 'journal_entry_id': entry['id']}


def accrue_other_tax(
    conn: sqlite3.Connection,
    *,
    doc_date: str,
    amount,
    tax_account: str = '3339',
    expense_account: str | None = None,
    notes: str = '',
    created_by: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Trích thuế/phí khác: Nợ 642|632 / Có 333x."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_ledger_ops_schema(conn, commit=False)
    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số thuế phải > 0')
    date_s = str(doc_date or '')[:10]
    tax_acc = (tax_account or '3339').strip()
    exp = expense_account
    if not exp:
        exp = next((e for c, _n, e in OTHER_TAX_DEFS if c == tax_acc), '642')
    desc = notes or f'Trích {tax_acc}'
    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, None)
    doc_no = _next_no(conn, 'THK')
    d_acc = resolve_postable_account(conn, exp)
    c_acc = resolve_postable_account(conn, tax_acc)
    entry = post_journal_entry(
        conn,
        posting_date=date_s, document_date=date_s,
        document_type='THUEK', document_no=doc_no,
        business_type='TRICH_THUE_KHAC', description=desc,
        created_by=created_by, branch_code=branch,
        lines=[
            {'sequence': 1, 'account_code': d_acc, 'debit': float(amt), 'credit': 0, 'description': desc},
            {'sequence': 2, 'account_code': c_acc, 'debit': 0, 'credit': float(amt), 'description': desc},
        ],
    )
    oid = _save_op(
        conn, op_type='other_tax_accrue', doc_no=doc_no, doc_date=date_s,
        amount=amt, debit=d_acc, credit=c_acc, party='',
        entry_id=entry['id'], notes=desc, created_by=created_by, branch=branch,
    )
    if commit:
        conn.commit()
    return {'id': oid, 'doc_no': doc_no, 'journal_entry_id': entry['id']}


def pay_other_tax(
    conn: sqlite3.Connection,
    *,
    doc_date: str,
    amount,
    tax_account: str = '3339',
    payment_method: str = 'bank',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Nộp thuế/phí/KPCĐ: Nợ 333x|3382 / Có 111|112 (phiếu chi)."""
    from Services.sme.vouchers import create_payment

    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số nộp phải > 0')
    date_s = str(doc_date or '')[:10]
    tax_acc = (tax_account or '3339').strip()
    voucher = create_payment(
        conn,
        voucher_date=date_s,
        party_name='NSNN / công đoàn',
        amount=float(amt),
        payment_method=payment_method or 'bank',
        debit_account=tax_acc,
        reason=notes or f'Nộp {tax_acc}',
        source_type='other_tax',
        created_by=created_by,
        commit=False,
    )
    if commit:
        conn.commit()
    return {'voucher': voucher, 'tax_account': tax_acc}


def acquire_finance_lease(
    conn: sqlite3.Connection,
    *,
    doc_date: str,
    amount,
    lessor_name: str = '',
    asset_account: str = '212',
    liability_account: str = '3412',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Nhận TSCĐ thuê tài chính: Nợ 212 / Có 3412."""
    from Services.sme.loans_deposits import disburse_loan

    return disburse_loan(
        conn,
        start_date=doc_date,
        lender_name=lessor_name or 'Bên cho thuê',
        principal=amount,
        liability_account=liability_account or '3412',
        cash_account=asset_account or '212',
        notes=notes or 'TSCĐ thuê tài chính',
        created_by=created_by,
        commit=commit,
    )


def kpcd_employer_amount(gross_salary) -> Decimal:
    return _money(_money(gross_salary) * KPCD_EMPLOYER_RATE)


def list_ledger_ops(
    conn: sqlite3.Connection,
    *,
    op_type: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_ledger_ops_schema(conn, commit=False)
    sql = "SELECT * FROM sme_ledger_ops WHERE status != 'void'"
    params: list[Any] = []
    if op_type:
        sql += ' AND op_type LIKE ?'
        params.append(f'{op_type}%')
    sql += ' ORDER BY doc_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
