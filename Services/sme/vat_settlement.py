"""Quyết toán GTGT cuối kỳ — bù trừ 133 ↔ 33311."""
from __future__ import annotations

import calendar
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import (
    post_journal_entry,
    resolve_postable_account,
    reverse_journal_entry,
)

DOC_VAT = 'QTGT'
MONEY_Q = Decimal('0.01')
INPUT_PREFIXES = ('13311', '13312', '1332')
OUTPUT_PREFIXES = ('33311',)


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _matches(code: str, prefixes: tuple[str, ...]) -> bool:
    return any(code == p or code.startswith(p) for p in prefixes)


def _active_vat_entry(conn: sqlite3.Connection, document_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id FROM sme_journal_entries
        WHERE document_type = ? AND document_id = ?
          AND status = 'posted' AND reverses_id IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (DOC_VAT, document_id),
    ).fetchone()
    return int(row[0]) if row else None


def _balances_through_period(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period: int,
) -> dict[str, dict[str, Decimal]]:
    """Số dư lũy kế đến cuối kỳ, loại trừ QTGT của chính kỳ này (để có thể tính lại)."""
    rows = conn.execute(
        """
        SELECT jl.account_code,
               SUM(jl.debit) AS debit,
               SUM(jl.credit) AS credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND (
              je.fiscal_year < ?
              OR (je.fiscal_year = ? AND je.period < ?)
              OR (
                  je.fiscal_year = ? AND je.period = ?
                  AND je.document_type != ?
              )
          )
        GROUP BY jl.account_code
        """,
        (fiscal_year, fiscal_year, period, fiscal_year, period, DOC_VAT),
    ).fetchall()
    return {
        r[0]: {'debit': _money(r[1]), 'credit': _money(r[2])}
        for r in rows
    }


