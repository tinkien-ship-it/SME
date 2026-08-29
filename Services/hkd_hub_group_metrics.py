"""Chỉ số hiển thị trên trang nhóm hub (Công nợ, Sổ sách, …)."""
from datetime import date

from Services.employee_debt_helpers import get_total_salary_debt
from Services.hkd_dashboard_metrics import (
    _cash_balances,
    _columns,
    _payable_total,
    _receivable_total,
    _year_date_range,
)
from Services.hkd_revenue import _calc_line_total
from Services.profit_report_helpers import compute_profit_report

CHART_COLORS = (
    '#0d6efd', '#dc3545', '#198754', '#ffc107', '#6f42c1',
    '#fd7e14', '#20c997', '#6610f2', '#d63384', '#0dcaf0',
)


def _fmt_currency(value):
    v = round(float(value or 0))
    return f'{v:,.0f}'.replace(',', '.') + ' ₫'


def _fmt_count(value, unit=''):
    n = int(value or 0)
    return f'{n:,}'.replace(',', '.') + (f' {unit}' if unit else '')


def _metric(value, kind='currency', unit='', detail=None, breakdown=None):
    v = float(value or 0)
    if kind == 'currency':
        display = _fmt_currency(v)
    elif kind == 'count':
        display = _fmt_count(v, unit)
    else:
        display = str(value) if value is not None else '—'
    out = {
        'value': v,
        'kind': kind,
        'display': display,
    }
    if detail:
        out['detail'] = detail
    if breakdown is not None:
        out['breakdown'] = breakdown
    return out


def _count_suppliers(cursor):
    if not _columns(cursor, 'suppliers'):
        return 0
    row = cursor.execute("SELECT COUNT(*) FROM suppliers").fetchone()
    return int(row[0] if row else 0)


def _count_customers(cursor):
    if not _columns(cursor, 'customers'):
        return 0
    row = cursor.execute("SELECT COUNT(*) FROM customers").fetchone()
    return int(row[0] if row else 0)


def _product_catalog_counts(cursor):
    """Đếm theo nhóm: sản phẩm, thành phẩm, VT/NVL, dịch vụ."""
    if not _columns(cursor, 'products'):
        return {'goods': 0, 'finished_goods': 0, 'materials': 0, 'service': 0}

    rows = cursor.execute(
        """
        SELECT COALESCE(LOWER(TRIM(product_type)), 'goods') AS pt, COUNT(*) AS cnt
        FROM products
        GROUP BY COALESCE(LOWER(TRIM(product_type)), 'goods')
        """
    ).fetchall()

    counts = {'goods': 0, 'finished_goods': 0, 'materials': 0, 'service': 0}
    goods_aliases = {'goods', 'subscription', 'ready_made', 'processed', ''}
    material_aliases = {'materials', 'raw_materials', 'fixed_asset', 'tools'}

    for row in rows:
        pt = (row['pt'] if hasattr(row, 'keys') else row[0]) or 'goods'
        cnt = int(row['cnt'] if hasattr(row, 'keys') else row[1] or 0)
        if pt == 'finished_goods':
            counts['finished_goods'] += cnt
        elif pt == 'service':
            counts['service'] += cnt
        elif pt in material_aliases:
            counts['materials'] += cnt
        elif pt in goods_aliases:
            counts['goods'] += cnt
        else:
            counts['goods'] += cnt

    return counts


def _metric_product_catalog(cursor):
    c = _product_catalog_counts(cursor)
    total = sum(c.values())
    detail = (
        f"Sản phẩm: {c['goods']} · "
        f"Thành phẩm: {c['finished_goods']} · "
        f"VT/NVL: {c['materials']} · "
        f"Dịch vụ: {c['service']}"
    )
    return _metric(total, 'count', 'mặt hàng', detail=detail, breakdown=c)


def _chart_product_type_key(pt):
    """Gom loại SP / TP / DV cho biểu đồ; bỏ VT/NVL."""
    pt = (pt or 'goods').strip().lower()
    if pt == 'finished_goods':
        return 'finished_goods'
    if pt == 'service':
        return 'service'
    material_aliases = {'materials', 'raw_materials', 'fixed_asset', 'tools'}
    if pt in material_aliases:
        return None
    return 'goods'


