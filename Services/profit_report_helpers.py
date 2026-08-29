"""Logic P&L / S2c thống nhất — doanh thu, giá vốn, khấu hao, chi phí dồn tích, thuế.

Nguyên tắc:
- Doanh thu = sale.status = completed (không lọc invoice_status)
- COGS chỉ xuất bán (SALE / SALE_RECIPE) + hoàn (RETURN_SALE); không gồm xuất SX
- Khấu hao: mức tháng = NG/số_tháng; tháng đầu/cuối tỷ lệ ngày trong tháng
- Chi phí theo kỳ phát sinh (không lùi tháng/quý)
- Lương = thực lãnh + BH chủ hộ phải đóng; không TNCN NLĐ, không BH trừ lương NLĐ
- % thuế từ tax_rate_schedules (không hardcode)
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Any

_COGS_TYPES_SQL = "'SALE', 'SALE_RECIPE', 'RETURN_SALE', 'DELETE_SALE'"


def _columns(cursor, table):
    try:
        cursor.execute(f'PRAGMA table_info({table})')
        return {row[1] for row in cursor.fetchall()}
    except Exception:
        try:
            cursor.connection.rollback()
        except Exception:
            pass
        return set()


def _table_exists(cursor, table_name):
    try:
        row = cursor.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = ?
            LIMIT 1
            """,
            (table_name,),
        ).fetchone()
        return row is not None
    except Exception:
        try:
            cursor.connection.rollback()
        except Exception:
            pass
        return False


def _resolve_salary_detail_table(cursor):
    for name in ('salary_detail', 'Salary_Detail'):
        if _table_exists(cursor, name):
            return name
    return None


def _scalar(cursor, sql, params=(), default=0.0):
    try:
        row = cursor.execute(sql, params).fetchone()
        if not row:
            return default
        val = row[0] if not hasattr(row, 'keys') else row[0]
        return float(val or 0)
    except Exception:
        return default


def _normalize_search_bounds(start_search, end_search):
    """Chuẩn hóa mọi định dạng ngày về 'YYYY-MM-DD HH:MM:SS'."""
    def _to_text(val, default_time):
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.strftime('%Y-%m-%d %H:%M:%S')
        if isinstance(val, date):
            return f"{val.isoformat()} {default_time}"
        text = str(val).strip()
        if not text:
            return None
        if len(text) == 10:
            return f"{text} {default_time}"
        if len(text) == 19:
            return text
        if ' ' in text:
            return text[:19]
        return f"{text[:10]} {default_time}"

    start = _to_text(start_search, '00:00:00')
    end = _to_text(end_search, '23:59:59')
    if not start or not end:
        raise ValueError('Thiếu khoảng thời gian tính giá vốn')
    return start, end


def _parse_date(val) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return datetime(val.year, val.month, val.day)
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    text = str(val).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text.split(' ')[0][:10], '%Y-%m-%d')
    except ValueError:
        return None


def compute_cogs(cursor, start_search, end_search):
    """
    Giá vốn hàng bán trong kỳ — từ sổ cái stock_moves.
    Chỉ giao dịch bán / trả hàng bán. Xuất NVL sản xuất không vào đây.
    """
    if not _table_exists(cursor, 'stock_moves'):
        return 0.0
    start, end = _normalize_search_bounds(start_search, end_search)
    sql = f"""
        SELECT COALESCE(SUM(
            CASE
                WHEN type IN ({_COGS_TYPES_SQL})
                    THEN -quantity * COALESCE(cost_price, 0)
                ELSE 0
            END
        ), 0)
        FROM stock_moves
        WHERE date >= ? AND date <= ?
    """
    cursor.execute(sql, (start, end))
    row = cursor.fetchone()
    if not row:
        return 0.0
    return float(row[0] or 0)


def get_days_in_quarter(year, month):
    """Xác định quý, số ngày thực tế của quý và ngày bắt đầu/kết thúc quý đó."""
    quarter = (month - 1) // 3 + 1
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2

    q_start = datetime(year, start_month, 1)
    last_day_of_q = calendar.monthrange(year, end_month)[1]
    q_end = datetime(year, end_month, last_day_of_q)

    total_days = (q_end - q_start).days + 1
    return total_days, q_start, q_end


