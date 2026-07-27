"""Truy vấn sổ chi tiết doanh thu HKD (S2a/S2b) theo nhóm G1–G4."""

from datetime import datetime, timedelta

from Services.hkd_sector import calc_sector_taxes, resolve_item_hkd_sector
from Services.invoice_buyer import DEFAULT_RETAIL_BUYER_NAME


def _calc_line_total(quantity, unit_price, discount_pct, tax_pct, line_total_raw=None):
    qty = float(quantity or 0)
    price = float(unit_price or 0)
    disc_pct = float(discount_pct or 0)
    tax_pct = float(tax_pct or 0)
    if line_total_raw is not None and float(line_total_raw or 0) > 0:
        return round(float(line_total_raw))
    line_sub = round(qty * price)
    after_disc = line_sub - round(line_sub * disc_pct / 100)
    tax_amt = round(after_disc * tax_pct / 100)
    return after_disc + tax_amt


def _table_has_column(cursor, table, column):
    cursor.execute(f"PRAGMA table_info({table})")
    return column in [r[1] for r in cursor.fetchall()]


def _aggregate_revenue_rows(cursor, start_date, end_date):
    """Gom doanh thu theo chứng từ, phân cột G1–G4."""
    item_sector_col = 'si.hkd_sector_code' if _table_has_column(cursor, 'sale_items', 'hkd_sector_code') else 'NULL'
    menu_join = 'LEFT JOIN menu m ON m.id = si.menu_id' if _table_has_column(cursor, 'sale_items', 'menu_id') else ''
    menu_type_col = 'COALESCE(m.product_type, \'\')' if _table_has_column(cursor, 'sale_items', 'menu_id') else "''"
    business_line_col = 'COALESCE(s.business_line, \'\')' if _table_has_column(cursor, 'sale', 'business_line') else "''"

    sql = f"""
        SELECT
            s.id AS sale_id,
            s.date AS sale_date,
            COALESCE(NULLIF(TRIM(s.sale_no), ''), 'ĐH' || printf('%06d', s.id)) AS sale_no,
            COALESCE(s.customer_name, '{DEFAULT_RETAIL_BUYER_NAME}') AS customer_name,
            COALESCE(s.invoice_number, '') AS invoice_number,
            si.quantity,
            si.price,
            COALESCE(si.discount_pct, 0) AS discount_pct,
            COALESCE(si.tax_pct, 0) AS tax_pct,
            si.line_total,
            {item_sector_col} AS item_sector,
            p.hkd_sector_code AS product_sector,
            p.product_type AS product_type,
            {menu_type_col} AS menu_product_type,
            {business_line_col} AS business_line
        FROM sale s
        INNER JOIN sale_items si ON si.sale_id = s.id
        LEFT JOIN products p ON p.id = si.product_id
        {menu_join}
        WHERE s.status = 'completed'
          AND si.quantity > 0
          AND date(s.date) >= date(?)
          AND date(s.date) <= date(?)
        ORDER BY s.date ASC, s.id ASC
    """
    cursor.execute(sql, (start_date, end_date))

    grouped = {}
    for row in cursor.fetchall():
        r = dict(row)
        sale_id = r['sale_id']
        if sale_id not in grouped:
            grouped[sale_id] = {
                'sale_id': sale_id,
                'sale_no': r['sale_no'],
                'date': (r['sale_date'] or '')[:10],
                'customer_name': r['customer_name'],
                'invoice_number': r.get('invoice_number') or '',
                'g1': 0.0,
                'g2': 0.0,
                'g3': 0.0,
                'g4': 0.0,
            }

        sector = resolve_item_hkd_sector(
            r.get('item_sector'),
            r.get('product_sector'),
            r.get('product_type'),
            r.get('menu_product_type'),
            r.get('business_line'),
        )
        amount = _calc_line_total(
            r['quantity'], r['price'], r['discount_pct'], r['tax_pct'], r.get('line_total'),
        )
        grouped[sale_id][sector.lower()] += amount

    rows = []
    totals = {'g1': 0.0, 'g2': 0.0, 'g3': 0.0, 'g4': 0.0, 'total': 0.0}
    invoice_count = 0

    for sale_id in sorted(grouped.keys(), key=lambda x: (grouped[x]['date'], x)):
        item = grouped[sale_id]
        row_total = item['g1'] + item['g2'] + item['g3'] + item['g4']
        item['total'] = row_total
        item['description'] = f"Bán hàng cho {item['customer_name']}"
        rows.append(item)

        for key in ('g1', 'g2', 'g3', 'g4'):
            totals[key] += item[key]
        totals['total'] += row_total
        if (item.get('invoice_number') or '').strip():
            invoice_count += 1

    return rows, totals, {'order_count': len(rows), 'invoice_count': invoice_count}


def _ytd_total_before_period(cursor, period_start_date):
    """DT lũy kế từ 01/01/năm đến ngày trước period_start."""
    start = (period_start_date or '')[:10]
    if not start:
        return 0.0
    try:
        d = datetime.strptime(start, '%Y-%m-%d')
    except ValueError:
        return 0.0
    if d.month == 1 and d.day == 1:
        return 0.0
    ytd_start = f"{d.year}-01-01"
    prev_day = (d - timedelta(days=1)).strftime('%Y-%m-%d')
    _, totals, _ = _aggregate_revenue_rows(cursor, ytd_start, prev_day)
    return float(totals.get('total') or 0)


def fetch_hkd_revenue_ledger(cursor, start_date, end_date, apply_tncn_threshold=True):
    """
    Gom doanh thu theo chứng từ bán hàng, phân cột G1–G4 từ sale_items + products.
    apply_tncn_threshold: TNCN chỉ trên phần DT vượt 1 tỷ lũy kế năm (TT 50/2026).
    Trả về dict: rows, totals, taxes, summary.
    """
    rows, totals, summary = _aggregate_revenue_rows(cursor, start_date, end_date)

    ytd_before = None
    if apply_tncn_threshold:
        ytd_before = _ytd_total_before_period(cursor, start_date)

    taxes = calc_sector_taxes(totals, ytd_before=ytd_before)

    return {
        'rows': rows,
        'totals': totals,
        'taxes': taxes,
        'summary': summary,
    }
