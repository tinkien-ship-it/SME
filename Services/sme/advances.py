"""Tạm ứng / thanh toán tạm ứng / đề nghị thanh toán SME (03-TT, 04-TT, 05-TT)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry, reverse_journal_entry
from Services.sme.vouchers import create_payment, create_receipt, ensure_sme_voucher_schema
from db_utils import sqlite_commit

MONEY_Q = Decimal('0.01')

FORM_ADVANCE_REQUEST = '03-TT'
FORM_ADVANCE_SETTLEMENT = '04-TT'
FORM_PAYMENT_REQUEST = '05-TT'

DOC_ADVANCE_REQUEST = 'advance_request'
DOC_ADVANCE_SETTLEMENT = 'advance_settlement'
DOC_PAYMENT_REQUEST = 'payment_request'


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_advance_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_advance_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_type TEXT NOT NULL,
            form_code TEXT NOT NULL,
            doc_no TEXT NOT NULL UNIQUE,
            doc_date TEXT NOT NULL,
            employee_id INTEGER,
            employee_name TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            purpose TEXT,
            expense_account TEXT,
            payment_method TEXT DEFAULT 'cash',
            status TEXT NOT NULL DEFAULT 'draft',
            advance_doc_id INTEGER,
            expense_amount REAL DEFAULT 0,
            cash_return_amount REAL DEFAULT 0,
            additional_payment REAL DEFAULT 0,
            disbursement_voucher_id INTEGER,
            cash_return_voucher_id INTEGER,
            additional_voucher_id INTEGER,
            settlement_journal_id INTEGER,
            journal_entry_id INTEGER,
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cols = {r[1] for r in c.execute('PRAGMA table_info(sme_advance_docs)').fetchall()}
    if 'branch_code' not in cols:
        try:
            c.execute('ALTER TABLE sme_advance_docs ADD COLUMN branch_code TEXT')
        except sqlite3.OperationalError:
            pass
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_advance_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL,
            line_no INTEGER NOT NULL DEFAULT 1,
            description TEXT,
            account_code TEXT,
            amount REAL NOT NULL DEFAULT 0,
            invoice_ref TEXT,
            FOREIGN KEY(doc_id) REFERENCES sme_advance_docs(id)
        )
        """
    )
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_advance_docs_type ON sme_advance_docs(doc_type, status)'
    )
    c.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_advance_docs_emp ON sme_advance_docs(employee_id)'
    )
    if commit:
        sqlite_commit(conn, label='advances')


def _prefix_for(doc_type: str) -> str:
    return {
        DOC_ADVANCE_REQUEST: 'TU',
        DOC_ADVANCE_SETTLEMENT: 'TTU',
        DOC_PAYMENT_REQUEST: 'DNT',
    }.get(doc_type, 'ADV')


def _next_doc_no(conn: sqlite3.Connection, doc_type: str) -> str:
    prefix = _prefix_for(doc_type)
    row = conn.execute(
        """
        SELECT doc_no FROM sme_advance_docs
        WHERE doc_type = ? AND doc_no LIKE ?
        ORDER BY id DESC LIMIT 1
        """,
        (doc_type, f'{prefix}%'),
    ).fetchone()
    if not row:
        return f'{prefix}000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'{prefix}{int(digits) + 1:06d}'


def _cash_account(payment_method: str) -> str:
    method = (payment_method or 'cash').strip().lower()
    if method in ('112', 'bank', 'bank_transfer', 'ck', 'transfer'):
        return '1121'
    return '1111'