def asset_depreciation_end(asset_start: datetime, so_thang: int) -> datetime:
    """Ngày kết thúc KH: bắt đầu + so_thang tháng − 1 ngày (vd. 15/1/25 → 14/1/28 khi 36 tháng)."""
    y, m = asset_start.year, asset_start.month
    m += so_thang
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    # cùng ngày trong tháng kết thúc rồi lùi 1 ngày
    day = min(asset_start.day, calendar.monthrange(y, m)[1])
    end_same = datetime(y, m, day)
    return end_same - timedelta(days=1)


def depreciation_for_month(
    nguyen_gia: float,
    so_thang: int,
    asset_start: datetime,
    year: int,
    month: int,
) -> float:
    """
    Khấu hao một tháng lịch.
    Tháng tròn = NG/so_thang; tháng đầu/cuối = mức × ngày_dùng / ngày_tháng.
    Tháng cuối có thể lấy phần còn lại để khớp tổng NG (tránh lệch làm tròn).
    """
    if nguyen_gia <= 0 or so_thang <= 0:
        return 0.0
    monthly = nguyen_gia / so_thang
    asset_end = asset_depreciation_end(asset_start, so_thang)
    month_start = datetime(year, month, 1)
    month_last_day = calendar.monthrange(year, month)[1]
    month_end = datetime(year, month, month_last_day)

    if month_end < asset_start or month_start > asset_end:
        return 0.0

    use_start = max(month_start, asset_start)
    use_end = min(month_end, asset_end)
    if use_start > use_end:
        return 0.0

    days_used = (use_end - use_start).days + 1
    days_in_month = month_last_day
    is_first = (year, month) == (asset_start.year, asset_start.month)
    is_last = (year, month) == (asset_end.year, asset_end.month)

    if is_first or is_last or days_used < days_in_month:
        amount = round(monthly * days_used / days_in_month)
    else:
        amount = round(monthly)

    # Tháng cuối: khớp phần còn lại
    if is_last:
        # Tổng đã trích trước tháng cuối
        prior = 0.0
        cy, cm = asset_start.year, asset_start.month
        while (cy, cm) < (year, month):
            prior += depreciation_for_month(nguyen_gia, so_thang, asset_start, cy, cm)
            if cm == 12:
                cy, cm = cy + 1, 1
            else:
                cm += 1
        amount = round(nguyen_gia - prior)
    return max(0.0, amount)


def compute_depreciation(cursor, start_dt: datetime, end_dt: datetime) -> float:
    """Tổng KH TSCĐ Active giao với kỳ [start_dt, end_dt]."""
    if not _table_exists(cursor, 'fixed_assets'):
        return 0.0
    fa_cols = _columns(cursor, 'fixed_assets')
    needed = {'nguyen_gia_tinh_khau_hao', 'so_thang_khau_hao', 'ngay_bat_dau_su_dung'}
    if not needed <= fa_cols:
        return 0.0

    assets = cursor.execute("""
        SELECT nguyen_gia_tinh_khau_hao, so_thang_khau_hao, ngay_bat_dau_su_dung
        FROM fixed_assets WHERE tinh_trang = 'Active'
    """).fetchall()

    total = 0.0
    months = []
    curr = datetime(start_dt.year, start_dt.month, 1)
    last = datetime(end_dt.year, end_dt.month, 1)
    while curr <= last:
        months.append((curr.year, curr.month))
        if curr.month == 12:
            curr = datetime(curr.year + 1, 1, 1)
        else:
            curr = datetime(curr.year, curr.month + 1, 1)

    for asset in assets:
        try:
            ng = float(asset['nguyen_gia_tinh_khau_hao'] or 0)
            st = int(asset['so_thang_khau_hao'] or 0)
            asset_start = _parse_date(asset['ngay_bat_dau_su_dung'])
            if not asset_start or ng <= 0 or st <= 0:
                continue
            for y, m in months:
                dep_full = depreciation_for_month(ng, st, asset_start, y, m)
                if dep_full <= 0:
                    continue
                month_start = datetime(y, m, 1)
                month_end = datetime(y, m, calendar.monthrange(y, m)[1])
                clip_start = max(month_start, start_dt)
                clip_end = min(month_end, end_dt)
                if clip_start > clip_end:
                    continue
                days_month = (month_end - month_start).days + 1
                days_clip = (clip_end - clip_start).days + 1
                if days_clip >= days_month:
                    total += dep_full
                else:
                    # Kỳ báo cáo cắt giữa tháng — tỷ lệ ngày trên mức tháng đã tính
                    monthly = ng / st
                    total += round(monthly * days_clip / days_month)
        except Exception:
            continue
    return float(total)


