"""Đối chiếu ngân hàng SME — sao kê ↔ phát sinh TK 112*."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.cash_books import cash_account_book
from Services.sme.journal_engine import ensure_sme_journal_ready

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_bank_reconcile_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_bank_reconciliations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reconcile_date TEXT NOT NULL,
            account_code TEXT NOT NULL DEFAULT '1121',
            date_from TEXT NOT NULL,
            date_to TEXT NOT NULL,
            statement_balance REAL NOT NULL DEFAULT 0,
            book_balance REAL NOT NULL DEFAULT 0,
            unmatched_book REAL NOT NULL DEFAULT 0,
            unmatched_bank REAL NOT NULL DEFAULT 0,
            difference REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            branch_code TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_bank_reconcile_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reconcile_id INTEGER NOT NULL,
            journal_line_id INTEGER,
            bank_txn_id INTEGER,
            amount REAL NOT NULL DEFAULT 0,
            match_note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(reconcile_id, journal_line_id, bank_txn_id),
            FOREIGN KEY(reconcile_id) REFERENCES sme_bank_reconciliations(id)
        )
        """
    )
    # Cột đánh dấu dòng sổ đã khớp (độc lập kỳ đối chiếu gần nhất)
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_journal_lines)').fetchall()}
        if 'bank_reconciled' not in cols:
            conn.execute('ALTER TABLE sme_journal_lines ADD COLUMN bank_reconciled INTEGER DEFAULT 0')
        if 'bank_reconcile_id' not in cols:
            conn.execute('ALTER TABLE sme_journal_lines ADD COLUMN bank_reconcile_id INTEGER')
    except sqlite3.Error:
        pass
    try:
        from Services.payment_bank import ensure_bank_transactions_table
        ensure_bank_transactions_table(conn)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(bank_transactions)').fetchall()}
        if 'ledger_reconciled' not in cols:
            conn.execute('ALTER TABLE bank_transactions ADD COLUMN ledger_reconciled INTEGER DEFAULT 0')
        if 'ledger_reconcile_id' not in cols:
            conn.execute('ALTER TABLE bank_transactions ADD COLUMN ledger_reconcile_id INTEGER')
    except Exception:
        pass
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, 'sme_bank_reconciliations')
    if commit:
        conn.commit()


def book_bank_balance(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    account_code: str = '1121',
    branch_code: str | None = None,
) -> float:
    ensure_sme_journal_ready(conn, commit=False)
    date_s = str(as_of or '')[:10]
    year = int(date_s[:4])
    selected = (account_code or '1121').strip() or '1121'
    book = cash_account_book(
        conn,
        fiscal_year=year,
        account_prefix='112',
        account_code=selected,
        branch_code=branch_code,
    )
    bal = _money(book.get('opening_balance'))
    for ln in book.get('rows') or []:
        if str(ln.get('posting_date') or '')[:10] > date_s:
            break
        bal += _money(ln.get('receipt')) - _money(ln.get('payment'))
    return _f(bal)


def list_book_movements(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
    account_code: str = '1121',
    only_unmatched: bool = True,
    branch_code: str | None = None,
) -> list[dict[str, Any]]:
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_bank_reconcile_schema(conn, commit=False)
    selected = (account_code or '1121').strip() or '1121'
    from Services.sme.branches import branch_sql_filter
    sql = """
        SELECT
            jl.id AS line_id,
            jl.entry_id,
            jl.account_code,
            jl.debit,
            jl.credit,
            COALESCE(jl.description, je.description, '') AS description,
            je.entry_no,
            je.posting_date,
            je.document_no,
            je.document_type,
            COALESCE(jl.bank_reconciled, 0) AS bank_reconciled
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND date(je.posting_date) >= date(?)
          AND date(je.posting_date) <= date(?)
          AND (jl.account_code = ? OR jl.account_code LIKE ?)
    """
    params: list[Any] = [date_from[:10], date_to[:10], selected, f'{selected}%']
    bf, bp = branch_sql_filter(branch_code, alias='je')
    sql += bf
    params.extend(bp)
    if only_unmatched:
        sql += ' AND COALESCE(jl.bank_reconciled, 0) = 0'
    sql += ' ORDER BY je.posting_date, jl.id'
    rows = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d['amount'] = _f(_money(d['debit']) - _money(d['credit']))
        d['signed_amount'] = d['amount']
        rows.append(d)
    return rows


