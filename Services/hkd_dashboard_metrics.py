"""Chỉ số tổng hợp cho dashboard POS & Kế Toán HKD."""
from datetime import date, datetime

from Services.profit_report_helpers import compute_profit_report

S3_EXPENSE_TYPES = (
    'CP_LUONG', 'CP_DIEN', 'CP_NUOC', 'CP_VT', 'CP_MB', 'CP_VPP', 'CP_KHAC',
)


def _columns(cursor, table):
    cursor.execute(f'PRAGMA table_info({table})')
    return {row[1] for row in cursor.fetchall()}


def _year_date_range(year, *, through_today=True):
    today = date.today()
    start = f'{int(year)}-01-01'
    if through_today and int(year) == today.year:
        end = today.isoformat()
    else:
        end = f'{int(year)}-12-31'
    return start, end


def _cash_balances(cursor):
    row = cursor.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN debit_account LIKE '111%' THEN amount ELSE 0 END), 0)
            - COALESCE(SUM(CASE WHEN credit_account LIKE '111%' THEN amount ELSE 0 END), 0) AS tm,
            COALESCE(SUM(CASE WHEN debit_account LIKE '112%' THEN amount ELSE 0 END), 0)
            - COALESCE(SUM(CASE WHEN credit_account LIKE '112%' THEN amount ELSE 0 END), 0) AS nh
        FROM (
            SELECT debit_account, credit_account, amount FROM phieu_thu
            UNION ALL
            SELECT debit_account, credit_account, amount FROM phieu_chi
        )
    """).fetchone()
    if not row:
        return 0.0, 0.0
    return float(row[0] or 0), float(row[1] or 0)


def _payable_total(cursor):
    imp_cols = _columns(cursor, 'import')
    if 'remaining_amount' in imp_cols:
        row = cursor.execute("""
            SELECT COALESCE(SUM(remaining_amount), 0)
            FROM import
            WHERE COALESCE(remaining_amount, 0) > 0
        """).fetchone()
    else:
        row = cursor.execute("""
            SELECT COALESCE(SUM(total_value - COALESCE(paid_amount, 0)), 0)
            FROM import
            WHERE (total_value - COALESCE(paid_amount, 0)) > 0
        """).fetchone()
    return float(row[0] if row else 0)


def _receivable_total(cursor):
    if not _columns(cursor, 'cong_no'):
        return 0.0
    cn_cols = _columns(cursor, 'cong_no')
    if 'remaining_amount' in cn_cols:
        row = cursor.execute("""
            SELECT COALESCE(SUM(remaining_amount), 0)
            FROM cong_no
            WHERE COALESCE(remaining_amount, 0) > 0
        """).fetchone()
    else:
        row = cursor.execute("""
            SELECT COALESCE(SUM(unpaid_amount - COALESCE(paid_amount, 0)), 0)
            FROM cong_no
            WHERE (unpaid_amount - COALESCE(paid_amount, 0)) > 0
        """).fetchone()
    return float(row[0] if row else 0)


def fetch_hkd_dashboard_metrics(cursor, year=None):
    """
    Doanh thu & chi phí: cùng logic trang Báo cáo Lợi nhuận (/api/reports/profit).
    Chi phí SXKD trên dashboard = GVHB + chi phí vận hành (TỔNG CHI PHÍ trên trang LN).
    """
    if year is None:
        year = date.today().year
    year = int(year)
    start, end = _year_date_range(year)

    profit = compute_profit_report(cursor, start, end)
    tm, nh = _cash_balances(cursor)

    return {
        'year': year,
        'period_start': start,
        'period_end': end,
        'doanh_thu_luy_ke': float(profit['revenue']),
        'chi_phi_sxkd_luy_ke': float(profit['total_chi_phi']),
        'gross_profit': float(profit['gross_profit']),
        'net_profit': float(profit['net_profit']),
        'cogs': float(profit['cogs']),
        'operating_expenses_total': float(profit['operating_expenses']['total']),
        'so_du_tien_mat': round(tm, 0),
        'so_du_ngan_hang': round(nh, 0),
        'cong_no_phai_tra': round(_payable_total(cursor), 0),
        'cong_no_phai_thu': round(_receivable_total(cursor), 0),
        'computed_at': datetime.now().isoformat(timespec='seconds'),
        'source': 'profit_report',
    }
