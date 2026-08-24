"""P2 SME — vay nợ, lãi vay, ký quỹ/ký cược."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry, reverse_journal_entry
from Services.sme.vouchers import create_payment, create_receipt
from db_utils import sqlite_commit

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _normalize_interest_rate(raw) -> float:
    """Lãi suất năm dạng thập phân (0.12 = 12%). Chấp nhận nhập 12 hoặc 0.12."""
    rate = Decimal(str(raw if raw is not None else 0))
    if rate < 0:
        raise ValueError('Lãi suất không được âm')
    if rate > 1:
        rate = rate / Decimal('100')
    if rate > Decimal('1'):
        raise ValueError('Lãi suất không hợp lệ')
    return float(rate.quantize(Decimal('0.0001')))


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
        sqlite_commit(conn, label='loans_deposits')


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
    """Giải ngân vay: Nợ 111/112 / Có 341|311. Thuê TC: Nợ 212 / Có 3412."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_loans_schema(conn, commit=False)
    amt = _money(principal)
    if amt <= 0:
        raise ValueError('Số tiền vay phải > 0')
    date_s = str(start_date or '')[:10]
    name = (lender_name or '').strip()
    if not date_s or not name:
        raise ValueError('Thiếu ngày / bên cho vay')
    rate_dec = _normalize_interest_rate(interest_rate)
    loan_no = _next_loan_no(conn)
    liab = (liability_account or '3411').strip() or '3411'
    cash = (cash_account or '1121').strip() or '1121'
    desc = notes or f'Giải ngân vay {loan_no} — {name}'
    biz = 'THUE_TAI_CHINH' if cash.startswith('212') or liab.startswith('3412') else 'GIAI_NGAN_VAY'
    if biz == 'THUE_TAI_CHINH' and not notes:
        desc = f'TSCĐ thuê tài chính {loan_no} — {name}'
    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, None)
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='VAY',
        document_no=loan_no,
        business_type=biz,
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
            float(amt), rate_dec, liab, entry['id'], notes or '', created_by, _now(), branch,
        ),
    )
    if commit:
        sqlite_commit(conn, label='loans_deposits')
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
        sqlite_commit(conn, label='loans_deposits')
    return {'id': cur.lastrowid, 'amount': float(interest), 'journal_entry_id': entry['id'], 'loan_no': loan['loan_no']}


