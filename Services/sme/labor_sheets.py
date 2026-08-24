"""Bảng thanh toán LĐTL phụ: 02 thưởng, 03 làm thêm, 04 thuê ngoài."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry, reverse_journal_entry
from Services.sme.vouchers import create_payment
from db_utils import sqlite_commit

MONEY_Q = Decimal('0.01')

FORM_MAP = {
    'bonus': '02-LĐTL',
    'overtime': '03-LĐTL',
    'external': '04-LĐTL',
}
TITLE_MAP = {
    'bonus': 'Bảng thanh toán tiền thưởng',
    'overtime': 'Bảng thanh toán tiền làm thêm giờ',
    'external': 'Bảng thanh toán tiền thuê ngoài',
}


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_labor_sheets_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_labor_sheets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_type TEXT NOT NULL,
            form_code TEXT NOT NULL,
            sheet_no TEXT NOT NULL UNIQUE,
            sheet_date TEXT NOT NULL,
            department TEXT,
            total_amount REAL NOT NULL DEFAULT 0,
            tax_withheld REAL NOT NULL DEFAULT 0,
            net_amount REAL NOT NULL DEFAULT 0,
            expense_account TEXT NOT NULL DEFAULT '642',
            liability_account TEXT NOT NULL DEFAULT '3341',
            journal_entry_id INTEGER,
            payment_voucher_id INTEGER,
            status TEXT NOT NULL DEFAULT 'posted',
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            branch_code TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_labor_sheet_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_id INTEGER NOT NULL,
            line_no INTEGER NOT NULL DEFAULT 1,
            person_name TEXT NOT NULL,
            person_id_no TEXT,
            work_content TEXT,
            quantity REAL DEFAULT 0,
            unit_price REAL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            tax_amount REAL DEFAULT 0,
            net_amount REAL DEFAULT 0,
            FOREIGN KEY(sheet_id) REFERENCES sme_labor_sheets(id)
        )
        """
    )
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, 'sme_labor_sheets')
    if commit:
        sqlite_commit(conn, label='labor_sheets')


def _next_no(conn: sqlite3.Connection, prefix: str) -> str:
    row = conn.execute(
        "SELECT sheet_no FROM sme_labor_sheets WHERE sheet_no LIKE ? ORDER BY id DESC LIMIT 1",
        (f'{prefix}%',),
    ).fetchone()
    if not row:
        return f'{prefix}000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['sheet_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'{prefix}{int(digits) + 1:06d}'