def list_statement_movements(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
    only_unmatched: bool = True,
    branch_code: str | None = None,
) -> list[dict[str, Any]]:
    ensure_sme_bank_reconcile_schema(conn, commit=False)
    try:
        sql = """
            SELECT id, provider, external_id, amount, content, transaction_date,
                   direction, match_status, counterparty_name,
                   COALESCE(ledger_reconciled, 0) AS ledger_reconciled
            FROM bank_transactions
            WHERE date(substr(COALESCE(transaction_date,''),1,10)) >= date(?)
              AND date(substr(COALESCE(transaction_date,''),1,10)) <= date(?)
        """
        params: list[Any] = [date_from[:10], date_to[:10]]
        if only_unmatched:
            sql += ' AND COALESCE(ledger_reconciled, 0) = 0'
        code = (branch_code or '').strip().upper()
        if code and code != 'ALL':
            # Ẩn dòng sao kê đã gắn sale của CN khác; dòng chưa gắn sale vẫn hiện (feed NH dùng chung)
            try:
                from Services.sme.branches import sale_branch_filter_sql
                bf, bp = sale_branch_filter_sql(conn, code, alias='s')
                sql += f"""
                  AND (
                    (COALESCE(sale_id, extracted_sale_id) IS NULL)
                    OR COALESCE(sale_id, extracted_sale_id) IN (
                        SELECT s.id FROM sale s WHERE 1=1 {bf}
                    )
                  )
                """
                params.extend(bp)
            except Exception:
                pass
        sql += ' ORDER BY transaction_date, id'
        rows = []
        for r in conn.execute(sql, params).fetchall():
            d = dict(r)
            amt = _money(d.get('amount'))
            direction = (d.get('direction') or 'in').lower()
            # Chuẩn hóa: vào NH > 0, ra NH < 0 (khớp chiều sổ 112: Nợ tăng)
            signed = amt if direction in ('in', 'credit', '+') else -abs(amt)
            if direction not in ('in', 'out', 'credit', 'debit', '+', '-'):
                signed = amt  # giữ nguyên nếu provider đã signed
            d['signed_amount'] = _f(signed)
            rows.append(d)
        return rows
    except sqlite3.Error:
        return []