def list_employees_brief(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT id, fullname, position, phone
            FROM employees
            WHERE COALESCE(status, 1) = 1 OR COALESCE(is_active, 1) = 1
            ORDER BY fullname
            LIMIT 500
            """
        ).fetchall()
    except sqlite3.Error:
        try:
            rows = conn.execute(
                'SELECT id, fullname, position, phone FROM employees ORDER BY fullname LIMIT 500'
            ).fetchall()
        except sqlite3.Error:
            return []
    return [dict(r) for r in rows]


def employee_open_advance(
    conn: sqlite3.Connection,
    *,
    employee_id: int | None = None,
    employee_name: str | None = None,
) -> float:
    """Số dư tạm ứng còn lại (Nợ 141*) theo nhân viên trên journal."""
    ensure_sme_journal_ready(conn, commit=False)
    clauses = [
        "je.status IN ('posted', 'reversed')",
        "(jl.account_code = '141' OR jl.account_code LIKE '141%')",
    ]
    params: list[Any] = []
    if employee_id:
        clauses.append('jl.employee_id = ?')
        params.append(int(employee_id))
    elif employee_name:
        clauses.append(
            """
            EXISTS (
              SELECT 1 FROM sme_advance_docs d
              WHERE d.employee_name = ?
                AND (
                  d.disbursement_voucher_id IN (
                    SELECT id FROM sme_vouchers WHERE journal_entry_id = je.id
                  )
                  OR d.settlement_journal_id = je.id
                )
            )
            """
        )
        params.append(employee_name)
    else:
        return 0.0
    sql = f"""
        SELECT COALESCE(SUM(jl.debit),0) - COALESCE(SUM(jl.credit),0) AS bal
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE {' AND '.join(clauses)}
    """
    row = conn.execute(sql, params).fetchone()
    bal = row[0] if row and not isinstance(row, sqlite3.Row) else (row['bal'] if row else 0)
    return _f(max(0, bal or 0))


def _stamp_advance_branch(conn: sqlite3.Connection, doc_id: int) -> None:
    from Services.sme.branches import resolve_posting_branch
    try:
        br = resolve_posting_branch(conn, None)
        conn.execute(
            'UPDATE sme_advance_docs SET branch_code = ? WHERE id = ?',
            (br, doc_id),
        )
    except Exception:
        pass


def create_advance_request(
    conn: sqlite3.Connection,
    *,
    doc_date: str,
    employee_name: str,
    amount,
    purpose: str = '',
    employee_id: int | None = None,
    payment_method: str = 'cash',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Giấy đề nghị tạm ứng 03-TT (chưa chi tiền)."""
    ensure_sme_advance_schema(conn, commit=False)
    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số tiền tạm ứng phải > 0')
    name = (employee_name or '').strip()
    if not name:
        raise ValueError('Thiếu họ tên người đề nghị tạm ứng')
    date_s = str(doc_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày chứng từ')

    doc_no = _next_doc_no(conn, DOC_ADVANCE_REQUEST)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_advance_docs (
            doc_type, form_code, doc_no, doc_date, employee_id, employee_name,
            amount, purpose, payment_method, status, created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,'approved',?,?,?)
        """,
        (
            DOC_ADVANCE_REQUEST, FORM_ADVANCE_REQUEST, doc_no, date_s,
            employee_id, name, float(amt), purpose or '', payment_method or 'cash',
            created_by, _now(), _now(),
        ),
    )
    doc_id = cur.lastrowid
    _stamp_advance_branch(conn, doc_id)
    if commit:
        sqlite_commit(conn, label='advances')
    return get_advance_doc(conn, doc_id)


def disburse_advance(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    voucher_date: str | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Chi tạm ứng: phiếu chi 02-TT Nợ 141 / Có 111|112 + gắn employee_id."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_voucher_schema(conn, commit=False)
    ensure_sme_advance_schema(conn, commit=False)

    doc = get_advance_doc(conn, doc_id)
    if not doc:
        raise ValueError('Không tìm thấy giấy đề nghị tạm ứng')
    if doc['doc_type'] != DOC_ADVANCE_REQUEST:
        raise ValueError('Chỉ chi tạm ứng từ chứng từ 03-TT')
    if doc['status'] == 'disbursed':
        raise ValueError('Đã chi tạm ứng trước đó')
    if doc['status'] == 'void':
        raise ValueError('Chứng từ đã hủy')
    if doc['status'] not in ('draft', 'approved'):
        raise ValueError(f'Không thể chi ở trạng thái {doc["status"]}')

    date_s = (voucher_date or doc['doc_date'] or '')[:10]
    voucher = create_payment(
        conn,
        voucher_date=date_s,
        party_name=doc['employee_name'],
        amount=doc['amount'],
        payment_method=doc.get('payment_method') or 'cash',
        debit_account='141',
        reason=doc.get('purpose') or f'Tạm ứng {doc["doc_no"]}',
        reference_document=doc['doc_no'],
        source_type='advance_request',
        source_id=doc_id,
        created_by=created_by,
        commit=False,
    )

    # Gắn employee_id lên dòng journal 141
    emp_id = doc.get('employee_id')
    if emp_id and voucher.get('journal_entry_id'):
        conn.execute(
            """
            UPDATE sme_journal_lines
            SET employee_id = ?, partner_type = 'employee', partner_id = ?
            WHERE entry_id = ? AND (account_code = '141' OR account_code LIKE '141%')
            """,
            (int(emp_id), int(emp_id), int(voucher['journal_entry_id'])),
        )

    conn.execute(
        """
        UPDATE sme_advance_docs
        SET status = 'disbursed',
            disbursement_voucher_id = ?,
            journal_entry_id = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (voucher['id'], voucher.get('journal_entry_id'), _now(), doc_id),
    )
    if commit:
        sqlite_commit(conn, label='advances')
    out = get_advance_doc(conn, doc_id)
    out['voucher'] = voucher
    return out


def create_payment_request(
    conn: sqlite3.Connection,
    *,
    doc_date: str,
    employee_name: str,
    amount,
    purpose: str = '',
    expense_account: str = '642',
    employee_id: int | None = None,
    payment_method: str = 'cash',
    lines: list[dict] | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Giấy đề nghị thanh toán 05-TT."""
    ensure_sme_advance_schema(conn, commit=False)
    amt = _money(amount)
    if amt <= 0 and not lines:
        raise ValueError('Số tiền đề nghị phải > 0')
    name = (employee_name or '').strip()
    if not name:
        raise ValueError('Thiếu họ tên người đề nghị')
    date_s = str(doc_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày chứng từ')

    line_rows = lines or []
    if line_rows:
        amt = sum((_money(x.get('amount')) for x in line_rows), Decimal('0.00'))
    if amt <= 0:
        raise ValueError('Tổng tiền dòng đề nghị phải > 0')

    expense = (expense_account or '642').strip() or '642'
    doc_no = _next_doc_no(conn, DOC_PAYMENT_REQUEST)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_advance_docs (
            doc_type, form_code, doc_no, doc_date, employee_id, employee_name,
            amount, purpose, expense_account, payment_method, status,
            created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,'approved',?,?,?)
        """,
        (
            DOC_PAYMENT_REQUEST, FORM_PAYMENT_REQUEST, doc_no, date_s,
            employee_id, name, float(amt), purpose or '', expense,
            payment_method or 'cash', created_by, _now(), _now(),
        ),
    )
    doc_id = cur.lastrowid
    _stamp_advance_branch(conn, doc_id)
    for i, ln in enumerate(line_rows or [{'description': purpose, 'account_code': expense, 'amount': float(amt)}], start=1):
        cur.execute(
            """
            INSERT INTO sme_advance_lines (doc_id, line_no, description, account_code, amount, invoice_ref)
            VALUES (?,?,?,?,?,?)
            """,
            (
                doc_id, i,
                ln.get('description') or purpose or '',
                ln.get('account_code') or expense,
                float(_money(ln.get('amount'))),
                ln.get('invoice_ref') or '',
            ),
        )
    if commit:
        sqlite_commit(conn, label='advances')
    return get_advance_doc(conn, doc_id)


def pay_payment_request(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    voucher_date: str | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Thanh toán đề nghị 05-TT → phiếu chi Nợ TK chi phí / Có tiền."""
    ensure_sme_voucher_schema(conn, commit=False)
    doc = get_advance_doc(conn, doc_id)
    if not doc or doc['doc_type'] != DOC_PAYMENT_REQUEST:
        raise ValueError('Không tìm thấy giấy đề nghị thanh toán 05-TT')
    if doc['status'] in ('paid', 'void'):
        raise ValueError(f'Không thể thanh toán ở trạng thái {doc["status"]}')

    date_s = (voucher_date or doc['doc_date'] or '')[:10]
    debit = (doc.get('expense_account') or '642').strip() or '642'
    voucher = create_payment(
        conn,
        voucher_date=date_s,
        party_name=doc['employee_name'],
        amount=doc['amount'],
        payment_method=doc.get('payment_method') or 'cash',
        debit_account=debit,
        reason=doc.get('purpose') or f'Thanh toán {doc["doc_no"]}',
        reference_document=doc['doc_no'],
        source_type='payment_request',
        source_id=doc_id,
        created_by=created_by,
        commit=False,
    )
    conn.execute(
        """
        UPDATE sme_advance_docs
        SET status = 'paid', disbursement_voucher_id = ?, journal_entry_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (voucher['id'], voucher.get('journal_entry_id'), _now(), doc_id),
    )
    if commit:
        sqlite_commit(conn, label='advances')
    out = get_advance_doc(conn, doc_id)
    out['voucher'] = voucher
    return out


def settle_advance(
    conn: sqlite3.Connection,
    *,
    advance_doc_id: int,
    doc_date: str,
    expense_amount,
    cash_return_amount=0,
    additional_payment=0,
    expense_account: str = '642',
    purpose: str = '',
    lines: list[dict] | None = None,
    payment_method: str = 'cash',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Giấy thanh toán tạm ứng 04-TT + bút toán quyết toán 141."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_voucher_schema(conn, commit=False)
    ensure_sme_advance_schema(conn, commit=False)

    adv = get_advance_doc(conn, advance_doc_id)
    if not adv or adv['doc_type'] != DOC_ADVANCE_REQUEST:
        raise ValueError('Cần giấy đề nghị tạm ứng 03-TT đã chi')
    if adv['status'] != 'disbursed':
        raise ValueError('Chỉ quyết toán tạm ứng đã chi tiền')
    if adv.get('settlement_journal_id'):
        raise ValueError('Tạm ứng đã được quyết toán')

    date_s = str(doc_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày quyết toán')

    exp = _money(expense_amount)
    ret = _money(cash_return_amount)
    add = _money(additional_payment)
    if lines:
        exp = sum((_money(x.get('amount')) for x in lines), Decimal('0.00'))

    advanced = _money(adv['amount'])
    # Công thức: tạm ứng = chi phí + hoàn tiền - chi thêm
    # => chi phí + hoàn - chi thêm phải = tạm ứng (cho phép lệch nhỏ do làm tròn)
    implied = exp + ret - add
    if abs(implied - advanced) > Decimal('1.00'):
        raise ValueError(
            f'Không cân: chi phí ({exp}) + hoàn ({ret}) - chi thêm ({add}) = {implied} '
            f'≠ tạm ứng {advanced}. Điều chỉnh cho khớp.'
        )

    if exp < 0 or ret < 0 or add < 0:
        raise ValueError('Số tiền không được âm')
    if exp + ret + add <= 0:
        raise ValueError('Phải có ít nhất một khoản quyết toán')

    expense_acc = (expense_account or '642').strip() or '642'
    cash_acc = _cash_account(payment_method or adv.get('payment_method') or 'cash')
    emp_id = adv.get('employee_id')
    desc = purpose or f'Quyết toán tạm ứng {adv["doc_no"]}'

    doc_no = _next_doc_no(conn, DOC_ADVANCE_SETTLEMENT)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_advance_docs (
            doc_type, form_code, doc_no, doc_date, employee_id, employee_name,
            amount, purpose, expense_account, payment_method, status, advance_doc_id,
            expense_amount, cash_return_amount, additional_payment,
            created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,'settled',?,?,?,?,?,?,?)
        """,
        (
            DOC_ADVANCE_SETTLEMENT, FORM_ADVANCE_SETTLEMENT, doc_no, date_s,
            emp_id, adv['employee_name'], float(advanced), desc, expense_acc,
            payment_method or adv.get('payment_method') or 'cash',
            advance_doc_id, float(exp), float(ret), float(add),
            created_by, _now(), _now(),
        ),
    )
    settle_id = cur.lastrowid

    line_rows = lines or (
        [{'description': desc, 'account_code': expense_acc, 'amount': float(exp)}] if exp > 0 else []
    )
    for i, ln in enumerate(line_rows, start=1):
        cur.execute(
            """
            INSERT INTO sme_advance_lines (doc_id, line_no, description, account_code, amount, invoice_ref)
            VALUES (?,?,?,?,?,?)
            """,
            (
                settle_id, i,
                ln.get('description') or desc,
                ln.get('account_code') or expense_acc,
                float(_money(ln.get('amount'))),
                ln.get('invoice_ref') or '',
            ),
        )

    # Bút toán quyết toán phần chi phí: Nợ CP / Có 141
    journal_lines: list[dict] = []
    seq = 1
    if exp > 0:
        # Nhiều TK chi phí nếu có dòng
        by_acc: dict[str, Decimal] = {}
        for ln in line_rows:
            acc = (ln.get('account_code') or expense_acc).strip() or expense_acc
            by_acc[acc] = by_acc.get(acc, Decimal('0.00')) + _money(ln.get('amount'))
        for acc, amt in by_acc.items():
            if amt <= 0:
                continue
            journal_lines.append({
                'sequence': seq, 'account_code': acc,
                'debit': float(amt), 'credit': 0, 'description': desc,
                'employee_id': emp_id, 'partner_type': 'employee', 'partner_id': emp_id,
            })
            seq += 1
        journal_lines.append({
            'sequence': seq, 'account_code': '141',
            'debit': 0, 'credit': float(exp), 'description': desc,
            'employee_id': emp_id, 'partner_type': 'employee', 'partner_id': emp_id,
        })
        seq += 1

    settlement_entry = None
    if journal_lines:
        # Cân Nợ/Có phần chi phí
        settlement_entry = post_journal_entry(
            conn,
            posting_date=date_s,
            document_date=date_s,
            document_type='TTU',
            document_no=doc_no,
            document_id=settle_id,
            business_type='QUYET_TOAN_TAM_UNG',
            description=desc,
            reference_document=adv['doc_no'],
            created_by=created_by,
            lines=journal_lines,
        )

    cash_return_voucher = None
    if ret > 0:
        cash_return_voucher = create_receipt(
            conn,
            voucher_date=date_s,
            party_name=adv['employee_name'],
            amount=float(ret),
            payment_method=payment_method or adv.get('payment_method') or 'cash',
            credit_account='141',
            reason=f'Hoàn tạm ứng {adv["doc_no"]}',
            reference_document=doc_no,
            source_type='advance_settlement',
            source_id=settle_id,
            created_by=created_by,
            commit=False,
        )
        if emp_id and cash_return_voucher.get('journal_entry_id'):
            conn.execute(
                """
                UPDATE sme_journal_lines
                SET employee_id = ?, partner_type = 'employee', partner_id = ?
                WHERE entry_id = ? AND (account_code = '141' OR account_code LIKE '141%')
                """,
                (int(emp_id), int(emp_id), int(cash_return_voucher['journal_entry_id'])),
            )

    additional_voucher = None
    if add > 0:
        additional_voucher = create_payment(
            conn,
            voucher_date=date_s,
            party_name=adv['employee_name'],
            amount=float(add),
            payment_method=payment_method or adv.get('payment_method') or 'cash',
            debit_account=expense_acc,
            reason=f'Chi thêm quyết toán {adv["doc_no"]}',
            reference_document=doc_no,
            source_type='advance_settlement',
            source_id=settle_id,
            created_by=created_by,
            commit=False,
        )

    conn.execute(
        """
        UPDATE sme_advance_docs
        SET settlement_journal_id = ?,
            cash_return_voucher_id = ?,
            additional_voucher_id = ?,
            journal_entry_id = COALESCE(?, journal_entry_id),
            updated_at = ?
        WHERE id = ?
        """,
        (
            settlement_entry['id'] if settlement_entry else None,
            cash_return_voucher['id'] if cash_return_voucher else None,
            additional_voucher['id'] if additional_voucher else None,
            settlement_entry['id'] if settlement_entry else None,
            _now(),
            settle_id,
        ),
    )
    conn.execute(
        """
        UPDATE sme_advance_docs
        SET status = 'settled', updated_at = ?
        WHERE id = ?
        """,
        (_now(), advance_doc_id),
    )

    if commit:
        sqlite_commit(conn, label='advances')
    out = get_advance_doc(conn, settle_id)
    out['cash_return_voucher'] = cash_return_voucher
    out['additional_voucher'] = additional_voucher
    out['settlement_entry'] = settlement_entry
    return out


def list_advance_docs(
    conn: sqlite3.Connection,
    *,
    doc_type: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    branch_code: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    ensure_sme_advance_schema(conn, commit=False)
    from Services.sme.branches import DEFAULT_BRANCH_CODE
    sql = 'SELECT * FROM sme_advance_docs WHERE 1=1'
    params: list[Any] = []
    if doc_type:
        sql += ' AND doc_type = ?'
        params.append(doc_type)
    if status:
        sql += ' AND status = ?'
        params.append(status)
    if date_from:
        sql += ' AND date(doc_date) >= date(?)'
        params.append(date_from[:10])
    if date_to:
        sql += ' AND date(doc_date) <= date(?)'
        params.append(date_to[:10])
    code = (branch_code or '').strip().upper()
    if code and code != 'ALL':
        if code == DEFAULT_BRANCH_CODE:
            sql += " AND (branch_code IS NULL OR branch_code = '' OR branch_code = ?)"
        else:
            sql += ' AND branch_code = ?'
        params.append(code)
    sql += ' ORDER BY doc_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_advance_doc(conn: sqlite3.Connection, doc_id: int) -> dict[str, Any] | None:
    ensure_sme_advance_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_advance_docs WHERE id = ?', (doc_id,)).fetchone()
    if not row:
        return None
    doc = dict(row)
    lines = conn.execute(
        'SELECT * FROM sme_advance_lines WHERE doc_id = ? ORDER BY line_no, id',
        (doc_id,),
    ).fetchall()
    doc['lines'] = [dict(x) for x in lines]
    return doc


def void_advance_doc(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    reason: str = 'Hủy chứng từ tạm ứng',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Hủy chứng từ tạm ứng — đảo journal/phiếu liên quan nếu đã ghi sổ."""
    from Services.sme.branch_filter import assert_row_in_branch
    from Services.sme.vouchers import void_voucher

    assert_row_in_branch(conn, 'sme_advance_docs', doc_id, label='Chứng từ tạm ứng')
    doc = get_advance_doc(conn, doc_id)
    if not doc:
        raise ValueError('Không tìm thấy chứng từ')
    if doc['status'] == 'void':
        raise ValueError('Chứng từ đã hủy')

    # Settlement: reverse settlement journal + void return/additional vouchers; reopen advance
    if doc['doc_type'] == DOC_ADVANCE_SETTLEMENT:
        if doc.get('settlement_journal_id'):
            reverse_journal_entry(
                conn, int(doc['settlement_journal_id']),
                created_by=created_by, reason=reason,
            )
        for vid_key in ('cash_return_voucher_id', 'additional_voucher_id'):
            vid = doc.get(vid_key)
            if vid:
                try:
                    void_voucher(conn, int(vid), reason=reason, created_by=created_by, commit=False)
                except ValueError:
                    pass
        if doc.get('advance_doc_id'):
            conn.execute(
                "UPDATE sme_advance_docs SET status = 'disbursed', updated_at = ? WHERE id = ?",
                (_now(), int(doc['advance_doc_id'])),
            )
    else:
        if doc.get('disbursement_voucher_id'):
            try:
                void_voucher(
                    conn, int(doc['disbursement_voucher_id']),
                    reason=reason, created_by=created_by, commit=False,
                )
            except ValueError:
                pass
        elif doc.get('journal_entry_id'):
            reverse_journal_entry(
                conn, int(doc['journal_entry_id']),
                created_by=created_by, reason=reason,
            )

    conn.execute(
        "UPDATE sme_advance_docs SET status = 'void', notes = ?, updated_at = ? WHERE id = ?",
        (reason, _now(), doc_id),
    )
    if commit:
        sqlite_commit(conn, label='advances')
    return get_advance_doc(conn, doc_id)
