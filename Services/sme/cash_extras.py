"""Chứng từ tiền tệ bổ sung TT99: 06-TT biên lai thu, 07-TT vàng, 09-TT bảng kê chi."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry
from Services.sme.vouchers import _cash_account, ensure_sme_voucher_schema, list_vouchers

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_cash_extras_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    ensure_sme_voucher_schema(conn, commit=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_cash_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_code TEXT NOT NULL,
            doc_no TEXT NOT NULL UNIQUE,
            listing_date TEXT NOT NULL,
            date_from TEXT,
            date_to TEXT,
            total_amount REAL NOT NULL DEFAULT 0,
            notes TEXT,
            payload_json TEXT,
            status TEXT NOT NULL DEFAULT 'posted',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_gold_sheets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_code TEXT NOT NULL DEFAULT '07-TT',
            doc_no TEXT NOT NULL UNIQUE,
            sheet_date TEXT NOT NULL,
            notes TEXT,
            total_weight REAL NOT NULL DEFAULT 0,
            total_amount REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'posted',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_gold_sheet_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_id INTEGER NOT NULL,
            line_no INTEGER NOT NULL DEFAULT 1,
            item_name TEXT NOT NULL,
            purity TEXT,
            weight REAL NOT NULL DEFAULT 0,
            unit_price REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            note TEXT,
            FOREIGN KEY(sheet_id) REFERENCES sme_gold_sheets(id)
        )
        """
    )
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, 'sme_cash_listings')
    ensure_branch_column(conn, 'sme_gold_sheets')
    if commit:
        conn.commit()