def create_reconciliation(
    conn: sqlite3.Connection,
    *,
    reconcile_date: str,
    date_from: str,
    date_to: str,
    statement_balance,
    account_code: str = '1121',
    notes: str = '',
    created_by: str | None = None,
    branch_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    ensure_sme_bank_reconcile_schema(conn, commit=False)
    date_s = str(reconcile_date or date_to or '')[:10]
    df = str(date_from or '')[:10]
    dt = str(date_to or date_s)[:10]
    if not date_s or not df or not dt:
        raise ValueError('Thiếu khoảng ngày đối chiếu')
    acc = (account_code or '1121').strip() or '1121'
    from Services.sme.branches import request_branch_filter
    br = branch_code if branch_code is not None else None
    if br is None:
        try:
            br = request_branch_filter()
        except Exception:
            br = None
    book = _money(book_bank_balance(conn, as_of=dt, account_code=acc, branch_code=br))
    stmt = _money(statement_balance)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_bank_reconciliations (
            reconcile_date, account_code, date_from, date_to,
            statement_balance, book_balance, difference, status,
            notes, created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,'open',?,?,?,?)
        """,
        (
            date_s, acc, df, dt, float(stmt), float(book), float(stmt - book),
            notes or '', created_by, _now(), _now(),
        ),
    )
    rid = cur.lastrowid
    from Services.sme.branch_filter import stamp_row_branch
    stamp_row_branch(conn, 'sme_bank_reconciliations', rid, br)
    if commit:
        conn.commit()
    return get_reconciliation(conn, rid)


def match_lines(
    conn: sqlite3.Connection,
    reconcile_id: int,
    *,
    journal_line_id: int,
    bank_txn_id: int,
    note: str = '',
    commit: bool = False,
) -> dict[str, Any]:
    ensure_sme_bank_reconcile_schema(conn, commit=False)
    rec = get_reconciliation(conn, reconcile_id)
    if not rec:
        raise ValueError('Không tìm thấy phiên đối chiếu')
    if rec['status'] == 'closed':
        raise ValueError('Phiên đối chiếu đã đóng')

    book = conn.execute(
        'SELECT id, debit, credit, COALESCE(bank_reconciled,0) AS bank_reconciled FROM sme_journal_lines WHERE id = ?',
        (journal_line_id,),
    ).fetchone()
    if not book:
        raise ValueError('Không tìm thấy dòng sổ cái')
    book_d = dict(book)
    if book_d.get('bank_reconciled'):
        raise ValueError('Dòng sổ đã được đối chiếu')

    bank = conn.execute(
        'SELECT id, amount, direction, COALESCE(ledger_reconciled,0) AS ledger_reconciled FROM bank_transactions WHERE id = ?',
        (bank_txn_id,),
    ).fetchone()
    if not bank:
        raise ValueError('Không tìm thấy giao dịch sao kê')
    bank_d = dict(bank)
    if bank_d.get('ledger_reconciled'):
        raise ValueError('Giao dịch sao kê đã đối chiếu')

    book_amt = abs(_money(book_d['debit']) - _money(book_d['credit']))
    bank_amt = abs(_money(bank_d['amount']))
    if abs(book_amt - bank_amt) > Decimal('1.00'):
        raise ValueError(f'Số tiền lệch: sổ {book_amt} ≠ sao kê {bank_amt}')

    conn.execute(
        """
        INSERT INTO sme_bank_reconcile_matches
            (reconcile_id, journal_line_id, bank_txn_id, amount, match_note)
        VALUES (?,?,?,?,?)
        """,
        (reconcile_id, journal_line_id, bank_txn_id, float(book_amt), note or ''),
    )
    conn.execute(
        'UPDATE sme_journal_lines SET bank_reconciled = 1, bank_reconcile_id = ? WHERE id = ?',
        (reconcile_id, journal_line_id),
    )
    conn.execute(
        'UPDATE bank_transactions SET ledger_reconciled = 1, ledger_reconcile_id = ? WHERE id = ?',
        (reconcile_id, bank_txn_id),
    )
    _refresh_totals(conn, reconcile_id)
    if commit:
        conn.commit()
    return get_reconciliation(conn, reconcile_id)


def unmatch(
    conn: sqlite3.Connection,
    match_id: int,
    *,
    commit: bool = False,
) -> dict[str, Any]:
    ensure_sme_bank_reconcile_schema(conn, commit=False)
    row = conn.execute(
        'SELECT * FROM sme_bank_reconcile_matches WHERE id = ?', (match_id,)
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy cặp khớp')
    m = dict(row)
    rid = int(m['reconcile_id'])
    rec = get_reconciliation(conn, rid)
    if rec and rec['status'] == 'closed':
        raise ValueError('Phiên đã đóng — mở lại trước khi bỏ khớp')
    if m.get('journal_line_id'):
        conn.execute(
            'UPDATE sme_journal_lines SET bank_reconciled = 0, bank_reconcile_id = NULL WHERE id = ?',
            (int(m['journal_line_id']),),
        )
    if m.get('bank_txn_id'):
        conn.execute(
            'UPDATE bank_transactions SET ledger_reconciled = 0, ledger_reconcile_id = NULL WHERE id = ?',
            (int(m['bank_txn_id']),),
        )
    conn.execute('DELETE FROM sme_bank_reconcile_matches WHERE id = ?', (match_id,))
    _refresh_totals(conn, rid)
    if commit:
        conn.commit()
    return get_reconciliation(conn, rid)


def _refresh_totals(conn: sqlite3.Connection, reconcile_id: int) -> None:
    rec = get_reconciliation(conn, reconcile_id)
    if not rec:
        return
    book_un = list_book_movements(
        conn,
        date_from=rec['date_from'], date_to=rec['date_to'],
        account_code=rec['account_code'], only_unmatched=True,
        branch_code=rec.get('branch_code'),
    )
    bank_un = list_statement_movements(
        conn, date_from=rec['date_from'], date_to=rec['date_to'], only_unmatched=True,
        branch_code=rec.get('branch_code'),
    )
    ub = sum((_money(x.get('signed_amount')) for x in book_un), Decimal('0.00'))
    uk = sum((_money(x.get('signed_amount')) for x in bank_un), Decimal('0.00'))
    book = _money(book_bank_balance(
        conn, as_of=rec['date_to'], account_code=rec['account_code'],
        branch_code=rec.get('branch_code'),
    ))
    stmt = _money(rec['statement_balance'])
    conn.execute(
        """
        UPDATE sme_bank_reconciliations
        SET book_balance = ?, unmatched_book = ?, unmatched_bank = ?,
            difference = ?, updated_at = ?
        WHERE id = ?
        """,
        (float(book), float(ub), float(uk), float(stmt - book), _now(), reconcile_id),
    )


def close_reconciliation(
    conn: sqlite3.Connection,
    reconcile_id: int,
    *,
    force: bool = False,
    commit: bool = False,
) -> dict[str, Any]:
    _refresh_totals(conn, reconcile_id)
    rec = get_reconciliation(conn, reconcile_id)
    if not rec:
        raise ValueError('Không tìm thấy phiên đối chiếu')
    diff = abs(_money(rec.get('difference')))
    if diff > Decimal('1.00') and not force:
        raise ValueError(
            f'Chưa khớp số dư (lệch {diff}). Khớp thêm dòng hoặc force=true nếu chấp nhận.'
        )
    conn.execute(
        "UPDATE sme_bank_reconciliations SET status = 'closed', updated_at = ? WHERE id = ?",
        (_now(), reconcile_id),
    )
    if commit:
        conn.commit()
    return get_reconciliation(conn, reconcile_id)


def get_reconciliation(conn: sqlite3.Connection, reconcile_id: int) -> dict[str, Any] | None:
    ensure_sme_bank_reconcile_schema(conn, commit=False)
    row = conn.execute(
        'SELECT * FROM sme_bank_reconciliations WHERE id = ?', (reconcile_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    matches = conn.execute(
        'SELECT * FROM sme_bank_reconcile_matches WHERE reconcile_id = ? ORDER BY id',
        (reconcile_id,),
    ).fetchall()
    d['matches'] = [dict(m) for m in matches]
    return d


def list_reconciliations(
    conn: sqlite3.Connection,
    *,
    branch_code: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    ensure_sme_bank_reconcile_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where
    sql = 'SELECT * FROM sme_bank_reconciliations WHERE 1=1'
    params: list[Any] = []
    bf, bp = branch_where(branch_code)
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY reconcile_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def workspace(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
    account_code: str = '1121',
    statement_balance: float | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Payload UI: sổ + sao kê chưa khớp + số dư."""
    ensure_sme_bank_reconcile_schema(conn, commit=False)
    book_lines = list_book_movements(
        conn, date_from=date_from, date_to=date_to,
        account_code=account_code, only_unmatched=True,
        branch_code=branch_code,
    )
    bank_lines = list_statement_movements(
        conn, date_from=date_from, date_to=date_to, only_unmatched=True,
        branch_code=branch_code,
    )
    book_bal = book_bank_balance(
        conn, as_of=date_to, account_code=account_code, branch_code=branch_code,
    )
    return {
        'account_code': account_code,
        'date_from': date_from[:10],
        'date_to': date_to[:10],
        'book_balance': book_bal,
        'statement_balance': statement_balance,
        'difference': None if statement_balance is None else _f(_money(statement_balance) - _money(book_bal)),
        'book_lines': book_lines,
        'bank_lines': bank_lines,
        'branch_code': branch_code or 'ALL',
    }
