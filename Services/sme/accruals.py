"""Chi phí phải trả (335) và doanh thu chưa thực hiện (3387)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    post_journal_entry,
    resolve_postable_account,
    reverse_journal_entry,
)

MONEY_Q = Decimal('0.01')
TABLE = 'sme_accrual_docs'
KIND_EXPENSE = 'expense'  # Nợ CP / Có 335
KIND_INCOME = 'income'    # Nợ tiền / Có 3387


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_accrual_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_accrual_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            doc_no TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            doc_date TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            contra_account TEXT NOT NULL,
            liability_account TEXT NOT NULL,
            payment_method TEXT DEFAULT 'bank',
            journal_entry_id INTEGER,
            settle_journal_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            branch_code TEXT
        )
        """
    )
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, TABLE)
    if commit:
        conn.commit()


def _next_no(conn: sqlite3.Connection, prefix: str) -> str:
    row = conn.execute(
        "SELECT doc_no FROM sme_accrual_docs WHERE doc_no LIKE ? ORDER BY id DESC LIMIT 1",
        (f'{prefix}%',),
    ).fetchone()
    if not row:
        return f'{prefix}000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'{prefix}{int(digits) + 1:06d}'


def get_accrual(conn: sqlite3.Connection, doc_id: int) -> dict[str, Any] | None:
    ensure_accrual_schema(conn, commit=False)
    row = conn.execute(f'SELECT * FROM {TABLE} WHERE id = ?', (doc_id,)).fetchone()
    return dict(row) if row else None