def _next_bl_no(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        SELECT voucher_no FROM sme_vouchers
        WHERE form_code = '06-TT' AND voucher_no LIKE 'BL%'
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if not row:
        return 'BL000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['voucher_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'BL{int(digits) + 1:06d}'


def _next_listing_no(conn: sqlite3.Connection, prefix: str) -> str:
    row = conn.execute(
        "SELECT doc_no FROM sme_cash_listings WHERE doc_no LIKE ? ORDER BY id DESC LIMIT 1",
        (f'{prefix}%',),
    ).fetchone()
    if not row:
        return f'{prefix}000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'{prefix}{int(digits) + 1:06d}'


def create_temp_receipt(
    conn: sqlite3.Connection,
    *,
    voucher_date: str,
    party_name: str,
    amount,
    payment_method: str = 'cash',
    credit_account: str = '131',
    reason: str = '',
    party_address: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Biên lai thu tiền 06-TT — hạch toán giống phiếu thu, mẫu riêng."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_cash_extras_schema(conn, commit=False)

    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số tiền biên lai phải > 0')
    date_s = str(voucher_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày biên lai')
    if not (party_name or '').strip():
        raise ValueError('Thiếu tên người nộp')

    debit = _cash_account(payment_method)
    credit = str(credit_account or '131').strip() or '131'
    vno = _next_bl_no(conn)
    desc = reason or f'Biên lai thu tiền {party_name}'.strip()

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='BL',
        document_no=vno,
        business_type='BIEN_LAI_THU',
        description=desc,
        created_by=created_by,
        lines=[
            {'sequence': 1, 'account_code': debit, 'debit': float(amt), 'credit': 0, 'description': desc},
            {'sequence': 2, 'account_code': credit, 'debit': 0, 'credit': float(amt), 'description': desc},
        ],
    )
    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, None)
    cur = conn.cursor()
    now = _now()
    cur.execute(
        """
        INSERT INTO sme_vouchers (
            voucher_type, form_code, voucher_no, voucher_date,
            party_name, party_address, amount, debit_account, credit_account,
            reason, journal_entry_id, status, created_by, created_at, updated_at, branch_code
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            'temp_receipt', '06-TT', vno, date_s,
            party_name, party_address or '', float(amt), debit, credit,
            desc, entry['id'], 'posted', created_by, now, now, branch,
        ),
    )
    if commit:
        conn.commit()
    return {
        'id': cur.lastrowid,
        'voucher_no': vno,
        'form_code': '06-TT',
        'journal_entry_id': entry['id'],
        'amount': float(amt),
        'party_name': party_name,
        'voucher_date': date_s,
        'reason': desc,
        'debit_account': debit,
        'credit_account': credit,
    }


def void_temp_receipt(
    conn: sqlite3.Connection,
    voucher_id: int,
    *,
    reason: str = 'Hủy biên lai thu 06-TT',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    from Services.sme.vouchers import void_voucher
    doc = get_temp_receipt(conn, voucher_id)
    if not doc:
        raise ValueError('Không tìm thấy biên lai 06-TT')
    return void_voucher(
        conn, voucher_id, reason=reason, created_by=created_by, commit=commit,
    )


def list_temp_receipts(
    conn: sqlite3.Connection,
    *,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_cash_extras_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where
    sql = """
        SELECT * FROM sme_vouchers
        WHERE form_code = '06-TT' AND status != 'void'
    """
    params: list[Any] = []
    bf, bp = branch_where(branch_code)
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY voucher_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_temp_receipt(conn: sqlite3.Connection, voucher_id: int) -> dict[str, Any] | None:
    ensure_sme_cash_extras_schema(conn, commit=False)
    row = conn.execute(
        "SELECT * FROM sme_vouchers WHERE id = ? AND form_code = '06-TT'",
        (voucher_id,),
    ).fetchone()
    return dict(row) if row else None


def build_payment_listing(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
    listing_date: str | None = None,
    notes: str = '',
    created_by: str | None = None,
    branch_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Bảng kê chi tiền 09-TT — chốt danh sách phiếu chi trong kỳ."""
    import json
    ensure_sme_cash_extras_schema(conn, commit=False)
    df = str(date_from or '')[:10]
    dt = str(date_to or '')[:10]
    if not df or not dt:
        raise ValueError('Thiếu khoảng ngày')
    payments = list_vouchers(
        conn, voucher_type='payment', date_from=df, date_to=dt,
        branch_code=branch_code, limit=2000,
    )
    total = sum(_money(p.get('amount')) for p in payments)
    doc_no = _next_listing_no(conn, 'BKCT')
    date_s = str(listing_date or dt)[:10]
    payload = {
        'lines': [
            {
                'id': p.get('id'),
                'voucher_no': p.get('voucher_no'),
                'voucher_date': p.get('voucher_date'),
                'party_name': p.get('party_name'),
                'reason': p.get('reason'),
                'amount': float(p.get('amount') or 0),
                'debit_account': p.get('debit_account'),
            }
            for p in payments
        ]
    }
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_cash_listings (
            form_code, doc_no, listing_date, date_from, date_to,
            total_amount, notes, payload_json, status, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            '09-TT', doc_no, date_s, df, dt, float(total), notes or '',
            json.dumps(payload, ensure_ascii=False), 'posted', created_by, _now(),
        ),
    )
    lid = int(cur.lastrowid)
    from Services.sme.branch_filter import stamp_row_branch
    stamp_row_branch(conn, 'sme_cash_listings', lid, branch_code=branch_code)
    if commit:
        conn.commit()
    return get_cash_listing(conn, lid)


def get_cash_listing(conn: sqlite3.Connection, doc_id: int) -> dict[str, Any] | None:
    import json
    ensure_sme_cash_extras_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_cash_listings WHERE id = ?', (doc_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d['lines'] = (json.loads(d.get('payload_json') or '{}') or {}).get('lines') or []
    except Exception:
        d['lines'] = []
    return d


def list_cash_listings(
    conn: sqlite3.Connection,
    *,
    form_code: str = '09-TT',
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_cash_extras_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where
    bf, bp = branch_where(branch_code)
    sql = """
        SELECT id, form_code, doc_no, listing_date, date_from, date_to,
               total_amount, notes, status, created_at
        FROM sme_cash_listings
        WHERE form_code = ? AND status != 'void'
    """
    params: list[Any] = [form_code]
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY listing_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def create_gold_sheet(
    conn: sqlite3.Connection,
    *,
    sheet_date: str,
    lines: list[dict],
    notes: str = '',
    created_by: str | None = None,
    branch_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Bảng kê vàng tiền tệ 07-TT (chứng từ theo dõi, không bắt buộc GL)."""
    ensure_sme_cash_extras_schema(conn, commit=False)
    date_s = str(sheet_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày')
    prepared = []
    tw = Decimal('0.00')
    ta = Decimal('0.00')
    for i, raw in enumerate(lines or [], start=1):
        name = (raw.get('item_name') or raw.get('name') or '').strip()
        w = _money(raw.get('weight') or 0)
        price = _money(raw.get('unit_price') or 0)
        amt = _money(raw.get('amount')) if raw.get('amount') is not None else (w * price)
        if not name or w <= 0:
            continue
        prepared.append({
            'line_no': i, 'item_name': name, 'purity': raw.get('purity') or '',
            'weight': float(w), 'unit_price': float(price), 'amount': float(amt),
            'note': raw.get('note') or '',
        })
        tw += w
        ta += amt
    if not prepared:
        raise ValueError('Thiếu dòng vàng hợp lệ')

    row = conn.execute(
        "SELECT doc_no FROM sme_gold_sheets WHERE doc_no LIKE 'BKCV%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        doc_no = 'BKCV000001'
    else:
        raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
        digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
        doc_no = f'BKCV{int(digits) + 1:06d}'

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_gold_sheets (
            form_code, doc_no, sheet_date, notes, total_weight, total_amount,
            status, created_by, created_at
        ) VALUES ('07-TT',?,?,?,?,?,'posted',?,?)
        """,
        (doc_no, date_s, notes or '', float(tw), float(ta), created_by, _now()),
    )
    sid = cur.lastrowid
    for ln in prepared:
        cur.execute(
            """
            INSERT INTO sme_gold_sheet_lines (
                sheet_id, line_no, item_name, purity, weight, unit_price, amount, note
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                sid, ln['line_no'], ln['item_name'], ln['purity'],
                ln['weight'], ln['unit_price'], ln['amount'], ln['note'],
            ),
        )
    from Services.sme.branch_filter import stamp_row_branch
    stamp_row_branch(conn, 'sme_gold_sheets', sid, branch_code=branch_code)
    if commit:
        conn.commit()
    return get_gold_sheet(conn, sid)


def get_gold_sheet(conn: sqlite3.Connection, sheet_id: int) -> dict[str, Any] | None:
    ensure_sme_cash_extras_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_gold_sheets WHERE id = ?', (sheet_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['lines'] = [dict(x) for x in conn.execute(
        'SELECT * FROM sme_gold_sheet_lines WHERE sheet_id = ? ORDER BY line_no, id',
        (sheet_id,),
    ).fetchall()]
    return d


def list_gold_sheets(
    conn: sqlite3.Connection,
    *,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_cash_extras_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where
    bf, bp = branch_where(branch_code)
    sql = "SELECT * FROM sme_gold_sheets WHERE status != 'void'"
    params: list[Any] = []
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY sheet_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def void_gold_sheet(
    conn: sqlite3.Connection,
    sheet_id: int,
    *,
    reason: str = 'Hủy bảng kê vàng',
    commit: bool = False,
) -> dict[str, Any]:
    doc = get_gold_sheet(conn, sheet_id)
    if not doc:
        raise ValueError('Không tìm thấy bảng kê vàng')
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_gold_sheets', sheet_id, label='Bảng kê vàng')
    if doc.get('status') == 'void':
        raise ValueError('Đã hủy')
    conn.execute(
        "UPDATE sme_gold_sheets SET status = 'void', notes = ? WHERE id = ?",
        ((doc.get('notes') or '') + f' | {reason}', sheet_id),
    )
    if commit:
        conn.commit()
    return get_gold_sheet(conn, sheet_id)


def void_cash_listing(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    reason: str = 'Hủy bảng kê chi tiền',
    commit: bool = False,
) -> dict[str, Any]:
    doc = get_cash_listing(conn, doc_id)
    if not doc:
        raise ValueError('Không tìm thấy bảng kê chi')
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_cash_listings', doc_id, label='Bảng kê chi tiền')
    if doc.get('status') == 'void':
        raise ValueError('Đã hủy')
    conn.execute(
        "UPDATE sme_cash_listings SET status = 'void', notes = ? WHERE id = ?",
        ((doc.get('notes') or '') + f' | {reason}', doc_id),
    )
    if commit:
        conn.commit()
    return get_cash_listing(conn, doc_id)
