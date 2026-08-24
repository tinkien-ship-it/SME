"""Kiểm kê quỹ tiền mặt SME — mẫu 08a-TT."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.cash_books import cash_account_book
from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry, reverse_journal_entry
from db_utils import sqlite_commit

MONEY_Q = Decimal('0.01')
FORM_CASH_COUNT = '08a-TT'


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_cash_count_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_cash_counts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_code TEXT NOT NULL DEFAULT '08a-TT',
            doc_no TEXT NOT NULL UNIQUE,
            count_date TEXT NOT NULL,
            account_code TEXT NOT NULL DEFAULT '1111',
            book_balance REAL NOT NULL DEFAULT 0,
            counted_amount REAL NOT NULL DEFAULT 0,
            difference REAL NOT NULL DEFAULT 0,
            denominations_json TEXT,
            committee TEXT,
            notes TEXT,
            surplus_account TEXT DEFAULT '711',
            shortage_account TEXT DEFAULT '811',
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'draft',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_cash_counts_date ON sme_cash_counts(count_date)'
    )
    
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_cash_counts)').fetchall()}
    if 'branch_code' not in cols:
        try:
            conn.execute('ALTER TABLE sme_cash_counts ADD COLUMN branch_code TEXT')
        except Exception:
            pass
    if commit:
        sqlite_commit(conn, label='cash_count')


def _next_doc_no(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT doc_no FROM sme_cash_counts WHERE doc_no LIKE 'KKQ%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return 'KKQ000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'KKQ{int(digits) + 1:06d}'


def book_cash_balance(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    account_code: str = '1111',
    branch_code: str | None = None,
) -> float:
    """Số dư sổ quỹ đến hết ngày as_of (TK 111*)."""
    ensure_sme_journal_ready(conn, commit=False)
    date_s = str(as_of or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày kiểm kê')
    year = int(date_s[:4])
    selected = (account_code or '1111').strip() or '1111'
    prefix = '111' if selected.startswith('111') else selected[:3]
    book = cash_account_book(
        conn, fiscal_year=year, account_prefix=prefix, account_code=selected,
        branch_code=branch_code,
    )
    bal = _money(book.get('opening_balance'))
    for ln in book.get('rows') or []:
        if str(ln.get('posting_date') or '')[:10] > date_s:
            break
        bal += _money(ln.get('receipt')) - _money(ln.get('payment'))
    return _f(bal)


def create_cash_count(
    conn: sqlite3.Connection,
    *,
    count_date: str,
    counted_amount,
    account_code: str = '1111',
    denominations: dict | list | None = None,
    committee: str = '',
    notes: str = '',
    post_difference: bool = True,
    surplus_account: str = '711',
    shortage_account: str = '811',
    branch_code: str | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Lập biên bản kiểm kê quỹ 08a-TT; tùy chọn ghi sổ chênh lệch."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_cash_count_schema(conn, commit=False)

    date_s = str(count_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày kiểm kê')
    counted = _money(counted_amount)
    if counted < 0:
        raise ValueError('Số tiền kiểm kê không được âm')

    acc = (account_code or '1111').strip() or '1111'
    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, branch_code)
    book = _money(book_cash_balance(conn, as_of=date_s, account_code=acc, branch_code=branch))
    diff = counted - book
    doc_no = _next_doc_no(conn)
    den_json = json.dumps(denominations or {}, ensure_ascii=False)

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_cash_counts (
            form_code, doc_no, count_date, account_code, book_balance, counted_amount,
            difference, denominations_json, committee, notes,
            surplus_account, shortage_account, status, created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?)
        """,
        (
            FORM_CASH_COUNT, doc_no, date_s, acc, float(book), float(counted),
            float(diff), den_json, committee or '', notes or '',
            surplus_account or '711', shortage_account or '811',
            created_by, _now(), _now(),
        ),
    )
    doc_id = cur.lastrowid
    try:
        conn.execute('UPDATE sme_cash_counts SET branch_code = ? WHERE id = ?', (branch, doc_id))
    except Exception:
        pass
    entry = None

    if post_difference and diff != 0:
        if diff > 0:
            # Thừa quỹ: Nợ tiền / Có thu nhập khác
            lines = [
                {'sequence': 1, 'account_code': acc, 'debit': float(diff), 'credit': 0,
                 'description': f'Thừa quỹ {doc_no}'},
                {'sequence': 2, 'account_code': surplus_account or '711', 'debit': 0, 'credit': float(diff),
                 'description': f'Thừa quỹ {doc_no}'},
            ]
            biz = 'KIEM_KE_QUY_THUA'
        else:
            shortage = abs(diff)
            lines = [
                {'sequence': 1, 'account_code': shortage_account or '811', 'debit': float(shortage), 'credit': 0,
                 'description': f'Thiếu quỹ {doc_no}'},
                {'sequence': 2, 'account_code': acc, 'debit': 0, 'credit': float(shortage),
                 'description': f'Thiếu quỹ {doc_no}'},
            ]
            biz = 'KIEM_KE_QUY_THIEU'
        entry = post_journal_entry(
            conn,
            posting_date=date_s,
            document_date=date_s,
            document_type='KKQ',
            document_no=doc_no,
            document_id=doc_id,
            business_type=biz,
            description=f'Kiểm kê quỹ {doc_no}',
            created_by=created_by,
            branch_code=branch,
            lines=lines,
        )
        conn.execute(
            """
            UPDATE sme_cash_counts
            SET journal_entry_id = ?, status = 'posted', updated_at = ?
            WHERE id = ?
            """,
            (entry['id'], _now(), doc_id),
        )
    else:
        conn.execute(
            "UPDATE sme_cash_counts SET status = 'posted', updated_at = ? WHERE id = ?",
            (_now(), doc_id),
        )

    if commit:
        sqlite_commit(conn, label='cash_count')
    return get_cash_count(conn, doc_id)


