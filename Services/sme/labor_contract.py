"""Hợp đồng giao khoán SME — mẫu 05-LĐTL + biên bản thanh lý 06-LĐTL."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry, reverse_journal_entry
from Services.sme.vouchers import create_payment

MONEY_Q = Decimal('0.01')
FORM_CONTRACT = '05-LĐTL'
FORM_SETTLEMENT = '06-LĐTL'


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_labor_contract_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_labor_contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_code TEXT NOT NULL DEFAULT '05-LĐTL',
            contract_no TEXT NOT NULL UNIQUE,
            contract_date TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            employer_rep_name TEXT,
            employer_rep_title TEXT,
            contractor_name TEXT NOT NULL,
            contractor_title TEXT,
            contractor_address TEXT,
            contractor_id_no TEXT,
            method TEXT,
            conditions TEXT,
            other_terms TEXT,
            work_content TEXT,
            contract_amount REAL NOT NULL DEFAULT 0,
            expense_account TEXT NOT NULL DEFAULT '622',
            liability_account TEXT NOT NULL DEFAULT '331',
            status TEXT NOT NULL DEFAULT 'active',
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
        CREATE TABLE IF NOT EXISTS sme_labor_contract_settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_code TEXT NOT NULL DEFAULT '06-LĐTL',
            settlement_no TEXT NOT NULL UNIQUE,
            settlement_date TEXT NOT NULL,
            contract_id INTEGER NOT NULL,
            accepted_amount REAL NOT NULL DEFAULT 0,
            paid_amount REAL NOT NULL DEFAULT 0,
            penalty_amount REAL NOT NULL DEFAULT 0,
            payable_amount REAL NOT NULL DEFAULT 0,
            quality_note TEXT,
            conclusion TEXT,
            journal_entry_id INTEGER,
            payment_voucher_id INTEGER,
            status TEXT NOT NULL DEFAULT 'posted',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(contract_id) REFERENCES sme_labor_contracts(id)
        )
        """
    )
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, 'sme_labor_contracts')
    if commit:
        conn.commit()


def _next_no(conn: sqlite3.Connection, table: str, col: str, prefix: str) -> str:
    row = conn.execute(
        f"SELECT {col} FROM {table} WHERE {col} LIKE ? ORDER BY id DESC LIMIT 1",
        (f'{prefix}%',),
    ).fetchone()
    if not row:
        return f'{prefix}000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row[col]
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'{prefix}{int(digits) + 1:06d}'


def create_labor_contract(
    conn: sqlite3.Connection,
    *,
    contract_date: str,
    contractor_name: str,
    contract_amount,
    work_content: str = '',
    start_date: str = '',
    end_date: str = '',
    employer_rep_name: str = '',
    employer_rep_title: str = 'Đại diện bên giao khoán',
    contractor_title: str = 'Đại diện bên nhận khoán',
    contractor_address: str = '',
    contractor_id_no: str = '',
    method: str = 'Giao khoán khối lượng công việc',
    conditions: str = '',
    other_terms: str = '',
    expense_account: str = '622',
    liability_account: str = '331',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Lập Hợp đồng giao khoán 05-LĐTL (chưa ghi sổ — chờ thanh lý 06)."""
    ensure_sme_labor_contract_schema(conn, commit=False)
    name = (contractor_name or '').strip()
    if not name:
        raise ValueError('Thiếu tên bên nhận khoán')
    date_s = str(contract_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày hợp đồng')
    amt = _money(contract_amount)
    if amt < 0:
        raise ValueError('Giá trị HĐ không hợp lệ')

    cno = _next_no(conn, 'sme_labor_contracts', 'contract_no', 'HDGK')
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_labor_contracts (
            form_code, contract_no, contract_date, start_date, end_date,
            employer_rep_name, employer_rep_title, contractor_name, contractor_title,
            contractor_address, contractor_id_no, method, conditions, other_terms,
            work_content, contract_amount, expense_account, liability_account,
            status, notes, created_by, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?)
        """,
        (
            FORM_CONTRACT, cno, date_s,
            (start_date or date_s)[:10], (end_date or '')[:10] or None,
            employer_rep_name or '', employer_rep_title or '',
            name, contractor_title or '',
            contractor_address or '', contractor_id_no or '',
            method or '', conditions or '', other_terms or '',
            work_content or '', float(amt),
            (expense_account or '622').strip() or '622',
            (liability_account or '331').strip() or '331',
            notes or '', created_by, _now(), _now(),
        ),
    )
    cid = cur.lastrowid
    from Services.sme.branch_filter import stamp_row_branch
    stamp_row_branch(conn, 'sme_labor_contracts', cid)
    if commit:
        conn.commit()
    return get_labor_contract(conn, cid)


