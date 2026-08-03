"""Kết chuyển kết quả kinh doanh theo kỳ: DT/CP → 911 → 4212."""
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

DOC_CLOSE = 'KCKQ'
MONEY_Q = Decimal('0.01')

# Doanh thu / thu nhập (đóng Nợ TK / Có 911)
REVENUE_PREFIXES = ('511', '515', '711')
# Chi phí / giảm trừ DT (đóng Nợ 911 / Có TK)
EXPENSE_PREFIXES = (
    '521', '632', '635', '641', '642', '811', '821',
    '621', '622', '623', '627', '631', '611',
)
SKIP_CODES = frozenset({'911', '421', '4211', '4212'})


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _period_pl_activity(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period: int,
) -> dict[str, dict[str, Decimal]]:
    """Phát sinh kỳ trên TK P&L, loại trừ chính bút toán kết chuyển."""
    rows = conn.execute(
        """
        SELECT jl.account_code,
               SUM(jl.debit) AS debit,
               SUM(jl.credit) AS credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND je.fiscal_year = ?
          AND je.period = ?
          AND je.document_type != ?
        GROUP BY jl.account_code
        """,
        (fiscal_year, period, DOC_CLOSE),
    ).fetchall()
    return {
        r[0]: {'debit': _money(r[1]), 'credit': _money(r[2])}
        for r in rows
    }


