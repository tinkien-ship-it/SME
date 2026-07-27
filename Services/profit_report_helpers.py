"""Logic báo cáo lợi nhuận — dùng chung cho /api/reports/profit và dashboard HKD."""
import calendar
from datetime import date, datetime, timedelta

_COGS_TYPES_SQL = (
    "'SALE', 'SALE_RECIPE', 'export_material', 'export_for_use', "
    "'RETURN_SALE', 'DELETE_SALE'"
)


def _columns(cursor, table):
    cursor.execute(f'PRAGMA table_info({table})')
    return {row[1] for row in cursor.fetchall()}


def _table_exists(cursor, table_name):
    row = cursor.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = ? COLLATE NOCASE
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


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


def compute_cogs(cursor, start_search, end_search):
    """
    Giá vốn hàng bán trong kỳ — từ sổ cái stock_moves.
    Xuất (−qty) cộng COGS; hoàn hàng (+qty) trừ COGS.
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


def compute_profit_report(cursor, from_date_iso, to_date_iso):
    """
    Tính báo cáo lợi nhuận giống GET /api/reports/profit.
    from_date_iso, to_date_iso: 'YYYY-MM-DD'
    """
    start_dt = datetime.strptime(from_date_iso, '%Y-%m-%d')
    end_dt = datetime.strptime(to_date_iso, '%Y-%m-%d')

    start_search = f'{from_date_iso} 00:00:00'
    end_search = f'{to_date_iso} 23:59:59'

    # Lùi kỳ chi phí (giống trang Lợi nhuận)
    m_start_p = (start_dt + timedelta(days=20)).strftime('%Y-%m-%d') + ' 00:00:00'
    m_end_p = (end_dt + timedelta(days=45)).strftime('%Y-%m-%d') + ' 23:59:59'
    q_start_p = (start_dt + timedelta(days=60)).strftime('%Y-%m-%d') + ' 00:00:00'
    q_end_p = (end_dt + timedelta(days=120)).strftime('%Y-%m-%d') + ' 23:59:59'

    total_revenue = _scalar(cursor, """
        SELECT COALESCE(SUM(total_amount), 0) FROM sale
        WHERE status='completed' AND date BETWEEN ? AND ?
    """, (start_search, end_search))

    total_cogs = compute_cogs(cursor, start_search, end_search)

    months_years = []
    curr = datetime(start_dt.year, start_dt.month, 1)
    while curr <= datetime(end_dt.year, end_dt.month, 1):
        months_years.append((curr.month, curr.year))
        if curr.month == 12:
            curr = datetime(curr.year + 1, 1, 1)
        else:
            curr = datetime(curr.year, curr.month + 1, 1)

    cost_labor = 0.0
    salary_table = _resolve_salary_detail_table(cursor)
    if months_years and salary_table:
        conditions = ' OR '.join(['(month = ? AND year = ?)' for _ in months_years])
        params = [val for pair in months_years for val in pair]
        cost_labor = _scalar(
            cursor,
            f'SELECT COALESCE(SUM(salary_rate), 0) FROM {salary_table} WHERE {conditions}',
            params,
        )

    cost_tax = 0.0
    cost_loan_interest = 0.0
    cost_services_outsource = 0.0
    if _table_exists(cursor, 'phieu_chi'):
        cost_loan_interest = _scalar(cursor, """
            SELECT COALESCE(SUM(amount), 0) FROM phieu_chi
            WHERE expense_type = 'CP_TRALAIVAY'
            AND date BETWEEN ? AND ?
            AND (source_type IS NULL OR source_type != 'salary')
        """, (m_start_p, m_end_p))

        cost_tax = _scalar(cursor, """
            SELECT COALESCE(SUM(amount), 0) FROM phieu_chi
            WHERE expense_type = 'CP_THUE'
            AND date BETWEEN ? AND ?
            AND (source_type IS NULL OR source_type != 'salary')
        """, (q_start_p, q_end_p))

        cost_services_monthly = _scalar(cursor, """
            SELECT COALESCE(SUM(amount), 0) FROM phieu_chi
            WHERE expense_type IN ('CP_DIEN', 'CP_NUOC', 'CP_VT', 'CP_MB')
            AND date BETWEEN ? AND ?
            AND (source_type IS NULL OR source_type != 'salary')
        """, (m_start_p, m_end_p))

        cost_services_immediate = _scalar(cursor, """
            SELECT COALESCE(SUM(amount), 0) FROM phieu_chi
            WHERE expense_type IN ('CP_VPP', 'CP_KHAC', 'CP_DV')
            AND date BETWEEN ? AND ?
            AND (source_type IS NULL OR source_type != 'salary')
        """, (start_search, end_search))

        cost_services_outsource = cost_services_monthly + cost_services_immediate

    cost_depreciation = 0.0
    if _table_exists(cursor, 'fixed_assets'):
        fa_cols = _columns(cursor, 'fixed_assets')
        if {'nguyen_gia_tinh_khau_hao', 'so_thang_khau_hao', 'ngay_bat_dau_su_dung'} <= fa_cols:
            assets = cursor.execute("""
                SELECT nguyen_gia_tinh_khau_hao, so_thang_khau_hao, ngay_bat_dau_su_dung
                FROM fixed_assets WHERE tinh_trang = 'Active'
            """).fetchall()
            days_in_q, _, _ = get_days_in_quarter(start_dt.year, start_dt.month)

            for asset in assets:
                try:
                    ng_gia = float(asset['nguyen_gia_tinh_khau_hao'] or 0)
                    s_thang = int(asset['so_thang_khau_hao'] or 1)
                    if ng_gia <= 0:
                        continue
                    raw_date = asset['ngay_bat_dau_su_dung']
                    if isinstance(raw_date, str):
                        asset_start = datetime.strptime(raw_date.split(' ')[0], '%Y-%m-%d')
                    elif hasattr(raw_date, 'year'):
                        asset_start = datetime(raw_date.year, raw_date.month, raw_date.day)
                    else:
                        continue
                    asset_end = asset_start + timedelta(days=int(s_thang * 30.44))
                    overlap_start = max(start_dt, asset_start)
                    overlap_end = min(end_dt, asset_end)
                    if overlap_start <= overlap_end:
                        days_selected = (overlap_end - overlap_start).days + 1
                        dep_per_month = ng_gia / s_thang
                        dep_per_quarter = dep_per_month * 3
                        cost_depreciation += (dep_per_quarter / days_in_q) * days_selected
                except Exception:
                    continue

    total_op_exp = (
        cost_labor + cost_depreciation + cost_tax + cost_loan_interest + cost_services_outsource
    )
    gross_profit = total_revenue - total_cogs
    net_profit = gross_profit - total_op_exp
    total_chi_phi = total_cogs + total_op_exp

    return {
        'revenue': round(total_revenue),
        'cogs': round(total_cogs),
        'gross_profit': round(gross_profit),
        'operating_expenses': {
            'labor': round(cost_labor),
            'depreciation': round(cost_depreciation),
            'tax': round(cost_tax),
            'loan_interest': round(cost_loan_interest),
            'services_outsource': round(cost_services_outsource),
            'total': round(total_op_exp),
        },
        'total_chi_phi': round(total_chi_phi),
        'net_profit': round(net_profit),
    }