def _revenue_by_product_type(cursor, year):
    """Doanh thu lũy kế năm theo SP / TP / DV."""
    if not _columns(cursor, 'sale') or not _columns(cursor, 'sale_items'):
        return {'goods': 0.0, 'finished_goods': 0.0, 'service': 0.0}
    start, end = _year_date_range(year)
    si_cols = _columns(cursor, 'sale_items')
    line_total_sel = 'si.line_total' if 'line_total' in si_cols else 'NULL'
    rows = cursor.execute(
        f"""
        SELECT
            COALESCE(LOWER(TRIM(p.product_type)), 'goods') AS pt,
            si.quantity,
            si.price,
            COALESCE(si.discount_pct, 0) AS discount_pct,
            COALESCE(si.tax_pct, 0) AS tax_pct,
            {line_total_sel} AS line_total
        FROM sale s
        INNER JOIN sale_items si ON si.sale_id = s.id
        LEFT JOIN products p ON p.id = si.product_id
        WHERE s.status = 'completed'
          AND si.quantity > 0
          AND date(s.date) BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchall()
    totals = {'goods': 0.0, 'finished_goods': 0.0, 'service': 0.0}
    for row in rows:
        r = dict(row)
        key = _chart_product_type_key(r.get('pt'))
        if not key:
            continue
        totals[key] += _calc_line_total(
            r['quantity'], r['price'], r['discount_pct'], r['tax_pct'], r.get('line_total'),
        )
    return {k: round(v) for k, v in totals.items()}


def _employee_debt_total(cursor):
    from Services.employee_debt_helpers import get_total_salary_debt
    return get_total_salary_debt(cursor)


def _insurance_debt_total(cursor):
    from Services.insurance_debt_helpers import get_insurance_debt_list
    summary = get_insurance_debt_list(cursor, include_paid=False)['summary']
    return float(summary.get('total_unpaid') or 0)


def _metric_insurance_debt(cursor, year=None):
    from Services.insurance_debt_helpers import get_insurance_debt_list
    data = get_insurance_debt_list(cursor, include_paid=False)
    s = data['summary']
    detail = None
    if int(s.get('unpaid_periods') or 0) > 0:
        detail = (
            f"{s['unpaid_periods']} kỳ chưa nộp · "
            f"NLĐ {_fmt_currency(s.get('nld_unpaid', 0))} · "
            f"Chủ hộ {_fmt_currency(s.get('chu_unpaid', 0))}"
        )
    return _metric(s.get('total_unpaid', 0), detail=detail)


def _nsnn_balance(cursor, year):
    """Số dư NSNN (phải nộp − đã nộp) — rút gọn."""
    sales = cursor.execute(
        "SELECT date, total_amount FROM sale WHERE status = 'completed'"
    ).fetchall()
    total_sales = 0.0
    for row in sales:
        d = row['date']
        if not d:
            continue
        ds = str(d)[:10]
        try:
            y = int(ds[:4])
        except ValueError:
            continue
        if y <= int(year):
            total_sales += float(row['total_amount'] or 0)
    phai_nop = round(total_sales * 0.015, 0)
    da_nop = 0.0
    for row in cursor.execute(
        "SELECT amount, debit_account, reason FROM phieu_chi"
    ).fetchall():
        reason = (row['reason'] or '').lower()
        if row['debit_account'] == '333' or 'thuế' in reason or 'nộp thuế' in reason:
            da_nop += float(row['amount'] or 0)
    return max(phai_nop - da_nop, 0)


def _s3_expense_total(cursor, year):
    start, end = _year_date_range(year)
    row = cursor.execute(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM phieu_chi
        WHERE date BETWEEN ? AND ?
          AND (expense_type IS NOT NULL AND expense_type != '')
        """,
        (start, end + ' 23:59:59'),
    ).fetchone()
    return float(row[0] if row else 0)


def _salary_ytd(cursor, year):
    row = cursor.execute(
        """
        SELECT COALESCE(SUM(final_amount), 0)
        FROM salary_detail
        WHERE year = ?
        """,
        (int(year),),
    ).fetchone()
    return float(row[0] if row else 0)


def _salary_period_count(cursor, year):
    row = cursor.execute(
        """
        SELECT COUNT(DISTINCT month || '/' || year)
        FROM salary_detail
        WHERE year = ?
        """,
        (int(year),),
    ).fetchone()
    return int(row[0] if row else 0)


