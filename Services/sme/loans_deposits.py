"""P2 SME — vay nợ, lãi vay, ký quỹ/ký cược."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry, reverse_journal_entry
from Services.sme.vouchers import create_payment, create_receipt

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_loans_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_no TEXT NOT NULL UNIQUE,
            lender_name TEXT NOT NULL,
            contract_no TEXT,
            start_date TEXT NOT NULL,
            due_date TEXT,
            principal REAL NOT NULL DEFAULT 0,
            interest_rate REAL NOT NULL DEFAULT 0,
            liability_account TEXT NOT NULL DEFAULT '3411',
            currency TEXT DEFAULT 'VND',
            status TEXT NOT NULL DEFAULT 'active',
            disbursement_journal_id INTEGER,
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            branch_code TEXT
        )
        """
    )
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, 'sme_loans')
    ensure_branch_column(conn, 'sme_deposits')
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_loan_interest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL,
            period_year INTEGER NOT NULL,
            period_month INTEGER NOT NULL,
            interest_date TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'accrued',
            UNIQUE(loan_id, period_year, period_month),
            FOREIGN KEY(loan_id) REFERENCES sme_loans(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_no TEXT NOT NULL UNIQUE,
            doc_date TEXT NOT NULL,
            direction TEXT NOT NULL,
            party_name TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            deposit_account TEXT NOT NULL,
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
    if commit:
        conn.commit()


def _next_loan_no(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT loan_no FROM sme_loans WHERE loan_no LIKE 'VAY%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return 'VAY000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['loan_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'VAY{int(digits) + 1:06d}'


def disburse_loan(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    lender_name: str,
    principal,
    liability_account: str = '3411',
    cash_account: str = '1121',
    interest_rate: float = 0,
    due_date: str = '',
    contract_no: str = '',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Giải ngân vay: Nợ 111/112 / Có 341|311."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_loans_schema(conn, commit=False)
    amt = _money(principal)
    if amt <= 0:
        raise ValueError('Số tiền vay phải > 0')
    date_s = str(start_date or '')[:10]
    name = (lender_name or '').strip()
    if not date_s or not name:
        raise ValueError('Thiếu ngày / bên cho vay')
    loan_no = _next_loan_no(conn)
    liab = (liability_account or '3411').strip() or '3411'
    cash = (cash_account or '1121').strip() or '1121'
    desc = notes or f'Giải ngân vay {loan_no} — {name}'
    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, None)
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='VAY',
        document_no=loan_no,
        business_type='GIAI_NGAN_VAY',
        description=desc,
        created_by=created_by,
        branch_code=branch,
        lines=[
            {'sequence': 1, 'account_code': cash, 'debit': float(amt), 'credit': 0, 'description': desc},
            {'sequence': 2, 'account_code': liab, 'debit': 0, 'credit': float(amt), 'description': desc},
        ],
    )
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_loans (
            loan_no, lender_name, contract_no, start_date, due_date, principal,
            interest_rate, liability_account, status, disbursement_journal_id, notes, created_by, created_at, branch_code
        ) VALUES (?,?,?,?,?,?,?,?,'active',?,?,?,?,?)
        """,
        (
            loan_no, name, contract_no or '', date_s, (due_date or '')[:10] or None,
            float(amt), float(interest_rate or 0), liab, entry['id'], notes or '', created_by, _now(), branch,
        ),
    )
    if commit:
        conn.commit()
    return get_loan(conn, cur.lastrowid)


def accrue_loan_interest(
    conn: sqlite3.Connection,
    *,
    loan_id: int,
    period_year: int,
    period_month: int,
    amount=None,
    interest_date: str | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Trích lãi vay: Nợ 635 / Có 335."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_loans_schema(conn, commit=False)
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_loans', loan_id, label='Khoản vay')
    loan = get_loan(conn, loan_id)
    if not loan or loan['status'] != 'active':
        raise ValueError('Khoản vay không hợp lệ')
    year, month = int(period_year), int(period_month)
    existing = conn.execute(
        'SELECT id FROM sme_loan_interest WHERE loan_id=? AND period_year=? AND period_month=?',
        (loan_id, year, month),
    ).fetchone()
    if existing:
        raise ValueError('Đã trích lãi kỳ này')

    if amount is not None:
        interest = _money(amount)
    else:
        # Lãi tháng ≈ gốc * lãi suất năm / 12
        interest = (_money(loan['principal']) * _money(loan['interest_rate']) / Decimal('12')).quantize(MONEY_Q)
    if interest <= 0:
        raise ValueError('Số lãi phải > 0')
    date_s = (interest_date or f'{year:04d}-{month:02d}-28')[:10]
    desc = f"Lãi vay {loan['loan_no']} kỳ {month}/{year}"
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='LAIV',
        document_no=f"{loan['loan_no']}-{year}{month:02d}",
        document_id=loan_id,
        business_type='TRICH_LAI_VAY',
        description=desc,
        created_by=created_by,
        branch_code=loan.get('branch_code'),
        lines=[
            {'sequence': 1, 'account_code': '635', 'debit': float(interest), 'credit': 0, 'description': desc},
            {'sequence': 2, 'account_code': '335', 'debit': 0, 'credit': float(interest), 'description': desc},
        ],
    )
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_loan_interest
            (loan_id, period_year, period_month, interest_date, amount, journal_entry_id, status)
        VALUES (?,?,?,?,?,?,'accrued')
        """,
        (loan_id, year, month, date_s, float(interest), entry['id']),
    )
    if commit:
        conn.commit()
    return {'id': cur.lastrowid, 'amount': float(interest), 'journal_entry_id': entry['id'], 'loan_no': loan['loan_no']}


def repay_loan(
    conn: sqlite3.Connection,
    *,
    loan_id: int,
    amount,
    pay_date: str,
    payment_method: str = 'bank',
    include_interest: float = 0,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Trả gốc (+ lãi nếu có): Nợ 341/335 / Có tiền."""
    ensure_sme_loans_schema(conn, commit=False)
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_loans', loan_id, label='Khoản vay')
    loan = get_loan(conn, loan_id)
    if not loan:
        raise ValueError('Không tìm thấy khoản vay')
    principal_pay = _money(amount)
    interest_pay = _money(include_interest)
    if principal_pay <= 0 and interest_pay <= 0:
        raise ValueError('Số tiền trả phải > 0')
    date_s = str(pay_date or '')[:10]
    cash = '1121' if (payment_method or 'bank') != 'cash' else '1111'
    lines = []
    seq = 1
    if principal_pay > 0:
        lines.append({'sequence': seq, 'account_code': loan['liability_account'], 'debit': float(principal_pay), 'credit': 0,
                      'description': f"Trả gốc {loan['loan_no']}"})
        seq += 1
    if interest_pay > 0:
        lines.append({'sequence': seq, 'account_code': '335', 'debit': float(interest_pay), 'credit': 0,
                      'description': f"Trả lãi {loan['loan_no']}"})
        seq += 1
    total = principal_pay + interest_pay
    lines.append({'sequence': seq, 'account_code': cash, 'debit': 0, 'credit': float(total),
                  'description': f"Trả vay {loan['loan_no']}"})
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='TRAV',
        document_no=f"TV-{loan['loan_no']}-{date_s.replace('-','')}",
        document_id=loan_id,
        business_type='TRA_VAY',
        description=f"Trả vay {loan['loan_no']}",
        created_by=created_by,
        branch_code=loan.get('branch_code'),
        lines=lines,
    )
    # Đóng nếu trả hết gốc (xấp xỉ)
    if principal_pay >= _money(loan['principal']) - Decimal('1'):
        conn.execute("UPDATE sme_loans SET status = 'closed' WHERE id = ?", (loan_id,))
    if commit:
        conn.commit()
    return {'journal_entry_id': entry['id'], 'amount': float(total), 'loan': get_loan(conn, loan_id)}


def post_deposit(
    conn: sqlite3.Connection,
    *,
    doc_date: str,
    direction: str,
    party_name: str,
    amount,
    payment_method: str = 'bank',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """
    direction:
      - placed: đặt cọc đi → Nợ 138 / Có tiền
      - received: nhận cọc → Nợ tiền / Có 344
      - refund_placed: thu hồi cọc đã đặt → Nợ tiền / Có 138
      - refund_received: trả lại cọc nhận → Nợ 344 / Có tiền
    """
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_loans_schema(conn, commit=False)
    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số tiền phải > 0')
    date_s = str(doc_date or '')[:10]
    name = (party_name or '').strip()
    if not date_s or not name:
        raise ValueError('Thiếu ngày / đối tượng')
    cash = '1121' if (payment_method or 'bank') != 'cash' else '1111'
    d = (direction or '').strip().lower()
    mapping = {
        'placed': ('138', cash, 'debit_deposit'),      # Nợ 138 Có tiền
        'received': (cash, '344', 'credit_deposit'),   # Nợ tiền Có 344
        'refund_placed': (cash, '138', 'refund_out'),
        'refund_received': ('344', cash, 'refund_in'),
    }
    if d not in mapping:
        raise ValueError('direction không hợp lệ')
    debit_acc, credit_acc, _ = mapping[d]
    # placed: Nợ 138 Có cash — mapping says ('138', cash) meaning debit first
    # 138 là TK tổng hợp → dùng 1388; 344 thường postable trực tiếp
    placed_acc = '1388'
    received_acc = '344'
    if d == 'placed':
        lines = [
            {'sequence': 1, 'account_code': placed_acc, 'debit': float(amt), 'credit': 0},
            {'sequence': 2, 'account_code': cash, 'debit': 0, 'credit': float(amt)},
        ]
        deposit_account = placed_acc
    elif d == 'received':
        lines = [
            {'sequence': 1, 'account_code': cash, 'debit': float(amt), 'credit': 0},
            {'sequence': 2, 'account_code': received_acc, 'debit': 0, 'credit': float(amt)},
        ]
        deposit_account = received_acc
    elif d == 'refund_placed':
        lines = [
            {'sequence': 1, 'account_code': cash, 'debit': float(amt), 'credit': 0},
            {'sequence': 2, 'account_code': placed_acc, 'debit': 0, 'credit': float(amt)},
        ]
        deposit_account = placed_acc
    else:
        lines = [
            {'sequence': 1, 'account_code': received_acc, 'debit': float(amt), 'credit': 0},
            {'sequence': 2, 'account_code': cash, 'debit': 0, 'credit': float(amt)},
        ]
        deposit_account = received_acc

    row = conn.execute(
        "SELECT doc_no FROM sme_deposits WHERE doc_no LIKE 'KQ%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        doc_no = 'KQ000001'
    else:
        raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
        digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
        doc_no = f'KQ{int(digits) + 1:06d}'

    desc = notes or f'Ký quỹ {d} — {name}'
    for ln in lines:
        ln['description'] = desc
    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, None)
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='KYQUY',
        document_no=doc_no,
        business_type='KY_QUY',
        description=desc,
        created_by=created_by,
        branch_code=branch,
        lines=lines,
    )
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_deposits (
            doc_no, doc_date, direction, party_name, amount, deposit_account, cash_account,
            journal_entry_id, status, notes, created_by, created_at, branch_code
        ) VALUES (?,?,?,?,?,?,?,?,'posted',?,?,?,?)
        """,
        (doc_no, date_s, d, name, float(amt), deposit_account, cash, entry['id'], notes or '', created_by, _now(), branch),
    )
    if commit:
        conn.commit()
    return dict(conn.execute('SELECT * FROM sme_deposits WHERE id = ?', (cur.lastrowid,)).fetchone())


def get_loan(conn: sqlite3.Connection, loan_id: int) -> dict[str, Any] | None:
    ensure_sme_loans_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_loans WHERE id = ?', (loan_id,)).fetchone()
    return dict(row) if row else None


def list_loans(
    conn: sqlite3.Connection,
    *,
    branch_code: str | None = None,
) -> list[dict[str, Any]]:
    ensure_sme_loans_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where
    sql = "SELECT * FROM sme_loans WHERE status != 'void'"
    bf, bp = branch_where(branch_code)
    sql += bf + ' ORDER BY start_date DESC, id DESC'
    return [dict(r) for r in conn.execute(sql, bp).fetchall()]


def list_deposits(
    conn: sqlite3.Connection,
    *,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_loans_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where
    sql = "SELECT * FROM sme_deposits WHERE status != 'void'"
    params: list[Any] = []
    bf, bp = branch_where(branch_code)
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY doc_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def void_loan(
    conn: sqlite3.Connection,
    loan_id: int,
    *,
    reason: str = 'Hủy khoản vay',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Hủy giải ngân vay — đảo bút toán giải ngân + lãi đã trích (chưa trả)."""
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_loans', loan_id, label='Khoản vay')
    loan = get_loan(conn, loan_id)
    if not loan:
        raise ValueError('Không tìm thấy khoản vay')
    if loan.get('status') == 'void':
        raise ValueError('Đã hủy')
    if loan.get('status') == 'closed':
        raise ValueError('Khoản vay đã tất toán — không hủy giải ngân')
    if loan.get('disbursement_journal_id'):
        reverse_journal_entry(
            conn, int(loan['disbursement_journal_id']),
            created_by=created_by, reason=reason,
        )
    interests = conn.execute(
        "SELECT * FROM sme_loan_interest WHERE loan_id = ? AND status = 'accrued'",
        (loan_id,),
    ).fetchall()
    for row in interests:
        d = dict(row)
        if d.get('journal_entry_id'):
            reverse_journal_entry(
                conn, int(d['journal_entry_id']),
                created_by=created_by, reason=reason,
            )
        conn.execute(
            "UPDATE sme_loan_interest SET status = 'void' WHERE id = ?",
            (d['id'],),
        )
    conn.execute(
        "UPDATE sme_loans SET status = 'void', notes = ? WHERE id = ?",
        ((loan.get('notes') or '') + f' | {reason}', loan_id),
    )
    if commit:
        conn.commit()
    return get_loan(conn, loan_id)


def void_deposit(
    conn: sqlite3.Connection,
    deposit_id: int,
    *,
    reason: str = 'Hủy ký quỹ',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    ensure_sme_loans_schema(conn, commit=False)
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_deposits', deposit_id, label='Chứng từ ký quỹ')
    row = conn.execute('SELECT * FROM sme_deposits WHERE id = ?', (deposit_id,)).fetchone()
    if not row:
        raise ValueError('Không tìm thấy chứng từ ký quỹ')
    doc = dict(row)
    if doc.get('status') == 'void':
        raise ValueError('Đã hủy')
    if doc.get('journal_entry_id'):
        reverse_journal_entry(
            conn, int(doc['journal_entry_id']),
            created_by=created_by, reason=reason,
        )
    conn.execute(
        "UPDATE sme_deposits SET status = 'void', notes = ? WHERE id = ?",
        ((doc.get('notes') or '') + f' | {reason}', deposit_id),
    )
    if commit:
        conn.commit()
    row2 = conn.execute('SELECT * FROM sme_deposits WHERE id = ?', (deposit_id,)).fetchone()
    return dict(row2) if row2 else doc
