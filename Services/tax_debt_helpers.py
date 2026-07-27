"""Theo dõi công nợ thuế tổng hợp — NSNN (S4 GTGT/TNCN) + Thuế khác (S3a)."""
import calendar
import sqlite3
from datetime import date

from Services.hkd_dashboard_metrics import _columns
from Services.nsnn_report_helpers import _tax_status, build_nsnn_report


def _as_cursor(db):
    """Chuẩn hóa connection/cursor — compute_profit_report cần cursor thật."""
    if isinstance(db, sqlite3.Connection):
        return db.cursor()
    return db


def _serialize_date(value):
    if value is None:
        return None
    if hasattr(value, 'isoformat'):
        return value.isoformat()[:10]
    return str(value)[:10]

QUARTERS = (
    ('Q1', 1, 3),
    ('Q2', 4, 6),
    ('Q3', 7, 9),
    ('Q4', 10, 12),
)


def _year_from_date_str(value):
    if not value:
        return None
    ds = str(value)[:10]
    try:
        return int(ds[:4])
    except ValueError:
        return None


def quarter_periods(year):
    year = int(year)
    periods = []
    for label, month_start, month_end in QUARTERS:
        last_day = calendar.monthrange(year, month_end)[1]
        start = f'{year}-{month_start:02d}-01'
        end = f'{year}-{month_end:02d}-{last_day:02d}'
        periods.append({
            'quarter': label,
            'label': f'{label}/{year}',
            'start': start,
            'end': end,
        })
    return periods


def _thue_khac_status(phai_nop, paid_amount):
    return _tax_status(phai_nop, paid_amount)


def get_thue_khac_debt_items(db, year=None, include_paid=True):
    cursor = _as_cursor(db)
    if not _columns(cursor, 'thue_khac'):
        return []

    cols = _columns(cursor, 'thue_khac')
    paid_expr = 'COALESCE(t.paid_amount, 0)' if 'paid_amount' in cols else '0'

    sql = f"""
        SELECT
            t.id,
            t.ngay_ghi_so,
            t.dien_giai,
            t.thue_phai_nop,
            {paid_expr} AS paid_amount,
            p.id AS phieu_chi_id,
            p.voucher_no
        FROM thue_khac t
        LEFT JOIN phieu_chi p
            ON t.id = p.source_id
           AND p.source_type = 'tax'
        ORDER BY t.ngay_ghi_so ASC, t.id ASC
    """
    rows = cursor.execute(sql).fetchall()
    items = []
    for row in rows:
        ngay = _serialize_date(row['ngay_ghi_so'])
        y = _year_from_date_str(ngay)
        if year is not None and y is not None and y != int(year):
            continue

        phai = float(row['thue_phai_nop'] or 0)
        da = float(row['paid_amount'] or 0)
        con = max(0.0, phai - da)
        if not include_paid and con <= 0.01:
            continue

        status, status_class = _thue_khac_status(phai, da)
        items.append({
            'id': row['id'],
            'ngay_ghi_so': ngay,
            'dien_giai': row['dien_giai'] or '',
            'phai_nop': round(phai),
            'da_nop': round(da),
            'con_lai': round(con),
            'status': status,
            'status_class': status_class,
            'voucher_no': row['voucher_no'],
            'phieu_chi_id': row['phieu_chi_id'],
            'source': 'S3A',
            'source_label': 'Thuế khác (S3a)',
        })
    return items