def accrue_period_loan_interest(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    interest_date: str | None = None,
    replace_existing: bool = False,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Trích lãi mọi khoản vay đang hiệu lực trong kỳ (Nợ 635 / Có 335)."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_loans_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row
    year, month = int(fiscal_year), int(period)
    date_s = (interest_date or f'{year:04d}-{month:02d}-28')[:10]
    existing = conn.execute(
        """
        SELECT id, loan_id, amount, journal_entry_id, status
        FROM sme_loan_interest
        WHERE period_year = ? AND period_month = ?
        """,
        (year, month),
    ).fetchall()
    accrued = [dict(r) for r in existing if str(dict(r).get('status') or 'accrued') == 'accrued']
    if accrued and not replace_existing:
        total = sum(float(r.get('amount') or 0) for r in accrued)
        return {
            'posted': False,
            'reason': 'already_posted',
            'loans': len(accrued),
            'amount': total,
            'details': [],
        }
    reversed_ids: list[int] = []
    if replace_existing:
        for r in existing:
            d = dict(r)
            jid = d.get('journal_entry_id')
            if jid and d.get('status') == 'accrued':
                try:
                    reverse_journal_entry(
                        conn, int(jid), created_by=created_by,
                        reason=f'Thay thế trích lãi vay {year}/{month:02d}',
                    )
                    reversed_ids.append(int(jid))
                except Exception:
                    pass
            conn.execute('DELETE FROM sme_loan_interest WHERE id = ?', (int(d['id']),))

    posted: list[dict[str, Any]] = []
    skipped: list[str] = []
    for loan in list_loans(conn):
        if str(loan.get('status') or '') != 'active':
            continue
        rate = float(loan.get('interest_rate') or 0)
        principal = float(loan.get('principal') or 0)
        if rate <= 0 or principal <= 0:
            continue
        start = str(loan.get('start_date') or '')[:10]
        if start and start > date_s:
            continue
        try:
            rec = accrue_loan_interest(
                conn,
                loan_id=int(loan['id']),
                period_year=year,
                period_month=month,
                interest_date=date_s,
                created_by=created_by,
                commit=False,
            )
            posted.append(rec)
        except ValueError as e:
            skipped.append(f"{loan.get('loan_no')}: {e}")
    total = sum(float(x.get('amount') or 0) for x in posted)
    if commit:
        sqlite_commit(conn, label='loans_deposits')
    return {
        'posted': bool(posted),
        'loans': len(posted),
        'amount': total,
        'details': posted,
        'skipped': skipped,
        'reversed_entry_ids': reversed_ids,
        'posting_date': date_s,
    }


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
        sqlite_commit(conn, label='loans_deposits')
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
        sqlite_commit(conn, label='loans_deposits')
    return dict(conn.execute('SELECT * FROM sme_deposits WHERE id = ?', (cur.lastrowid,)).fetchone())


def get_loan(conn: sqlite3.Connection, loan_id: int) -> dict[str, Any] | None:
    ensure_sme_loans_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_loans WHERE id = ?', (loan_id,)).fetchone()
    return dict(row) if row else None


def _loan_has_follow_on(conn: sqlite3.Connection, loan_id: int) -> bool:
    """Đã trích lãi hoặc đã trả nợ → không cho sửa gốc/ngày/TK hạch toán."""
    n_int = conn.execute(
        """
        SELECT COUNT(*) FROM sme_loan_interest
        WHERE loan_id = ? AND COALESCE(status, 'accrued') != 'void'
        """,
        (loan_id,),
    ).fetchone()[0]
    if int(n_int or 0) > 0:
        return True
    n_pay = conn.execute(
        """
        SELECT COUNT(*) FROM sme_journal_entries
        WHERE document_type = 'TRAV' AND document_id = ?
          AND status = 'posted' AND reverses_id IS NULL
        """,
        (loan_id,),
    ).fetchone()[0]
    return int(n_pay or 0) > 0


def update_loan(
    conn: sqlite3.Connection,
    loan_id: int,
    *,
    lender_name: str | None = None,
    contract_no: str | None = None,
    due_date: str | None = None,
    interest_rate=None,
    notes: str | None = None,
    principal=None,
    start_date: str | None = None,
    liability_account: str | None = None,
    cash_account: str | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Sửa thông tin chi tiết khoản vay (active).

    Luôn cho sửa: bên cho vay, số HĐ, hạn trả, lãi suất, ghi chú.
    Sửa gốc / ngày giải ngân / TK: chỉ khi chưa trích lãi và chưa trả nợ
    (đảo bút toán giải ngân rồi ghi lại).
    """
    from Services.sme.branch_filter import assert_row_in_branch

    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_loans_schema(conn, commit=False)
    assert_row_in_branch(conn, 'sme_loans', loan_id, label='Khoản vay')

    loan = get_loan(conn, loan_id)
    if not loan:
        raise ValueError('Không tìm thấy khoản vay')
    if loan.get('status') == 'void':
        raise ValueError('Khoản vay đã hủy — không thể sửa')
    if loan.get('status') == 'closed':
        raise ValueError('Khoản vay đã tất toán — chỉ được xem, không sửa')

    name = (lender_name if lender_name is not None else loan.get('lender_name') or '').strip()
    if not name:
        raise ValueError('Thiếu bên cho vay')

    new_rate = (
        _normalize_interest_rate(interest_rate)
        if interest_rate is not None
        else float(loan.get('interest_rate') or 0)
    )
    new_contract = (
        (contract_no if contract_no is not None else loan.get('contract_no') or '')
    ).strip()
    new_due = (
        str(due_date if due_date is not None else loan.get('due_date') or '')[:10] or None
    )
    new_notes = notes if notes is not None else (loan.get('notes') or '')

    new_principal = _money(principal if principal is not None else loan.get('principal'))
    if new_principal <= 0:
        raise ValueError('Số tiền gốc phải > 0')
    new_start = str(
        start_date if start_date is not None else loan.get('start_date') or ''
    )[:10]
    if not new_start:
        raise ValueError('Thiếu ngày giải ngân')
    new_liab = (
        liability_account if liability_account is not None else loan.get('liability_account') or '3411'
    ).strip() or '3411'
    new_cash = (
        cash_account if cash_account is not None else '1121'
    ).strip() or '1121'

    old_principal = _money(loan.get('principal'))
    old_start = str(loan.get('start_date') or '')[:10]
    old_liab = str(loan.get('liability_account') or '3411')
    money_changed = (
        new_principal != old_principal
        or new_start != old_start
        or new_liab != old_liab
    )

    journal_id = loan.get('disbursement_journal_id')
    if money_changed:
        if _loan_has_follow_on(conn, loan_id):
            raise ValueError(
                'Đã trích lãi hoặc trả nợ — không thể sửa gốc / ngày / TK. '
                'Chỉ sửa bên cho vay, số HĐ, hạn trả, lãi suất, ghi chú.'
            )
        if journal_id:
            reverse_journal_entry(
                conn,
                int(journal_id),
                posting_date=new_start,
                created_by=created_by,
                reason=f'Sửa khoản vay {loan.get("loan_no")}',
            )
        desc = (new_notes or '').strip() or f'Giải ngân vay {loan["loan_no"]} — {name}'
        entry = post_journal_entry(
            conn,
            posting_date=new_start,
            document_date=new_start,
            document_type='VAY',
            document_no=loan['loan_no'],
            document_id=loan_id,
            business_type='GIAI_NGAN_VAY',
            description=desc,
            created_by=created_by,
            branch_code=loan.get('branch_code'),
            lines=[
                {
                    'sequence': 1,
                    'account_code': new_cash,
                    'debit': float(new_principal),
                    'credit': 0,
                    'description': desc,
                },
                {
                    'sequence': 2,
                    'account_code': new_liab,
                    'debit': 0,
                    'credit': float(new_principal),
                    'description': desc,
                },
            ],
        )
        journal_id = entry['id']

    conn.execute(
        """
        UPDATE sme_loans SET
            lender_name = ?,
            contract_no = ?,
            start_date = ?,
            due_date = ?,
            principal = ?,
            interest_rate = ?,
            liability_account = ?,
            notes = ?,
            disbursement_journal_id = COALESCE(?, disbursement_journal_id)
        WHERE id = ?
        """,
        (
            name,
            new_contract or None,
            new_start,
            new_due,
            float(new_principal),
            new_rate,
            new_liab,
            new_notes or '',
            int(journal_id) if journal_id else None,
            loan_id,
        ),
    )
    if commit:
        sqlite_commit(conn, label='loans_deposits')
    return get_loan(conn, loan_id)


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
        sqlite_commit(conn, label='loans_deposits')
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
        sqlite_commit(conn, label='loans_deposits')
    row2 = conn.execute('SELECT * FROM sme_deposits WHERE id = ?', (deposit_id,)).fetchone()
    return dict(row2) if row2 else doc