def _months_in_range(start_dt: datetime, end_dt: datetime) -> list[tuple[int, int]]:
    out = []
    curr = datetime(start_dt.year, start_dt.month, 1)
    last = datetime(end_dt.year, end_dt.month, 1)
    while curr <= last:
        out.append((curr.month, curr.year))
        if curr.month == 12:
            curr = datetime(curr.year + 1, 1, 1)
        else:
            curr = datetime(curr.year, curr.month + 1, 1)
    return out


def compute_labor_cost(cursor, start_dt: datetime, end_dt: datetime) -> dict[str, float]:
    """
    Chi phí nhân công kỳ:
    - lương thực lãnh (final_amount) — đã trừ BH/TNCN NLĐ
    - BHXH/BHYT/BHTN = phần NLĐ (trích từ lương) + phần NSDLĐ/chủ hộ
    - TNCN của nhân viên (nsdlđ khấu trừ/chi trả) — tính vào chi phí lãi/lỗ
    """
    net_pay = 0.0
    employee_ins = 0.0
    employer_ins = 0.0
    employee_tncn = 0.0
    salary_table = _resolve_salary_detail_table(cursor)
    months = _months_in_range(start_dt, end_dt)
    if salary_table and months:
        conditions = ' OR '.join(['(month = ? AND year = ?)' for _ in months])
        params = [val for pair in months for val in pair]
        cols = _columns(cursor, salary_table)
        if 'final_amount' in cols:
            net_pay = _scalar(
                cursor,
                f'SELECT COALESCE(SUM(final_amount), 0) FROM {salary_table} WHERE {conditions}',
                params,
            )
        elif 'salary_rate' in cols:
            net_pay = _scalar(
                cursor,
                f'SELECT COALESCE(SUM(salary_rate), 0) FROM {salary_table} WHERE {conditions}',
                params,
            )
        bh_cols = [c for c in ('bhxh', 'bhyt', 'bhtn') if c in cols]
        if bh_cols:
            expr = ' + '.join(f'COALESCE({c}, 0)' for c in bh_cols)
            employee_ins = _scalar(
                cursor,
                f'SELECT COALESCE(SUM({expr}), 0) FROM {salary_table} WHERE {conditions}',
                params,
            )
        if 'tncn_tax' in cols:
            employee_tncn = _scalar(
                cursor,
                f'SELECT COALESCE(SUM(tncn_tax), 0) FROM {salary_table} WHERE {conditions}',
                params,
            )

    try:
        from Services.insurance_debt_helpers import compute_period_insurance_chu
        conn = cursor.connection if hasattr(cursor, 'connection') else None
        if conn is not None:
            for month, year in months:
                data = compute_period_insurance_chu(conn, month, year)
                if data and data.get('summary'):
                    employer_ins += float(data['summary'].get('total_phai_nop') or 0)
    except Exception:
        pass

    insurance_total = float(employee_ins + employer_ins)
    return {
        'net_pay': float(net_pay),
        'employee_insurance': float(employee_ins),
        'employer_insurance': float(employer_ins),
        'insurance_total': insurance_total,
        'employee_tncn': float(employee_tncn),
        'total': float(net_pay + insurance_total + employee_tncn),
    }