def get_nsnn_debt_periods(
    db,
    year=None,
    business_group=None,
    include_paid=True,
    *,
    revenue_tier=None,
    default_hkd_sector='G1',
):
    cursor = _as_cursor(db)
    if year is None:
        year = date.today().year
    year = int(year)

    periods = []
    ytd_before = 0.0
    for qp in quarter_periods(year):
        report = build_nsnn_report(
            cursor,
            qp['start'],
            qp['end'],
            business_group=business_group,
            revenue_tier=revenue_tier,
            default_hkd_sector=default_hkd_sector,
            ytd_before=ytd_before,
        )
        ytd_before += float(report.get('revenue') or 0)
        summary = report.get('summary') or {}
        con_lai = float(summary.get('total_con_lai') or 0)
        phai = float(summary.get('total_phai_nop') or 0)
        da = float(summary.get('total_da_nop') or 0)

        if not include_paid and con_lai <= 0.01:
            continue
        if not include_paid and phai <= 0:
            continue

        tax_rows = []
        for row in report.get('rows') or []:
            tax_rows.append({
                'tax_type': row['tax_type'],
                'dien_giai': row['dien_giai'],
                'note': row.get('note') or '',
                'phai_nop': row['phai_nop'],
                'da_nop': row['da_nop'],
                'con_lai': row['con_lai'],
                'status': row['status'],
                'status_class': row['status_class'],
                'voucher_no': row.get('voucher_no'),
                'phieu_chi_id': row.get('phieu_chi_id'),
            })

        if phai <= 0 and da <= 0 and not include_paid:
            continue

        tier = report.get('revenue_tier')
        periods.append({
            'source': 'NSNN',
            'source_label': 'NSNN (S4)',
            'quarter': qp['quarter'],
            'label': qp['label'],
            'start': qp['start'],
            'end': qp['end'],
            'business_group': report.get('business_group'),
            'revenue_tier': tier,
            'revenue': report.get('revenue') or 0,
            'total_expenses': report.get('total_expenses') or 0,
            'phai_nop': round(phai),
            'da_nop': round(da),
            'con_lai': round(con_lai),
            'tax_rows': tax_rows,
            'is_paid': con_lai <= 0.01,
        })
    return periods


def get_tax_debt_summary(
    db,
    year=None,
    business_group=None,
    include_paid=True,
    *,
    revenue_tier=None,
    default_hkd_sector='G1',
):
    if year is None:
        year = date.today().year
    year = int(year)

    nsnn_periods = get_nsnn_debt_periods(
        db,
        year=year,
        business_group=business_group,
        include_paid=include_paid,
        revenue_tier=revenue_tier,
        default_hkd_sector=default_hkd_sector,
    )
    s3a_items = get_thue_khac_debt_items(db, year=year, include_paid=include_paid)

    nsnn_unpaid = sum(p['con_lai'] for p in nsnn_periods)
    s3a_unpaid = sum(i['con_lai'] for i in s3a_items)
    nsnn_phai = sum(p['phai_nop'] for p in nsnn_periods)
    s3a_phai = sum(i['phai_nop'] for i in s3a_items)
    nsnn_da = sum(p['da_nop'] for p in nsnn_periods)
    s3a_da = sum(i['da_nop'] for i in s3a_items)

    unpaid_periods = sum(1 for p in nsnn_periods if p['con_lai'] > 0.01)
    unpaid_s3a = sum(1 for i in s3a_items if i['con_lai'] > 0.01)

    tier = revenue_tier
    if nsnn_periods:
        tier = nsnn_periods[0].get('revenue_tier') or tier

    return {
        'year': year,
        'business_group': nsnn_periods[0].get('business_group') if nsnn_periods else str(business_group or '3'),
        'revenue_tier': tier,
        'default_hkd_sector': default_hkd_sector,
        'nsnn_periods': nsnn_periods,
        's3a_items': s3a_items,
        'summary': {
            'nsnn_unpaid': round(nsnn_unpaid),
            's3a_unpaid': round(s3a_unpaid),
            'total_unpaid': round(nsnn_unpaid + s3a_unpaid),
            'nsnn_phai_nop': round(nsnn_phai),
            's3a_phai_nop': round(s3a_phai),
            'total_phai_nop': round(nsnn_phai + s3a_phai),
            'nsnn_da_nop': round(nsnn_da),
            's3a_da_nop': round(s3a_da),
            'total_da_nop': round(nsnn_da + s3a_da),
            'unpaid_nsnn_periods': unpaid_periods,
            'unpaid_s3a_items': unpaid_s3a,
            'unpaid_count': unpaid_periods + unpaid_s3a,
        },
    }


def get_tax_debt_total(
    cursor,
    year=None,
    business_group=None,
    *,
    revenue_tier=None,
    default_hkd_sector='G1',
):
    data = get_tax_debt_summary(
        cursor,
        year=year,
        business_group=business_group,
        include_paid=False,
        revenue_tier=revenue_tier,
        default_hkd_sector=default_hkd_sector,
    )
    return float(data['summary']['total_unpaid'])
