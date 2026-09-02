"""Phân tích DT/CP từ sổ nhật ký — đối chiếu B02 với bảng cân đối phát sinh.

Nguyên tắc (khi chưa khóa sổ / loại KCKQ):
  Mỗi TK lấy phát sinh kỳ rồi **bù trừ Nợ ↔ Có** (không lấy một phía).
  Ví dụ trả hàng: ghi Nợ 511 (giảm DT), Có 632 (giảm giá vốn) → phải trừ vào số net.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

MONEY_Q = Decimal('0.01')

# TK doanh thu thuần (BCPS): 511*, 515* trừ 521* giảm trừ (711 = thu nhập khác)
_REVENUE_PREFIXES = ('511', '515')
_CONTRA_REVENUE_PREFIXES = ('521',)
_OTHER_INCOME_PREFIXES = ('711',)

# Nhóm chi phí: 627 chỉ 6271/6272; 641 & 642 ghi cấp 1 (LIKE vẫn khớp TK con legacy nếu còn).
_EXPENSE_BUCKETS: tuple[tuple[str, str, str], ...] = (
    ('cogs', 'Giá vốn / NVL trực tiếp', "jl.account_code LIKE '632%' OR jl.account_code LIKE '631%' OR jl.account_code LIKE '621%'"),
    ('labor', 'Chi phí nhân công / lương', (
        "jl.account_code LIKE '622%' "
        "OR je.business_type LIKE '%LUONG%' OR je.business_type LIKE '%PAYROLL%' "
        "OR je.document_type IN ('BL', 'TL', 'PC02')"
    )),
    ('depreciation', 'Khấu hao TSCĐ', (
        "je.document_type = 'KHTS' OR je.business_type = 'KHAU_HAO_TSCD'"
    )),
    ('tools_allocation', 'Phân bổ CCDC', (
        "je.document_type = 'PBCC' OR je.business_type = 'PHAN_BO_CCDC'"
    )),
    ('selling', 'Chi phí bán hàng', "jl.account_code LIKE '641%'"),
    ('admin', 'Chi phí quản lý doanh nghiệp', "jl.account_code LIKE '642%'"),
    ('financial', 'Chi phí tài chính', "jl.account_code LIKE '635%'"),
    ('production_overhead', 'Chi phí SX chung (627*)', "jl.account_code LIKE '627%'"),
    ('other', 'Chi phí khác', "jl.account_code LIKE '811%'"),
    ('income_tax', 'Chi phí thuế TNDN', "jl.account_code LIKE '821%'"),
)


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _base_journal_sql(branch_code: str | None) -> tuple[str, list[Any]]:
    from Services.sme.branches import branch_sql_filter

    bf, bp = branch_sql_filter(branch_code, alias='je')
    sql = f"""
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        LEFT JOIN sme_chart_of_accounts coa
            ON coa.code = jl.account_code AND COALESCE(coa.is_active, 1) = 1
        WHERE je.status IN ('posted', 'reversed')
          AND je.posting_date >= ? AND je.posting_date <= ?
          AND COALESCE(je.document_type, '') != 'KCKQ'
          {bf}
    """
    return sql, bp


def trial_balance_pl_totals(
    conn: sqlite3.Connection,
    date_from: str,
    date_to: str,
    *,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Tổng DT/CP từ phát sinh BCPS (posting_date), đã bù trừ Nợ↔Có từng TK.

    Loại KCKQ để lấy số trước khóa sổ (giống B02). Mỗi TK: net = Có−Nợ (DT)
    hoặc Nợ−Có (CP) — gồm cả bút toán giảm DT / giảm GV do trả hàng.
    """
    base, bp = _base_journal_sql(branch_code)
    params: list[Any] = [date_from[:10], date_to[:10], *bp]

    rows = conn.execute(
        f"""
        SELECT jl.account_code,
               COALESCE(coa.account_class, '') AS account_class,
               COALESCE(coa.normal_balance, 'debit') AS normal_balance,
               SUM(jl.debit) AS debit,
               SUM(jl.credit) AS credit
        {base}
        GROUP BY jl.account_code, account_class, normal_balance
        """,
        params,
    ).fetchall()

    revenue_gross = Decimal('0.00')
    revenue_contra = Decimal('0.00')
    other_income = Decimal('0.00')
    expense_total = Decimal('0.00')
    unmapped: list[dict[str, float]] = []
    account_nets: list[dict[str, Any]] = []

    for r in rows:
        code = str(r[0] or '')
        cls = str(r[1] or '').lower()
        normal = str(r[2] or 'debit').lower()
        d = _money(r[3])
        c = _money(r[4])

        if code.startswith('911'):
            continue
        if d == 0 and c == 0:
            continue

        if code.startswith(_CONTRA_REVENUE_PREFIXES):
            net = d - c
            revenue_contra += net
            account_nets.append({'account_code': code, 'side': 'contra_revenue', 'debit': _f(d), 'credit': _f(c), 'net': _f(net)})
            continue
        if code.startswith(_OTHER_INCOME_PREFIXES):
            net = c - d
            other_income += net
            account_nets.append({'account_code': code, 'side': 'other_income', 'debit': _f(d), 'credit': _f(c), 'net': _f(net)})
            continue
        if code.startswith(_REVENUE_PREFIXES) or (cls == 'revenue' and normal == 'credit'):
            net = c - d  # bù trừ: DT ghi Có − giảm DT ghi Nợ (trả hàng…)
            revenue_gross += net
            account_nets.append({'account_code': code, 'side': 'revenue', 'debit': _f(d), 'credit': _f(c), 'net': _f(net)})
            continue
        if cls == 'expense' or code.startswith(('632', '635', '641', '642', '811', '821', '621', '622', '623', '627', '631')):
            net = d - c  # bù trừ: CP ghi Nợ − giảm CP ghi Có (trả hàng…)
            expense_total += net
            account_nets.append({'account_code': code, 'side': 'expense', 'debit': _f(d), 'credit': _f(c), 'net': _f(net)})
            continue
        if (d - c) != 0 and cls not in ('asset', 'liability', 'equity', ''):
            unmapped.append({
                'account_code': code,
                'account_class': cls,
                'net': _f(d - c),
            })

    revenue_net = _money(revenue_gross - revenue_contra)
    # Khớp B02-DNSN mã 01 (DT + thu nhập) và B02-DN mã 10 (chỉ DT thuần bán hàng)
    revenue_and_income = _money(revenue_net + other_income)
    profit_before_tax = _money(revenue_and_income - expense_total)

    return {
        'revenue_gross': _f(revenue_gross),
        'revenue_contra': _f(revenue_contra),
        'revenue_net': _f(revenue_net),
        'other_income': _f(other_income),
        'revenue_and_income': _f(revenue_and_income),
        'expense_total': _f(expense_total),
        'profit_before_tax': _f(profit_before_tax),
        'account_nets': account_nets[:50],
        'unmapped_accounts': unmapped[:20],
    }