def compute_unissued_invoice_warning(cursor, start_search: str, end_search: str) -> dict:
    """Đơn completed nhưng chưa xuất HĐĐT trong kỳ."""
    if not _table_exists(cursor, 'sale'):
        return {'count': 0, 'amount': 0.0, 'sale_ids': []}
    cols = _columns(cursor, 'sale')
    if 'invoice_status' not in cols:
        return {'count': 0, 'amount': 0.0, 'sale_ids': []}
    rows = cursor.execute(
        """
        SELECT id, COALESCE(total_amount, 0) AS total_amount
        FROM sale
        WHERE status = 'completed'
          AND date >= ? AND date <= ?
          AND (
            COALESCE(invoice_status, 'none') IN ('none', 'draft', '')
            OR invoice_number IS NULL
            OR TRIM(COALESCE(invoice_number, '')) = ''
          )
        ORDER BY id DESC
        LIMIT 200
        """,
        (start_search, end_search),
    ).fetchall()
    ids = []
    amount = 0.0
    for r in rows:
        ids.append(int(r['id'] if hasattr(r, 'keys') else r[0]))
        amount += float(r['total_amount'] if hasattr(r, 'keys') else r[1] or 0)
    return {'count': len(ids), 'amount': amount, 'sale_ids': ids}


def _resolve_revenue_tier(tenant_profile: dict | None = None) -> str:
    try:
        from Services.tenant_profile import infer_revenue_tier, normalize_revenue_tier
        if tenant_profile:
            if tenant_profile.get('revenue_tier'):
                return normalize_revenue_tier(tenant_profile['revenue_tier'])
            return infer_revenue_tier(tenant_profile)
    except Exception:
        pass
    return 'DT3'


def compute_owner_taxes(
    revenue: float,
    profit_before_tax: float,
    *,
    revenue_tier: str,
    as_of: str,
    sector_totals: dict | None = None,
) -> dict[str, Any]:
    """GTGT / TNCN chủ HKD theo schedule + tier."""
    from Services.tax_rate_helpers import get_tax_rate_pct

    tier = (revenue_tier or 'DT3').upper()
    result = {
        'revenue_tier': tier,
        'gtgt': 0.0,
        'tncn': 0.0,
        'gtgt_rate_pct': 0.0,
        'tncn_rate_pct': 0.0,
        'note_gtgt': '',
        'note_tncn': '',
    }

    if tier == 'DT1':
        result['note_gtgt'] = 'DT1 — Miễn'
        result['note_tncn'] = 'DT1 — Miễn'
        return result

    if tier == 'DT2':
        # Theo ngành — dùng schedule NN hoặc fallback calc_sector_taxes
        try:
            from Services.hkd_sector import calc_sector_taxes, nn_to_totals_key, normalize_nn_code
            totals = {'g1': 0.0, 'g2': 0.0, 'g3': 0.0, 'g4': 0.0}
            if sector_totals:
                for k, v in sector_totals.items():
                    key = str(k).lower()
                    if key.startswith('nn'):
                        key = nn_to_totals_key(normalize_nn_code(key))
                    if key in totals:
                        totals[key] += float(v or 0)
            else:
                totals['g1'] = float(revenue or 0)
            # Ưu tiên rate từ schedule nếu có đủ
            gtgt = 0.0
            tncn = 0.0
            for nn, gkey in (('NN1', 'g1'), ('NN2', 'g2'), ('NN3', 'g3'), ('NN4', 'g4')):
                base = totals.get(gkey) or 0.0
                if base <= 0:
                    continue
                rg = get_tax_rate_pct(
                    'hkd_nn_gtgt', revenue_tier='DT2', nn_code=nn, as_of=as_of
                )
                rt = get_tax_rate_pct(
                    'hkd_nn_tncn', revenue_tier='DT2', nn_code=nn, as_of=as_of
                )
                if rg is None or rt is None:
                    taxes = calc_sector_taxes(totals)
                    result['gtgt'] = float(taxes.get('total_gtgt') or 0)
                    result['tncn'] = float(taxes.get('total_tncn') or 0)
                    result['note_gtgt'] = 'GTGT theo NN1–NN4 (DT2)'
                    result['note_tncn'] = 'TNCN theo NN1–NN4 (DT2)'
                    return result
                gtgt += base * float(rg) / 100.0
                tncn += base * float(rt) / 100.0
            result['gtgt'] = round(gtgt)
            result['tncn'] = round(tncn)
            result['note_gtgt'] = 'GTGT theo NN (schedule)'
            result['note_tncn'] = 'TNCN theo NN (schedule)'
            return result
        except Exception:
            result['note_gtgt'] = 'DT2 — lỗi tính ngành'
            return result

    # DT3 / DT4
    gtgt_pct = get_tax_rate_pct(
        'hkd_gtgt_on_revenue', revenue_tier=tier, as_of=as_of,
        default=1.0,
    )
    tncn_pct = get_tax_rate_pct(
        'hkd_tncn_on_profit', revenue_tier=tier, as_of=as_of,
        default=17.0 if tier == 'DT3' else 20.0,
    )
    result['gtgt_rate_pct'] = float(gtgt_pct or 0)
    result['tncn_rate_pct'] = float(tncn_pct or 0)
    result['gtgt'] = round(float(revenue or 0) * result['gtgt_rate_pct'] / 100.0)
    result['tncn'] = round(max(0.0, float(profit_before_tax or 0)) * result['tncn_rate_pct'] / 100.0)
    result['note_gtgt'] = f'{result["gtgt_rate_pct"]:g}% Doanh thu'
    result['note_tncn'] = f'{result["tncn_rate_pct"]:g}% Lãi'
    return result


