"""Chi phí trả trước TK 242 — ghi nhận và phân bổ theo tháng."""
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
from Services.profit_report_helpers import depreciation_for_month

MONEY_Q = Decimal('0.01')
TABLE = 'sme_prepaid_expenses'
ASSET_TABLE = 'sme_prepaid_expenses'
KIND = 'PREPAID_ALLOC'


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _parse_date(value) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            sample = text[:19] if ' ' in text and fmt.startswith('%Y-%m-%d %') else text[:10]
            return datetime.strptime(sample, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text[:19])
    except ValueError:
        return None


def ensure_prepaid_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_prepaid_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_no TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            months INTEGER NOT NULL DEFAULT 12,
            original_amount REAL NOT NULL DEFAULT 0,
            vat_amount REAL NOT NULL DEFAULT 0,
            expense_account TEXT NOT NULL DEFAULT '642',
            credit_account TEXT NOT NULL DEFAULT '1121',
            payment_method TEXT NOT NULL DEFAULT 'bank',
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
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


def _next_no(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT doc_no FROM sme_prepaid_expenses WHERE doc_no LIKE 'CPTT%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return 'CPTT000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'CPTT{int(digits) + 1:06d}'


def _allocated_to_date(conn: sqlite3.Connection, prepaid_id: int) -> float:
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) FROM sme_auto_asset_postings
            WHERE kind = ? AND asset_table = ? AND asset_id = ?
            """,
            (KIND, ASSET_TABLE, int(prepaid_id)),
        ).fetchone()
        return float(row[0] or 0)
    except sqlite3.Error:
        return 0.0


def _enrich(conn: sqlite3.Connection, d: dict[str, Any]) -> dict[str, Any]:
    orig = float(d.get('original_amount') or 0)
    alloc = _allocated_to_date(conn, int(d['id']))
    remain = max(0.0, orig - alloc)
    d['allocated_amount'] = round(alloc, 0)
    d['remaining_amount'] = round(remain, 0)
    if d.get('status') != 'void':
        d['status'] = 'closed' if remain <= 0.5 else 'active'
    return d


def get_prepaid(conn: sqlite3.Connection, doc_id: int) -> dict[str, Any] | None:
    ensure_prepaid_schema(conn, commit=False)
    row = conn.execute(f'SELECT * FROM {TABLE} WHERE id = ?', (doc_id,)).fetchone()
    return _enrich(conn, dict(row)) if row else None


def list_prepaid(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    branch_code: str | None = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    ensure_prepaid_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where
    sql = f'SELECT * FROM {TABLE} WHERE 1=1'
    params: list[Any] = []
    st = (status or '').strip().lower()
    if st == 'void':
        sql += " AND status = 'void'"
    elif st != 'all':
        sql += " AND status != 'void'"
    bf, bp = branch_where(branch_code)
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY start_date DESC, id DESC LIMIT ?'
    params.append(int(limit) or 300)
    rows = [_enrich(conn, dict(r)) for r in conn.execute(sql, params).fetchall()]
    if st == 'active':
        rows = [r for r in rows if r.get('status') == 'active']
    elif st == 'closed':
        rows = [r for r in rows if r.get('status') == 'closed']
    return rows


def create_prepaid(
    conn: sqlite3.Connection,
    *,
    name: str,
    start_date: str,
    amount,
    months: int = 12,
    vat_amount=0,
    expense_account: str = '642',
    payment_method: str = 'bank',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Ghi nhận CPTB: Nợ 242 (+ 1331 nếu có VAT) / Có 111|112|331."""
    from Services.sme.branches import resolve_posting_branch
    from Services.sme.branch_filter import stamp_row_branch

    ensure_sme_journal_ready(conn, commit=False)
    ensure_prepaid_schema(conn, commit=False)

    title = (name or '').strip()
    if not title:
        raise ValueError('Thiếu nội dung chi phí trả trước')
    date_s = str(start_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày bắt đầu')
    months_n = int(months or 0)
    if months_n <= 0:
        raise ValueError('Số tháng phân bổ phải > 0')
    net = _money(amount)
    vat = _money(vat_amount)
    if net <= 0:
        raise ValueError('Số tiền phải > 0')
    total = net + vat
    pm = (payment_method or 'bank').strip().lower()
    if pm in ('ck', 'transfer', '112', '1121'):
        pm = 'bank'
    if pm in ('tm', '111', '1111'):
        pm = 'cash'
    if pm in ('ncc', 'ap', 'cong_no', '331'):
        pm = 'credit'
    if pm not in ('cash', 'bank', 'credit'):
        pm = 'bank'
    credit = '1111' if pm == 'cash' else ('331' if pm == 'credit' else '1121')
    exp = (expense_account or '642').strip() or '642'
    desc = notes or f'Chi phí trả trước: {title}'
    branch = resolve_posting_branch(conn, None)
    doc_no = _next_no(conn)
    d242 = resolve_postable_account(conn, '242')
    d133 = resolve_postable_account(conn, '1331') if vat > 0 else None
    c_acc = resolve_postable_account(conn, credit)

    if pm in ('cash', 'bank'):
        from Services.sme.vouchers import create_payment
        debit_lines = [
            {'account_code': d242, 'amount': float(net), 'description': desc},
        ]
        if vat > 0 and d133:
            debit_lines.append(
                {'account_code': d133, 'amount': float(vat), 'description': f'VAT {title}'}
            )
        voucher = create_payment(
            conn,
            voucher_date=date_s,
            party_name=title,
            amount=float(total),
            payment_method=pm,
            debit_account=d242,
            reason=desc,
            source_type='prepaid',
            created_by=created_by,
            debit_lines=debit_lines,
            commit=False,
        )
        entry_id = voucher.get('journal_entry_id')
    else:
        lines = [
            {'sequence': 1, 'account_code': d242, 'debit': float(net), 'credit': 0, 'description': desc},
        ]
        seq = 2
        if vat > 0 and d133:
            lines.append({
                'sequence': seq, 'account_code': d133,
                'debit': float(vat), 'credit': 0, 'description': f'VAT {title}',
            })
            seq += 1
        lines.append({
            'sequence': seq, 'account_code': c_acc,
            'debit': 0, 'credit': float(total), 'description': desc,
        })
        entry = post_journal_entry(
            conn,
            posting_date=date_s, document_date=date_s,
            document_type='CPTT', document_no=doc_no,
            business_type='CHI_PHI_TRA_TRUOC', description=desc,
            created_by=created_by, branch_code=branch, lines=lines,
        )
        entry_id = entry['id']

    cur = conn.cursor()
    cur.execute(
        f"""
        INSERT INTO {TABLE} (
            doc_no, name, start_date, months, original_amount, vat_amount,
            expense_account, credit_account, payment_method, journal_entry_id,
            status, notes, created_by, created_at, branch_code
        ) VALUES (?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?)
        """,
        (
            doc_no, title, date_s, months_n, float(net), float(vat),
            exp, credit, pm, entry_id, notes or '', created_by, _now(), branch,
        ),
    )
    rid = int(cur.lastrowid)
    stamp_row_branch(conn, TABLE, rid, branch)
    if commit:
        conn.commit()
    return get_prepaid(conn, rid) or {'id': rid}


def void_prepaid(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    reason: str = 'Hủy chi phí trả trước',
    created_by: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    from Services.sme.branch_filter import assert_row_in_branch

    ensure_prepaid_schema(conn, commit=False)
    assert_row_in_branch(conn, TABLE, doc_id, label='Chi phí trả trước')
    row = get_prepaid(conn, doc_id)
    if not row:
        raise ValueError('Không tìm thấy chứng từ')
    if row.get('status') == 'void':
        return row
    if float(row.get('allocated_amount') or 0) > 0.5:
        raise ValueError('Đã phân bổ một phần — không hủy gốc. Đảo bút toán phân bổ kỳ nếu cần.')
    jid = row.get('journal_entry_id')
    if jid:
        reverse_journal_entry(
            conn, int(jid), created_by=created_by, reason=reason or 'Hủy CPTB',
        )
    conn.execute(
        f"UPDATE {TABLE} SET status = 'void' WHERE id = ?",
        (int(doc_id),),
    )
    if commit:
        conn.commit()
    return get_prepaid(conn, doc_id) or row


def collect_prepaid_amounts(
    conn: sqlite3.Connection,
    year: int,
    month: int,
) -> list[dict[str, Any]]:
    """Số phân bổ tháng: đường thẳng, không vượt số còn lại."""
    ensure_prepaid_schema(conn, commit=False)
    rows = conn.execute(
        f"SELECT * FROM {TABLE} WHERE status != 'void'"
    ).fetchall()
    out = []
    for raw in rows:
        d = dict(raw)
        start = _parse_date(d.get('start_date'))
        cost = float(d.get('original_amount') or 0)
        months = int(d.get('months') or 0)
        if not start or cost <= 0 or months <= 0:
            continue
        amount = float(depreciation_for_month(cost, months, start, year, month) or 0)
        try:
            prior_row = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0) FROM sme_auto_asset_postings
                WHERE kind = ? AND asset_table = ? AND asset_id = ?
                  AND (fiscal_year < ? OR (fiscal_year = ? AND period < ?))
                """,
                (KIND, ASSET_TABLE, int(d['id']), year, year, month),
            ).fetchone()
            prior = float(prior_row[0] or 0)
        except sqlite3.Error:
            prior = 0.0
        remain = max(0.0, cost - prior)
        amount = min(amount, remain)
        if amount <= 0:
            continue
        out.append({
            'asset_id': int(d['id']),
            'code': d.get('doc_no') or '',
            'name': d.get('name') or '',
            'amount': _money(amount),
            'expense_account': (d.get('expense_account') or '642').strip() or '642',
        })
    return out