def journal_expense_breakdown(
    conn: sqlite3.Connection,
    date_from: str,
    date_to: str,
    *,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Phân rã chi phí theo loại từ nhật ký (không trùng bucket — ưu tiên thứ tự)."""
    base, bp = _base_journal_sql(branch_code)
    params_base: list[Any] = [date_from[:10], date_to[:10], *bp]

    assigned: set[int] = set()
    buckets: dict[str, Decimal] = {}
    labels: dict[str, str] = {}

    for key, label, where in _EXPENSE_BUCKETS:
        labels[key] = label
        rows = conn.execute(
            f"""
            SELECT jl.id, jl.debit, jl.credit
            {base}
              AND ({where})
              AND (jl.debit != 0 OR jl.credit != 0)
            """,
            params_base,
        ).fetchall()
        total = Decimal('0.00')
        for row in rows:
            lid = int(row[0])
            if lid in assigned:
                continue
            assigned.add(lid)
            # Bù trừ Nợ − Có (có dòng Có khi trả hàng / điều chỉnh giảm CP)
            total += _money(row[1]) - _money(row[2])
        buckets[key] = _money(total)

    total = sum(buckets.values(), Decimal('0.00'))
    return {
        'labels': labels,
        'amounts': {k: _f(v) for k, v in buckets.items()},
        'total': _f(total),
    }


def b02_expense_total(by_code: dict) -> Decimal:
    """Tổng chi phí theo chỉ tiêu B02-DN (TT99)."""
    keys = ('11', '22', '25', '26', '32', '51')
    return _money(sum(_money(by_code.get(k, 0)) for k in keys))


_PL_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ('revenue', 'Doanh thu bán hàng và cung cấp dịch vụ', 'revenue'),
    ('contra_revenue', 'Giảm trừ doanh thu', 'contra_revenue'),
    ('other_income', 'Thu nhập khác', 'other_income'),
    ('expense', 'Chi phí', 'expense'),
)


def _classify_pl_side(code: str, cls: str, normal: str) -> str | None:
    if code.startswith('911'):
        return None
    if code.startswith(_CONTRA_REVENUE_PREFIXES):
        return 'contra_revenue'
    if code.startswith(_OTHER_INCOME_PREFIXES):
        return 'other_income'
    if code.startswith(_REVENUE_PREFIXES) or (cls == 'revenue' and normal == 'credit'):
        return 'revenue'
    if cls == 'expense' or code.startswith(
        ('632', '635', '641', '642', '811', '821', '621', '622', '623', '627', '631')
    ):
        return 'expense'
    return None


def pl_account_detail_rows(
    conn: sqlite3.Connection,
    date_from: str,
    date_to: str,
    *,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Chi tiết DT/CP theo từng TK — khớp tên/mã trên Sổ cái & BCPS (bù trừ Nợ↔Có)."""
    base, bp = _base_journal_sql(branch_code)
    params: list[Any] = [date_from[:10], date_to[:10], *bp]

    rows = conn.execute(
        f"""
        SELECT jl.account_code,
               COALESCE(coa.name, '') AS account_name,
               COALESCE(coa.level, 1) AS level,
               COALESCE(coa.parent_code, '') AS parent_code,
               COALESCE(coa.account_class, '') AS account_class,
               COALESCE(coa.normal_balance, 'debit') AS normal_balance,
               SUM(jl.debit) AS debit,
               SUM(jl.credit) AS credit
        {base}
        GROUP BY jl.account_code, account_name, level, parent_code, account_class, normal_balance
        ORDER BY jl.account_code
        """,
        params,
    ).fetchall()

    by_side: dict[str, list[dict[str, Any]]] = {k: [] for k, _, _ in _PL_SECTIONS}
    section_totals: dict[str, Decimal] = {k: Decimal('0.00') for k, _, _ in _PL_SECTIONS}

    for r in rows:
        code = str(r[0] or '')
        d = _money(r[6])
        c = _money(r[7])
        if d == 0 and c == 0:
            continue
        cls = str(r[4] or '').lower()
        normal = str(r[5] or 'debit').lower()
        side = _classify_pl_side(code, cls, normal)
        if not side:
            continue

        if side == 'revenue' or side == 'other_income':
            net = c - d
        elif side == 'contra_revenue':
            net = d - c
        else:
            net = d - c

        if net == 0:
            continue

        name = (r[1] or '').strip() or code
        item = {
            'account_code': code,
            'name': name,
            'level': int(r[2] or 1),
            'parent_code': (r[3] or '') or None,
            'account_class': cls or None,
            'side': side,
            'period_debit': _f(d),
            'period_credit': _f(c),
            'net': _f(net),
        }
        by_side[side].append(item)
        if side == 'contra_revenue':
            section_totals['contra_revenue'] += net
            section_totals['revenue'] -= net
        else:
            section_totals[side] += net

    revenue_net = _money(section_totals['revenue'])
    other_income = _money(section_totals['other_income'])
    expense_total = _money(section_totals['expense'])
    revenue_and_income = _money(revenue_net + other_income)
    profit_before_tax = _money(revenue_and_income - expense_total)

    sections: list[dict[str, Any]] = []
    flat_rows: list[dict[str, Any]] = []
    for key, label, side_key in _PL_SECTIONS:
        items = by_side.get(side_key) or []
        if not items:
            continue
        total = section_totals.get(side_key, Decimal('0.00'))
        if side_key == 'contra_revenue':
            total = section_totals['contra_revenue']
        sections.append({
            'key': key,
            'label': label,
            'side': side_key,
            'total': _f(total),
            'account_count': len(items),
        })
        flat_rows.append({
            'kind': 'section',
            'label': label,
            'side': side_key,
            'total': _f(total),
        })
        for item in items:
            row = {**item, 'kind': 'account'}
            flat_rows.append(row)

    flat_rows.append({
        'kind': 'total',
        'label': 'Lợi nhuận trước thuế (DT net + TN khác − CP net)',
        'net': _f(profit_before_tax),
        'bold': True,
    })

    revenue_accounts = (
        by_side.get('revenue', [])
        + by_side.get('contra_revenue', [])
        + by_side.get('other_income', [])
    )
    expense_accounts = list(by_side.get('expense') or [])

    return {
        'sections': sections,
        'rows': flat_rows,
        'revenue_accounts': revenue_accounts,
        'expense_accounts': expense_accounts,
        'totals': {
            'revenue_net': _f(revenue_net),
            'other_income': _f(other_income),
            'revenue_and_income': _f(revenue_and_income),
            'expense_total': _f(expense_total),
            'profit_before_tax': _f(profit_before_tax),
        },
    }


def pl_expense_detail_from_bcps(
    conn: sqlite3.Connection,
    date_from: str,
    date_to: str,
    *,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Phân rã chi phí theo từng TK — nguồn BCPS (bù trừ Nợ/Có, loại KCKQ)."""
    detail = pl_account_detail_rows(
        conn, date_from, date_to, branch_code=branch_code,
    )
    accounts = detail.get('expense_accounts') or []
    rows = [
        {**acc, 'source': 'bcps'}
        for acc in accounts
    ]
    return {
        'source': 'bcps',
        'label': 'Phân rã chi phí (Cân đối phát sinh)',
        'rows': rows,
        'total': detail.get('totals', {}).get('expense_total', 0),
        'account_count': len(rows),
    }