def compute_period_pnl(
    cursor,
    from_date_iso: str,
    to_date_iso: str,
    *,
    tenant_profile: dict | None = None,
    include_unissued_warning: bool = True,
) -> dict[str, Any]:
    """
    P&L kỳ thống nhất — dùng cho S2c, báo cáo lợi nhuận, dashboard.
    """
    start_dt = datetime.strptime(from_date_iso[:10], '%Y-%m-%d')
    end_dt = datetime.strptime(to_date_iso[:10], '%Y-%m-%d')
    start_search = f'{from_date_iso[:10]} 00:00:00'
    end_search = f'{to_date_iso[:10]} 23:59:59'
    as_of = to_date_iso[:10]
    revenue_tier = _resolve_revenue_tier(tenant_profile)

    revenue = _scalar(cursor, """
        SELECT COALESCE(SUM(total_amount), 0) FROM sale
        WHERE status = 'completed' AND date >= ? AND date <= ?
    """, (start_search, end_search))

    cogs = compute_cogs(cursor, start_search, end_search)

    labor = compute_labor_cost(cursor, start_dt, end_dt)
    depreciation = compute_depreciation(cursor, start_dt, end_dt)

    cost_loan = 0.0
    cost_services = 0.0
    cost_other = 0.0

    if _table_exists(cursor, 'phieu_chi'):
        cost_loan = _scalar(cursor, """
            SELECT COALESCE(SUM(amount), 0) FROM phieu_chi
            WHERE expense_type = 'CP_TRALAIVAY'
              AND date >= ? AND date <= ?
              AND (source_type IS NULL OR source_type NOT IN ('salary', 'return_sale'))
        """, (start_search, end_search))

        cost_services = _scalar(cursor, """
            SELECT COALESCE(SUM(amount), 0) FROM phieu_chi
            WHERE expense_type IN ('CP_DIEN', 'CP_NUOC', 'CP_VT', 'CP_MB', 'CP_VPP', 'CP_DV')
              AND date >= ? AND date <= ?
              AND (source_type IS NULL OR source_type NOT IN ('salary', 'return_sale'))
        """, (start_search, end_search))

        cost_other = _scalar(cursor, """
            SELECT COALESCE(SUM(amount), 0) FROM phieu_chi
            WHERE expense_type IN ('CP_KHAC')
              AND date >= ? AND date <= ?
              AND (source_type IS NULL OR source_type NOT IN ('salary', 'return_sale'))
        """, (start_search, end_search))

    costs = {
        'a': round(cogs),
        'b': round(labor['total']),
        'c': round(depreciation),
        'd': round(cost_services),
        'dh': round(cost_loan),
        'e': round(cost_other),
        'b_net_pay': round(labor['net_pay']),
        'b_employee_ins': round(labor['employee_insurance']),
        'b_employer_ins': round(labor['employer_insurance']),
        'b_insurance_total': round(labor['insurance_total']),
        'b_employee_tncn': round(labor['employee_tncn']),
    }

    total_before_owner_tax = (
        costs['a'] + costs['b'] + costs['c'] + costs['d'] + costs['dh'] + costs['e']
    )
    profit_before_tax = revenue - total_before_owner_tax

    taxes = compute_owner_taxes(
        revenue, profit_before_tax,
        revenue_tier=revenue_tier,
        as_of=as_of,
    )
    # GTGT + TNCN chủ HKD (và thuế trên lãi) tính vào tổng chi phí
    owner_tax_expense = float(taxes['gtgt'] or 0) + float(taxes['tncn'] or 0)
    total_expenses = total_before_owner_tax + owner_tax_expense
    net_profit = revenue - total_expenses

    unissued = {'count': 0, 'amount': 0.0, 'sale_ids': []}
    show_warning = include_unissued_warning and revenue_tier in ('DT2', 'DT3', 'DT4')
    if show_warning:
        unissued = compute_unissued_invoice_warning(cursor, start_search, end_search)

    return {
        'revenue': round(revenue),
        'cogs': costs['a'],
        'gross_profit': round(revenue - costs['a']),
        'costs': costs,
        'total_expenses_before_tax': round(total_before_owner_tax),
        'total_expenses': round(total_expenses),
        'profit_before_tax': round(profit_before_tax),
        'net_profit': round(net_profit),
        'tax_tncn': int(round(taxes['tncn'] or 0)),
        'tax_gtgt': int(round(taxes['gtgt'] or 0)),
        'taxes': taxes,
        'revenue_tier': revenue_tier,
        'unissued_invoice_warning': unissued if show_warning else None,
        'operating_expenses': {
            'labor': costs['b'],
            'depreciation': costs['c'],
            'tax': round(owner_tax_expense),
            'loan_interest': costs['dh'],
            'services_outsource': costs['d'],
            'other': costs['e'],
            'total': round(total_expenses - costs['a']),
        },
        'total_chi_phi': round(total_expenses),
    }


