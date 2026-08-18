"""Chỉ số dashboard SME — từ nhật ký bút toán."""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.bctc_report import _closing_balances, _period_activity

# Báo cáo/hubs doanh thu–chi phí: loại bút toán kết chuyển (làm PS DT/CP về 0)
_EXCLUDE_CLOSE = ('KCKQ',)


def _ytd_activity(conn, fiscal_year: int, period_to: int, branch_code: str | None = None):
    return _period_activity(
        conn, fiscal_year, 1, period_to,
        exclude_document_types=_EXCLUDE_CLOSE,
        branch_code=branch_code,
    )


def _month_activity(conn, fiscal_year: int, period: int, branch_code: str | None = None):
    return _period_activity(
        conn, fiscal_year, period, period,
        exclude_document_types=_EXCLUDE_CLOSE,
        branch_code=branch_code,
    )


def _branch_asset_sql(branch_code: str | None) -> tuple[str, list]:
    """Điều kiện lọc TSCĐ/CCDC theo CN."""
    from Services.sme.branches import DEFAULT_BRANCH_CODE
    code = (branch_code or '').strip().upper()
    if not code or code == 'ALL':
        return '', []
    if code == DEFAULT_BRANCH_CODE:
        return " AND (branch_code IS NULL OR branch_code = '' OR branch_code = ?)", [DEFAULT_BRANCH_CODE]
    return ' AND branch_code = ?', [code]
from Services.sme.journal_engine import ensure_sme_journal_ready

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _sum_activity(
    activity: dict[str, dict[str, Decimal]],
    prefixes: tuple[str, ...],
    *,
    side: str,
) -> Decimal:
    total = Decimal('0.00')
    for code, bal in activity.items():
        if not any(code == p or code.startswith(p) for p in prefixes):
            continue
        if side == 'credit':
            total += _money(bal.get('credit')) - _money(bal.get('debit'))
        else:
            total += _money(bal.get('debit')) - _money(bal.get('credit'))
    return _money(total)


def _sum_balance(
    bals: dict[str, dict[str, Decimal]],
    prefixes: tuple[str, ...],
    *,
    normal: str,
) -> Decimal:
    total = Decimal('0.00')
    for code, bal in bals.items():
        if not any(code == p or code.startswith(p) for p in prefixes):
            continue
        d, c = _money(bal.get('debit')), _money(bal.get('credit'))
        if normal == 'credit':
            total += c - d
        else:
            total += d - c
    return _money(total)