def _coa_postable(conn: sqlite3.Connection) -> dict[str, dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT code, name, is_postable, normal_balance
        FROM sme_chart_of_accounts WHERE is_active = 1
        """
    ).fetchall()
    return {r['code']: dict(r) for r in rows}


def build_vat_settlement_lines(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
) -> tuple[list[dict], dict[str, Any]]:
    """
    Bù trừ GTGT đầu vào với đầu ra:
    - Có đầu ra O, Nợ đầu vào I
    - Dr 33311 min(O,I)… / Cr 133* …
    - Phần còn lại giữ trên TK gốc (phải nộp hoặc còn được khấu trừ).
    """
    coa = _coa_postable(conn)
    bals = _balances_through_period(conn, fiscal_year, period)

    input_rows: list[dict] = []
    output_rows: list[dict] = []
    input_total = Decimal('0.00')
    output_total = Decimal('0.00')

    for code in sorted(bals.keys()):
        meta = coa.get(code) or {}
        if meta and not meta.get('is_postable'):
            continue
        bal = bals[code]
        if _matches(code, INPUT_PREFIXES):
            net = _money(bal['debit']) - _money(bal['credit'])
            if net <= 0:
                continue
            input_rows.append({
                'account_code': code,
                'name': (meta.get('name') if meta else None) or code,
                'amount': net,
            })
            input_total += net
        elif _matches(code, OUTPUT_PREFIXES):
            net = _money(bal['credit']) - _money(bal['debit'])
            if net <= 0:
                continue
            output_rows.append({
                'account_code': code,
                'name': (meta.get('name') if meta else None) or code,
                'amount': net,
            })
            output_total += net

    offset = min(input_total, output_total)
    meta_out = {
        'input_total': float(input_total),
        'output_total': float(output_total),
        'offset_amount': float(offset),
        'vat_payable': float(max(Decimal('0.00'), output_total - input_total)),
        'vat_credit_carry': float(max(Decimal('0.00'), input_total - output_total)),
        'input_details': [
            {'account_code': r['account_code'], 'name': r['name'], 'amount': float(r['amount'])}
            for r in input_rows
        ],
        'output_details': [
            {'account_code': r['account_code'], 'name': r['name'], 'amount': float(r['amount'])}
            for r in output_rows
        ],
    }
    if offset <= 0:
        return [], meta_out

    # Phân bổ offset theo tỷ lệ từng TK đầu vào / đầu ra
    lines: list[dict] = []
    seq = 1
    remain_out = offset
    for i, row in enumerate(output_rows):
        if remain_out <= 0:
            break
        if i == len(output_rows) - 1:
            share = remain_out
        else:
            share = _money(offset * (row['amount'] / output_total)) if output_total else Decimal('0')
            share = min(share, remain_out, row['amount'])
        if share <= 0:
            continue
        code = resolve_postable_account(conn, row['account_code'])
        lines.append({
            'sequence': seq,
            'account_code': code,
            'debit': share,
            'credit': 0,
            'description': f'Bù trừ GTGT đầu ra {code}',
        })
        seq += 1
        remain_out -= share

    remain_in = offset
    for i, row in enumerate(input_rows):
        if remain_in <= 0:
            break
        if i == len(input_rows) - 1:
            share = remain_in
        else:
            share = _money(offset * (row['amount'] / input_total)) if input_total else Decimal('0')
            share = min(share, remain_in, row['amount'])
        if share <= 0:
            continue
        code = resolve_postable_account(conn, row['account_code'])
        lines.append({
            'sequence': seq,
            'account_code': code,
            'debit': 0,
            'credit': share,
            'description': f'Bù trừ GTGT đầu vào {code}',
        })
        seq += 1
        remain_in -= share

    # Cân chỉnh làm tròn: đảm bảo Nợ = Có
    dsum = sum((_money(x['debit']) for x in lines), Decimal('0'))
    csum = sum((_money(x['credit']) for x in lines), Decimal('0'))
    if dsum != csum and lines:
        diff = dsum - csum
        # chỉnh dòng Có cuối (đầu vào)
        for ln in reversed(lines):
            if ln['credit'] > 0:
                ln['credit'] = _money(ln['credit'] + diff)
                break

    return lines, meta_out


def run_vat_settlement(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    accounting_regime: str | None = None,
    features: dict | None = None,
    created_by: str | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Ghi bút toán QTGT. Không commit."""
    regime = str(accounting_regime or '').upper()
    if features is not None:
        if not features.get('journal_posting'):
            return {'posted': False, 'reason': 'journal_posting_disabled', 'entry_ids': []}
        if features.get('auto_vat_settlement') is False:
            return {'posted': False, 'reason': 'auto_vat_settlement_disabled', 'entry_ids': []}
    elif not regime.startswith('SME'):
        return {'posted': False, 'reason': 'not_sme', 'entry_ids': []}

    if period < 1 or period > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    from Services.sme.bootstrap import ensure_sme_accounting_ready
    from Services.sme.period_lock import assert_period_open, is_period_locked, unlock_period

    ensure_sme_accounting_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row

    if is_period_locked(conn, fiscal_year, period) and not replace_existing:
        return {
            'posted': False,
            'reason': 'period_locked',
            'entry_ids': [],
        }
    if replace_existing and is_period_locked(conn, fiscal_year, period):
        unlock_period(conn, fiscal_year=fiscal_year, period=period)

    doc_id = fiscal_year * 100 + period
    last_day = calendar.monthrange(fiscal_year, period)[1]
    posting_date = f'{fiscal_year:04d}-{period:02d}-{last_day:02d}'
    reversed_ids: list[int] = []

    existing = _active_vat_entry(conn, doc_id)
    if existing and replace_existing:
        assert_period_open(conn, fiscal_year, period, action='đảo quyết toán GTGT')
        rev = reverse_journal_entry(
            conn,
            existing,
            posting_date=posting_date,
            created_by=created_by,
            reason=f'Thay thế quyết toán GTGT {period:02d}/{fiscal_year}',
        )
        reversed_ids.append(int(rev['id']))
        existing = None
    if existing:
        return {
            'posted': False,
            'reason': 'already_posted',
            'entry_id': existing,
            'entry_ids': [existing],
            'reversed_entry_ids': reversed_ids,
        }

    assert_period_open(conn, fiscal_year, period, action='quyết toán GTGT')
    lines, meta = build_vat_settlement_lines(conn, fiscal_year=fiscal_year, period=period)
    if not lines:
        return {
            'posted': False,
            'reason': 'nothing_to_settle',
            'entry_ids': [],
            'reversed_entry_ids': reversed_ids,
            **meta,
        }

    entry = post_journal_entry(
        conn,
        posting_date=posting_date,
        document_date=posting_date,
        document_type=DOC_VAT,
        document_no=f'GT{fiscal_year}{period:02d}',
        document_id=doc_id,
        business_type='QUYET_TOAN_GTGT',
        description=(
            f'Quyết toán GTGT {period:02d}/{fiscal_year} '
            f'(bù trừ {meta["offset_amount"]:,.0f}; '
            f'phải nộp {meta["vat_payable"]:,.0f}; '
            f'còn khấu trừ {meta["vat_credit_carry"]:,.0f})'
        ),
        created_by=created_by,
        lines=lines,
    )
    return {
        'posted': True,
        'entry_id': entry['id'],
        'entry_ids': [entry['id']],
        'reversed_entry_ids': reversed_ids,
        'posting_date': posting_date,
        **meta,
    }