def compute_profit_report(cursor, from_date_iso, to_date_iso, tenant_profile=None):
    """Alias tương thích — GET /api/reports/profit."""
    pnl = compute_period_pnl(
        cursor, from_date_iso, to_date_iso,
        tenant_profile=tenant_profile,
        include_unissued_warning=True,
    )
    return {
        'revenue': pnl['revenue'],
        'cogs': pnl['cogs'],
        'gross_profit': pnl['gross_profit'],
        'operating_expenses': pnl['operating_expenses'],
        'total_chi_phi': pnl['total_chi_phi'],
        'net_profit': pnl['net_profit'],
        'tax_tncn': pnl['tax_tncn'],
        'tax_gtgt': pnl['tax_gtgt'],
        'taxes': pnl['taxes'],
        'revenue_tier': pnl['revenue_tier'],
        'costs': pnl['costs'],
        'unissued_invoice_warning': pnl.get('unissued_invoice_warning'),
        'total_expenses': pnl['total_expenses'],
        'profit_before_tax': pnl['profit_before_tax'],
    }


def compute_s2c_report(cursor, from_date_iso, to_date_iso, tenant_profile=None):
    """Payload cho /api/reports/s2c và trang in."""
    pnl = compute_period_pnl(
        cursor, from_date_iso, to_date_iso,
        tenant_profile=tenant_profile,
        include_unissued_warning=True,
    )
    return {
        'revenue': float(pnl['revenue']),
        'total_expenses': float(pnl['total_expenses_before_tax']),
        'total_expenses_with_tax': float(pnl['total_expenses']),
        'tax_tncn': int(pnl['tax_tncn']),
        'tax_gtgt': int(pnl['tax_gtgt']),
        'taxes': pnl['taxes'],
        'costs': {
            'a': float(pnl['costs']['a']),
            'b': float(pnl['costs']['b']),
            'c': float(pnl['costs']['c']),
            'd': float(pnl['costs']['d']),
            'dh': float(pnl['costs']['dh']),
            'e': float(pnl['costs']['e']),
        },
        'diff': float(pnl['profit_before_tax']),
        'revenue_tier': pnl['revenue_tier'],
        'unissued_invoice_warning': pnl.get('unissued_invoice_warning'),
    }
