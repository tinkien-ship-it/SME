"""Phân tích DT/CP từ sổ nhật ký — đối chiếu B02 với bảng cân đối phát sinh."""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

MONEY_Q = Decimal('0.01')

# TK doanh thu thuần (BCPS): 511*, 515* trừ 521* giảm trừ (711 = thu nhập khác)
_REVENUE_PREFIXES = ('511', '515')
_CONTRA_REVENUE_PREFIXES = ('521',)
_OTHER_INCOME_PREFIXES = ('711',)

# Nhóm chi phí theo TT58/TT99 + thực tế SME (payroll, KH, CCDC)
_EXPENSE_BUCKETS: tuple[tuple[str, str, str], ...] = (
    ('cogs', 'Giá vốn / NVL trực tiếp', "jl.account_code LIKE '632%' OR jl.account_code LIKE '631%' OR jl.account_code LIKE '621%'"),
    ('labor', 'Chi phí nhân công / lương', (
        "jl.account_code LIKE '622%' OR jl.account_code LIKE '6272%' "
        "OR jl.account_code LIKE '6411%' OR jl.account_code LIKE '6421%' "
        "OR je.business_type LIKE '%LUONG%' OR je.business_type LIKE '%PAYROLL%' "
        "OR je.document_type IN ('BL', 'TL', 'PC02')"
    )),
    ('depreciation', 'Khấu hao TSCĐ', (
        "je.document_type = 'KHTS' OR je.business_type = 'KHAU_HAO_TSCD' "
        "OR jl.account_code LIKE '6273%' OR jl.account_code LIKE '6412%' OR jl.account_code LIKE '6422%'"
    )),
    ('tools_allocation', 'Phân bổ CCDC', (
        "je.document_type = 'PBCC' OR je.business_type = 'PHAN_BO_CCDC'"
    )),
    ('selling', 'Chi phí bán hàng', (
        "(jl.account_code LIKE '641%' "
        "AND jl.account_code NOT LIKE '6411%' AND jl.account_code NOT LIKE '6412%')"
    )),
    ('admin', 'Chi phí quản lý doanh nghiệp', (
        "(jl.account_code LIKE '642%' "
        "AND jl.account_code NOT LIKE '6421%' AND jl.account_code NOT LIKE '6422%')"
    )),
    ('financial', 'Chi phí tài chính', "jl.account_code LIKE '635%'"),
    ('production_overhead', 'Chi phí SX chung (627*)', (
        "(jl.account_code LIKE '627%' "
        "AND jl.account_code NOT LIKE '6272%' AND jl.account_code NOT LIKE '6273%')"
    )),
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
    """Tổng DT thuần & CP từ BCPS (posting_date) — class 5/6/7/8."""
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

    for r in rows:
        code = str(r[0] or '')
        cls = str(r[1] or '').lower()
        normal = str(r[2] or 'debit').lower()
        d = _money(r[3])
        c = _money(r[4])

        if code.startswith('911'):
            continue

        if code.startswith(_CONTRA_REVENUE_PREFIXES):
            revenue_contra += d - c
            continue
        if code.startswith(_OTHER_INCOME_PREFIXES):
            other_income += c - d
            continue
        if code.startswith(_REVENUE_PREFIXES) or (cls == 'revenue' and normal == 'credit'):
            revenue_gross += c - d
            continue
        if cls == 'expense' or code.startswith(('632', '635', '641', '642', '811', '821', '621', '622', '623', '627', '631')):
            expense_total += d - c
            continue
        if (d - c) != 0 and cls not in ('asset', 'liability', 'equity', ''):
            unmapped.append({
                'account_code': code,
                'account_class': cls,
                'net': _f(d - c),
            })

    revenue_net = _money(revenue_gross - revenue_contra)
    profit_before_tax = _money(revenue_net + other_income - expense_total)

    return {
        'revenue_gross': _f(revenue_gross),
        'revenue_contra': _f(revenue_contra),
        'revenue_net': _f(revenue_net),
        'other_income': _f(other_income),
        'expense_total': _f(expense_total),
        'profit_before_tax': _f(profit_before_tax),
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
              AND jl.debit > 0
            """,
            params_base,
        ).fetchall()
        total = Decimal('0.00')
        for row in rows:
            lid = int(row[0])
            if lid in assigned:
                continue
            assigned.add(lid)
            total += _money(row[1]) - _money(row[2])
        buckets[key] = _money(max(total, Decimal('0.00')))

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
