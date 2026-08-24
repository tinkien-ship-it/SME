"""Đánh giá lại chênh lệch tỷ giá cuối kỳ SME (TK tiền/công nợ ngoại tệ → 515/635 hoặc 413)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry, reverse_journal_entry
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


def ensure_sme_fx_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_fx_revaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fiscal_year INTEGER NOT NULL,
            period INTEGER NOT NULL,
            reval_date TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            rate REAL NOT NULL,
            unrealized_gain REAL NOT NULL DEFAULT 0,
            unrealized_loss REAL NOT NULL DEFAULT 0,
            equity_mode INTEGER NOT NULL DEFAULT 0,
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'posted',
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            branch_code TEXT,
            UNIQUE(fiscal_year, period, currency)
        )
        """
    )
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, 'sme_fx_revaluations')
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_fx_revaluation_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reval_id INTEGER NOT NULL,
            account_code TEXT NOT NULL,
            balance_fc REAL NOT NULL DEFAULT 0,
            book_vnd REAL NOT NULL DEFAULT 0,
            revalued_vnd REAL NOT NULL DEFAULT 0,
            difference REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(reval_id) REFERENCES sme_fx_revaluations(id)
        )
        """
    )
    if commit:
        sqlite_commit(conn, label='fx_revaluation')


def revalue_foreign_currency(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    currency: str,
    rate,
    lines: list[dict],
    reval_date: str | None = None,
    equity_mode: bool = False,
    notes: str = '',
    created_by: str | None = None,
    replace_existing: bool = False,
    commit: bool = False,
) -> dict[str, Any]:
    """
    lines[]: account_code, balance_fc, book_vnd (số dư sổ VND hiện tại).
    revalued = balance_fc * rate; diff = revalued - book_vnd.
    Asset/debit-normal tăng → lãi 515 (hoặc 413); giảm → lỗ 635.
    Liability/credit-normal: dấu ngược.
    """
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_fx_schema(conn, commit=False)
    year, per = int(fiscal_year), int(period)
    cur_code = (currency or 'USD').strip().upper()
    fx_rate = _money(rate)
    if fx_rate <= 0:
        raise ValueError('Tỷ giá phải > 0')
    if not lines:
        raise ValueError('Thiếu dòng đánh giá lại')

    existing = conn.execute(
        'SELECT * FROM sme_fx_revaluations WHERE fiscal_year=? AND period=? AND currency=?',
        (year, per, cur_code),
    ).fetchone()
    if existing:
        ex = dict(existing)
        if not replace_existing:
            raise ValueError(f'Đã đánh giá {cur_code} kỳ {per}/{year}')
        if ex.get('journal_entry_id'):
            reverse_journal_entry(
                conn, int(ex['journal_entry_id']),
                created_by=created_by, reason='Thay đánh giá tỷ giá',
            )
        conn.execute('DELETE FROM sme_fx_revaluation_lines WHERE reval_id = ?', (ex['id'],))
        conn.execute('DELETE FROM sme_fx_revaluations WHERE id = ?', (ex['id'],))

    date_s = (reval_date or f'{year:04d}-{per:02d}-28')[:10]
    gain_acc = '413' if equity_mode else '515'
    loss_acc = '413' if equity_mode else '635'

    detail_rows = []
    total_gain = Decimal('0.00')
    total_loss = Decimal('0.00')
    for raw in lines:
        acc = str(raw.get('account_code') or '').strip()
        if not acc:
            continue
        bal_fc = _money(raw.get('balance_fc'))
        book = _money(raw.get('book_vnd'))
        revalued = (bal_fc * fx_rate).quantize(MONEY_Q)
        diff = revalued - book
        # Tài khoản tiền/phải thu (prefix 11x, 13x): số dư Nợ — tăng = lãi
        # Phải trả (33x): số dư Có — tăng nghĩa vụ VND = lỗ
        is_liability = acc.startswith('33') or acc.startswith('34')
        economic = -diff if is_liability else diff
        detail_rows.append({
            'account_code': acc,
            'balance_fc': float(bal_fc),
            'book_vnd': float(book),
            'revalued_vnd': float(revalued),
            'difference': float(economic),
        })
        if economic > 0:
            total_gain += economic
        elif economic < 0:
            total_loss += abs(economic)

    if total_gain == 0 and total_loss == 0:
        raise ValueError('Không có chênh lệch tỷ giá')

    doc_no = f'FX{year}{per:02d}{cur_code}'
    desc = notes or f'Đánh giá lại {cur_code} kỳ {per}/{year} @ {fx_rate}'
    jlines = []
    seq = 1
    for d in detail_rows:
        eco = _money(d['difference'])
        if eco == 0:
            continue
        acc = d['account_code']
        is_liability = acc.startswith('33') or acc.startswith('34')
        if not is_liability:
            # Điều chỉnh tài sản: lãi → Nợ tiền; lỗ → Có tiền
            if eco > 0:
                jlines.append({'sequence': seq, 'account_code': acc, 'debit': float(eco), 'credit': 0, 'description': desc})
            else:
                jlines.append({'sequence': seq, 'account_code': acc, 'debit': 0, 'credit': float(abs(eco)), 'description': desc})
        else:
            # Điều chỉnh nợ: lãi (eco>0 nghĩa vụ giảm) → Nợ phải trả; lỗ → Có phải trả
            if eco > 0:
                jlines.append({'sequence': seq, 'account_code': acc, 'debit': float(eco), 'credit': 0, 'description': desc})
            else:
                jlines.append({'sequence': seq, 'account_code': acc, 'debit': 0, 'credit': float(abs(eco)), 'description': desc})
        seq += 1

    if total_gain > 0:
        jlines.append({
            'sequence': seq, 'account_code': gain_acc,
            'debit': 0, 'credit': float(total_gain), 'description': desc,
        })
        seq += 1
    if total_loss > 0:
        jlines.append({
            'sequence': seq, 'account_code': loss_acc,
            'debit': float(total_loss), 'credit': 0, 'description': desc,
        })

    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, None)
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='FXRV',
        document_no=doc_no,
        document_id=per,
        business_type='DANH_GIA_TY_GIA',
        currency=cur_code,
        exchange_rate=float(fx_rate),
        description=desc,
        created_by=created_by,
        branch_code=branch,
        lines=jlines,
    )
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_fx_revaluations (
            fiscal_year, period, reval_date, currency, rate,
            unrealized_gain, unrealized_loss, equity_mode,
            journal_entry_id, status, notes, created_by, created_at, branch_code
        ) VALUES (?,?,?,?,?,?,?,?,?,'posted',?,?,?,?)
        """,
        (
            year, per, date_s, cur_code, float(fx_rate),
            float(total_gain), float(total_loss), 1 if equity_mode else 0,
            entry['id'], notes or '', created_by, _now(), branch,
        ),
    )
    rid = cur.lastrowid
    for d in detail_rows:
        cur.execute(
            """
            INSERT INTO sme_fx_revaluation_lines
                (reval_id, account_code, balance_fc, book_vnd, revalued_vnd, difference)
            VALUES (?,?,?,?,?,?)
            """,
            (rid, d['account_code'], d['balance_fc'], d['book_vnd'], d['revalued_vnd'], d['difference']),
        )
    if commit:
        sqlite_commit(conn, label='fx_revaluation')
    return get_fx_revaluation(conn, rid)