def settle_labor_contract(
    conn: sqlite3.Connection,
    *,
    contract_id: int,
    settlement_date: str,
    accepted_amount=None,
    paid_amount=0,
    penalty_amount=0,
    quality_note: str = '',
    conclusion: str = '',
    pay_now: bool = True,
    payment_method: str = 'cash',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """
    Biên bản thanh lý 06-LĐTL + hạch toán:
      Nợ TK chi phí (622/642…) / Có TK phải trả (331/334…) = còn phải trả
      (tuỳ chọn) lập phiếu chi thanh toán phần còn lại.
    """
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_labor_contract_schema(conn, commit=False)

    contract = get_labor_contract(conn, contract_id)
    if not contract:
        raise ValueError('Không tìm thấy hợp đồng giao khoán')
    if contract['status'] == 'settled':
        raise ValueError('Hợp đồng đã thanh lý')
    if contract['status'] == 'void':
        raise ValueError('Hợp đồng đã hủy')

    date_s = str(settlement_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày thanh lý')

    accepted = _money(accepted_amount if accepted_amount is not None else contract['contract_amount'])
    paid = _money(paid_amount)
    penalty = _money(penalty_amount)
    if accepted < 0 or paid < 0 or penalty < 0:
        raise ValueError('Số tiền không hợp lệ')

    # Còn phải trả = nghiệm thu - đã trả - phạt (phạt trừ người nhận khoán)
    payable = accepted - paid - penalty
    if payable < 0:
        # Đã trả thừa — ghi nhận phải thu lại (đơn giản: không âm payable, báo lỗi)
        raise ValueError(
            f'Đã thanh toán vượt nghiệm thu (còn lại {payable}). '
            'Điều chỉnh số đã trả / nghiệm thu.'
        )

    sno = _next_no(conn, 'sme_labor_contract_settlements', 'settlement_no', 'TLGK')
    desc = f"Thanh lý HĐGK {contract['contract_no']} — {contract['contractor_name']}"
    expense = contract.get('expense_account') or '622'
    liability = contract.get('liability_account') or '331'

    entry = None
    payment_voucher = None
    if payable > 0:
        entry = post_journal_entry(
            conn,
            posting_date=date_s,
            document_date=date_s,
            document_type='TLGK',
            document_no=sno,
            document_id=contract_id,
            business_type='THANH_LY_GIAO_KHOAN',
            description=desc,
            reference_document=contract['contract_no'],
            created_by=created_by,
            lines=[
                {
                    'sequence': 1, 'account_code': expense,
                    'debit': float(payable), 'credit': 0, 'description': desc,
                },
                {
                    'sequence': 2, 'account_code': liability,
                    'debit': 0, 'credit': float(payable), 'description': desc,
                },
            ],
        )
        if pay_now:
            payment_voucher = create_payment(
                conn,
                voucher_date=date_s,
                party_name=contract['contractor_name'],
                amount=float(payable),
                payment_method=payment_method or 'cash',
                debit_account=liability,
                reason=f'Thanh toán HĐGK {contract["contract_no"]}',
                reference_document=sno,
                source_type='labor_contract',
                source_id=contract_id,
                created_by=created_by,
                commit=False,
            )

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_labor_contract_settlements (
            form_code, settlement_no, settlement_date, contract_id,
            accepted_amount, paid_amount, penalty_amount, payable_amount,
            quality_note, conclusion, journal_entry_id, payment_voucher_id,
            status, created_by, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'posted',?,?)
        """,
        (
            FORM_SETTLEMENT, sno, date_s, contract_id,
            float(accepted), float(paid), float(penalty), float(payable),
            quality_note or '', conclusion or 'Hai bên nhất trí thanh lý hợp đồng.',
            entry['id'] if entry else None,
            payment_voucher['id'] if payment_voucher else None,
            created_by, _now(),
        ),
    )
    settle_id = cur.lastrowid
    conn.execute(
        """
        UPDATE sme_labor_contracts
        SET status = 'settled', updated_at = ?
        WHERE id = ?
        """,
        (_now(), contract_id),
    )
    if commit:
        conn.commit()
    out = get_settlement(conn, settle_id)
    out['contract'] = get_labor_contract(conn, contract_id)
    out['payment_voucher'] = payment_voucher
    return out


def get_labor_contract(conn: sqlite3.Connection, contract_id: int) -> dict[str, Any] | None:
    ensure_sme_labor_contract_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_labor_contracts WHERE id = ?', (contract_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    settles = conn.execute(
        'SELECT * FROM sme_labor_contract_settlements WHERE contract_id = ? ORDER BY id',
        (contract_id,),
    ).fetchall()
    d['settlements'] = [dict(s) for s in settles]
    return d


def get_settlement(conn: sqlite3.Connection, settlement_id: int) -> dict[str, Any] | None:
    ensure_sme_labor_contract_schema(conn, commit=False)
    row = conn.execute(
        'SELECT * FROM sme_labor_contract_settlements WHERE id = ?', (settlement_id,)
    ).fetchone()
    return dict(row) if row else None


def list_labor_contracts(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_labor_contract_schema(conn, commit=False)
    sql = "SELECT * FROM sme_labor_contracts WHERE status != 'void'"
    params: list[Any] = []
    if status:
        sql += ' AND status = ?'
        params.append(status)
    from Services.sme.branch_filter import branch_where
    bf, bp = branch_where(branch_code)
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY contract_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for d in rows:
        settles = conn.execute(
            """
            SELECT id, settlement_no, status FROM sme_labor_contract_settlements
            WHERE contract_id = ? AND status != 'void' ORDER BY id DESC LIMIT 1
            """,
            (d['id'],),
        ).fetchall()
        d['settlements'] = [dict(s) for s in settles]
    return rows


def void_labor_contract(
    conn: sqlite3.Connection,
    contract_id: int,
    *,
    reason: str = 'Hủy hợp đồng giao khoán',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    from Services.sme.branch_filter import assert_row_in_branch
    from Services.sme.vouchers import void_voucher

    assert_row_in_branch(conn, 'sme_labor_contracts', contract_id, label='Hợp đồng giao khoán')
    doc = get_labor_contract(conn, contract_id)
    if not doc:
        raise ValueError('Không tìm thấy HĐ')
    if doc['status'] == 'void':
        raise ValueError('Đã hủy')
    for s in doc.get('settlements') or []:
        if s.get('journal_entry_id'):
            try:
                reverse_journal_entry(
                    conn, int(s['journal_entry_id']),
                    created_by=created_by, reason=reason,
                )
            except ValueError:
                pass
        if s.get('payment_voucher_id'):
            try:
                void_voucher(
                    conn, int(s['payment_voucher_id']),
                    reason=reason, created_by=created_by, commit=False,
                )
            except ValueError:
                pass
        conn.execute(
            "UPDATE sme_labor_contract_settlements SET status = 'void' WHERE id = ?",
            (s['id'],),
        )
    conn.execute(
        "UPDATE sme_labor_contracts SET status = 'void', notes = ?, updated_at = ? WHERE id = ?",
        ((doc.get('notes') or '') + f' | {reason}', _now(), contract_id),
    )
    if commit:
        conn.commit()
    return get_labor_contract(conn, contract_id)
