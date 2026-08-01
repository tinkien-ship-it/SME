"""Góp vốn / cổ tức SME — TK 4111, 4212, 111/112, 3388."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry, reverse_journal_entry

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_capital_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_capital_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_type TEXT NOT NULL,
            doc_no TEXT NOT NULL UNIQUE,
            doc_date TEXT NOT NULL,
            party_name TEXT,
            amount REAL NOT NULL DEFAULT 0,
            equity_account TEXT NOT NULL DEFAULT '4111',
            cash_account TEXT NOT NULL DEFAULT '1121',
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'posted',
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            branch_code TEXT
        )
        """
    )
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, 'sme_capital_docs')
    if commit:
        conn.commit()


def _next_no(conn: sqlite3.Connection, prefix: str) -> str:
    row = conn.execute(
        "SELECT doc_no FROM sme_capital_docs WHERE doc_no LIKE ? ORDER BY id DESC LIMIT 1",
        (f'{prefix}%',),
    ).fetchone()
    if not row:
        return f'{prefix}000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'{prefix}{int(digits) + 1:06d}'


def contribute_capital(
    conn: sqlite3.Connection,
    *,
    doc_date: str,
    amount,
    party_name: str = '',
    equity_account: str = '4111',
    cash_account: str = '1121',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Góp vốn: Nợ 111/112 / Có 4111."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_capital_schema(conn, commit=False)
    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số tiền góp vốn phải > 0')
    date_s = str(doc_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày')
    eq = (equity_account or '4111').strip() or '4111'
    cash = (cash_account or '1121').strip() or '1121'
    doc_no = _next_no(conn, 'GV')
    desc = notes or f'Góp vốn {party_name or ""}'.strip() or f'Góp vốn {doc_no}'
    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, None)
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='GOPVON',
        document_no=doc_no,
        business_type='GOP_VON',
        description=desc,
        created_by=created_by,
        branch_code=branch,
        lines=[
            {'sequence': 1, 'account_code': cash, 'debit': float(amt), 'credit': 0, 'description': desc},
            {'sequence': 2, 'account_code': eq, 'debit': 0, 'credit': float(amt), 'description': desc},
        ],
    )
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_capital_docs (
            doc_type, doc_no, doc_date, party_name, amount, equity_account, cash_account,
            journal_entry_id, status, notes, created_by, created_at, branch_code
        ) VALUES ('contribute',?,?,?,?,?,?,?,'posted',?,?,?,?)
        """,
        (doc_no, date_s, party_name or '', float(amt), eq, cash, entry['id'], notes or '', created_by, _now(), branch),
    )
    if commit:
        conn.commit()
    return get_capital_doc(conn, cur.lastrowid)


def declare_dividend(
    conn: sqlite3.Connection,
    *,
    doc_date: str,
    amount,
    party_name: str = 'Cổ đông / chủ sở hữu',
    equity_account: str = '4212',
    payable_account: str = '3388',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Công bố cổ tức / phân phối LN: Nợ 4212 / Có 3388."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_capital_schema(conn, commit=False)
    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số tiền cổ tức phải > 0')
    date_s = str(doc_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày')
    eq = (equity_account or '4212').strip() or '4212'
    pay = (payable_account or '3388').strip() or '3388'
    doc_no = _next_no(conn, 'CT')
    desc = notes or f'Công bố cổ tức {doc_no}'
    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, None)
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='COTUC',
        document_no=doc_no,
        business_type='CONG_BO_CO_TUC',
        description=desc,
        created_by=created_by,
        branch_code=branch,
        lines=[
            {'sequence': 1, 'account_code': eq, 'debit': float(amt), 'credit': 0, 'description': desc},
            {'sequence': 2, 'account_code': pay, 'debit': 0, 'credit': float(amt), 'description': desc},
        ],
    )
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_capital_docs (
            doc_type, doc_no, doc_date, party_name, amount, equity_account, cash_account,
            journal_entry_id, status, notes, created_by, created_at, branch_code
        ) VALUES ('dividend',?,?,?,?,?,?,?,'posted',?,?,?,?)
        """,
        (doc_no, date_s, party_name or '', float(amt), eq, pay, entry['id'], notes or '', created_by, _now(), branch),
    )
    if commit:
        conn.commit()
    return get_capital_doc(conn, cur.lastrowid)


def pay_dividend(
    conn: sqlite3.Connection,
    *,
    doc_date: str,
    amount,
    party_name: str = 'Cổ đông / chủ sở hữu',
    payable_account: str = '3388',
    cash_account: str = '1121',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Chi trả cổ tức: Nợ 3388 / Có tiền."""
    from Services.sme.vouchers import create_payment

    ensure_sme_capital_schema(conn, commit=False)
    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số tiền chi trả phải > 0')
    date_s = str(doc_date or '')[:10]
    method = 'bank' if (cash_account or '1121').startswith('112') else 'cash'
    voucher = create_payment(
        conn,
        voucher_date=date_s,
        party_name=party_name or 'Cổ đông',
        amount=float(amt),
        payment_method=method,
        debit_account=(payable_account or '3388').strip() or '3388',
        reason=notes or 'Chi trả cổ tức / phân phối LN',
        reference_document='DIVIDEND',
        source_type='dividend',
        created_by=created_by,
        commit=False,
    )
    doc_no = _next_no(conn, 'TTCT')
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_capital_docs (
            doc_type, doc_no, doc_date, party_name, amount, equity_account, cash_account,
            journal_entry_id, status, notes, created_by, created_at
        ) VALUES ('dividend_pay',?,?,?,?,?,?,?,'posted',?,?,?)
        """,
        (
            doc_no, date_s, party_name or '', float(amt),
            payable_account or '3388', cash_account or '1121',
            voucher.get('journal_entry_id'), notes or '', created_by, _now(),
        ),
    )
    if commit:
        conn.commit()
    out = get_capital_doc(conn, cur.lastrowid)
    out['voucher'] = voucher
    return out


def get_capital_doc(conn: sqlite3.Connection, doc_id: int) -> dict[str, Any] | None:
    ensure_sme_capital_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_capital_docs WHERE id = ?', (doc_id,)).fetchone()
    return dict(row) if row else None


def list_capital_docs(
    conn: sqlite3.Connection,
    *,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_capital_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where
    sql = "SELECT * FROM sme_capital_docs WHERE status != 'void'"
    params: list[Any] = []
    bf, bp = branch_where(branch_code)
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY doc_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def void_capital_doc(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    reason: str = 'Hủy chứng từ vốn',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_capital_docs', doc_id, label='Chứng từ vốn')
    doc = get_capital_doc(conn, doc_id)
    if not doc:
        raise ValueError('Không tìm thấy chứng từ')
    if doc['status'] == 'void':
        raise ValueError('Đã hủy')
    if doc.get('journal_entry_id'):
        reverse_journal_entry(
            conn, int(doc['journal_entry_id']),
            created_by=created_by, reason=reason,
        )
    conn.execute(
        "UPDATE sme_capital_docs SET status = 'void', notes = ? WHERE id = ?",
        ((doc.get('notes') or '') + f' | {reason}', doc_id),
    )
    if commit:
        conn.commit()
    return get_capital_doc(conn, doc_id)