def get_fx_revaluation(conn: sqlite3.Connection, reval_id: int) -> dict[str, Any] | None:
    ensure_sme_fx_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_fx_revaluations WHERE id = ?', (reval_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['lines'] = [dict(x) for x in conn.execute(
        'SELECT * FROM sme_fx_revaluation_lines WHERE reval_id = ?', (reval_id,)
    ).fetchall()]
    return d


def list_fx_revaluations(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int | None = None,
    branch_code: str | None = None,
) -> list[dict[str, Any]]:
    ensure_sme_fx_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where
    sql = "SELECT * FROM sme_fx_revaluations WHERE status != 'void'"
    params: list[Any] = []
    if fiscal_year:
        sql += ' AND fiscal_year = ?'
        params.append(int(fiscal_year))
    bf, bp = branch_where(branch_code)
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY fiscal_year DESC, period DESC, currency LIMIT 48'
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def void_fx_revaluation(
    conn: sqlite3.Connection,
    reval_id: int,
    *,
    reason: str = 'Hủy đánh giá tỷ giá',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_fx_revaluations', reval_id, label='Phiếu đánh giá tỷ giá')
    doc = get_fx_revaluation(conn, reval_id)
    if not doc:
        raise ValueError('Không tìm thấy phiếu đánh giá tỷ giá')
    if str(doc.get('status') or '').lower() == 'void':
        raise ValueError('Đã hủy')
    if doc.get('journal_entry_id'):
        reverse_journal_entry(
            conn, int(doc['journal_entry_id']),
            created_by=created_by, reason=reason,
        )
    conn.execute(
        "UPDATE sme_fx_revaluations SET status = 'void', notes = ? WHERE id = ?",
        ((doc.get('notes') or '') + f' | {reason}', reval_id),
    )
    if commit:
        sqlite_commit(conn, label='fx_revaluation')
    return get_fx_revaluation(conn, reval_id)