def create_labor_sheet(
    conn: sqlite3.Connection,
    *,
    sheet_type: str,
    sheet_date: str,
    lines: list[dict],
    department: str = '',
    expense_account: str | None = None,
    liability_account: str | None = None,
    pay_now: bool = True,
    payment_method: str = 'cash',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_labor_sheets_schema(conn, commit=False)

    st = (sheet_type or '').strip().lower()
    if st not in FORM_MAP:
        raise ValueError('sheet_type phải là bonus|overtime|external')
    date_s = str(sheet_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày')
    if not lines:
        raise ValueError('Thiếu dòng thanh toán')

    defaults_exp = {'bonus': '642', 'overtime': '622', 'external': '642'}
    defaults_liab = {'bonus': '3341', 'overtime': '3341', 'external': '331'}
    exp = (expense_account or defaults_exp[st]).strip()
    liab = (liability_account or defaults_liab[st]).strip()

    prepared = []
    total = Decimal('0.00')
    tax_total = Decimal('0.00')
    for i, raw in enumerate(lines, start=1):
        name = (raw.get('person_name') or raw.get('name') or '').strip()
        if not name:
            continue
        qty = _money(raw.get('quantity') or 0)
        price = _money(raw.get('unit_price') or 0)
        amt = _money(raw.get('amount')) if raw.get('amount') is not None else (qty * price)
        if amt <= 0 and qty > 0 and price > 0:
            amt = qty * price
        tax = _money(raw.get('tax_amount') or 0)
        net = amt - tax
        if amt <= 0:
            continue
        prepared.append({
            'line_no': i, 'person_name': name,
            'person_id_no': raw.get('person_id_no') or '',
            'work_content': raw.get('work_content') or '',
            'quantity': float(qty), 'unit_price': float(price),
            'amount': float(amt), 'tax_amount': float(tax), 'net_amount': float(net),
        })
        total += amt
        tax_total += tax
    if not prepared or total <= 0:
        raise ValueError('Không có dòng hợp lệ')

    net_total = total - tax_total
    prefix = {'bonus': 'THUONG', 'overtime': 'OT', 'external': 'THUE'}[st]
    sno = _next_no(conn, prefix)
    form = FORM_MAP[st]
    desc = f'{TITLE_MAP[st]} {sno}'

    # Nợ CP = tổng gross; Có 334/331 = net; Có 3335 = thuế TNCN khấu trừ (nếu có)
    jlines = [
        {'sequence': 1, 'account_code': exp, 'debit': float(total), 'credit': 0, 'description': desc},
    ]
    seq = 2
    if net_total > 0:
        jlines.append({
            'sequence': seq, 'account_code': liab,
            'debit': 0, 'credit': float(net_total), 'description': desc,
        })
        seq += 1
    if tax_total > 0:
        jlines.append({
            'sequence': seq, 'account_code': '3335',
            'debit': 0, 'credit': float(tax_total), 'description': f'TNCN khấu trừ {sno}',
        })

    entry = post_journal_entry(
        conn,
        posting_date=date_s, document_date=date_s,
        document_type='LDTL', document_no=sno,
        business_type=f'LDTL_{st.upper()}',
        description=desc, created_by=created_by, lines=jlines,
    )

    payment_voucher = None
    if pay_now and net_total > 0:
        # Chi tổng cho tập thể / người đầu tiên nếu 1 dòng
        receiver = prepared[0]['person_name'] if len(prepared) == 1 else 'Tập thể NLĐ'
        payment_voucher = create_payment(
            conn,
            voucher_date=date_s,
            party_name=receiver,
            amount=float(net_total),
            payment_method=payment_method or 'cash',
            debit_account=liab,
            reason=desc,
            reference_document=sno,
            source_type='labor_sheet',
            created_by=created_by,
            commit=False,
        )

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_labor_sheets (
            sheet_type, form_code, sheet_no, sheet_date, department,
            total_amount, tax_withheld, net_amount, expense_account, liability_account,
            journal_entry_id, payment_voucher_id, status, notes, created_by, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'posted',?,?,?)
        """,
        (
            st, form, sno, date_s, department or '',
            float(total), float(tax_total), float(net_total), exp, liab,
            entry['id'], payment_voucher['id'] if payment_voucher else None,
            notes or '', created_by, _now(),
        ),
    )
    sid = cur.lastrowid
    from Services.sme.branch_filter import stamp_row_branch
    stamp_row_branch(conn, 'sme_labor_sheets', sid)
    for ln in prepared:
        cur.execute(
            """
            INSERT INTO sme_labor_sheet_lines (
                sheet_id, line_no, person_name, person_id_no, work_content,
                quantity, unit_price, amount, tax_amount, net_amount
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                sid, ln['line_no'], ln['person_name'], ln['person_id_no'], ln['work_content'],
                ln['quantity'], ln['unit_price'], ln['amount'], ln['tax_amount'], ln['net_amount'],
            ),
        )
    if commit:
        sqlite_commit(conn, label='labor_sheets')
    return get_labor_sheet(conn, sid)


def get_labor_sheet(conn: sqlite3.Connection, sheet_id: int) -> dict[str, Any] | None:
    ensure_sme_labor_sheets_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_labor_sheets WHERE id = ?', (sheet_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['title'] = TITLE_MAP.get(d.get('sheet_type'), d.get('form_code'))
    d['lines'] = [dict(x) for x in conn.execute(
        'SELECT * FROM sme_labor_sheet_lines WHERE sheet_id = ? ORDER BY line_no, id',
        (sheet_id,),
    ).fetchall()]
    return d


def list_labor_sheets(
    conn: sqlite3.Connection,
    *,
    sheet_type: str | None = None,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_labor_sheets_schema(conn, commit=False)
    sql = "SELECT * FROM sme_labor_sheets WHERE status != 'void'"
    params: list[Any] = []
    if sheet_type:
        sql += ' AND sheet_type = ?'
        params.append(sheet_type)
    from Services.sme.branch_filter import branch_where
    bf, bp = branch_where(branch_code)
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY sheet_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        r['title'] = TITLE_MAP.get(r.get('sheet_type'), r.get('form_code'))
    return rows


def void_labor_sheet(
    conn: sqlite3.Connection,
    sheet_id: int,
    *,
    reason: str = 'Hủy bảng LĐTL',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_labor_sheets', sheet_id, label='Bảng thanh toán LĐTL')
    doc = get_labor_sheet(conn, sheet_id)
    if not doc:
        raise ValueError('Không tìm thấy bảng thanh toán')
    if doc.get('status') == 'void':
        raise ValueError('Đã hủy')
    if doc.get('journal_entry_id'):
        reverse_journal_entry(
            conn, int(doc['journal_entry_id']),
            created_by=created_by, reason=reason,
        )
    if doc.get('payment_voucher_id'):
        try:
            from Services.sme.vouchers import void_voucher
            void_voucher(
                conn, int(doc['payment_voucher_id']),
                reason=reason, created_by=created_by, commit=False,
            )
        except Exception:
            pass
    conn.execute(
        "UPDATE sme_labor_sheets SET status = 'void', notes = COALESCE(notes,'') || ? WHERE id = ?",
        (f' | VOID: {reason}', sheet_id),
    )
    if commit:
        sqlite_commit(conn, label='labor_sheets')
    return get_labor_sheet(conn, sheet_id)