def _voucher_amount_count_ytd(cursor, table, year):
    """Tổng tiền + số phiếu trong năm (phieu_thu / phieu_chi)."""
    start, end = _year_date_range(year)
    row = cursor.execute(
        f"""
        SELECT COALESCE(SUM(amount), 0), COUNT(*)
        FROM {table}
        WHERE date(date) BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchone()
    return float(row[0] if row else 0), int(row[1] if row else 0)


def _stock_moves_value_count_ytd(cursor, year, move_types):
    start, end = _year_date_range(year)
    placeholders = ','.join('?' * len(move_types))
    row = cursor.execute(
        f"""
        SELECT COALESCE(SUM(ABS(quantity * cost_price)), 0),
               COUNT(DISTINCT COALESCE(ref_document, CAST(ref_id AS TEXT) || '-' || type))
        FROM stock_moves
        WHERE type IN ({placeholders})
          AND date(date) BETWEEN ? AND ?
        """,
        (*move_types, start, end),
    ).fetchone()
    return float(row[0] if row else 0), int(row[1] if row else 0)


def _metric_chung_tu_ytd(cursor, year, voucher_kind):
    """Giá trị phát sinh lũy kế trong năm cho từng loại chứng từ."""
    year = int(year)
    if voucher_kind == 'phieu_thu':
        amount, count = _voucher_amount_count_ytd(cursor, 'phieu_thu', year)
    elif voucher_kind == 'phieu_chi':
        amount, count = _voucher_amount_count_ytd(cursor, 'phieu_chi', year)
    elif voucher_kind == 'phieu_nhap':
        amount, count = _stock_moves_value_count_ytd(
            cursor, year, ('import', 'RETURN_SALE', 'DELETE_SALE')
        )
    elif voucher_kind == 'phieu_xuat':
        amount, count = _stock_moves_value_count_ytd(
            cursor, year, ('SALE', 'RETURN_IMPORT', 'DELETE_IMPORT', 'export')
        )
    elif voucher_kind == 'bang_luong':
        amount = _salary_ytd(cursor, year)
        count = _salary_period_count(cursor, year)
    else:
        return None

    unit_label = 'kỳ lương' if voucher_kind == 'bang_luong' else 'phiếu'
    detail = f"{count} {unit_label} · Phát sinh lũy kế năm {year}"
    return _metric(amount, 'currency', detail=detail)


def _metric_tax_debt(cursor, year=None, business_group=None, revenue_tier=None, default_hkd_sector='G1'):
    from Services.tax_debt_helpers import get_tax_debt_summary
    data = get_tax_debt_summary(
        cursor,
        year=year,
        business_group=business_group,
        revenue_tier=revenue_tier,
        default_hkd_sector=default_hkd_sector,
        include_paid=False,
    )
    s = data.get('summary') or {}
    detail = (
        f"NSNN {_fmt_currency(s.get('nsnn_unpaid', 0))} · "
        f"S3a {_fmt_currency(s.get('s3a_unpaid', 0))}"
    )
    return _metric(s.get('total_unpaid', 0), detail=detail)


def _tax_debt_total_for_chart(cursor, year=None):
    from Services.tax_debt_helpers import get_tax_debt_total
    return get_tax_debt_total(cursor, year=year, **_tenant_tax_kwargs())


def _thue_khac_unpaid(cursor):
    if not _columns(cursor, 'thue_khac'):
        return 0.0
    row = cursor.execute(
        """
        SELECT COALESCE(SUM(thue_phai_nop - COALESCE(paid_amount, 0)), 0)
        FROM thue_khac
        WHERE (thue_phai_nop - COALESCE(paid_amount, 0)) > 0
        """
    ).fetchone()
    return float(row[0] if row else 0)


def _tscd_value(cursor):
    if not _columns(cursor, 'fixed_assets'):
        return 0.0
    cols = _columns(cursor, 'fixed_assets')
    if 'nguyen_gia_tinh_khau_hao' in cols:
        row = cursor.execute(
            """
            SELECT COALESCE(SUM(nguyen_gia_tinh_khau_hao), 0)
            FROM fixed_assets
            WHERE tinh_trang = 'Active'
            """
        ).fetchone()
    else:
        row = cursor.execute(
            """
            SELECT COALESCE(SUM(nguyen_gia), 0)
            FROM fixed_assets
            WHERE tinh_trang = 'Active'
            """
        ).fetchone()
    return float(row[0] if row else 0)


def _ccdc_value(cursor):
    if not _columns(cursor, 'tools'):
        return 0.0
    row = cursor.execute(
        """
        SELECT COALESCE(SUM(nguyen_gia), 0)
        FROM tools
        WHERE tinh_trang = 'Active'
        """
    ).fetchone()
    return float(row[0] if row else 0)


def _loan_balance(cursor):
    if not _columns(cursor, 'loans'):
        return 0.0
    cols = _columns(cursor, 'loans')
    if 'amount_paid' in cols:
        row = cursor.execute(
            """
            SELECT COALESCE(SUM(loan_amount - COALESCE(amount_paid, 0)), 0)
            FROM loans
            WHERE (loan_amount - COALESCE(amount_paid, 0)) > 0
            """
        ).fetchone()
    else:
        row = cursor.execute("SELECT COALESCE(SUM(loan_amount), 0) FROM loans").fetchone()
    return float(row[0] if row else 0)


def _inventory_value(cursor):
    row = cursor.execute(
        """
        SELECT COALESCE(SUM(i.quantity * COALESCE(i.avg_cost, 0)), 0)
        FROM inventory i
        JOIN products p ON p.id = i.product_id
        WHERE COALESCE(p.product_type, 'goods') != 'service'
        """
    ).fetchone()
    return float(row[0] if row else 0)


def _employee_status_counts(cursor):
    """Đếm NV theo status: 1 = đang làm, 0 = đã nghỉ việc."""
    if not _columns(cursor, 'employees'):
        return {'active': 0, 'inactive': 0, 'total': 0}
    row = cursor.execute(
        """
        SELECT
            SUM(CASE WHEN CAST(COALESCE(status, '1') AS TEXT) IN ('0', '0.0') THEN 0 ELSE 1 END) AS active,
            SUM(CASE WHEN CAST(COALESCE(status, '1') AS TEXT) IN ('0', '0.0') THEN 1 ELSE 0 END) AS inactive,
            COUNT(*) AS total
        FROM employees
        """
    ).fetchone()
    return {
        'active': int(row[0] if row else 0),
        'inactive': int(row[1] if row else 0),
        'total': int(row[2] if row else 0),
    }


def _count_employees(cursor, active_only=True):
    counts = _employee_status_counts(cursor)
    if active_only:
        return counts['active']
    return counts['total']


def _metric_employees_page(cursor):
    counts = _employee_status_counts(cursor)
    return _metric(counts['active'], 'count', 'nhân viên đang làm việc')


def _metric_attendance_page(cursor, year):
    counts = _employee_status_counts(cursor)
    return _metric(counts['active'], 'count', 'nhân viên đang làm việc')


def _count_pending_invoices(cursor):
    row = cursor.execute(
        """
        SELECT COUNT(*) FROM sale
        WHERE status = 'completed'
          AND (invoice_number IS NULL OR invoice_number = '')
        """
    ).fetchone()
    return int(row[0] if row else 0)


def _count_draft_orders(cursor):
    row = cursor.execute(
        "SELECT COUNT(*) FROM sale WHERE status = 'draft'"
    ).fetchone()
    return int(row[0] if row else 0)


def _profit_metrics(cursor, year):
    start, end = _year_date_range(year)
    return compute_profit_report(cursor, start, end)


def _import_table_total(cursor, year):
    """Tổng total_value trong bảng import (lũy kế năm)."""
    if not _columns(cursor, 'import'):
        return 0.0, 0
    start, end = _year_date_range(year)
    row = cursor.execute(
        """
        SELECT COALESCE(SUM(total_value), 0), COUNT(*)
        FROM import
        WHERE date(date) BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchone()
    return float(row[0] if row else 0), int(row[1] if row else 0)


def _supplier_invoice_total(cursor, year):
    """Tổng giá trị hóa đơn mua (supplier_invoice.total)."""
    if not _columns(cursor, 'supplier_invoice'):
        return 0.0, 0
    cols = _columns(cursor, 'supplier_invoice')
    date_col = 'invoice_date' if 'invoice_date' in cols else 'date'
    start, end = _year_date_range(year)
    row = cursor.execute(
        f"""
        SELECT COALESCE(SUM(total), 0), COUNT(*)
        FROM supplier_invoice
        WHERE date({date_col}) BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchone()
    return float(row[0] if row else 0), int(row[1] if row else 0)


def _return_import_total(cursor, year):
    """Tổng giá trị trả hàng nhập."""
    if not _columns(cursor, 'return_import'):
        return 0.0, 0
    cols = _columns(cursor, 'return_import')
    if 'refund_amount' in cols:
        amount_expr = 'COALESCE(refund_amount, quantity * COALESCE(cost_price, 0))'
    else:
        amount_expr = 'quantity * COALESCE(cost_price, 0)'
    start, end = _year_date_range(year)
    row = cursor.execute(
        f"""
        SELECT COALESCE(SUM({amount_expr}), 0), COUNT(*)
        FROM return_import
        WHERE date(date) BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchone()
    return float(row[0] if row else 0), int(row[1] if row else 0)


def _metric_import_details_page(cursor, year):
    """Tổng mua hàng từ import_details — gồm dịch vụ không qua stock_moves."""
    from Services.import_line_helpers import sum_import_details_payment_period

    if not _columns(cursor, 'import_details') or not _columns(cursor, 'import'):
        return _metric(0, 'currency', detail=f'0 dòng · Phát sinh lũy kế năm {year}')
    start, end = _year_date_range(year)
    amount, count = sum_import_details_payment_period(cursor, start, end)
    detail = f'{count} dòng · Phát sinh lũy kế năm {year}'
    return _metric(amount, 'currency', detail=detail)


def _metric_import_list(cursor, year):
    amount, count = _import_table_total(cursor, year)
    detail = f'{count} phiếu · Phát sinh lũy kế năm {year}'
    return _metric(amount, 'currency', detail=detail)


def _metric_supplier_invoice(cursor, year):
    amount, count = _supplier_invoice_total(cursor, year)
    detail = f'{count} hóa đơn · Phát sinh lũy kế năm {year}'
    return _metric(amount, 'currency', detail=detail)


def _metric_return_import(cursor, year):
    amount, count = _return_import_total(cursor, year)
    detail = f'{count} phiếu trả · Phát sinh lũy kế năm {year}'
    return _metric(amount, 'currency', detail=detail)


def _build_currency_chart(title, items, min_slices=2):
    """Tạo biểu đồ doughnut từ danh sách {label, value[, display]}."""
    slices = []
    for item in items:
        v = float(item.get('value') or 0)
        display = item.get('display') or _fmt_currency(v)
        pie_val = abs(v)
        if pie_val > 0:
            slices.append({
                'label': item['label'],
                'value': pie_val,
                'display': display,
            })
    if len(slices) < min_slices:
        return None
    total = sum(s['value'] for s in slices)
    return {
        'title': title,
        'labels': [s['label'] for s in slices],
        'values': [s['value'] for s in slices],
        'displays': [s['display'] for s in slices],
        'colors': [CHART_COLORS[i % len(CHART_COLORS)] for i in range(len(slices))],
        'total': total,
        'total_display': _fmt_currency(total),
    }


def fetch_main_dashboard_charts(cursor, year=None):
    """Ba biểu đồ tóm tắt trang chủ — chỉ trả chart có ít nhất 1 tiêu chí phát sinh."""
    if year is None:
        year = date.today().year
    year = int(year)
    profit = _profit_metrics(cursor, year)
    tm, nh = _cash_balances(cursor)

    candidates = [
        _build_currency_chart('Cơ cấu Doanh Thu, Chi Phí, Lợi Nhuận', [
            {'label': 'Doanh thu', 'value': profit['revenue']},
            {'label': 'Chi phí', 'value': profit['total_chi_phi']},
            {'label': 'Lợi nhuận', 'value': profit['net_profit']},
        ], min_slices=1),
        _build_currency_chart('Cơ cấu Tài Sản', [
            {'label': 'Giá trị hàng tồn kho', 'value': _inventory_value(cursor)},
            {'label': 'Tiền mặt', 'value': tm},
            {'label': 'Tiền gửi ngân hàng', 'value': nh},
            {'label': 'TSCĐ', 'value': _tscd_value(cursor)},
            {'label': 'CCDC', 'value': _ccdc_value(cursor)},
        ], min_slices=1),
        _build_currency_chart('Công Nợ Phải Trả NCC & Phải Thu KH', [
            {'label': 'Phải trả NCC', 'value': _payable_total(cursor)},
            {'label': 'Phải thu khách hàng', 'value': _receivable_total(cursor)},
        ], min_slices=1),
    ]
    return [c for c in candidates if c]


def _fetch_bao_cao_charts(cursor, year):
    """Ba biểu đồ cơ cấu cho nhóm menu Báo Cáo."""
    profit = _profit_metrics(cursor, year)
    import_val, _ = _stock_moves_value_count_ytd(
        cursor, year, ('import', 'RETURN_SALE', 'DELETE_SALE')
    )
    inventory = _inventory_value(cursor)
    thu, _ = _voucher_amount_count_ytd(cursor, 'phieu_thu', year)
    chi, _ = _voucher_amount_count_ytd(cursor, 'phieu_chi', year)

    return [
        _build_currency_chart('Cơ cấu Doanh Thu, Chi Phí, Lợi Nhuận', [
            {'label': 'Doanh thu', 'value': profit['revenue']},
            {'label': 'Chi phí', 'value': profit['total_chi_phi']},
            {'label': 'Lợi nhuận', 'value': profit['net_profit']},
        ]),
        _build_currency_chart('Cơ cấu Hàng Hóa', [
            {'label': 'Giá trị hàng nhập', 'value': import_val},
            {'label': 'Giá vốn hàng bán', 'value': profit['cogs']},
            {'label': 'Giá trị tồn kho', 'value': inventory},
        ]),
        _build_currency_chart('Cơ cấu Thu và Chi', [
            {'label': 'Thu', 'value': thu},
            {'label': 'Chi', 'value': chi},
        ]),
    ]


def _fetch_so_sach_charts(cursor, year):
    """Hai biểu đồ cơ cấu cho nhóm menu Sổ Sách Kế Toán."""
    profit = _profit_metrics(cursor, year)
    tm, nh = _cash_balances(cursor)

    return [
        _build_currency_chart('Cơ cấu Doanh Thu, Chi Phí, Lợi Nhuận', [
            {'label': 'Doanh thu', 'value': profit['revenue']},
            {'label': 'Chi phí', 'value': profit['total_chi_phi']},
            {'label': 'Lợi nhuận', 'value': profit['net_profit']},
        ]),
        _build_currency_chart('Cơ cấu Tài Sản', [
            {'label': 'Giá trị hàng tồn kho', 'value': _inventory_value(cursor)},
            {'label': 'Tiền mặt', 'value': tm},
            {'label': 'Tiền gửi ngân hàng', 'value': nh},
            {'label': 'TSCĐ', 'value': _tscd_value(cursor)},
            {'label': 'CCDC', 'value': _ccdc_value(cursor)},
        ]),
    ]


def _fetch_ban_hang_charts(cursor, year):
    """Biểu đồ cơ cấu doanh thu SP / TP / DV cho menu Bán Hàng."""
    rev = _revenue_by_product_type(cursor, year)
    type_labels = (
        ('goods', 'Sản Phẩm (SP)'),
        ('finished_goods', 'Thành Phẩm (TP)'),
        ('service', 'Dịch Vụ (DV)'),
    )
    active_types = sum(1 for key, _ in type_labels if float(rev.get(key) or 0) > 0)
    if active_types < 2:
        return []
    chart = _build_currency_chart('Cơ cấu Doanh Thu', [
        {'label': label, 'value': rev[key]}
        for key, label in type_labels
        if float(rev.get(key) or 0) > 0
    ])
    return [chart] if chart else []


def _fetch_mua_hang_charts(cursor, year):
    """Biểu đồ cơ cấu mua hàng / trả NCC cho menu Mua Hàng."""
    import_val, _ = _import_table_total(cursor, year)
    return_val, _ = _return_import_total(cursor, year)
    chart = _build_currency_chart('Cơ cấu Mua Hàng', [
        {'label': 'Giá trị hàng mua', 'value': import_val},
        {'label': 'Giá trị hàng trả lại NCC', 'value': return_val},
    ])
    return [chart] if chart else []


def _fetch_chung_tu_charts(cursor, year):
    """Hai biểu đồ cơ cấu cho nhóm menu Chứng Từ Kế Toán."""
    thu, _ = _voucher_amount_count_ytd(cursor, 'phieu_thu', year)
    chi, _ = _voucher_amount_count_ytd(cursor, 'phieu_chi', year)

    return [
        _build_currency_chart('Cơ cấu Tổng Thu và Chi', [
            {'label': 'Tổng thu', 'value': thu},
            {'label': 'Tổng chi', 'value': chi},
        ]),
        _build_currency_chart('Cơ cấu Công Nợ', [
            {'label': 'Phải trả NCC', 'value': _payable_total(cursor)},
            {'label': 'Phải trả nhân viên', 'value': _employee_debt_total(cursor)},
            {'label': 'Phải nộp BH', 'value': _insurance_debt_total(cursor)},
            {'label': 'Phải nộp thuế', 'value': _tax_debt_total_for_chart(cursor)},
            {'label': 'Phải thu khách hàng', 'value': _receivable_total(cursor)},
        ]),
    ]


def _tenant_tax_kwargs():
    try:
        from flask import g
        profile = getattr(g, 'tenant_profile', None) or {}
        return {
            'revenue_tier': profile.get('revenue_tier'),
            'default_hkd_sector': profile.get('default_hkd_sector', 'G1'),
        }
    except Exception:
        return {}


def fetch_endpoint_metrics(cursor, endpoint, year=None):
    """Trả metric cho một endpoint menu; None nếu không hỗ trợ."""
    if year is None:
        year = date.today().year
    year = int(year)
    tm, nh = _cash_balances(cursor)
    profit = _profit_metrics(cursor, year)
    tax_kw = _tenant_tax_kwargs()

    mapping = {
        'SoCongNoPhaiThu': lambda: _metric(_receivable_total(cursor)),
        'SoCongNoPhaiTra': lambda: _metric(_payable_total(cursor)),
        'SoCongNoPhaiTraNhanVien': lambda: _metric(_employee_debt_total(cursor)),
        'SoCongNoBaoHiem': lambda: _metric_insurance_debt(cursor),
        'SoCongNoThueNSNN': lambda: _metric_tax_debt(cursor, year, **tax_kw),
        'SoQuyTienMat': lambda: _metric(tm),
        'SoTienGuiNganHang': lambda: _metric(nh),
        'SoChiTietDoanhThu': lambda: _metric(profit['revenue']),
        'SoChiTietDoanhThu_S2a': lambda: _metric(profit['revenue']),
        'SoChiTietDoanhThu_S2b': lambda: _metric(profit['revenue']),
        'SoChiTietDoanhThu_ChiPhi_S2c': lambda: _metric(profit['total_chi_phi']),
        'SoChiPhiSXKD': lambda: _metric(_s3_expense_total(cursor, year)),
        'SoTheoDoiThueKhac': lambda: _metric(_thue_khac_unpaid(cursor)),
        'SoTheoDoiNSNN': lambda: _metric(_nsnn_balance(cursor, year)),
        'SoTheoDoiTienLuong': lambda: _metric(_salary_ytd(cursor, year)),
        'TaiSanCoDinh': lambda: _metric(_tscd_value(cursor)),
        'CongCuDungCu': lambda: _metric(_ccdc_value(cursor)),
        'SoTheoDoiKhoanVay': lambda: _metric(_loan_balance(cursor)),
        'SoChiTietHangHoa': lambda: _metric(_inventory_value(cursor)),
        'tax_report': lambda: _metric(_nsnn_balance(cursor, year)),
        'employees_page': lambda: _metric_employees_page(cursor),
        'attendance_page': lambda: _metric_attendance_page(cursor, year),
        'reports': lambda: _metric(profit['revenue']),
        'profit': lambda: _metric(profit['net_profit']),
        'import_details_page': lambda: _metric_import_details_page(cursor, year),
        'import_list': lambda: _metric_import_list(cursor, year),
        'inward_invoice': lambda: _metric_supplier_invoice(cursor, year),
        'return_import_page': lambda: _metric_return_import(cursor, year),
        'sale_details_page': lambda: _metric(profit['revenue']),
        'inventory': lambda: _metric(_inventory_value(cursor)),
        'inventory_detail': lambda: _metric(_inventory_value(cursor)),
        'outward_invoice': lambda: _metric(_count_pending_invoices(cursor), 'count', 'HĐ chờ'),
        'order': lambda: _metric(_count_draft_orders(cursor), 'count', 'đơn nháp'),
        'sale': lambda: _metric(profit['revenue']),
        'products': lambda: _metric_product_catalog(cursor),
        'suppliers_page': lambda: _metric(_count_suppliers(cursor), 'count', 'nhà cung cấp'),
        'customers_page': lambda: _metric(_count_customers(cursor), 'count', 'khách hàng'),
        'DanhSachPhieuThu': lambda: _metric_chung_tu_ytd(cursor, year, 'phieu_thu'),
        'DanhSachPhieuChi': lambda: _metric_chung_tu_ytd(cursor, year, 'phieu_chi'),
        'DanhSachPhieuNhapKho': lambda: _metric_chung_tu_ytd(cursor, year, 'phieu_nhap'),
        'DanhSachPhieuXuatKho': lambda: _metric_chung_tu_ytd(cursor, year, 'phieu_xuat'),
        'DanhSachBangLuong_05LDTL': lambda: _metric_chung_tu_ytd(cursor, year, 'bang_luong'),
    }

    fn = mapping.get(endpoint)
    if not fn:
        return None
    return fn()


def fetch_hub_group_metrics(cursor, group, year=None):
    """group: dict từ get_hub_group_by_id."""
    if year is None:
        year = date.today().year
    year = int(year)
    group_id = group.get('id')

    items_metrics = {}
    chart_slices = []

    for item in group.get('items') or []:
        ep = item.get('endpoint')
        if not ep:
            continue
        m = fetch_endpoint_metrics(cursor, ep, year)
        if m:
            items_metrics[ep] = m
            if group_id not in ('pos_danh_muc', 'pos_ban_hang', 'pos_nhap_kho') and m['value'] > 0:
                chart_slices.append({
                    'endpoint': ep,
                    'label': item.get('label') or ep,
                    'value': m['value'],
                    'display': m['display'],
                })

    chart = None
    charts = None
    if group_id == 'pos_bao_cao':
        charts = _fetch_bao_cao_charts(cursor, year)
    elif group_id == 'hkd_so_sach':
        charts = _fetch_so_sach_charts(cursor, year)
    elif group_id == 'hkd_chung_tu':
        charts = _fetch_chung_tu_charts(cursor, year)
    elif group_id == 'pos_ban_hang':
        charts = _fetch_ban_hang_charts(cursor, year)
    elif group_id == 'pos_nhap_kho':
        charts = _fetch_mua_hang_charts(cursor, year)
    elif group_id == 'pos_danh_muc':
        prod = items_metrics.get('products', {})
        breakdown = prod.get('breakdown') or {}
        chart_slices = []
        type_labels = (
            ('goods', 'Hàng Hóa (SP)'),
            ('finished_goods', 'Thành Phẩm (TP)'),
            ('service', 'Dịch Vụ (DV)'),
        )
        active_types = sum(
            1 for key, _ in type_labels if int(breakdown.get(key) or 0) > 0
        )
        if active_types >= 2:
            for key, label in type_labels:
                val = int(breakdown.get(key) or 0)
                if val > 0:
                    chart_slices.append({
                        'label': label,
                        'value': val,
                        'display': _fmt_count(val, 'loại'),
                    })

    if group_id not in ('pos_bao_cao', 'hkd_so_sach', 'hkd_chung_tu', 'pos_ban_hang', 'pos_nhap_kho') and len(chart_slices) >= 2:
        total = sum(s['value'] for s in chart_slices)
        is_count_chart = group.get('id') == 'pos_danh_muc'
        chart = {
            'labels': [s['label'] for s in chart_slices],
            'values': [s['value'] for s in chart_slices],
            'displays': [s['display'] for s in chart_slices],
            'colors': [CHART_COLORS[i % len(CHART_COLORS)] for i in range(len(chart_slices))],
            'total': total,
            'total_display': _fmt_count(total, 'bản ghi') if is_count_chart else _fmt_currency(total),
        }

    return {
        'year': year,
        'group_id': group.get('id'),
        'items': items_metrics,
        'chart': chart,
        'charts': charts,
    }