def dashboard_metrics(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Doanh thu, LN gộp, phải thu/trả, cơ cấu thuế theo kỳ YTD."""
    ensure_sme_journal_ready(conn, commit=False)
    from datetime import datetime
    period_to = period_to or datetime.now().month
    if period_to < 1 or period_to > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    activity = _ytd_activity(conn, fiscal_year, period_to, branch_code)
    bals = _closing_balances(conn, fiscal_year, period_to, branch_code=branch_code)

    revenue = _sum_activity(activity, ('511', '515', '711'), side='credit')
    cogs = _sum_activity(activity, ('632',), side='debit')
    selling = _sum_activity(activity, ('641',), side='debit')
    admin = _sum_activity(activity, ('642',), side='debit')
    other_exp = _sum_activity(activity, ('635', '811', '821'), side='debit')
    gross = revenue - cogs
    operating = gross - selling - admin
    profit = operating - other_exp

    receivable = _sum_balance(bals, ('131',), normal='debit')
    payable = _sum_balance(bals, ('331',), normal='credit')
    cash = _sum_balance(bals, ('111', '112'), normal='debit')
    vat_in = _sum_balance(bals, ('133',), normal='debit')
    vat_out = _sum_balance(bals, ('33311', '3331'), normal='credit')
    # Tránh đếm cha+con: ưu tiên lá 33311 nếu có
    vat_out_leaf = _sum_balance(bals, ('33311',), normal='credit')
    if vat_out_leaf != 0:
        vat_out = vat_out_leaf
    cit = _sum_balance(bals, ('3334',), normal='credit')
    pit = _sum_balance(bals, ('3335',), normal='credit')
    other_tax = _sum_balance(bals, ('3332', '3333', '3336', '3337', '3338', '3339'), normal='credit')

    # P&L theo tháng (1..period_to)
    monthly = []
    for m in range(1, period_to + 1):
        act = _month_activity(conn, fiscal_year, m, branch_code)
        rev_m = _sum_activity(act, ('511', '515', '711'), side='credit')
        cogs_m = _sum_activity(act, ('632',), side='debit')
        exp_m = _sum_activity(act, ('641', '642', '635', '811'), side='debit')
        monthly.append({
            'period': m,
            'label': f'T{m:02d}',
            'revenue': _f(rev_m),
            'cogs': _f(cogs_m),
            'expenses': _f(exp_m),
            'profit': _f(rev_m - cogs_m - exp_m),
        })

    return {
        'fiscal_year': fiscal_year,
        'period_to': period_to,
        'revenue': _f(revenue),
        'cogs': _f(cogs),
        'gross_profit': _f(gross),
        'selling_expense': _f(selling),
        'admin_expense': _f(admin),
        'operating_profit': _f(operating),
        'profit': _f(profit),
        'receivable': _f(receivable),
        'payable': _f(payable),
        'cash': _f(cash),
        'vat_input': _f(vat_in),
        'vat_output': _f(vat_out),
        'vat_payable': _f(max(Decimal('0'), vat_out - vat_in)),
        'vat_credit': _f(max(Decimal('0'), vat_in - vat_out)),
        'tax_breakdown': {
            'gtgt': _f(vat_out),
            'tndn': _f(cit),
            'tncn': _f(pit),
            'other': _f(other_tax),
        },
        'monthly': monthly,
    }


def debt_hub_metrics(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Hub công nợ: phải thu 131, phải trả 331, tạm ứng 141, tiền."""
    from datetime import datetime
    ensure_sme_journal_ready(conn, commit=False)
    period_to = period_to or datetime.now().month
    if period_to < 1 or period_to > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    bals = _closing_balances(conn, fiscal_year, period_to, branch_code=branch_code)
    activity = _ytd_activity(conn, fiscal_year, period_to, branch_code)

    receivable = _sum_balance(bals, ('131',), normal='debit')
    payable = _sum_balance(bals, ('331',), normal='credit')
    employee_advance = _sum_balance(bals, ('141',), normal='debit')
    cash = _sum_balance(bals, ('111', '112'), normal='debit')
    # Tăng công nợ phải thu / phải trả trong kỳ
    ar_increase = _sum_activity(activity, ('131',), side='debit')
    ap_increase = _sum_activity(activity, ('331',), side='credit')
    ar_collected = _sum_activity(activity, ('131',), side='credit')
    ap_paid = _sum_activity(activity, ('331',), side='debit')

    sub_ar = 0.0
    sub_ap = 0.0
    try:
        from Services.sme.debt_aging import subledger_open_totals
        sub = subledger_open_totals(conn)
        sub_ar = float(sub.get('ar') or 0)
        sub_ap = float(sub.get('ap') or 0)
    except Exception:
        pass

    monthly = []
    for m in range(1, period_to + 1):
        act = _month_activity(conn, fiscal_year, m, branch_code)
        monthly.append({
            'period': m,
            'label': f'T{m:02d}',
            'receivable_increase': _f(_sum_activity(act, ('131',), side='debit')),
            'payable_increase': _f(_sum_activity(act, ('331',), side='credit')),
        })

    return {
        'fiscal_year': fiscal_year,
        'period_to': period_to,
        'receivable': _f(max(Decimal('0'), receivable)),
        'payable': _f(max(Decimal('0'), payable)),
        'employee_advance': _f(max(Decimal('0'), employee_advance)),
        'cash': _f(max(Decimal('0'), cash)),
        'ar_increase_ytd': _f(max(Decimal('0'), ar_increase)),
        'ap_increase_ytd': _f(max(Decimal('0'), ap_increase)),
        'ar_collected_ytd': _f(max(Decimal('0'), ar_collected)),
        'ap_paid_ytd': _f(max(Decimal('0'), ap_paid)),
        'net_working_capital': _f(receivable - payable),
        'subledger_ar': sub_ar,
        'subledger_ap': sub_ap,
        'gl_ar': _f(max(Decimal('0'), receivable)),
        'gl_ap': _f(max(Decimal('0'), payable)),
        'ar_vs_gl_diff': round(sub_ar - _f(max(Decimal('0'), receivable)), 0),
        'ap_vs_gl_diff': round(sub_ap - _f(max(Decimal('0'), payable)), 0),
        'monthly': monthly,
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return bool(row)


def _safe_count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        return int(conn.execute(sql, params).fetchone()[0] or 0)
    except sqlite3.Error:
        return 0


def _safe_sum(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> float:
    try:
        return float(conn.execute(sql, params).fetchone()[0] or 0)
    except sqlite3.Error:
        return 0.0


def physical_inventory_value(conn: sqlite3.Connection) -> float:
    """Giá trị tồn kho vật lý = Σ (số lượng × giá vốn bình quân), loại dịch vụ.

    Khớp trang Tồn kho / Báo cáo tồn kho (không dùng số dư sổ cái 152–156).
    Số lượng ưu tiên tổng stock_moves; fallback cột inventory.quantity.
    """
    if not _table_exists(conn, 'inventory') or not _table_exists(conn, 'products'):
        return 0.0
    has_moves = _table_exists(conn, 'stock_moves')
    try:
        if has_moves:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(
                    COALESCE(
                        (SELECT SUM(sm.quantity) FROM stock_moves sm WHERE sm.product_id = i.product_id),
                        i.quantity,
                        0
                    ) * COALESCE(i.avg_cost, 0)
                ), 0)
                FROM inventory i
                JOIN products p ON p.id = i.product_id
                WHERE COALESCE(p.product_type, 'goods') != 'service'
                  AND UPPER(COALESCE(p.product_code, '')) NOT LIKE 'DV%'
                """
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(
                    COALESCE(i.quantity, 0) * COALESCE(i.avg_cost, 0)
                ), 0)
                FROM inventory i
                JOIN products p ON p.id = i.product_id
                WHERE COALESCE(p.product_type, 'goods') != 'service'
                  AND UPPER(COALESCE(p.product_code, '')) NOT LIKE 'DV%'
                """
            ).fetchone()
        return float(row[0] if row else 0)
    except sqlite3.Error:
        return 0.0


def warehouse_hub_metrics(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Hub kho: số dư HTK, PS nhập/xuất (giá vốn), số mặt hàng."""
    from datetime import datetime
    ensure_sme_journal_ready(conn, commit=False)
    period_to = period_to or datetime.now().month
    if period_to < 1 or period_to > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    bals = _closing_balances(conn, fiscal_year, period_to, branch_code=branch_code)
    activity = _ytd_activity(conn, fiscal_year, period_to, branch_code)

    raw = _sum_balance(bals, ('152',), normal='debit')
    tools_inv = _sum_balance(bals, ('153',), normal='debit')
    wip = _sum_balance(bals, ('154',), normal='debit')
    fg = _sum_balance(bals, ('155',), normal='debit')
    goods = _sum_balance(bals, ('156',), normal='debit')
    inventory = raw + tools_inv + wip + fg + goods
    purchase_in = _sum_activity(activity, ('152', '153', '155', '156'), side='debit')
    cogs = _sum_activity(activity, ('632',), side='debit')

    product_count = _safe_count(conn, "SELECT COUNT(*) FROM products") if _table_exists(conn, 'products') else 0
    sku_with_stock = 0
    stock_wac = Decimal('0.00')
    wac_by = {'152': Decimal('0'), '153': Decimal('0'), '155': Decimal('0'), '156': Decimal('0')}
    if _table_exists(conn, 'inventory'):
        sku_with_stock = _safe_count(conn, "SELECT COUNT(*) FROM inventory WHERE COALESCE(quantity,0) > 0")
        try:
            from Services.sme.inventory_ops import inventory_account_for_product
            rows = conn.execute(
                """
                SELECT i.product_id, COALESCE(i.quantity, 0) AS qty, COALESCE(i.avg_cost, 0) AS cost
                FROM inventory i
                WHERE COALESCE(i.quantity, 0) <> 0
                """
            ).fetchall()
            for r in rows:
                d = dict(r)
                val = _money(d.get('qty')) * _money(d.get('cost'))
                stock_wac += val
                acc = inventory_account_for_product(conn, int(d['product_id']))
                if acc in wac_by:
                    wac_by[acc] += val
        except Exception:
            pass
    elif _table_exists(conn, 'products'):
        try:
            sku_with_stock = _safe_count(conn, "SELECT COUNT(*) FROM products WHERE COALESCE(quantity,0) > 0")
        except Exception:
            sku_with_stock = 0

    monthly = []
    for m in range(1, period_to + 1):
        act = _month_activity(conn, fiscal_year, m, branch_code)
        monthly.append({
            'period': m,
            'label': f'T{m:02d}',
            'purchase_in': _f(_sum_activity(act, ('152', '153', '155', '156'), side='debit')),
            'cogs': _f(_sum_activity(act, ('632',), side='debit')),
        })

    return {
        'fiscal_year': fiscal_year,
        'period_to': period_to,
        'inventory_total': _f(max(Decimal('0'), inventory)),
        'inventory_raw': _f(max(Decimal('0'), raw)),
        'inventory_goods': _f(max(Decimal('0'), goods)),
        'inventory_fg': _f(max(Decimal('0'), fg)),
        'inventory_wip': _f(max(Decimal('0'), wip)),
        'purchase_in_ytd': _f(max(Decimal('0'), purchase_in)),
        'cogs_ytd': _f(max(Decimal('0'), cogs)),
        'product_count': product_count,
        'sku_with_stock': sku_with_stock,
        'stock_wac': _f(stock_wac),
        'gl_stock_tradable': _f(max(Decimal('0'), raw + fg + goods)),
        'wac_vs_gl_diff': _f(stock_wac - max(Decimal('0'), raw + fg + goods)),
        'wac_by_account': {k: _f(v) for k, v in wac_by.items()},
        'monthly': monthly,
    }


def fixed_asset_hub_metrics(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Hub TSCĐ: nguyên giá 211, KH lũy kế 214, KH kỳ."""
    from datetime import datetime
    from Services.fixed_assets_helpers import FIXED_ASSETS_TABLE, STATUS_ACTIVE, ensure_fixed_assets_schema

    ensure_sme_journal_ready(conn, commit=False)
    try:
        ensure_fixed_assets_schema(conn)
    except Exception:
        pass
    period_to = period_to or datetime.now().month
    if period_to < 1 or period_to > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    bals = _closing_balances(conn, fiscal_year, period_to, branch_code=branch_code)
    activity = _ytd_activity(conn, fiscal_year, period_to, branch_code)

    cost = _sum_balance(bals, ('211',), normal='debit')
    accum = _sum_balance(bals, ('214',), normal='credit')
    net = cost - accum
    dep_ytd = _sum_activity(activity, ('214',), side='credit')

    active_n = 0
    instock_n = 0
    book_cost = 0.0
    if _table_exists(conn, FIXED_ASSETS_TABLE):
        bf, bp = _branch_asset_sql(branch_code)
        active_n = _safe_count(
            conn,
            f"SELECT COUNT(*) FROM {FIXED_ASSETS_TABLE} WHERE tinh_trang = ?" + bf,
            (STATUS_ACTIVE, *bp),
        )
        instock_n = _safe_count(
            conn,
            f"SELECT COUNT(*) FROM {FIXED_ASSETS_TABLE} WHERE tinh_trang = 'InStock'" + bf,
            tuple(bp),
        )
        book_cost = _safe_sum(
            conn,
            f"SELECT COALESCE(SUM(nguyen_gia_tinh_khau_hao),0) FROM {FIXED_ASSETS_TABLE} WHERE tinh_trang = ?" + bf,
            (STATUS_ACTIVE, *bp),
        )

    monthly = []
    for m in range(1, period_to + 1):
        act = _month_activity(conn, fiscal_year, m, branch_code)
        monthly.append({
            'period': m,
            'label': f'T{m:02d}',
            'depreciation': _f(_sum_activity(act, ('214',), side='credit')),
            'additions': _f(_sum_activity(act, ('211',), side='debit')),
        })

    return {
        'fiscal_year': fiscal_year,
        'period_to': period_to,
        'gross_cost': _f(max(Decimal('0'), cost)),
        'accum_dep': _f(max(Decimal('0'), accum)),
        'net_book': _f(net),
        'dep_ytd': _f(max(Decimal('0'), dep_ytd)),
        'active_count': active_n,
        'instock_count': instock_n,
        'register_cost': book_cost,
        'monthly': monthly,
    }


def tools_hub_metrics(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Hub CCDC: TK 153 + phân bổ (PS Có 153 / Nợ CP)."""
    from datetime import datetime
    from Services.fixed_assets_helpers import STATUS_ACTIVE, TOOLS_TABLE, ensure_fixed_assets_schema

    ensure_sme_journal_ready(conn, commit=False)
    try:
        ensure_fixed_assets_schema(conn)
    except Exception:
        pass
    period_to = period_to or datetime.now().month
    if period_to < 1 or period_to > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    bals = _closing_balances(conn, fiscal_year, period_to, branch_code=branch_code)
    activity = _ytd_activity(conn, fiscal_year, period_to, branch_code)

    balance = _sum_balance(bals, ('153',), normal='debit')
    additions = _sum_activity(activity, ('153',), side='debit')
    allocated = _sum_activity(activity, ('153',), side='credit')

    active_n = 0
    instock_n = 0
    register_cost = 0.0
    if _table_exists(conn, TOOLS_TABLE):
        bf, bp = _branch_asset_sql(branch_code)
        active_n = _safe_count(
            conn,
            f"SELECT COUNT(*) FROM {TOOLS_TABLE} WHERE tinh_trang = ?" + bf,
            (STATUS_ACTIVE, *bp),
        )
        instock_n = _safe_count(
            conn,
            f"SELECT COUNT(*) FROM {TOOLS_TABLE} WHERE tinh_trang = 'InStock'" + bf,
            tuple(bp),
        )
        register_cost = _safe_sum(
            conn,
            f"SELECT COALESCE(SUM(nguyen_gia),0) FROM {TOOLS_TABLE} WHERE tinh_trang = ?" + bf,
            (STATUS_ACTIVE, *bp),
        )

    monthly = []
    for m in range(1, period_to + 1):
        act = _month_activity(conn, fiscal_year, m, branch_code)
        monthly.append({
            'period': m,
            'label': f'T{m:02d}',
            'additions': _f(_sum_activity(act, ('153',), side='debit')),
            'allocated': _f(_sum_activity(act, ('153',), side='credit')),
        })

    return {
        'fiscal_year': fiscal_year,
        'period_to': period_to,
        'balance': _f(max(Decimal('0'), balance)),
        'additions_ytd': _f(max(Decimal('0'), additions)),
        'allocated_ytd': _f(max(Decimal('0'), allocated)),
        'active_count': active_n,
        'instock_count': instock_n,
        'register_cost': register_cost,
        'monthly': monthly,
    }


def hr_hub_metrics(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Hub NS-TL: phải trả lương 334, BHXH 3383, tạm ứng 141, CP nhân công."""
    from datetime import datetime
    ensure_sme_journal_ready(conn, commit=False)
    period_to = period_to or datetime.now().month
    if period_to < 1 or period_to > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    bals = _closing_balances(conn, fiscal_year, period_to, branch_code=branch_code)
    activity = _ytd_activity(conn, fiscal_year, period_to, branch_code)

    salary_payable = _sum_balance(bals, ('334',), normal='credit')
    social_ins = _sum_balance(bals, ('3383', '338'), normal='credit')
    advance = _sum_balance(bals, ('141',), normal='debit')
    # CP nhân công / lương thường vào 6271, 6411, 6421 — lấy PS Nợ các TK bắt đầu 62x/64x có 'lương' khó;
    # dùng tăng Có 334 làm proxy chi phí lương ghi nhận
    salary_accrued = _sum_activity(activity, ('334',), side='credit')
    salary_paid = _sum_activity(activity, ('334',), side='debit')

    emp_count = 0
    if _table_exists(conn, 'employees'):
        emp_count = _safe_count(conn, "SELECT COUNT(*) FROM employees")
        for sql in (
            "SELECT COUNT(*) FROM employees WHERE COALESCE(status, 1) = 1",
            "SELECT COUNT(*) FROM employees WHERE COALESCE(is_active, 1) = 1",
        ):
            n = _safe_count(conn, sql)
            if n:
                emp_count = n
                break

    monthly = []
    for m in range(1, period_to + 1):
        act = _month_activity(conn, fiscal_year, m, branch_code)
        monthly.append({
            'period': m,
            'label': f'T{m:02d}',
            'accrued': _f(_sum_activity(act, ('334',), side='credit')),
            'paid': _f(_sum_activity(act, ('334',), side='debit')),
        })

    return {
        'fiscal_year': fiscal_year,
        'period_to': period_to,
        'salary_payable': _f(max(Decimal('0'), salary_payable)),
        'social_insurance': _f(max(Decimal('0'), social_ins)),
        'employee_advance': _f(max(Decimal('0'), advance)),
        'salary_accrued_ytd': _f(max(Decimal('0'), salary_accrued)),
        'salary_paid_ytd': _f(max(Decimal('0'), salary_paid)),
        'employee_count': emp_count,
        'monthly': monthly,
    }


def sales_hub_metrics(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Hub bán hàng: DT 511, giá vốn 632, phải thu 131, tiền thu."""
    from datetime import datetime
    ensure_sme_journal_ready(conn, commit=False)
    period_to = period_to or datetime.now().month
    if period_to < 1 or period_to > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    activity = _ytd_activity(conn, fiscal_year, period_to, branch_code)
    bals = _closing_balances(conn, fiscal_year, period_to, branch_code=branch_code)

    revenue = _sum_activity(activity, ('511', '515', '711'), side='credit')
    cogs = _sum_activity(activity, ('632',), side='debit')
    receivable = _sum_balance(bals, ('131',), normal='debit')
    cash_in = _sum_activity(activity, ('111', '112'), side='debit')
    # thu tiền từ KH thường: Nợ 111/112 Có 131 — xấp xỉ PS Có 131
    collected = _sum_activity(activity, ('131',), side='credit')
    gross = revenue - cogs

    order_count = 0
    if _table_exists(conn, 'orders'):
        order_count = _safe_count(conn, "SELECT COUNT(*) FROM orders")

    monthly = []
    for m in range(1, period_to + 1):
        act = _month_activity(conn, fiscal_year, m, branch_code)
        rev = _sum_activity(act, ('511', '515', '711'), side='credit')
        cog = _sum_activity(act, ('632',), side='debit')
        monthly.append({
            'period': m,
            'label': f'T{m:02d}',
            'revenue': _f(rev),
            'cogs': _f(cog),
            'gross': _f(rev - cog),
        })

    return {
        'fiscal_year': fiscal_year,
        'period_to': period_to,
        'revenue': _f(max(Decimal('0'), revenue)),
        'cogs': _f(max(Decimal('0'), cogs)),
        'gross_profit': _f(gross),
        'receivable': _f(max(Decimal('0'), receivable)),
        'collected_ytd': _f(max(Decimal('0'), collected)),
        'cash_in_ytd': _f(max(Decimal('0'), cash_in)),
        'order_count': order_count,
        'gross_margin_pct': float(gross / revenue * 100) if revenue > 0 else None,
        'monthly': monthly,
    }


def books_hub_metrics(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Hub sổ sách: số bút toán, khóa sổ, cân đối phát sinh."""
    from datetime import datetime
    from Services.sme.period_lock import list_locked_periods

    ensure_sme_journal_ready(conn, commit=False)
    period_to = period_to or datetime.now().month
    if period_to < 1 or period_to > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    activity = _period_activity(conn, fiscal_year, 1, period_to, branch_code=branch_code)
    period_debit = sum((_money(v.get('debit')) for v in activity.values()), Decimal('0.00'))
    period_credit = sum((_money(v.get('credit')) for v in activity.values()), Decimal('0.00'))

    entry_count = 0
    try:
        from Services.sme.branches import branch_sql_filter
        bf, bp = branch_sql_filter(branch_code, alias='sme_journal_entries')
        # branch_sql_filter uses alias.branch_code — table name as alias works in SQLite
        entry_count = _safe_count(
            conn,
            f"""
            SELECT COUNT(*) FROM sme_journal_entries
            WHERE status IN ('posted','reversed')
              AND fiscal_year = ? AND period <= ?
            {bf}
            """,
            (fiscal_year, period_to, *bp),
        )
    except Exception:
        entry_count = 0

    locks = []
    try:
        locks = list_locked_periods(conn, fiscal_year=fiscal_year) or []
    except Exception:
        locks = []
    locked_n = len([x for x in locks if int(x.get('period') or 0) <= period_to])

    account_touched = len(activity)
    balanced = abs(period_debit - period_credit) < Decimal('0.05')

    monthly = []
    for m in range(1, period_to + 1):
        act = _period_activity(conn, fiscal_year, m, m)
        d = sum((_money(v.get('debit')) for v in act.values()), Decimal('0.00'))
        c = sum((_money(v.get('credit')) for v in act.values()), Decimal('0.00'))
        monthly.append({
            'period': m,
            'label': f'T{m:02d}',
            'debit': _f(d),
            'credit': _f(c),
            'entries': _safe_count(
                conn,
                """
                SELECT COUNT(*) FROM sme_journal_entries
                WHERE status IN ('posted','reversed') AND fiscal_year=? AND period=?
                """,
                (fiscal_year, m),
            ) if _table_exists(conn, 'sme_journal_entries') else 0,
        })

    return {
        'fiscal_year': fiscal_year,
        'period_to': period_to,
        'entry_count': entry_count,
        'accounts_touched': account_touched,
        'period_debit': _f(period_debit),
        'period_credit': _f(period_credit),
        'balanced': balanced,
        'locked_periods': locked_n,
        'open_periods': max(0, period_to - locked_n),
        'monthly': monthly,
    }


def bctc_hub_metrics(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int | None = None,
) -> dict[str, Any]:
    """Hub BCTC: tóm tắt từ dashboard_metrics + tiền / VCSH."""
    base = dashboard_metrics(conn, fiscal_year=fiscal_year, period_to=period_to)
    period_to = base['period_to']
    bals = _closing_balances(conn, fiscal_year, period_to)
    assets = (
        _sum_balance(bals, ('111', '112'), normal='debit')
        + _sum_balance(bals, ('131',), normal='debit')
        + _sum_balance(bals, ('152', '153', '154', '155', '156'), normal='debit')
        + _sum_balance(bals, ('211',), normal='debit')
        - _sum_balance(bals, ('214',), normal='credit')
        + _sum_balance(bals, ('133',), normal='debit')
    )
    liabilities = (
        _sum_balance(bals, ('331',), normal='credit')
        + _sum_balance(bals, ('333',), normal='credit')
        + _sum_balance(bals, ('334',), normal='credit')
        + _sum_balance(bals, ('338',), normal='credit')
    )
    equity = (
        _sum_balance(bals, ('411',), normal='credit')
        + _sum_balance(bals, ('421',), normal='credit')
    )
    return {
        **base,
        'total_assets_approx': _f(assets),
        'total_liabilities_approx': _f(max(Decimal('0'), liabilities)),
        'equity_approx': _f(equity),
    }
