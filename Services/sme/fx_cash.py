"""Theo dõi quỹ ngoại tệ (1112/1122) và bán ngoại tệ có lãi/lỗ chênh lệch tỷ giá."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    post_journal_entry,
    resolve_postable_account,
)
from Services.sme.vouchers import (
    VOUCHER_FORM_PAYMENT,
    _now,
    _vnd_funding_account,
    ensure_sme_voucher_schema,
    _next_voucher_no,
)

MONEY_Q = Decimal('0.01')
FC_Q = Decimal('0.0001')
FX_ACCOUNTS = ('1112', '1122')


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _fc(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(FC_Q, rounding=ROUND_HALF_UP)


def fx_account_position(
    conn: sqlite3.Connection,
    *,
    account_code: str,
    as_of: str,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Số dư VND + FC và tỷ giá sổ bình quân của TK ngoại tệ."""
    from Services.sme.branches import branch_sql_filter

    ensure_sme_journal_ready(conn, commit=False)
    code = (account_code or '').strip()
    date_s = str(as_of or '')[:10] or datetime.now().strftime('%Y-%m-%d')
    if not code.startswith('1112') and not code.startswith('1122'):
        raise ValueError('Chỉ hỗ trợ TK 1112 hoặc 1122')

    bf, bp = branch_sql_filter(branch_code, alias='je')
    row = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(jl.debit), 0) AS debit,
            COALESCE(SUM(jl.credit), 0) AS credit,
            COALESCE(SUM(jl.debit_fc), 0) AS debit_fc,
            COALESCE(SUM(jl.credit_fc), 0) AS credit_fc
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND date(je.posting_date) <= date(?)
          AND (jl.account_code = ? OR jl.account_code LIKE ?)
          {bf}
        """,
        (date_s, code, f'{code}%', *bp),
    ).fetchone()
    debit = _money(row[0] if not isinstance(row, sqlite3.Row) else row['debit'])
    credit = _money(row[1] if not isinstance(row, sqlite3.Row) else row['credit'])
    debit_fc = _fc(row[2] if not isinstance(row, sqlite3.Row) else row['debit_fc'])
    credit_fc = _fc(row[3] if not isinstance(row, sqlite3.Row) else row['credit_fc'])
    bal_vnd = debit - credit
    bal_fc = debit_fc - credit_fc
    avg_rate = Decimal('0')
    if bal_fc > 0:
        avg_rate = (bal_vnd / bal_fc).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
    return {
        'account_code': code,
        'as_of': date_s,
        'balance_vnd': float(bal_vnd),
        'balance_fc': float(bal_fc),
        'avg_book_rate': float(avg_rate),
        'debit_vnd': float(debit),
        'credit_vnd': float(credit),
        'debit_fc': float(debit_fc),
        'credit_fc': float(credit_fc),
    }


def fx_cash_positions(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    date_s = str(as_of or '')[:10] or datetime.now().strftime('%Y-%m-%d')
    positions = [
        fx_account_position(conn, account_code=code, as_of=date_s, branch_code=branch_code)
        for code in FX_ACCOUNTS
    ]
    return {
        'as_of': date_s,
        'branch_code': branch_code or 'ALL',
        'positions': positions,
        'accounts': {p['account_code']: p for p in positions},
    }


def fx_cash_ledger(
    conn: sqlite3.Connection,
    *,
    account_code: str = '1122',
    date_from: str | None = None,
    date_to: str | None = None,
    branch_code: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Nhật ký phát sinh ngoại tệ (VND + FC + tỷ giá dòng / tỷ giá sổ)."""
    from Services.sme.branches import branch_sql_filter

    ensure_sme_journal_ready(conn, commit=False)
    code = (account_code or '1122').strip()
    if not code.startswith('1112') and not code.startswith('1122'):
        raise ValueError('Chỉ hỗ trợ TK 1112 hoặc 1122')

    today = datetime.now().strftime('%Y-%m-%d')
    d_from = (date_from or f'{today[:4]}-01-01')[:10]
    d_to = (date_to or today)[:10]
    bf, bp = branch_sql_filter(branch_code, alias='je')

    opening = _fx_opening_before(
        conn, account_code=code, before_date=d_from, branch_code=branch_code,
    )

    rows_db = conn.execute(
        f"""
        SELECT
            jl.id AS line_id,
            jl.entry_id,
            jl.sequence,
            jl.account_code,
            jl.debit,
            jl.credit,
            COALESCE(jl.debit_fc, 0) AS debit_fc,
            COALESCE(jl.credit_fc, 0) AS credit_fc,
            COALESCE(jl.exchange_rate, je.exchange_rate, 1) AS exchange_rate,
            COALESCE(jl.currency, je.currency, 'VND') AS currency,
            COALESCE(jl.description, je.description, '') AS description,
            je.entry_no,
            je.posting_date,
            je.document_type,
            je.document_no,
            je.business_type,
            (
                SELECT GROUP_CONCAT(x.account_code, ', ')
                FROM (
                    SELECT DISTINCT other.account_code
                    FROM sme_journal_lines other
                    WHERE other.entry_id = jl.entry_id AND other.id <> jl.id
                    ORDER BY other.sequence, other.id
                ) x
            ) AS counterpart_accounts
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND date(je.posting_date) >= date(?)
          AND date(je.posting_date) <= date(?)
          AND (jl.account_code = ? OR jl.account_code LIKE ?)
          {bf}
        ORDER BY je.posting_date, je.id, jl.sequence, jl.id
        LIMIT ?
        """,
        (d_from, d_to, code, f'{code}%', *bp, int(limit)),
    ).fetchall()

    run_vnd = _money(opening.get('balance_vnd'))
    run_fc = _fc(opening.get('balance_fc'))
    rows: list[dict[str, Any]] = []
    for i, row in enumerate(rows_db, start=1):
        r = dict(row)
        d_vnd = _money(r['debit'])
        c_vnd = _money(r['credit'])
        d_fc = _fc(r['debit_fc'])
        c_fc = _fc(r['credit_fc'])
        run_vnd += d_vnd - c_vnd
        run_fc += d_fc - c_fc
        line_rate = _money(r['exchange_rate'])
        if d_fc > 0 and d_vnd > 0:
            line_rate = (d_vnd / d_fc).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
        elif c_fc > 0 and c_vnd > 0:
            line_rate = (c_vnd / c_fc).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
        avg = (run_vnd / run_fc).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP) if run_fc > 0 else Decimal('0')
        rows.append({
            'sequence': i,
            'line_id': r['line_id'],
            'entry_id': r['entry_id'],
            'posting_date': (r['posting_date'] or '')[:10],
            'entry_no': r['entry_no'],
            'document_type': r['document_type'],
            'document_no': r['document_no'],
            'business_type': r['business_type'] or '',
            'description': r['description'],
            'account_code': r['account_code'],
            'counterpart_accounts': r['counterpart_accounts'] or '',
            'currency': r['currency'] or 'USD',
            'debit_vnd': float(d_vnd),
            'credit_vnd': float(c_vnd),
            'debit_fc': float(d_fc),
            'credit_fc': float(c_fc),
            'line_rate': float(line_rate),
            'balance_vnd': float(run_vnd),
            'balance_fc': float(run_fc),
            'avg_book_rate': float(avg),
        })

    closing = {
        'balance_vnd': float(run_vnd),
        'balance_fc': float(run_fc),
        'avg_book_rate': float(
            (run_vnd / run_fc).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
            if run_fc > 0 else Decimal('0')
        ),
    }
    return {
        'account_code': code,
        'date_from': d_from,
        'date_to': d_to,
        'opening': opening,
        'closing': closing,
        'rows': rows,
    }