def list_accruals(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    status: str | None = None,
    branch_code: str | None = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    ensure_accrual_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where
    sql = f'SELECT * FROM {TABLE} WHERE 1=1'
    params: list[Any] = []
    if kind in (KIND_EXPENSE, KIND_INCOME):
        sql += ' AND kind = ?'
        params.append(kind)
    st = (status or '').strip().lower()
    if st == 'void':
        sql += " AND status = 'void'"
    elif st != 'all':
        sql += " AND status != 'void'"
    bf, bp = branch_where(branch_code)
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY doc_date DESC, id DESC LIMIT ?'
    params.append(int(limit) or 300)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def create_accrual(
    conn: sqlite3.Connection,
    *,
    kind: str,
    name: str,
    doc_date: str,
    amount,
    contra_account: str | None = None,
    payment_method: str = 'bank',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """expense: Nợ 642 / Có 335. income: Nợ 111|112 / Có 3387."""
    from Services.sme.branches import resolve_posting_branch
    from Services.sme.branch_filter import stamp_row_branch

    ensure_sme_journal_ready(conn, commit=False)
    ensure_accrual_schema(conn, commit=False)
    k = (kind or KIND_EXPENSE).strip().lower()
    if k not in (KIND_EXPENSE, KIND_INCOME):
        raise ValueError('Loại phải là expense (335) hoặc income (3387)')
    title = (name or '').strip()
    if not title:
        raise ValueError('Thiếu nội dung')
    date_s = str(doc_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày')
    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số tiền phải > 0')
    pm = (payment_method or 'bank').strip().lower()
    if pm in ('ck', '112', '1121', 'transfer'):
        pm = 'bank'
    if pm in ('tm', '111', '1111'):
        pm = 'cash'
    cash = '1111' if pm == 'cash' else '1121'
    branch = resolve_posting_branch(conn, None)
    desc = notes or title

    if k == KIND_EXPENSE:
        liab = resolve_postable_account(conn, '335')
        contra = resolve_postable_account(conn, (contra_account or '642').strip() or '642')
        prefix = 'CPPT'
        doc_no = _next_no(conn, prefix)
        entry = post_journal_entry(
            conn,
            posting_date=date_s, document_date=date_s,
            document_type='CPPT', document_no=doc_no,
            business_type='TRICH_CP_PHAI_TRA', description=desc,
            created_by=created_by, branch_code=branch,
            lines=[
                {'sequence': 1, 'account_code': contra, 'debit': float(amt), 'credit': 0, 'description': desc},
                {'sequence': 2, 'account_code': liab, 'debit': 0, 'credit': float(amt), 'description': desc},
            ],
        )
        entry_id = entry['id']
    else:
        liab = resolve_postable_account(conn, '3387')
        contra = resolve_postable_account(conn, (contra_account or '5111').strip() or '5111')
        from Services.sme.vouchers import create_receipt
        prefix = 'DTCT'
        doc_no = _next_no(conn, prefix)
        voucher = create_receipt(
            conn,
            voucher_date=date_s,
            party_name=title,
            amount=float(amt),
            payment_method=pm,
            credit_account='3387',
            reason=desc,
            source_type='unearned',
            created_by=created_by,
            commit=False,
        )
        entry_id = voucher.get('journal_entry_id')

    cur = conn.cursor()
    cur.execute(
        f"""
        INSERT INTO {TABLE} (
            kind, doc_no, name, doc_date, amount, contra_account, liability_account,
            payment_method, journal_entry_id, status, notes, created_by, created_at, branch_code
        ) VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?,?)
        """,
        (
            k, doc_no, title, date_s, float(amt),
            contra, liab, pm, entry_id,
            notes or '', created_by, _now(), branch,
        ),
    )
    rid = int(cur.lastrowid)
    stamp_row_branch(conn, TABLE, rid, branch)
    if commit:
        conn.commit()
    return get_accrual(conn, rid) or {'id': rid}


def settle_accrual(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    settle_date: str | None = None,
    payment_method: str | None = None,
    created_by: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """expense: Nợ 335 / Có tiền. income: Nợ 3387 / Có 511."""
    from Services.sme.branch_filter import assert_row_in_branch

    ensure_sme_journal_ready(conn, commit=False)
    ensure_accrual_schema(conn, commit=False)
    assert_row_in_branch(conn, TABLE, doc_id, label='Chứng từ 335/3387')
    row = get_accrual(conn, doc_id)
    if not row:
        raise ValueError('Không tìm thấy chứng từ')
    if row.get('status') != 'open':
        raise ValueError('Chứng từ không còn mở')
    date_s = str(settle_date or datetime.now().strftime('%Y-%m-%d'))[:10]
    amt = _money(row['amount'])
    pm = (payment_method or row.get('payment_method') or 'bank').strip().lower()
    desc = f"Tất toán {row['doc_no']}: {row['name']}"
    liab = resolve_postable_account(conn, row.get('liability_account') or ('335' if row['kind'] == KIND_EXPENSE else '3387'))

    if row['kind'] == KIND_EXPENSE:
        from Services.sme.vouchers import create_payment
        voucher = create_payment(
            conn,
            voucher_date=date_s,
            party_name=row['name'],
            amount=float(amt),
            payment_method='cash' if pm == 'cash' else 'bank',
            debit_account=liab,
            reason=desc,
            source_type='accrual',
            created_by=created_by,
            commit=False,
        )
        settle_id = voucher.get('journal_entry_id')
    else:
        rev = resolve_postable_account(conn, row.get('contra_account') or '5111')
        from Services.sme.branches import resolve_posting_branch
        branch = resolve_posting_branch(conn, row.get('branch_code'))
        entry = post_journal_entry(
            conn,
            posting_date=date_s, document_date=date_s,
            document_type='GHIDT', document_no=f"G{row['doc_no']}",
            business_type='GHI_NHAN_DT_CTT', description=desc,
            created_by=created_by, branch_code=branch,
            lines=[
                {'sequence': 1, 'account_code': liab, 'debit': float(amt), 'credit': 0, 'description': desc},
                {'sequence': 2, 'account_code': rev, 'debit': 0, 'credit': float(amt), 'description': desc},
            ],
        )
        settle_id = entry['id']

    conn.execute(
        f"UPDATE {TABLE} SET status = 'settled', settle_journal_id = ? WHERE id = ?",
        (settle_id, int(doc_id)),
    )
    if commit:
        conn.commit()
    return get_accrual(conn, doc_id) or row


def void_accrual(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    reason: str = 'Hủy chứng từ 335/3387',
    created_by: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    from Services.sme.branch_filter import assert_row_in_branch

    assert_row_in_branch(conn, TABLE, doc_id, label='Chứng từ 335/3387')
    row = get_accrual(conn, doc_id)
    if not row:
        raise ValueError('Không tìm thấy chứng từ')
    if row.get('status') == 'void':
        return row
    if row.get('status') == 'settled':
        raise ValueError('Đã tất toán — đảo bút toán tất toán trước khi hủy gốc')
    jid = row.get('journal_entry_id')
    if jid:
        reverse_journal_entry(conn, int(jid), created_by=created_by, reason=reason)
    conn.execute(f"UPDATE {TABLE} SET status = 'void' WHERE id = ?", (int(doc_id),))
    if commit:
        conn.commit()
    return get_accrual(conn, doc_id) or row