def _coa_postable(conn: sqlite3.Connection) -> dict[str, dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT code, name, is_postable, account_class, normal_balance
        FROM sme_chart_of_accounts WHERE is_active = 1
        """
    ).fetchall()
    return {r['code']: dict(r) for r in rows}


def _matches_prefix(code: str, prefixes: tuple[str, ...]) -> bool:
    return any(code == p or code.startswith(p) for p in prefixes)


def _active_close_entry(conn: sqlite3.Connection, document_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id FROM sme_journal_entries
        WHERE document_type = ? AND document_id = ?
          AND status = 'posted' AND reverses_id IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (DOC_CLOSE, document_id),
    ).fetchone()
    return int(row[0]) if row else None


def build_period_close_lines(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
) -> tuple[list[dict], dict[str, Any]]:
    """
    Dựng dòng kết chuyển:
    - Đóng doanh thu → Có 911
    - Đóng chi phí → Nợ 911
    - Chênh lệch 911 → 4212
    """
    coa = _coa_postable(conn)
    activity = _period_pl_activity(conn, fiscal_year, period)
    acct_911 = resolve_postable_account(conn, '911')
    acct_421 = resolve_postable_account(conn, '4212')

    lines: list[dict] = []
    seq = 1
    revenue_total = Decimal('0.00')
    expense_total = Decimal('0.00')
    revenue_details: list[dict] = []
    expense_details: list[dict] = []

    for code in sorted(activity.keys()):
        if code in SKIP_CODES or code.startswith('421'):
            continue
        meta = coa.get(code) or {}
        if meta and not meta.get('is_postable'):
            continue
        bal = activity[code]
        name = (meta.get('name') if meta else None) or code

        if _matches_prefix(code, REVENUE_PREFIXES):
            net = _money(bal['credit']) - _money(bal['debit'])
            if net <= 0:
                continue
            lines.append({
                'sequence': seq,
                'account_code': code,
                'debit': net,
                'credit': 0,
                'description': f'Kết chuyển DT {code}',
            })
            seq += 1
            revenue_total += net
            revenue_details.append({'account_code': code, 'name': name, 'amount': float(net)})
        elif _matches_prefix(code, EXPENSE_PREFIXES):
            net = _money(bal['debit']) - _money(bal['credit'])
            if net <= 0:
                continue
            lines.append({
                'sequence': seq,
                'account_code': code,
                'debit': 0,
                'credit': net,
                'description': f'Kết chuyển CP {code}',
            })
            seq += 1
            expense_total += net
            expense_details.append({'account_code': code, 'name': name, 'amount': float(net)})

    if revenue_total <= 0 and expense_total <= 0:
        return [], {
            'revenue_total': 0.0,
            'expense_total': 0.0,
            'profit': 0.0,
            'revenue_details': [],
            'expense_details': [],
        }

    # Có/Nợ 911 tổng hợp
    if revenue_total > 0:
        lines.append({
            'sequence': seq,
            'account_code': acct_911,
            'debit': 0,
            'credit': revenue_total,
            'description': 'Tập hợp doanh thu vào 911',
        })
        seq += 1
    if expense_total > 0:
        lines.append({
            'sequence': seq,
            'account_code': acct_911,
            'debit': expense_total,
            'credit': 0,
            'description': 'Tập hợp chi phí vào 911',
        })
        seq += 1

    profit = revenue_total - expense_total
    if profit > 0:
        lines.append({
            'sequence': seq,
            'account_code': acct_911,
            'debit': profit,
            'credit': 0,
            'description': 'Kết chuyển lãi sang 4212',
        })
        seq += 1
        lines.append({
            'sequence': seq,
            'account_code': acct_421,
            'debit': 0,
            'credit': profit,
            'description': 'Lãi kỳ này',
        })
    elif profit < 0:
        loss = -profit
        lines.append({
            'sequence': seq,
            'account_code': acct_421,
            'debit': loss,
            'credit': 0,
            'description': 'Lỗ kỳ này',
        })
        seq += 1
        lines.append({
            'sequence': seq,
            'account_code': acct_911,
            'debit': 0,
            'credit': loss,
            'description': 'Kết chuyển lỗ từ 911',
        })

    meta = {
        'revenue_total': float(revenue_total),
        'expense_total': float(expense_total),
        'profit': float(profit),
        'revenue_details': revenue_details,
        'expense_details': expense_details,
        'account_911': acct_911,
        'account_421': acct_421,
    }
    return lines, meta


def run_period_close(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    accounting_regime: str | None = None,
    features: dict | None = None,
    created_by: str | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Ghi bút toán kết chuyển KCKQ cho một kỳ. Không commit."""
    regime = str(accounting_regime or '').upper()
    if features is not None:
        if not features.get('journal_posting'):
            return {'posted': False, 'reason': 'journal_posting_disabled', 'entry_ids': []}
        if features.get('auto_period_close') is False:
            return {'posted': False, 'reason': 'auto_period_close_disabled', 'entry_ids': []}
    elif not regime.startswith('SME'):
        return {'posted': False, 'reason': 'not_sme', 'entry_ids': []}

    if period < 1 or period > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    from Services.sme.bootstrap import ensure_sme_accounting_ready

    ensure_sme_accounting_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row

    doc_id = fiscal_year * 100 + period
    last_day = calendar.monthrange(fiscal_year, period)[1]
    posting_date = f'{fiscal_year:04d}-{period:02d}-{last_day:02d}'
    reversed_ids: list[int] = []

    existing = _active_close_entry(conn, doc_id)
    if existing and replace_existing:
        rev = reverse_journal_entry(
            conn,
            existing,
            posting_date=posting_date,
            created_by=created_by,
            reason=f'Thay thế kết chuyển KQKD {period:02d}/{fiscal_year}',
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

    lines, meta = build_period_close_lines(conn, fiscal_year=fiscal_year, period=period)
    if not lines:
        return {
            'posted': False,
            'reason': 'nothing_to_close',
            'entry_ids': [],
            'reversed_entry_ids': reversed_ids,
            **meta,
        }

    from Services.sme.branches import DEFAULT_BRANCH_CODE
    entry = post_journal_entry(
        conn,
        posting_date=posting_date,
        document_date=posting_date,
        document_type=DOC_CLOSE,
        document_no=f'KC{fiscal_year}{period:02d}',
        document_id=doc_id,
        business_type='KET_CHUYEN_KQKD',
        description=(
            f'Kết chuyển KQKD {period:02d}/{fiscal_year} '
            f'(DT {meta["revenue_total"]:,.0f} − CP {meta["expense_total"]:,.0f})'
        ),
        created_by=created_by,
        branch_code=DEFAULT_BRANCH_CODE,
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


DOC_YEAR_END = 'KCN'


def _active_year_end_entry(conn: sqlite3.Connection, fiscal_year: int) -> int | None:
    row = conn.execute(
        """
        SELECT id FROM sme_journal_entries
        WHERE document_type = ? AND document_id = ?
          AND status = 'posted' AND reverses_id IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (DOC_YEAR_END, int(fiscal_year)),
    ).fetchone()
    return int(row[0]) if row else None


def _balance_of(conn: sqlite3.Connection, account_code: str, fiscal_year: int) -> Decimal:
    """Số dư cuối năm (Nợ − Có) của một TK / nhóm TK."""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(jl.debit),0) AS d, COALESCE(SUM(jl.credit),0) AS c
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND je.fiscal_year = ?
          AND (jl.account_code = ? OR jl.account_code LIKE ?)
          AND je.document_type != ?
        """,
        (int(fiscal_year), account_code, f'{account_code}%', DOC_YEAR_END),
    ).fetchone()
    return _money(row[0]) - _money(row[1])


def run_year_end_close(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    created_by: str | None = None,
    replace_existing: bool = False,
    lock_after: bool = True,
) -> dict[str, Any]:
    """
    Kết chuyển cuối năm: chuyển số dư 4212 (LN năm nay) sang 4211 (LN năm trước).
    Nên chạy sau khi đã kết chuyển KQKD tháng 12.
    """
    from Services.sme.bootstrap import ensure_sme_accounting_ready
    from Services.sme.period_lock import assert_year_lock_allowed, lock_year

    ensure_sme_accounting_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    year = int(fiscal_year)
    posting_date = f'{year:04d}-12-31'
    reversed_ids: list[int] = []

    existing = _active_year_end_entry(conn, year)
    if existing and replace_existing:
        rev = reverse_journal_entry(
            conn, existing, posting_date=posting_date, created_by=created_by,
            reason=f'Thay thế kết chuyển cuối năm {year}',
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

    bal_4212 = _balance_of(conn, '4212', year)
    # 4212 normal credit: số dư Có = âm theo (debit-credit); lãi → credit > debit → bal < 0
    # credit_balance = credit - debit = -bal_4212
    credit_bal = -bal_4212
    if credit_bal == 0:
        return {
            'posted': False,
            'reason': 'nothing_to_close',
            'balance_4212': 0.0,
            'entry_ids': [],
            'reversed_entry_ids': reversed_ids,
        }

    acct_4212 = resolve_postable_account(conn, '4212')
    acct_4211 = resolve_postable_account(conn, '4211')
    amt = abs(credit_bal)
    if credit_bal > 0:
        # Có lãi năm nay trên 4212 → Nợ 4212 / Có 4211
        lines = [
            {'sequence': 1, 'account_code': acct_4212, 'debit': float(amt), 'credit': 0,
             'description': f'KC LN năm nay {year} sang 4211'},
            {'sequence': 2, 'account_code': acct_4211, 'debit': 0, 'credit': float(amt),
             'description': f'Nhận LN năm trước từ {year}'},
        ]
        direction = 'profit'
    else:
        # Lỗ năm nay → Nợ 4211 / Có 4212
        lines = [
            {'sequence': 1, 'account_code': acct_4211, 'debit': float(amt), 'credit': 0,
             'description': f'KC lỗ năm nay {year} sang 4211'},
            {'sequence': 2, 'account_code': acct_4212, 'debit': 0, 'credit': float(amt),
             'description': f'Kết chuyển lỗ {year}'},
        ]
        direction = 'loss'

    from Services.sme.branches import DEFAULT_BRANCH_CODE
    entry = post_journal_entry(
        conn,
        posting_date=posting_date,
        document_date=posting_date,
        document_type=DOC_YEAR_END,
        document_no=f'KCN{year}',
        document_id=year,
        business_type='KET_CHUYEN_CUOI_NAM',
        description=f'Kết chuyển cuối năm {year}: 4212 → 4211 ({direction})',
        created_by=created_by,
        branch_code=DEFAULT_BRANCH_CODE,
        lines=lines,
    )

    year_lock = None
    locked = False
    if lock_after:
        try:
            assert_year_lock_allowed(year, action='khóa sổ năm sau KC cuối năm')
            year_lock = lock_year(
                conn,
                fiscal_year=year,
                locked_by=created_by,
                reason=f'Khóa sổ năm sau kết chuyển cuối năm {year}',
            )
            locked = True
        except ValueError as exc:
            year_lock = {'skipped': True, 'message': str(exc)}
            locked = False

    vat_filing_alert = None
    micro_enterprise_alert = None
    try:
        from flask import g
        from Services.sme.vat_filing_alert import evaluate_year_end_vat_filing
        from Services.sme.micro_enterprise import evaluate_tt58_to_tt99_alert
        from Services.tenant_profile import get_current_tenant_profile
        tid = getattr(g, 'tenant_id', None)
        if tid:
            settings = (get_current_tenant_profile() or {}).get('settings') or {}
            vat_filing_alert = evaluate_year_end_vat_filing(
                conn,
                tenant_id=str(tid),
                fiscal_year=year,
                settings=settings,
                persist=True,
            )
            micro_enterprise_alert = evaluate_tt58_to_tt99_alert(
                conn,
                tenant_id=str(tid),
                fiscal_year=year,
                settings=settings,
                persist=True,
            )
    except Exception:
        pass

    return {
        'posted': True,
        'entry_id': entry['id'],
        'entry_ids': [entry['id']],
        'reversed_entry_ids': reversed_ids,
        'posting_date': posting_date,
        'amount': float(amt),
        'direction': direction,
        'account_4211': acct_4211,
        'account_4212': acct_4212,
        'locked_period_12': locked,
        'year_lock': year_lock,
        'vat_filing_alert': vat_filing_alert,
        'micro_enterprise_alert': micro_enterprise_alert,
    }