def _fx_opening_before(
    conn: sqlite3.Connection,
    *,
    account_code: str,
    before_date: str,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Số dư mở đầu kỳ (trước ``before_date``)."""
    from Services.sme.branches import branch_sql_filter

    code = (account_code or '').strip()
    date_s = str(before_date or '')[:10]
    bf, bp = branch_sql_filter(branch_code, alias='je')
    row = conn.execute(
        f"""
        SELECT
            COALESCE(SUM(jl.debit), 0) AS debit,
            COALESCE(SUM(jl.credit), 0) AS credit,
            COALESCE(SUM(jl.debit_fc), 0) AS debit_fc,
            COALESCE(SUM(jl.credit_fc), 0) AS credit_fc
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND date(je.posting_date) < date(?)
          AND (jl.account_code = ? OR jl.account_code LIKE ?)
          {bf}
        """,
        (date_s, code, f'{code}%', *bp),
    ).fetchone()
    debit = _money(row[0] if not isinstance(row, sqlite3.Row) else row['debit'])
    credit = _money(row[1] if not isinstance(row, sqlite3.Row) else row['credit'])
    debit_fc = _fc(row[2] if not isinstance(row, sqlite3.Row) else row['debit_fc'])
    credit_fc = _fc(row[3] if not isinstance(row, sqlite3.Row) else row['credit_fc'])
    bal_vnd = debit - credit
    bal_fc = debit_fc - credit_fc
    avg = (bal_vnd / bal_fc).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP) if bal_fc > 0 else Decimal('0')
    return {
        'account_code': code,
        'as_of': date_s,
        'balance_vnd': float(bal_vnd),
        'balance_fc': float(bal_fc),
        'avg_book_rate': float(avg),
    }


def preview_sell_fx(
    conn: sqlite3.Connection,
    *,
    fx_account: str,
    amount_fc,
    sell_rate,
    as_of: str | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    pos = fx_account_position(
        conn,
        account_code=fx_account,
        as_of=str(as_of or datetime.now().strftime('%Y-%m-%d'))[:10],
        branch_code=branch_code,
    )
    fc = _fc(amount_fc)
    rate = _money(sell_rate)
    book_rate = _money(pos['avg_book_rate'])
    if fc <= 0:
        raise ValueError('Số ngoại tệ bán phải > 0')
    if rate <= 0:
        raise ValueError('Tỷ giá bán phải > 0')
    if book_rate <= 0:
        raise ValueError(
            f'TK {fx_account} chưa có số dư ngoại tệ / tỷ giá sổ để bán '
            f'(FC={pos["balance_fc"]}, VND={pos["balance_vnd"]})'
        )
    if fc > _fc(pos['balance_fc']) + Decimal('0.00005'):
        raise ValueError(
            f'Số dư FC TK {fx_account} không đủ: có {pos["balance_fc"]}, cần bán {float(fc)}'
        )
    book_vnd = _money(fc * book_rate)
    sell_vnd = _money(fc * rate)
    diff = sell_vnd - book_vnd
    return {
        'fx_account': fx_account,
        'amount_fc': float(fc),
        'sell_rate': float(rate),
        'book_rate': float(book_rate),
        'book_vnd': float(book_vnd),
        'sell_vnd': float(sell_vnd),
        'fx_difference': float(diff),
        'is_gain': diff > 0,
        'is_loss': diff < 0,
        'gain_account': '515',
        'loss_account': '635',
        'position': pos,
    }


def sell_foreign_currency(
    conn: sqlite3.Connection,
    *,
    voucher_date: str,
    amount_fc,
    sell_rate,
    fx_account: str = '1122',
    vnd_account: str | None = None,
    payment_method: str | None = None,
    currency: str = 'USD',
    party_name: str = '',
    reason: str = '',
    created_by: str | None = None,
    branch_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Bán ngoại tệ: Nợ TK VND (theo tỷ giá bán) · Có TK NT (theo tỷ giá sổ) · 515/635 CLTG.

    Ghi phiếu chi ``purpose=sell_fx`` để theo dõi (chi ngoại tệ khỏi quỹ/TGNH NT).
    """
    from Services.sme.branches import resolve_posting_branch

    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_voucher_schema(conn, commit=False)
    branch = resolve_posting_branch(conn, branch_code)
    date_s = str(voucher_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày bán ngoại tệ')

    fx_raw = str(fx_account or '1122').strip() or '1122'
    if not (fx_raw.startswith('1112') or fx_raw.startswith('1122')):
        fx_raw = '1122'
    try:
        fx_acc = resolve_postable_account(conn, fx_raw)
    except ValueError:
        fx_acc = resolve_postable_account(conn, '1112' if fx_raw.startswith('1112') else '1122')

    if payment_method:
        vnd_acc = _vnd_funding_account(payment_method)
    elif vnd_account:
        vnd_raw = str(vnd_account).strip()
        vnd_acc = '1111' if vnd_raw.startswith('111') else '1121'
    else:
        vnd_acc = '1111' if fx_acc.startswith('1112') else '1121'
    vnd_acc = resolve_postable_account(conn, vnd_acc)

    preview = preview_sell_fx(
        conn,
        fx_account=fx_acc,
        amount_fc=amount_fc,
        sell_rate=sell_rate,
        as_of=date_s,
        branch_code=branch,
    )
    fc = _fc(preview['amount_fc'])
    book_vnd = _money(preview['book_vnd'])
    sell_vnd = _money(preview['sell_vnd'])
    diff = _money(preview['fx_difference'])
    book_rate = _money(preview['book_rate'])
    sell_r = _money(preview['sell_rate'])
    cur = (currency or 'USD').strip().upper() or 'USD'

    desc = (reason or f'Bán ngoại tệ {float(fc):g} {cur}').strip()
    desc = (
        f'{desc} (sổ {float(book_rate):g} → bán {float(sell_r):g}; '
        f'CLTG {float(diff):+,.0f} ₫)'
    )

    gain_acc = resolve_postable_account(conn, '515')
    loss_acc = resolve_postable_account(conn, '635')

    lines: list[dict[str, Any]] = [
        {
            'sequence': 1,
            'account_code': vnd_acc,
            'debit': float(sell_vnd),
            'credit': 0,
            'debit_fc': 0,
            'credit_fc': 0,
            'description': desc,
        },
    ]
    seq = 2
    if diff < 0:
        lines.append({
            'sequence': seq,
            'account_code': loss_acc,
            'debit': float(-diff),
            'credit': 0,
            'debit_fc': 0,
            'credit_fc': 0,
            'description': f'Lỗ chênh lệch tỷ giá bán {cur}',
        })
        seq += 1
    lines.append({
        'sequence': seq,
        'account_code': fx_acc,
        'debit': 0,
        'credit': float(book_vnd),
        'debit_fc': 0,
        'credit_fc': float(fc),
        'description': desc,
    })
    seq += 1
    if diff > 0:
        lines.append({
            'sequence': seq,
            'account_code': gain_acc,
            'debit': 0,
            'credit': float(diff),
            'debit_fc': 0,
            'credit_fc': 0,
            'description': f'Lãi chênh lệch tỷ giá bán {cur}',
        })

    vno = _next_voucher_no(conn, 'payment')
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='PC',
        document_no=vno,
        business_type='BAN_NGOAI_TE',
        currency=cur,
        exchange_rate=float(sell_r),
        description=desc,
        created_by=created_by,
        branch_code=branch,
        lines=lines,
    )

    cur_db = conn.cursor()
    cols = {r[1] for r in cur_db.execute('PRAGMA table_info(sme_vouchers)').fetchall()}
    base_cols = [
        'voucher_type', 'form_code', 'voucher_no', 'voucher_date',
        'party_name', 'party_address', 'party_tax_code', 'amount',
        'debit_account', 'credit_account', 'reason', 'reference_document',
        'source_type', 'source_id', 'journal_entry_id', 'status', 'created_by',
        'created_at', 'updated_at', 'branch_code',
    ]
    base_vals: list[Any] = [
        'payment', VOUCHER_FORM_PAYMENT, vno, date_s,
        party_name or 'Bán ngoại tệ', '', '', float(sell_vnd),
        vnd_acc, fx_acc, desc, None,
        'sell_fx', None,
        entry['id'], 'posted', created_by, _now(), _now(), branch,
    ]
    if 'currency' in cols:
        base_cols.extend(['currency', 'exchange_rate', 'amount_fc'])
        base_vals.extend([cur, float(sell_r), float(fc)])
    if 'purpose' in cols:
        base_cols.append('purpose')
        base_vals.append('sell_fx')
    placeholders = ','.join('?' * len(base_cols))
    cur_db.execute(
        f"INSERT INTO sme_vouchers ({', '.join(base_cols)}) VALUES ({placeholders})",
        base_vals,
    )
    voucher_id = cur_db.lastrowid
    if commit:
        conn.commit()

    return {
        'id': voucher_id,
        'voucher_no': vno,
        'journal_entry_id': entry['id'],
        'purpose': 'sell_fx',
        'fx_account': fx_acc,
        'vnd_account': vnd_acc,
        'amount_fc': float(fc),
        'currency': cur,
        'sell_rate': float(sell_r),
        'book_rate': float(book_rate),
        'book_vnd': float(book_vnd),
        'sell_vnd': float(sell_vnd),
        'fx_difference': float(diff),
        'journal_lines': lines,
        **preview,
    }