def list_cash_counts(
    conn: sqlite3.Connection,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    form_code: str | None = None,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_cash_count_schema(conn, commit=False)
    from Services.sme.branches import DEFAULT_BRANCH_CODE
    sql = "SELECT * FROM sme_cash_counts WHERE status != 'void'"
    params: list[Any] = []
    if form_code:
        sql += ' AND form_code = ?'
        params.append(form_code)
    code = (branch_code or '').strip().upper()
    if code and code != 'ALL':
        if code == DEFAULT_BRANCH_CODE:
            sql += " AND (branch_code IS NULL OR branch_code = '' OR branch_code = ?)"
        else:
            sql += ' AND branch_code = ?'
        params.append(code)
    if date_from:
        sql += ' AND date(count_date) >= date(?)'
        params.append(date_from[:10])
    if date_to:
        sql += ' AND date(count_date) <= date(?)'
        params.append(date_to[:10])
    sql += ' ORDER BY count_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    rows = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        try:
            d['denominations'] = json.loads(d.get('denominations_json') or '{}')
        except json.JSONDecodeError:
            d['denominations'] = {}
        rows.append(d)
    return rows


def get_cash_count(conn: sqlite3.Connection, doc_id: int) -> dict[str, Any] | None:
    ensure_sme_cash_count_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_cash_counts WHERE id = ?', (doc_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d['denominations'] = json.loads(d.get('denominations_json') or '{}')
    except json.JSONDecodeError:
        d['denominations'] = {}
    return d


def void_cash_count(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    reason: str = 'Hủy biên bản kiểm kê quỹ',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_cash_counts', doc_id, label='Biên bản kiểm kê quỹ')
    doc = get_cash_count(conn, doc_id)
    if not doc:
        raise ValueError('Không tìm thấy biên bản kiểm kê')
    if doc['status'] == 'void':
        raise ValueError('Biên bản đã hủy')
    if doc.get('journal_entry_id'):
        reverse_journal_entry(
            conn, int(doc['journal_entry_id']),
            created_by=created_by, reason=reason,
        )
    conn.execute(
        "UPDATE sme_cash_counts SET status = 'void', notes = ?, updated_at = ? WHERE id = ?",
        ((doc.get('notes') or '') + f' | {reason}', _now(), doc_id),
    )
    if commit:
        sqlite_commit(conn, label='cash_count')
    return get_cash_count(conn, doc_id)
