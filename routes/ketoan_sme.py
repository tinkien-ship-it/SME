"""Routes kế toán SME — tách từ app.py."""
import json
import logging
import os
import re
import sqlite3
import traceback
import uuid
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from io import BytesIO

import pandas as pd
import requests
from flask import (
    Response,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from db_utils import BASE_DIR, MAIN_DB_PATH, get_db_connection

logger = logging.getLogger(__name__)



def round_money(val):
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _insert_sme_import_stock_move(
    c,
    sm_cols,
    *,
    product_id,
    import_date,
    import_id,
    qty,
    cost_per_retail,
    move_note,
    import_no,
    retail_unit,
    wholesale_unit,
    ratio,
    warehouse_code,
):
    """Ghi stock_moves khi nhập mua SME; bổ sung in/out nếu schema có."""
    fields = [
        'product_id', 'date', 'type', 'ref_id', 'quantity', 'cost_price', 'note',
        'ref_document', 'ref_type', 'type1', 'unit', 'unit1', 'unit_ratio',
    ]
    values = [
        product_id, import_date, 'import', import_id, float(qty), float(cost_per_retail),
        move_note, import_no, 'import', 'Nhập', retail_unit, wholesale_unit, float(ratio),
    ]
    if 'in_quantity' in sm_cols and 'out_quantity' in sm_cols:
        fields.extend(['in_quantity', 'out_quantity'])
        values.extend([float(qty), 0.0])
    if 'warehouse_code' in sm_cols:
        fields.append('warehouse_code')
        values.append(warehouse_code)
    placeholders = ', '.join(['?'] * len(values))
    c.execute(
        f"INSERT INTO stock_moves ({', '.join(fields)}) VALUES ({placeholders})",
        values,
    )


def _sme_assert_sale_branch_access(conn, sale_id, sale, branch):
    from Services.sme.branch_filter import assert_warehouse_in_session_branch
    from Services.sme.branches import sale_branch_filter_sql

    code = (branch or '').strip().upper()
    if not code or code == 'ALL':
        return None
    sale_cols = {r[1] for r in conn.execute('PRAGMA table_info(sale)').fetchall()}
    wh = (sale.get('warehouse_code') or '').strip()
    if 'warehouse_code' in sale_cols and wh:
        try:
            assert_warehouse_in_session_branch(conn, wh, allow_all=False)
        except ValueError as exc:
            return str(exc)
        return None
    bf, bp = sale_branch_filter_sql(conn, branch, alias='s')
    ok = conn.execute(
        f'SELECT 1 FROM sale s WHERE s.id = ? {bf}',
        (sale_id, *bp),
    ).fetchone()
    if not ok:
        return 'Đơn hàng không thuộc chi nhánh đang chọn'
    return None


def _sme_build_sale_detail_payload(conn, sale_id):
    from Services.invoice_buyer import DEFAULT_RETAIL_BUYER_NAME

    c = conn.cursor()
    c.execute('SELECT s.* FROM sale s WHERE s.id = ?', (sale_id,))
    sale_row = c.fetchone()
    if not sale_row:
        return None

    sale = dict(sale_row)
    customer_name = (sale.get('customer_name') or '').strip() or DEFAULT_RETAIL_BUYER_NAME
    customer_phone = (sale.get('customer_phone') or sale.get('phone') or '').strip()
    total_amount = float(sale.get('total_amount') or 0)
    discount_amount = float(sale.get('discount_amount') or 0)
    tax_pct = float(sale.get('tax_pct') or 0)
    sale_no = (sale.get('sale_no') or '').strip() or f"ĐH{str(sale_id).zfill(6)}"
    sale_date = sale.get('date')
    created_at = sale.get('created_at') or sale_date

    c.execute(
        """
        SELECT
            si.product_id,
            si.product_name AS si_name,
            si.quantity,
            si.price AS sold_price,
            si.discount_pct,
            si.cost_price,
            COALESCE(si.UseSaleUnit, 0) AS UseSaleUnit,
            si.unit AS si_unit,
            p.name AS product_name,
            p.product_code,
            p.barcode,
            p.barcode1,
            p.unit AS p_unit,
            p.unit1 AS p_unit1,
            COALESCE(p.unit_ratio, 1) AS unit_ratio
        FROM sale_items si
        LEFT JOIN products p ON p.id = si.product_id
        WHERE si.sale_id = ?
        ORDER BY si.rowid
        """,
        (sale_id,),
    )

    items = []
    for row in c.fetchall():
        row = dict(row)
        use_sale_unit = int(row.get('UseSaleUnit') or 0)
        product_id = row.get('product_id')
        name = (row.get('si_name') or row.get('product_name') or '').strip()
        sold_price = float(row.get('sold_price') or 0)
        quantity = float(row.get('quantity') or 0)
        discount_pct = float(row.get('discount_pct') or 0)
        unit1 = row.get('p_unit1') or 'Lốc/Thùng'
        unit = (
            unit1 if use_sale_unit == 1
            else (row.get('si_unit') or row.get('p_unit') or 'Cái')
        )

        c.execute(
            """
            SELECT COALESCE(SUM(quantity), 0)
            FROM return_sales
            WHERE sale_id = ? AND product_id = ? AND COALESCE(UseSaleUnit, 0) = ?
            """,
            (sale_id, product_id, use_sale_unit),
        )
        returned_qty = float(c.fetchone()[0])
        # sale_items.quantity đã trừ trả (legacy) → remaining = quantity hiện tại
        remaining_qty = max(0.0, quantity)
        product_code = row.get('product_code') or (str(product_id) if product_id is not None else '')
        items.append({
            'product_id': product_id,
            'name': name,
            'product_name': name,
            'product_code': product_code,
            'quantity': quantity,
            'sold_price': sold_price,
            'price': sold_price,
            'discount_pct': discount_pct,
            'discount': discount_pct,
            'tax_pct': tax_pct,
            'cost_price': float(row.get('cost_price') or 0),
            'UseSaleUnit': use_sale_unit,
            'unit': unit,
            'unit1': unit1,
            'unit_ratio': float(row.get('unit_ratio') or 1),
            'barcode': row.get('barcode') or '',
            'barcode1': row.get('barcode1') or '',
            'returned_qty': returned_qty,
            'remaining_qty': remaining_qty,
            'line_total': quantity * sold_price,
        })

    return {
        'id': sale['id'],
        'sale_no': sale_no,
        'date': sale_date,
        'created_at': created_at,
        'status': sale.get('status') or '',
        'customer_name': customer_name,
        'company_name': sale.get('company_name') or '',
        'tax_code': sale.get('tax_code') or '',
        'budget_unit_code': sale.get('budget_unit_code') or '',
        'passport_no': sale.get('passport_no') or '',
        'address': sale.get('address') or '',
        'customer_phone': customer_phone,
        'phone': customer_phone,
        'email': sale.get('email') or '',
        'tax_pct': tax_pct,
        'total_amount': total_amount,
        'discount_amount': discount_amount,
        'note': sale.get('note') or '',
        'invoice_number': sale.get('invoice_number') or '',
        'invoice_status': sale.get('invoice_status') or 'none',
        'inv_status': sale.get('invoice_status') or 'none',
        'items': items,
        '_sale_row': sale,
    }


def register_ketoan_sme_routes(app):
    """Đăng ký route KeToan SME (giữ nguyên URL/endpoint)."""
    from auth import login_required
    from helpers import parse_date
    from Services.tenant_profile import require_sme_regime

    @app.context_processor
    def inject_sme_hub():
        from Services.sme_menu import (
            SME_HUB_TITLE,
            get_sme_featured_links,
            get_sme_menu_groups,
            get_sme_quick_links,
            is_sme_endpoint,
            resolve_sme_current_group,
        )
        current_group = None
        if request.endpoint == 'SME_hub_group':
            current_group = (request.view_args or {}).get('group_id')
        else:
            current_group = resolve_sme_current_group(request.endpoint)
        sme_branch = {
            'multi_branch': False,
            'branches': [],
            'current_branch_code': session.get('sme_branch_code') or 'HQ',
            'filter': session.get('sme_branch_filter') or 'ALL',
        }
        if is_sme_endpoint(request.endpoint) or (request.endpoint or '').startswith('SME_'):
            try:
                from Services.sme.branches import branch_context
                conn = get_db_connection()
                conn.row_factory = sqlite3.Row
                try:
                    sme_branch = branch_context(conn)
                    sme_branch['filter'] = (
                        session.get('sme_branch_filter')
                        or sme_branch.get('current_branch_code')
                        or 'ALL'
                    )
                finally:
                    conn.close()
            except Exception:
                pass
        return {
            'sme_hub_title': SME_HUB_TITLE,
            'sme_menu_groups': get_sme_menu_groups(),
            'sme_dashboard_quick': get_sme_quick_links(),
            'sme_dashboard_featured': get_sme_featured_links(),
            'sme_hub_active': is_sme_endpoint(request.endpoint),
            'sme_current_group': current_group,
            'sme_branch': sme_branch,
        }

    def _bootstrap_sme_db():
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            from Services.tenant_profile import get_current_tenant_profile
            profile = get_current_tenant_profile()
            return ensure_sme_accounting_ready(
                conn,
                accounting_regime=profile.get('accounting_regime'),
            )
        finally:
            conn.close()

    #============================================================================== Start of SME Accounting=========================================================================#
    def _sme_branch_arg():
        from Services.sme.branches import request_branch_filter
        return request_branch_filter()


    @app.route('/SME_dashboard')
    @login_required
    @require_sme_regime
    def SME_dashboard():
        try:
            _bootstrap_sme_db()
        except Exception:
            logger.exception('SME bootstrap on dashboard')
        return render_template('KeToanSME/main_dashboard.html')

    @app.route('/SME_hub/<group_id>')
    @login_required
    @require_sme_regime
    def SME_hub_group(group_id):
        from Services.sme_menu import get_sme_group_by_id
        group = get_sme_group_by_id(group_id)
        if not group:
            abort(404)
        if group_id == 'overview':
            return redirect(url_for('SME_dashboard'))
        return render_template(
            'KeToanSME/group_dashboard.html',
            group=group,
            group_id=group_id,
        )

    @app.route('/SME_order')
    @login_required
    @require_sme_regime
    def SME_order():
        return render_template('order.html')

    @app.route('/SME_outward_invoice')
    @login_required
    @require_sme_regime
    def SME_outward_invoice():
        """HĐ bán (e-invoice) — pháp nhân toàn DN; shell SME + regime."""
        view = app.view_functions.get('outward_invoice')
        if view:
            return view()
        return redirect(url_for('outward_invoice'))

    @app.route('/SME_purchasing')
    @login_required
    @require_sme_regime
    def SME_purchasing():
        return render_template('KeToanSME/dashboard_purchasing.html')

    @app.route('/SME_dashboard_sale')
    @login_required
    @require_sme_regime
    def SME_dashboard_sale():
        return render_template('KeToanSME/dashboard_sale.html')

    @app.route('/SME_sale_details')
    @login_required
    @require_sme_regime
    def SME_sale_details():
        return render_template('sale_details.html')

    @app.route('/SME_purchase_order_create')
    @login_required
    @require_sme_regime
    def SME_purchase_order_create():
        try:
            _bootstrap_sme_db()
        except Exception:
            pass
        return render_template('KeToanSME/purchase_order_create.html')

    @app.route('/SME_purchase_order_list')
    @login_required
    @require_sme_regime
    def SME_purchase_order_list():
        try:
            _bootstrap_sme_db()
        except Exception:
            pass
        return render_template('KeToanSME/purchase_order_list.html')

    @app.route('/SME_inward_invoice')
    @login_required
    @require_sme_regime
    def SME_inward_invoice():
        return render_template('KeToanSME/inward_invoice.html')

    @app.route('/SME_import')
    @login_required
    @require_sme_regime
    def SME_import():
        from routes.inventory import peek_next_import_no
        from Services.import_line_helpers import list_active_warehouses

        import_mode = (request.args.get('mode') or 'stock').strip().lower()
        if import_mode not in ('stock', 'service'):
            import_mode = 'stock'
        warehouses = [{'code': 'KHO_001', 'name': 'Kho mặc định', 'is_default': 1}]
        next_import_no = 'HT000001' if import_mode == 'service' else 'PN000001'
        try:
            conn = get_db_connection()
            try:
                branch = (
                    session.get('sme_branch_filter')
                    or session.get('sme_branch_code')
                    or 'ALL'
                )
                warehouses = list_active_warehouses(
                    conn, branch_code=branch,
                ) or warehouses
            finally:
                conn.close()
        except Exception:
            logger.exception('SME_import load warehouses')
        try:
            next_import_no = peek_next_import_no(import_mode) or next_import_no
        except Exception:
            logger.exception('SME_import next_import_no')
        return render_template(
            'KeToanSME/import_sme.html',
            warehouses=warehouses,
            next_import_no=next_import_no,
            import_mode=import_mode,
        )

    @app.route('/SME_landed_cost')
    @login_required
    @require_sme_regime
    def SME_landed_cost():
        invoice_id = request.args.get('invoice_id', type=int)
        return render_template(
            'KeToanSME/landed_cost.html',
            invoice_id=invoice_id,
        )

    @app.route('/SME_import_list')
    @login_required
    @require_sme_regime
    def SME_DanhSachPhieuNhapKho():
        return render_template('KeToanSME/import_list.html')

    @app.route('/SME_return_supplier')
    @login_required
    @require_sme_regime
    def SME_return_supplier():
        return render_template('KeToanSME/return_supplier.html')

    @app.route('/SME_return_sale')
    @login_required
    @require_sme_regime
    def SME_return_sale():
        return render_template('KeToanSME/return_sale.html')

    @app.route('/SME_SoCongNoPhaiTra')
    @login_required
    @require_sme_regime
    def SME_SoCongNoPhaiTra():
        return render_template('KeToanSME/SoCongNoPhaiTra.html')

    @app.route('/SME_SoCongNoPhaiTra/in')
    @login_required
    @require_sme_regime
    def SME_SoCongNoPhaiTra_in():
        """In sổ công nợ phải trả SME (không dùng template HKD)."""
        supplier_name = request.args.get('supplier')
        start = request.args.get('start')
        end = request.args.get('end')
        if not supplier_name:
            return 'Lỗi: Phải chọn nhà cung cấp để in sổ!', 400
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            from Services.sme.branches import import_branch_filter_sql, request_branch_filter
            branch = request_branch_filter()
            ibf, ibp = import_branch_filter_sql(conn, branch, alias='i')
            supplier_data = conn.execute(
                'SELECT id, name, address FROM suppliers WHERE name = ?', (supplier_name,)
            ).fetchone()
            if not supplier_data:
                return 'Lỗi: Nhà cung cấp không tồn tại!', 404
            s_id = supplier_data['id']
            opening_balance = 0
            if start:
                res_opening = conn.execute(
                    f"""
                    SELECT (COALESCE(SUM(COALESCE(total_value,0)),0)
                            - COALESCE(SUM(COALESCE(paid_amount,0)),0)) AS balance
                    FROM import i WHERE supplier_id = ? AND date(date) < date(?)
                    {ibf}
                    """,
                    (s_id, start, *ibp),
                ).fetchone()
                opening_balance = round(float(res_opening['balance'] or 0), 0)
                if opening_balance < 1:
                    opening_balance = 0
            sql_main = f"""
                SELECT id, COALESCE(import_no, 'PN'||id) AS purchase_no,
                       date AS date,
                       'Nợ tiền mua hàng' AS dien_giai,
                       COALESCE(total_value, 0) AS no,
                       COALESCE(paid_amount, 0) AS co
                FROM import i WHERE supplier_id = ?
                {ibf}
            """
            params: list = [s_id, *ibp]
            if start:
                sql_main += ' AND date(date) >= date(?)'
                params.append(start)
            if end:
                sql_main += ' AND date(date) <= date(?)'
                params.append(end)
            sql_main += ' ORDER BY date ASC, id ASC'
            rows = []
            for r in conn.execute(sql_main, params).fetchall():
                item = dict(r)
                item['no'] = round(float(item['no'] or 0), 0)
                item['co'] = round(float(item['co'] or 0), 0)
                rows.append(item)
            total_no = sum(r['no'] for r in rows)
            total_co = sum(r['co'] for r in rows)
            closing = opening_balance + total_no - total_co
            if closing < 1:
                closing = 0
            info = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
            return render_template(
                'KeToanSME/SoCongNoPhaiTra_print.html',
                rows=rows,
                totals={'opening': opening_balance, 'no': total_no, 'co': total_co, 'closing': closing},
                start=start, end=end, supplier=supplier_name,
                info=dict(info) if info else {},
            )
        finally:
            conn.close()

    @app.route('/SME_SoCongNoPhaiThu')
    @login_required
    @require_sme_regime
    def SME_SoCongNoPhaiThu():
        """Công nợ phải thu SME — lọc theo chi nhánh qua /api/sme/debt/customers."""
        return render_template('KeToanSME/SME_SoCongNoPhaiThu.html')

    @app.route('/api/sme/debt/customers')
    @login_required
    @require_sme_regime
    def api_sme_debt_customers():
        from Services.sme.branches import request_branch_filter, sale_branch_filter_sql
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = request_branch_filter()
            sql = """
                SELECT DISTINCT cn.customer_name, s.company_name
                FROM cong_no cn
                LEFT JOIN sale s ON cn.sale_id = s.id
                WHERE cn.remaining_amount <> 0
            """
            params: list = []
            bf, bp = sale_branch_filter_sql(conn, branch, alias='s')
            sql += bf
            params.extend(bp)
            sql += ' ORDER BY s.company_name COLLATE NOCASE, cn.customer_name COLLATE NOCASE'
            rows = conn.execute(sql, params).fetchall()
            return jsonify([
                {
                    'customer_name': r['customer_name'],
                    'company_name': r['company_name'] or '',
                }
                for r in rows
            ])
        finally:
            conn.close()

    @app.route('/api/sme/debt/customer-detail')
    @login_required
    @require_sme_regime
    def api_sme_debt_customer_detail():
        customer_name = request.args.get('customer')
        if not customer_name:
            return jsonify(success=False, error='Thiếu tên khách hàng'), 400
        from Services.sme.branches import request_branch_filter, sale_branch_filter_sql
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = request_branch_filter()
            bf, bp = sale_branch_filter_sql(conn, branch, alias='s')
            sql_records = f"""
                SELECT
                    cn.debt_id,
                    cn.sale_no,
                    cn.date_of_debt,
                    cn.customer_name,
                    cn.sale_id,
                    s.company_name,
                    cn.unpaid_amount     AS total_debt,
                    cn.paid_amount       AS paid_amount,
                    cn.remaining_amount  AS remaining
                FROM cong_no cn
                LEFT JOIN sale s ON cn.sale_id = s.id
                WHERE cn.customer_name = ?
                  AND cn.remaining_amount > 0
                  {bf}
                ORDER BY cn.date_of_debt ASC, cn.debt_id ASC
            """
            records = [dict(r) for r in conn.execute(sql_records, [customer_name, *bp]).fetchall()]
            sql_summary = f"""
                SELECT
                    COALESCE(SUM(cn.unpaid_amount), 0)    AS total_debt_all,
                    COALESCE(SUM(cn.paid_amount), 0)      AS total_paid_all,
                    COALESCE(SUM(cn.remaining_amount), 0)  AS current_remaining
                FROM cong_no cn
                LEFT JOIN sale s ON cn.sale_id = s.id
                WHERE cn.customer_name = ?
                  {bf}
            """
            summary_row = conn.execute(sql_summary, [customer_name, *bp]).fetchone()
            summary = {
                'total': float(summary_row['total_debt_all']),
                'paid': float(summary_row['total_paid_all']),
                'remaining': float(summary_row['current_remaining']),
            }
            return jsonify(success=True, summary=summary, records=records)
        except Exception as e:
            return jsonify(success=False, error=str(e)), 500
        finally:
            conn.close()

    @app.route('/api/sme/sales', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_sales_list():
        from Services.sme.branches import request_branch_filter, sale_branch_filter_sql

        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        q = request.args.get('q', '').strip()
        start = request.args.get('start')
        end = request.args.get('end')
        status = request.args.get('status')
        offset = (page - 1) * limit

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = request_branch_filter()
            where = ['1=1']
            params: list = []

            if start:
                start_dt = start if len(start) > 10 else f'{start} 00:00:00'
                where.append('s.date >= ?')
                params.append(start_dt)
            if end:
                end_dt = end if len(end) > 10 else f'{end} 23:59:59'
                where.append('s.date <= ?')
                params.append(end_dt)
            if q:
                like_pattern = f'%{q}%'
                where.append(
                    '(CAST(s.id AS TEXT) LIKE ? OR COALESCE(s.customer_name,\'\') LIKE ? '
                    'OR COALESCE(s.customer_phone,\'\') LIKE ? OR COALESCE(s.invoice_number,\'\') LIKE ?)'
                )
                params.extend([like_pattern, like_pattern, like_pattern, like_pattern])
            if status == 'draft':
                where.append("s.status = 'draft'")
            elif status == 'pending':
                where.append(
                    "s.status = 'completed' AND (s.invoice_number IS NULL OR s.invoice_number = '')"
                )
            elif status == 'completed':
                where.append("s.status = 'completed'")

            bf, bp = sale_branch_filter_sql(conn, branch, alias='s')
            where_sql = ' AND '.join(where) + bf
            params_count = list(params) + list(bp)

            total_records = conn.execute(
                f'SELECT COUNT(*) FROM sale s WHERE {where_sql}',
                params_count,
            ).fetchone()[0]

            rows = conn.execute(
                f"""
                SELECT s.*, COALESCE(s.invoice_status, 'none') AS inv_status, tax_authority_status
                FROM sale s
                WHERE {where_sql}
                ORDER BY s.date DESC, s.id DESC
                LIMIT ? OFFSET ?
                """,
                params_count + [limit, offset],
            ).fetchall()

            orders = []
            for r in rows:
                d = dict(r)
                d['sale_no'] = d.get('sale_no') or f"ĐH{str(d['id']).zfill(6)}"
                d['has_invoice'] = bool(
                    d.get('invoice_number') and str(d['invoice_number']).strip()
                )
                orders.append(d)

            stats_where = ['1=1']
            stats_params: list = []
            if start:
                stats_where.append('s.date >= ?')
                stats_params.append(start if len(start) > 10 else f'{start} 00:00:00')
            if end:
                stats_where.append('s.date <= ?')
                stats_params.append(end if len(end) > 10 else f'{end} 23:59:59')
            stats_where_sql = ' AND '.join(stats_where) + bf
            stats_params = stats_params + list(bp)

            revenue = conn.execute(
                f"SELECT SUM(s.total_amount) FROM sale s WHERE s.status = 'completed' AND {stats_where_sql}",
                stats_params,
            ).fetchone()[0] or 0
            pending_invoice = conn.execute(
                f"SELECT COUNT(*) FROM sale s WHERE s.status = 'completed' "
                f"AND (s.invoice_number IS NULL OR s.invoice_number = '') AND {stats_where_sql}",
                stats_params,
            ).fetchone()[0]
            issued_invoice = conn.execute(
                f"SELECT COUNT(*) FROM sale s WHERE (s.invoice_number IS NOT NULL AND s.invoice_number != '') "
                f"AND {stats_where_sql}",
                stats_params,
            ).fetchone()[0]

            return jsonify({
                'orders': orders,
                'total': total_records,
                'stats': {
                    'revenue': float(revenue),
                    'pending_invoice': int(pending_invoice),
                    'issued_invoice': int(issued_invoice),
                },
            }), 200
        finally:
            conn.close()

    @app.route('/api/sme/sales/<int:sale_id>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_sale_detail(sale_id):
        from Services.sme.branches import request_branch_filter

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            payload = _sme_build_sale_detail_payload(conn, sale_id)
            if not payload:
                return jsonify({'error': 'Không tìm thấy đơn hàng'}), 404

            sale_row = payload.pop('_sale_row', {})
            branch = request_branch_filter()
            branch_err = _sme_assert_sale_branch_access(conn, sale_id, sale_row, branch)
            if branch_err:
                return jsonify({'error': branch_err}), 403

            return jsonify({'success': True, **payload, 'data': payload}), 200
        except Exception as exc:
            logger.exception('api_sme_sale_detail(%s)', sale_id)
            return jsonify({'error': str(exc)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/debt/suppliers')
    @login_required
    @require_sme_regime
    def api_sme_debt_suppliers():
        from Services.sme.branches import import_branch_filter_sql, request_branch_filter
        conn = get_db_connection()
        try:
            branch = request_branch_filter()
            bf, bp = import_branch_filter_sql(conn, branch, alias='i')
            sql = f"""
                SELECT DISTINCT s.name
                FROM suppliers s
                JOIN import i ON s.id = i.supplier_id
                WHERE (COALESCE(i.total_value, 0) - COALESCE(i.paid_amount, 0)) > 0
                {bf}
                ORDER BY s.name
            """
            rows = conn.execute(sql, bp).fetchall()
            return jsonify([r[0] for r in rows])
        finally:
            conn.close()

    @app.route('/api/sme/debt/supplier-detail')
    @login_required
    @require_sme_regime
    def api_sme_debt_supplier_detail():
        supplier_name = request.args.get('supplier')
        if not supplier_name:
            return jsonify({'success': False, 'error': 'Thiếu tên nhà cung cấp'}), 400
        from Services.sme.branches import import_branch_filter_sql, request_branch_filter
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = request_branch_filter()
            bf, bp = import_branch_filter_sql(conn, branch, alias='i')
            summary = conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(COALESCE(i.total_value, 0)), 0) AS total,
                    COALESCE(SUM(COALESCE(i.paid_amount, 0)), 0) AS paid,
                    COALESCE(SUM(
                        COALESCE(i.total_value, 0) - COALESCE(i.paid_amount, 0)
                    ), 0) AS remaining
                FROM import i
                JOIN suppliers s ON i.supplier_id = s.id
                WHERE s.name = ?
                {bf}
                """,
                (supplier_name, *bp),
            ).fetchone()
            records = conn.execute(
                f"""
                SELECT
                    i.id,
                    i.import_no,
                    i.bill_no,
                    i.date,
                    i.total_value,
                    i.paid_amount,
                    (COALESCE(i.total_value, 0) - COALESCE(i.paid_amount, 0))
                        AS remaining_amount,
                    s.address AS supplier_address,
                    s.name AS supplier_name
                FROM import i
                JOIN suppliers s ON i.supplier_id = s.id
                WHERE s.name = ?
                  AND (COALESCE(i.total_value, 0) - COALESCE(i.paid_amount, 0)) > 0
                {bf}
                ORDER BY i.date DESC
                """,
                (supplier_name, *bp),
            ).fetchall()
            return jsonify({
                'success': True,
                'summary': {
                    'total': summary['total'],
                    'paid': summary['paid'],
                    'remaining': summary['remaining'],
                },
                'records': [dict(row) for row in records],
            })
        finally:
            conn.close()

    @app.route('/SME_SoCongNoPhaiThu/in')
    @login_required
    @require_sme_regime
    def SME_SoCongNoPhaiThu_in():
        """In sổ công nợ phải thu SME."""
        customer = request.args.get('customer')
        start = request.args.get('start')
        end = request.args.get('end')
        if not customer:
            return 'Lỗi: Phải chọn khách hàng!', 400
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            from Services.sme.branches import request_branch_filter, sale_branch_filter_sql
            branch = request_branch_filter()
            sbf, sbp = sale_branch_filter_sql(conn, branch, alias='s')
            opening = 0.0
            if start:
                row = conn.execute(
                    f"""
                    SELECT COALESCE(SUM(cn.unpaid_amount),0) - COALESCE(SUM(cn.paid_amount),0) AS bal
                    FROM cong_no cn
                    LEFT JOIN sale s ON cn.sale_id = s.id
                    WHERE cn.customer_name = ? AND date(cn.date_of_debt) < date(?)
                    {sbf}
                    """,
                    (customer, start, *sbp),
                ).fetchone()
                opening = round(float(row['bal'] or 0), 0)
                if opening < 1:
                    opening = 0
            sql = f"""
                SELECT cn.sale_no AS doc_no, cn.date_of_debt AS date,
                       'Phải thu bán hàng' AS dien_giai,
                       COALESCE(cn.unpaid_amount,0) AS no,
                       COALESCE(cn.paid_amount,0) AS co,
                       (COALESCE(cn.unpaid_amount,0) - COALESCE(cn.paid_amount,0)) AS remaining
                FROM cong_no cn
                LEFT JOIN sale s ON cn.sale_id = s.id
                WHERE cn.customer_name = ?
                {sbf}
            """
            params: list = [customer, *sbp]
            if start:
                sql += ' AND date(cn.date_of_debt) >= date(?)'
                params.append(start)
            if end:
                sql += ' AND date(cn.date_of_debt) <= date(?)'
                params.append(end)
            sql += ' ORDER BY cn.date_of_debt ASC, cn.debt_id ASC'
            rows = []
            for r in conn.execute(sql, params).fetchall():
                item = dict(r)
                item['no'] = round(float(item['no'] or 0), 0)
                item['co'] = round(float(item['co'] or 0), 0)
                rows.append(item)
            total_no = sum(r['no'] for r in rows)
            total_co = sum(r['co'] for r in rows)
            closing = opening + total_no - total_co
            if closing < 1:
                closing = 0
            info = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
            return render_template(
                'KeToanSME/SoCongNoPhaiThu_print.html',
                rows=rows,
                totals={'opening': opening, 'no': total_no, 'co': total_co, 'closing': closing},
                start=start, end=end, customer=customer,
                info=dict(info) if info else {},
            )
        finally:
            conn.close()

    @app.route('/SME_DanhSachPhieuThu')
    @login_required
    @require_sme_regime
    def SME_DanhSachPhieuThu():
        return render_template('KeToanSME/SME_DanhSachPhieuThu.html')

    @app.route('/SME_DanhSachPhieuChi')
    @login_required
    @require_sme_regime
    def SME_DanhSachPhieuChi():
        return render_template('KeToanSME/SME_DanhSachPhieuChi.html')

    @app.route('/SME_DanhSachPhieuNhapKho_VT')
    @login_required
    @require_sme_regime
    def SME_DanhSachPhieuNhapKho_VT():
        """Danh sách phiếu nhập kho mẫu 01-VT (DN) — dữ liệu từ import SME/POS."""
        return render_template('KeToanSME/SME_DanhSachPhieuNhap_01VT.html')

    @app.route('/SME_DanhSachPhieuXuatKho_VT')
    @login_required
    @require_sme_regime
    def SME_DanhSachPhieuXuatKho_VT():
        """Danh sách phiếu xuất kho mẫu 02-VT (DN)."""
        return render_template('KeToanSME/SME_DanhSachPhieuXuat_02VT.html')

    @app.route('/SME_PhieuNhapKho/in/<int:import_id>')
    @login_required
    @require_sme_regime
    def SME_PhieuNhapKho_in(import_id):
        """In phiếu nhập kho mẫu 01-VT (DN) — không dùng 02-VT HKD."""
        from Services.sme.branches import assert_import_in_branch
        from Services.sme.stock_vouchers import get_stock_in_print_payload
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_import_in_branch(conn, import_id)
            except ValueError as exc:
                flash(str(exc), 'danger')
                return redirect(url_for('SME_DanhSachPhieuNhapKho_VT'))
            payload = get_stock_in_print_payload(conn, import_id)
            if not payload:
                flash('Không tìm thấy phiếu nhập', 'danger')
                return redirect(url_for('SME_DanhSachPhieuNhapKho_VT'))
            imp, items, info = payload
            return render_template(
                'KeToanSME/SME_PhieuNhapKho_01VT_print.html',
                imp=imp,
                items=items,
                info=info,
            )
        finally:
            conn.close()

    @app.route('/SME_PhieuXuatKho/in/<int:voucher_id>')
    @login_required
    @require_sme_regime
    def SME_PhieuXuatKho_in(voucher_id):
        """In phiếu xuất kho mẫu 02-VT (DN) — không dùng 04-VT HKD."""
        from Services.sme.branches import assert_sale_in_branch, request_branch_filter
        from Services.sme.stock_vouchers import get_stock_out_print_payload
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            payload = get_stock_out_print_payload(conn, voucher_id)
            if not payload:
                flash('Không tìm thấy phiếu xuất', 'danger')
                return redirect(url_for('SME_DanhSachPhieuXuatKho_VT'))
            px, info = payload
            branch = request_branch_filter()
            if branch and str(branch).upper() not in ('', 'ALL'):
                sale_id = px.get('sale_id') if isinstance(px, dict) else None
                if not sale_id:
                    flash('Phiếu xuất thủ công chỉ in khi lọc Tất cả chi nhánh', 'danger')
                    return redirect(url_for('SME_DanhSachPhieuXuatKho_VT'))
                try:
                    assert_sale_in_branch(conn, int(sale_id))
                except ValueError as exc:
                    flash(str(exc), 'danger')
                    return redirect(url_for('SME_DanhSachPhieuXuatKho_VT'))
            return render_template(
                'KeToanSME/SME_PhieuXuatKho_02VT_print.html',
                px=px,
                info=info,
            )
        finally:
            conn.close()

    @app.route('/SME_PhieuThu/in/<int:voucher_id>')
    @login_required
    @require_sme_regime
    def SME_PhieuThu_in(voucher_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.vouchers import get_voucher
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            try:
                assert_row_in_branch(
                    conn, 'sme_vouchers', voucher_id, label='Phiếu thu',
                )
            except ValueError as exc:
                flash(str(exc), 'danger')
                return redirect(url_for('SME_DanhSachPhieuThu'))
            voucher = get_voucher(conn, voucher_id)
            if not voucher or voucher.get('voucher_type') != 'receipt':
                flash('Không tìm thấy phiếu thu SME', 'danger')
                return redirect(url_for('SME_DanhSachPhieuThu'))
            info = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
            return render_template(
                'KeToanSME/SME_PhieuThu_print.html',
                receipt=voucher,
                info=dict(info) if info else {},
                voucher_no=voucher.get('voucher_no'),
            )
        finally:
            conn.close()

    @app.route('/SME_PhieuChi/in/<int:voucher_id>')
    @login_required
    @require_sme_regime
    def SME_PhieuChi_in(voucher_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.vouchers import get_voucher
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            try:
                assert_row_in_branch(
                    conn, 'sme_vouchers', voucher_id, label='Phiếu chi',
                )
            except ValueError as exc:
                flash(str(exc), 'danger')
                return redirect(url_for('SME_DanhSachPhieuChi'))
            voucher = get_voucher(conn, voucher_id)
            if not voucher or voucher.get('voucher_type') != 'payment':
                flash('Không tìm thấy phiếu chi SME', 'danger')
                return redirect(url_for('SME_DanhSachPhieuChi'))
            info = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
            return render_template(
                'KeToanSME/SME_PhieuChi_print.html',
                payment=voucher,
                info=dict(info) if info else {},
                voucher_no=voucher.get('voucher_no'),
            )
        finally:
            conn.close()

    @app.route('/api/sme/vouchers/receipts', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_vouchers_receipts():
        from Services.sme.vouchers import create_receipt, list_vouchers
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            if request.method == 'GET':
                branch = (
                    request.args.get('branch')
                    or session.get('sme_branch_filter')
                    or 'ALL'
                )
                rows = list_vouchers(
                    conn,
                    voucher_type='receipt',
                    date_from=request.args.get('from') or request.args.get('date_from'),
                    date_to=request.args.get('to') or request.args.get('date_to'),
                    branch_code=branch,
                )
                return jsonify({'success': True, 'data': rows, 'branch_code': branch})
            data = request.get_json(silent=True) or {}
            result = create_receipt(
                conn,
                voucher_date=data.get('date') or data.get('voucher_date'),
                party_name=data.get('payer_name') or data.get('party_name') or '',
                amount=data.get('amount'),
                payment_method=data.get('payment_method') or data.get('debit_account') or 'cash',
                credit_account=data.get('credit_account') or '131',
                reason=data.get('reason') or '',
                party_address=data.get('address') or data.get('party_address') or '',
                party_tax_code=data.get('tax_code') or '',
                reference_document=data.get('reference_document') or data.get('sale_no') or '',
                source_type=data.get('source_type'),
                source_id=data.get('source_id'),
                sale_id=data.get('sale_id'),
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify({'success': True, **result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_vouchers_receipts')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/vouchers/payments', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_vouchers_payments():
        from Services.sme.vouchers import create_payment, list_vouchers
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            if request.method == 'GET':
                branch = (
                    request.args.get('branch')
                    or session.get('sme_branch_filter')
                    or 'ALL'
                )
                rows = list_vouchers(
                    conn,
                    voucher_type='payment',
                    date_from=request.args.get('from') or request.args.get('date_from'),
                    date_to=request.args.get('to') or request.args.get('date_to'),
                    branch_code=branch,
                )
                return jsonify({'success': True, 'data': rows, 'branch_code': branch})
            data = request.get_json(silent=True) or {}
            result = create_payment(
                conn,
                voucher_date=data.get('date') or data.get('voucher_date'),
                party_name=data.get('receiver_name') or data.get('party_name') or '',
                amount=data.get('amount'),
                payment_method=data.get('payment_method') or data.get('credit_account') or 'cash',
                debit_account=data.get('debit_account') or '331',
                reason=data.get('reason') or '',
                party_address=data.get('address') or data.get('party_address') or '',
                party_tax_code=data.get('tax_code') or '',
                reference_document=data.get('reference_document') or data.get('import_no') or '',
                source_type=data.get('source_type'),
                source_id=data.get('source_id'),
                import_id=data.get('import_id') or data.get('import_ref_id'),
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify({'success': True, **result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_vouchers_payments')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/imports', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_imports():
        from Services.sme.stock_vouchers import list_stock_in
        from Services.sme.branches import request_branch_filter
        conn = get_db_connection()
        try:
            rows = list_stock_in(
                conn,
                date_from=request.args.get('start_date') or request.args.get('start'),
                date_to=request.args.get('end_date') or request.args.get('end'),
                branch_code=request_branch_filter(),
                q=request.args.get('q'),
            )
            data = []
            for r in rows:
                amt = float(r.get('total_amount') or 0)
                date_s = r.get('date') or ''
                if date_s and ' ' in str(date_s):
                    date_s = str(date_s).split(' ')[0]
                data.append({
                    'id': r['id'],
                    'import_no': r.get('import_no') or r.get('voucher_no'),
                    'date': date_s,
                    'supplier_name': r.get('supplier_name') or '',
                    'bill_no': r.get('bill_no') or '',
                    'payment_amt': amt,
                    'total_value': amt,
                    'payment_status': r.get('payment_status') or '',
                })
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/stock-in-vouchers', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_stock_in_vouchers():
        """Danh sách phiếu nhập kho cho mẫu 01-VT (nguồn import)."""
        from Services.sme.stock_vouchers import list_stock_in
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            from Services.sme.branches import request_branch_filter
            rows = list_stock_in(
                conn,
                date_from=request.args.get('from') or request.args.get('date_from'),
                date_to=request.args.get('to') or request.args.get('date_to'),
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': rows})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/stock-out-vouchers', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_stock_out_vouchers():
        """Danh sách phiếu xuất kho (nguồn phieu_xuat_kho / bán hàng) cho mẫu 02-VT."""
        from Services.sme.stock_vouchers import list_stock_out
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            from Services.sme.branches import request_branch_filter
            rows = list_stock_out(
                conn,
                date_from=request.args.get('from') or request.args.get('date_from'),
                date_to=request.args.get('to') or request.args.get('date_to'),
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': rows})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # --- Lối tắt nghiệp vụ POS dùng chung (endpoint SME_* để menu/bảo trì tách HKD) ---
    @app.route('/SME_bank_transactions')
    @login_required
    @require_sme_regime
    def SME_bank_transactions():
        return render_template('bank_transactions.html')

    @app.route('/SME_inventory_check')
    @login_required
    @require_sme_regime
    def SME_inventory_check():
        """Legacy HKD → chuyển sang kiểm kê kho 05-VT SME."""
        return redirect(url_for('SME_stock_count'))

    @app.route('/SME_import_details')
    @login_required
    @require_sme_regime
    def SME_import_details():
        return render_template('import_details.html')

    @app.route('/SME_revenue_report')
    @login_required
    @require_sme_regime
    def SME_revenue_report():
        return redirect(url_for('reports'))

    @app.route('/SME_profit_report')
    @login_required
    @require_sme_regime
    def SME_profit_report():
        return redirect(url_for('profit'))

    @app.route('/SME_employees')
    @login_required
    @require_sme_regime
    def SME_employees():
        return redirect(url_for('employees_page'))

    @app.route('/SME_attendance')
    @login_required
    @require_sme_regime
    def SME_attendance():
        return redirect(url_for('attendance_page'))

    @app.route('/SME_salary_create')
    @login_required
    @require_sme_regime
    def SME_salary_create():
        """Lập bảng lương SME (TT99/TT58) — không dùng LapBangLuong / 05-LĐTL HKD."""
        month = request.args.get('month', datetime.now().month, type=int)
        year = request.args.get('year', datetime.now().year, type=int)
        return render_template('KeToanSME/SME_salary.html', month=month, year=year, focus=None)

    @app.route('/api/sme/payroll/runs', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_payroll_runs():
        from Services.sme.payroll import list_payroll_runs
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            return jsonify({
                'success': True,
                'data': list_payroll_runs(conn, branch_code=_sme_branch_arg()),
                'branch_code': _sme_branch_arg(),
            })
        except Exception as e:
            logger.exception('api_sme_payroll_runs')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/payroll/accrue', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_payroll_accrue():
        from Services.sme.payroll import accrue_payroll
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            result = accrue_payroll(
                conn,
                month=int(data.get('month')),
                year=int(data.get('year')),
                records=data.get('records') or [],
                posting_date=data.get('date') or data.get('posting_date'),
                expense_account=data.get('expense_account') or '642',
                branch_code=_sme_branch_arg(),
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify({
                'success': True,
                **result,
                'message': (
                    f"Đã chốt lương T{result['month']}/{result['year']} "
                    f"({result['employee_count']} NV) và ghi sổ {result.get('entry_no') or ''}"
                ),
            })
        except (TypeError, ValueError) as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_payroll_accrue')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/payroll/pay', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_payroll_pay():
        from Services.sme.payroll import pay_payroll_period
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            result = pay_payroll_period(
                conn,
                month=int(data.get('month')),
                year=int(data.get('year')),
                amount=data.get('amount'),
                pay_date=data.get('pay_date') or data.get('date'),
                payment_method=data.get('payment_method') or data.get('pay_method') or 'bank',
                receiver_name=data.get('receiver') or 'Tập thể cán bộ nhân viên',
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify({
                'success': True,
                **result,
                'message': f"Đã lập phiếu chi trả lương {result.get('voucher_no')}",
            })
        except (TypeError, ValueError) as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_payroll_pay')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/payroll/preview', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_payroll_preview():
        try:
            month = int(request.args.get('month', datetime.now().month))
            year = int(request.args.get('year', datetime.now().year))
        except (TypeError, ValueError):
            return jsonify(success=False, message='Tháng hoặc năm không hợp lệ'), 400

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            from Services.sme.payroll import preview_payroll_grid
            result = preview_payroll_grid(conn, month, year)
            return jsonify({
                'success': True,
                'data': result['data'],
                'standard_days': result['standard_days'],
            })
        except Exception as e:
            logger.exception('api_sme_payroll_preview')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/SME_production')
    @login_required
    @require_sme_regime
    def SME_production():
        """Sản xuất & giá thành SME — UI riêng (không dùng KeToanHKD/production)."""
        return render_template('KeToanSME/production.html')

    @app.route('/SME_production/<int:order_id>/print')
    @login_required
    @require_sme_regime
    def SME_production_print(order_id):
        from Services.production_costing import ensure_production_schema, get_production_order
        from Services.sme.production_journal import ensure_production_journal_column
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            ensure_production_schema(conn)
            ensure_production_journal_column(conn, commit=True)
            order = get_production_order(conn, order_id)
            if not order:
                return 'Không tìm thấy phiếu sản xuất', 404
            info = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
            return render_template(
                'KeToanSME/production_print.html',
                order=order,
                info=dict(info) if info else {},
            )
        finally:
            conn.close()

    @app.route('/SME_audit_log')
    @login_required
    @require_sme_regime
    def SME_audit_log():
        return redirect(url_for('audit_log_page'))

    @app.route('/SME_dashboard_warehouse')
    @login_required
    @require_sme_regime
    def SME_dashboard_warehouse():
        return render_template('KeToanSME/dashboard_warehouse.html')

    @app.route('/SME_dashboard_debt')
    @login_required
    @require_sme_regime
    def SME_dashboard_debt():
        return render_template('KeToanSME/dashboard_debt.html')

    @app.route('/SME_PhaiThuCongNhanVien')
    @login_required
    @require_sme_regime
    def SME_PhaiThuCongNhanVien():
        try:
            _bootstrap_sme_db()
        except Exception:
            pass
        return render_template('KeToanSME/employee_receivable.html')

    @app.route('/SME_PhaiTraCongNhanVien')
    @login_required
    @require_sme_regime
    def SME_PhaiTraCongNhanVien():
        """Phải trả NV SME — số dư TK 334 trên sổ kép."""
        try:
            _bootstrap_sme_db()
        except Exception:
            pass
        return render_template('KeToanSME/employee_payable.html')

    @app.route('/SME_dashboard_HRSalary')
    @login_required
    @require_sme_regime
    def SME_dashboard_HRSalary():
        return render_template('KeToanSME/dashboard_HRSalary.html')

    @app.route('/SME_SoSachKeToan')
    @login_required
    @require_sme_regime
    def SME_SoSachKeToan():
        return render_template('KeToanSME/dashboard_sosachketoan.html')

    @app.route('/SME_TSCD')
    @login_required
    @require_sme_regime
    def SME_TSCD():
        return render_template('KeToanSME/dashboard_TSCD.html')

    @app.route('/SME_CCDC')
    @login_required
    @require_sme_regime
    def SME_CCDC():
        return render_template('KeToanSME/dashboard_CCDC.html')

    @app.route('/SME_BCTC')
    @login_required
    @require_sme_regime
    def SME_BCTC():
        return render_template('KeToanSME/dashboard_BCTC.html')

    @app.route('/SME_BCTC/reports')
    @login_required
    @require_sme_regime
    def SME_BCTC_reports():
        return render_template('KeToanSME/bctc_reports.html')

    @app.route('/SME_vat_declaration')
    @login_required
    @require_sme_regime
    def SME_vat_declaration():
        return render_template('KeToanSME/vat_declaration.html')

    @app.route('/SME_form_01_bh')
    @login_required
    @require_sme_regime
    def SME_form_01_bh():
        return render_template('KeToanSME/form_01_bh.html')

    @app.route('/SME_form_02_bh')
    @login_required
    @require_sme_regime
    def SME_form_02_bh():
        return render_template('KeToanSME/form_02_bh.html')

    def _render_sme_cash_book(account_prefix, template_name):
        from Services.sme.cash_books import cash_account_book

        selected_year = request.args.get(
            'year', default=datetime.today().year, type=int,
        )
        selected_account = (
            request.args.get('account') or account_prefix
        ).strip()
        conn = get_db_connection()
        try:
            try:
                info_row = conn.execute(
                    "SELECT * FROM business_info LIMIT 1"
                ).fetchone()
                info = dict(info_row) if info_row else {}
            except sqlite3.Error:
                info = {}
            branch = (
                request.args.get('branch')
                or session.get('sme_branch_filter')
                or 'ALL'
            )
            try:
                book = cash_account_book(
                    conn,
                    fiscal_year=selected_year,
                    account_prefix=account_prefix,
                    account_code=selected_account,
                    branch_code=branch,
                )
            except ValueError as exc:
                abort(400, description=str(exc))
        finally:
            conn.close()
        return render_template(
            template_name,
            book=book,
            info=info,
            year=selected_year,
            current_year_today=datetime.today().year,
            ngay_in=datetime.today().strftime('%d/%m/%Y'),
        )

    @app.route('/SME_SoQuyTienMat')
    @login_required
    @require_sme_regime
    def SME_SoQuyTienMat():
        return _render_sme_cash_book(
            '111', 'KeToanSME/SME_SoQuyTienMat.html',
        )

    @app.route('/SME_SoTienGuiNganHang')
    @login_required
    @require_sme_regime
    def SME_SoTienGuiNganHang():
        return _render_sme_cash_book(
            '112', 'KeToanSME/SME_SoTienGuiNganHang.html',
        )

    @app.route('/api/sme/import/next_sequence', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_import_next_sequence():
        from routes.inventory import _next_import_no_from_db

        payload = request.get_json(silent=True) or {}
        mode = (payload.get('mode') or request.args.get('mode') or 'stock').strip().lower()

        conn = get_db_connection()
        conn.execute('BEGIN IMMEDIATE')
        c = conn.cursor()
        try:
            next_no = _next_import_no_from_db(c, mode)
            conn.commit()
            return jsonify({'success': True, 'next_no': next_no})
        except Exception as e:
            conn.rollback()
            fallback = 'HT000001' if mode == 'service' else 'PN000001'
            return jsonify({
                'success': False,
                'error': str(e),
                'next_no': fallback,
            }), 500
        finally:
            conn.close()

    @app.route('/api/sme/import/<int:import_id>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_import_detail(import_id):
        from Services.import_line_helpers import load_import_for_edit
        from Services.sme.branches import assert_import_in_branch

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            imp = load_import_for_edit(conn, import_id)
            if not imp:
                return jsonify({'error': 'Không tìm thấy phiếu nhập'}), 404

            try:
                assert_import_in_branch(conn, import_id)
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 403

            items_out = []
            for it in imp.get('items') or []:
                pid = it.get('product_id')
                if pid in (None, '', 0, '0'):
                    continue
                qty = float(it.get('qty') or it.get('quantity') or 0)
                returned = float(
                    conn.execute(
                        """
                        SELECT COALESCE(SUM(quantity), 0)
                        FROM return_import
                        WHERE import_id = ? AND product_id = ?
                        """,
                        (import_id, pid),
                    ).fetchone()[0]
                )
                name = (
                    it.get('name')
                    or it.get('product_name')
                    or ''
                ).strip()
                items_out.append({
                    'product_id': pid,
                    'name': name,
                    'product_name': name,
                    'unit': it.get('unit') or 'Cái',
                    'quantity': qty,
                    'buyprice': float(it.get('buyprice') or 0),
                    'discount': float(it.get('discount') or 0),
                    'tax': float(it.get('tax') or 0),
                    'remaining_qty': qty - returned,
                })

            return jsonify({
                'id': imp.get('id'),
                'import_no': imp.get('import_no'),
                'date': imp.get('date'),
                'items': items_out,
            }), 200
        except Exception as exc:
            logger.exception('api_sme_import_detail(%s)', import_id)
            return jsonify({'error': f'Lỗi tải chi tiết phiếu nhập: {exc}'}), 500
        finally:
            conn.close()

    @app.route('/api/sme/import/<int:import_id>/edit', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_import_edit_detail(import_id):
        from Services.import_line_helpers import load_import_for_edit, prepare_import_edit_json
        from Services.sme.branches import assert_import_in_branch

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            imp = load_import_for_edit(conn, import_id)
            if not imp:
                return jsonify({'success': False, 'error': 'Không tìm thấy phiếu nhập'}), 404

            try:
                assert_import_in_branch(conn, import_id)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 403

            return jsonify({
                'success': True,
                'data': prepare_import_edit_json(imp),
            })
        except Exception as exc:
            logger.exception('api_sme_import_edit_detail(%s)', import_id)
            return jsonify({
                'success': False,
                'error': f'Lỗi tải chi tiết phiếu nhập: {exc}',
            }), 500
        finally:
            conn.close()

    @app.route('/api/import_sme', methods=['POST'])
    @app.route('/api/import_sme/<int:edit_import_id>', methods=['PUT'])
    @login_required
    @require_sme_regime
    def api_fb_import_post(edit_import_id=None):
        """Nhập mua SME: HH/VT (kho) + DV/TSCĐ/CCDC; định khoản TT99.
        PUT /api/import_sme/<id> — sửa phiếu (hoàn WAC + thay dòng + replace journals).
        """
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        try:
            from Services.inward_invoice_helpers import ensure_import_service_schema
            from Services.import_line_helpers import (
                ensure_warehouse_schema,
                insert_import_detail_row,
                tracks_retail_inventory,
            )
            from Services.fixed_assets_helpers import (
                count_active_assets_by_import_id,
                delete_assets_by_import_id,
                ensure_fixed_assets_schema,
                register_fixed_asset_from_import,
                register_tool_from_import,
            )
            from Services.inventory_stock_helpers import (
                apply_wac_inbound,
                reverse_import_moves_wac,
                sync_inventory_quantity_from_moves,
                sync_inventory_quantities,
            )
            from Services.sme.import_journal import (
                BUSINESS_TYPE_LABELS,
                sync_import_journals,
            )
            from Services.sme.journal_engine import ensure_sme_journal_ready

            ensure_import_service_schema(conn)
            ensure_warehouse_schema(conn)
            ensure_fixed_assets_schema(conn)
            ensure_sme_journal_ready(conn, commit=False)

            data = request.get_json()
            if not data:
                return jsonify({"error": "Dữ liệu gửi lên không hợp lệ"}), 400

            raw_edit = edit_import_id if edit_import_id is not None else data.get('import_id')
            try:
                edit_id = int(raw_edit) if raw_edit not in (None, '', 0, '0') else None
            except (TypeError, ValueError):
                edit_id = None

            if edit_id:
                from Services.sme.branches import assert_import_in_branch
                try:
                    assert_import_in_branch(conn, edit_id)
                except ValueError as exc:
                    return jsonify({'error': str(exc), 'success': False}), 403

            def _normalize_line_type(raw):
                t = str(raw or 'goods').strip().lower()
                if t in ('raw_materials', 'nvl'):
                    return 'materials'
                if t in ('ready_made', 'hang_hoa', 'finished_goods'):
                    # finished_goods chỉ qua SX — mua vào = hàng hóa (156)
                    return 'goods'
                if t in ('goods', 'materials', 'fixed_asset', 'tools', 'service'):
                    return t
                return 'goods'

            def _normalize_import_type(raw):
                t = str(raw or 'DOMESTIC').strip().upper().replace(' ', '_')
                if t in ('IMPORT', 'IMPORTED', 'IMPORTED_GOODS', 'NK', 'NHAP_KHAU'):
                    return 'IMPORT'
                return 'DOMESTIC'

            import_type = _normalize_import_type(data.get('import_type') or data.get('source_of_goods'))
            currency = (data.get('currency') or ('USD' if import_type == 'IMPORT' else 'VND')).strip().upper()
            try:
                exchange_rate = Decimal(str(data.get('exchange_rate', 1.0) or 1))
            except Exception:
                exchange_rate = Decimal('1.0')
            if import_type != 'IMPORT' or exchange_rate <= 0:
                exchange_rate = Decimal('1.0')
                if import_type != 'IMPORT':
                    currency = 'VND'

            items = data.get('items') or []
            supplier_id = data.get('supplier_id')
            import_date = data.get('date')
            bill_date = data.get('bill_date')
            import_no = data.get('import_no')
            bill_no = data.get('bill_no')
            tax_code = data.get('tax_code')
            note = data.get('note')
            # SME: không dùng extra_cost HKD — CP có HĐ riêng qua Phân bổ chi phí
            extra_cost = Decimal('0.00')
            payment_status_input = data.get('payment_status', 'Chưa thanh toán')
            from_invoice_id = data.get('from_invoice_id')
            default_warehouse = (data.get('warehouse_code') or 'KHO_001').strip()
            doc_type_input = str(data.get('doc_type') or data.get('mode') or 'stock').strip().lower()
            if doc_type_input not in ('stock', 'service'):
                doc_type_input = 'stock'
            # Nếu mọi dòng là dịch vụ → phiếu hạch toán (doc_type=service)
            if items and all(
                _normalize_line_type(
                    i.get('line_type') or i.get('invoice_product_type') or i.get('product_type')
                ) == 'service'
                for i in items
            ):
                doc_type_input = 'service'

            payment_method_raw = str(data.get('payment_method', 'cash')).strip().upper()
            if payment_status_input == 'Chưa thanh toán':
                payment_method = 'CREDIT'
            else:
                payment_method = 'CASH' if payment_method_raw == 'CASH' else 'BANK_TRANSFER'

            po_id = data.get('po_id')
            try:
                po_id = int(po_id) if po_id not in (None, '', 0, '0') else None
            except (TypeError, ValueError):
                po_id = None

            c.execute("SELECT name, address FROM suppliers WHERE id = ?", (supplier_id,))
            sup_row = c.fetchone()
            supplier_name = sup_row['name'] if sup_row else f"NCC ID {supplier_id}"

            # Validate warehouse for HH/VT + khớp chi nhánh session
            from Services.sme.branch_filter import assert_warehouse_in_session_branch
            try:
                if default_warehouse:
                    assert_warehouse_in_session_branch(conn, default_warehouse)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            for item in items:
                lt = _normalize_line_type(
                    item.get('line_type') or item.get('invoice_product_type') or item.get('product_type')
                )
                if tracks_retail_inventory(lt):
                    wh = (item.get('warehouse_code') or default_warehouse or '').strip()
                    if not wh:
                        name = (item.get('name') or item.get('invoice_name') or '').strip() or 'dòng hàng'
                        return jsonify({
                            "error": f'Dòng "{name}" (Hàng hóa/Vật tư) bắt buộc chọn kho.',
                        }), 400
                    try:
                        assert_warehouse_in_session_branch(conn, wh)
                    except ValueError as e:
                        return jsonify({"error": str(e)}), 400

            total_base_vnd = Decimal('0.00')
            for i in items:
                qty = round_money(i.get('qty', 0))
                price_vnd = round_money(round_money(i.get('buyprice', 0)) * exchange_rate)
                total_base_vnd += round_money(qty * price_vnd)
            total_base_safe = total_base_vnd if total_base_vnd > 0 else Decimal('1.00')

            c.execute('PRAGMA table_info(import)')
            import_cols = {col[1] for col in c.fetchall()}
            pay_method_db = (
                'cash' if payment_method == 'CASH'
                else ('bank' if payment_method == 'BANK_TRANSFER' else None)
            )

            if edit_id:
                c.execute("SELECT * FROM import WHERE id = ?", (edit_id,))
                old_imp = c.fetchone()
                if not old_imp:
                    return jsonify({"error": f"Không tìm thấy phiếu nhập #{edit_id}"}), 404
                import_id = int(edit_id)
                import_no = old_imp['import_no'] or import_no

                try:
                    c.execute(
                        "SELECT COUNT(*) AS cnt FROM return_import WHERE import_id = ?",
                        (import_id,),
                    )
                    if int(c.fetchone()['cnt'] or 0) > 0:
                        return jsonify({
                            "error": "Phiếu nhập đã phát sinh trả hàng NCC, không thể sửa.",
                        }), 403
                except sqlite3.Error:
                    pass

                fa_act, tools_act = count_active_assets_by_import_id(c, import_id)
                if fa_act or tools_act:
                    return jsonify({
                        "error": "Không thể sửa: TSCĐ/CCDC từ phiếu nhập đã đưa vào sử dụng.",
                    }), 403

                # Chặn sửa nếu đã có xuất kho sau phiếu nhập (cùng SP)
                c.execute(
                    """
                    SELECT DISTINCT d.product_id
                    FROM import_details d
                    WHERE d.import_id = ? AND d.product_id IS NOT NULL
                    """,
                    (import_id,),
                )
                old_pids = [r['product_id'] for r in c.fetchall() if r['product_id']]
                if old_pids:
                    outbound_types = (
                        'export', 'sale', 'SALE', 'adjustment_out', 'ADJUSTMENT_OUT',
                        'RETURN_IMPORT',
                    )
                    ph_p = ','.join('?' * len(old_pids))
                    ph_t = ','.join('?' * len(outbound_types))
                    c.execute(
                        f"""
                        SELECT COUNT(*) AS cnt FROM stock_moves
                        WHERE product_id IN ({ph_p})
                          AND type IN ({ph_t})
                          AND date >= ?
                          AND NOT (type = 'import' AND ref_id = ?)
                        """,
                        (*old_pids, *outbound_types, str(old_imp['date'] or '')[:10], import_id),
                    )
                    if int((c.fetchone()['cnt'] or 0)) > 0:
                        return jsonify({
                            "error": "Không thể sửa: hàng đã phát sinh xuất/bán sau phiếu nhập này.",
                        }), 403

                sync_pids = set(old_pids)
                reverse_import_moves_wac(c, import_id)
                c.execute("DELETE FROM import_details WHERE import_id = ?", (import_id,))
                c.execute(
                    "DELETE FROM stock_moves WHERE ref_id = ? AND type IN ('import', 'RETURN_IMPORT')",
                    (import_id,),
                )
                c.execute(
                    "DELETE FROM inventory_transactions WHERE reference_id = ? AND reference_type = 'import'",
                    (import_id,),
                )
                try:
                    c.execute("DELETE FROM chi_tiet_phieu_nhap_kho WHERE import_id = ?", (import_id,))
                except sqlite3.Error:
                    pass
                delete_assets_by_import_id(c, import_id)
                if sync_pids:
                    sync_inventory_quantities(c, list(sync_pids))

                set_parts = [
                    'date = ?', 'supplier_id = ?', 'bill_no = ?', 'bill_date = ?',
                    'note = ?', 'payment_status = ?', 'extra_cost = ?',
                    'total_value = ?', 'paid_amount = ?',
                ]
                set_vals = [
                    import_date, supplier_id, bill_no, bill_date,
                    note, payment_status_input, float(extra_cost), 0, 0,
                ]
                if 'warehouse_code' in import_cols:
                    set_parts.append('warehouse_code = ?')
                    set_vals.append(default_warehouse)
                if 'from_invoice_id' in import_cols and from_invoice_id:
                    set_parts.append('from_invoice_id = ?')
                    set_vals.append(int(from_invoice_id))
                if 'payment_method' in import_cols:
                    set_parts.append('payment_method = ?')
                    set_vals.append(pay_method_db)
                if 'import_type' in import_cols:
                    set_parts.append('import_type = ?')
                    set_vals.append(import_type)
                if 'currency' in import_cols:
                    set_parts.append('currency = ?')
                    set_vals.append(currency)
                if 'exchange_rate' in import_cols:
                    set_parts.append('exchange_rate = ?')
                    set_vals.append(float(exchange_rate))
                if 'import_tax_amount' in import_cols:
                    set_parts.append('import_tax_amount = ?')
                    set_vals.append(0)
                if 'doc_type' in import_cols:
                    set_parts.append('doc_type = ?')
                    set_vals.append(doc_type_input)
                set_vals.append(import_id)
                c.execute(
                    f"UPDATE import SET {', '.join(set_parts)} WHERE id = ?",
                    set_vals,
                )
            else:
                import_fields = [
                    'date', 'supplier_id', 'import_no', 'bill_no', 'bill_date',
                    'note', 'payment_status', 'extra_cost', 'total_value', 'paid_amount',
                ]
                import_values = [
                    import_date, supplier_id, import_no, bill_no, bill_date,
                    note, payment_status_input, float(extra_cost), 0, 0,
                ]
                if 'warehouse_code' in import_cols:
                    import_fields.append('warehouse_code')
                    import_values.append(default_warehouse)
                if 'from_invoice_id' in import_cols and from_invoice_id:
                    import_fields.append('from_invoice_id')
                    import_values.append(int(from_invoice_id))
                if 'payment_method' in import_cols:
                    import_fields.append('payment_method')
                    import_values.append(pay_method_db)
                if 'import_type' in import_cols:
                    import_fields.append('import_type')
                    import_values.append(import_type)
                if 'currency' in import_cols:
                    import_fields.append('currency')
                    import_values.append(currency)
                if 'exchange_rate' in import_cols:
                    import_fields.append('exchange_rate')
                    import_values.append(float(exchange_rate))
                if 'import_tax_amount' in import_cols:
                    import_fields.append('import_tax_amount')
                    import_values.append(0)
                if 'doc_type' in import_cols:
                    import_fields.append('doc_type')
                    import_values.append(doc_type_input)

                placeholders = ', '.join(['?'] * len(import_fields))
                c.execute(
                    f'INSERT INTO import ({", ".join(import_fields)}) VALUES ({placeholders})',
                    import_values,
                )
                import_id = c.lastrowid

            c.execute('PRAGMA table_info(stock_moves)')
            sm_cols = {col[1] for col in c.fetchall()}

            p_ids = [i.get('product_id') for i in items if i.get('product_id')]
            p_map = {}
            if p_ids:
                ph = ','.join(['?'] * len(p_ids))
                c.execute(
                    f"SELECT id, name, unit, unit1, unit_ratio, barcode, product_code, product_type "
                    f"FROM products WHERE id IN ({ph})",
                    p_ids,
                )
                p_map = {row['id']: row for row in c.fetchall()}

            items_for_json = []
            fixed_assets_created = 0
            tools_created = 0
            total_import_tax_vnd = Decimal('0.00')

            for item in items:
                line_type = _normalize_line_type(
                    item.get('line_type')
                    or item.get('invoice_product_type')
                    or item.get('product_type')
                )
                warehouse_code = (item.get('warehouse_code') or default_warehouse or 'KHO_001').strip()
                if not tracks_retail_inventory(line_type):
                    warehouse_code = warehouse_code or 'KHO_001'

                qty_in = round_money(item.get('qty', 0))
                if qty_in <= 0:
                    continue

                price_original = round_money(item.get('buyprice', 0))
                price_vnd = round_money(price_original * exchange_rate)
                tax_p = Decimal(str(item.get('tax_pct', 0) or 0))
                disc_p = Decimal(str(item.get('discount_pct', 0) or item.get('discountPct', 0) or 0))
                import_tax_p = (
                    Decimal(str(item.get('import_tax_pct', 0) or 0))
                    if import_type == 'IMPORT'
                    else Decimal('0.00')
                )
                unit_in = str(item.get('unit') or 'Cái').strip()
                inv_name = (
                    item.get('invoice_name')
                    or item.get('name')
                    or ''
                ).strip()

                line_subtotal_vnd = round_money(qty_in * price_vnd)
                line_disc_vnd = round_money(line_subtotal_vnd * (disc_p / Decimal('100.00')))
                line_net_vnd = line_subtotal_vnd - line_disc_vnd
                line_import_tax_vnd = Decimal('0.00')
                if import_type == 'IMPORT' and import_tax_p > 0 and line_type != 'service':
                    line_import_tax_vnd = round_money(line_net_vnd * (import_tax_p / Decimal('100.00')))
                total_import_tax_vnd += line_import_tax_vnd
                line_extra_vnd = round_money((line_subtotal_vnd / total_base_safe) * extra_cost)
                tax_base_vnd = line_net_vnd + line_import_tax_vnd if import_type == 'IMPORT' else line_net_vnd
                line_vat_vnd = round_money(tax_base_vnd * (tax_p / Decimal('100.00')))
                line_inventory_value_vnd = line_net_vnd + line_import_tax_vnd + line_extra_vnd
                # Tổng thanh toán hiển thị: hàng + VAT + CP khác (+ thuế NK nếu nhập khẩu)
                if import_type == 'IMPORT':
                    line_total_payment_vnd = line_net_vnd + line_import_tax_vnd + line_vat_vnd + line_extra_vnd
                else:
                    line_total_payment_vnd = line_net_vnd + line_vat_vnd + line_extra_vnd

                _lt_to_biz = {
                    'materials': 'NHAP_KHO_NVL',
                    'service': 'MUA_DICH_VU',
                    'fixed_asset': 'MUA_TSCD',
                    'tools': 'MUA_CCDC',
                }
                desc_label = BUSINESS_TYPE_LABELS.get(
                    _lt_to_biz.get(line_type, 'NHAP_KHO_HANG_HOA'), line_type
                )

                if line_type == 'service':
                    insert_import_detail_row(c, import_id, {
                        'import_id': import_id,
                        'product_id': None,
                        'qty': float(qty_in),
                        'buyprice': float(price_original),
                        'subtotal': float(line_subtotal_vnd),
                        'discount': float(line_disc_vnd),
                        'tax': float(line_vat_vnd),
                        'cost_price': float(line_total_payment_vnd / qty_in) if qty_in else 0,
                        'tax_pct': float(tax_p),
                        'discount_pct': float(disc_p),
                        'payment_amt': float(line_total_payment_vnd),
                        'product_name': inv_name or 'Dịch vụ',
                        'unit': unit_in,
                        'line_type': 'service',
                        'warehouse_code': warehouse_code,
                    })
                    items_for_json.append({
                        'product_name': inv_name or 'Dịch vụ',
                        'unit': unit_in,
                        'qty': float(qty_in),
                        'buyprice': float(price_original),
                        'discount_pct': float(disc_p),
                        'tax_pct': float(tax_p),
                        'line_type': 'service',
                        'invoice_product_type': 'service',
                        'warehouse_code': warehouse_code,
                        'line_total': float(line_total_payment_vnd),
                    })
                    continue

                pid = item.get('product_id')
                p_info = p_map.get(pid)
                if not p_info:
                    continue

                if inv_name:
                    try:
                        c.execute(
                            """
                            INSERT OR IGNORE INTO product_aliases (product_id, invoice_name, supplier_id)
                            VALUES (?, ?, ?)
                            """,
                            (pid, inv_name, supplier_id),
                        )
                    except sqlite3.Error:
                        pass

                product_name = p_info['name']
                retail_unit = str(p_info['unit'] or 'Cái').strip()
                wholesale_unit = str(p_info['unit1'] or '').strip().lower()
                ratio = Decimal(str(p_info['unit_ratio'] or 1))
                unit_in_lower = unit_in.lower()
                is_wholesale = bool(wholesale_unit and unit_in_lower == wholesale_unit)
                qty_retail = qty_in * ratio if is_wholesale else qty_in
                cost_per_retail = (
                    round_money(line_inventory_value_vnd / qty_retail)
                    if qty_retail > 0
                    else Decimal('0.00')
                )

                items_for_json.append({
                    'product_id': pid,
                    'product_name': product_name,
                    'barcode': p_info['barcode'] or '',
                    'product_code': p_info['product_code'] or '',
                    'unit': item.get('unit'),
                    'qty': float(qty_in),
                    'buyprice': float(price_original),
                    'discount_pct': float(disc_p),
                    'tax_pct': float(tax_p),
                    'import_tax_pct': float(import_tax_p),
                    'line_type': line_type,
                    'invoice_product_type': line_type,
                    'warehouse_code': warehouse_code,
                    'line_total': float(line_total_payment_vnd),
                })

                insert_import_detail_row(c, import_id, {
                    'import_id': import_id,
                    'product_id': pid,
                    'qty': float(qty_in),
                    'buyprice': float(price_original),
                    'subtotal': float(line_subtotal_vnd),
                    'discount': float(line_disc_vnd),
                    'tax': float(line_vat_vnd),
                    'cost_price': float(cost_per_retail),
                    'unit_type': 1 if is_wholesale else 0,
                    'tax_pct': float(tax_p),
                    'discount_pct': float(disc_p),
                    'payment_amt': float(line_total_payment_vnd),
                    'product_name': product_name,
                    'unit': unit_in,
                    'line_type': line_type,
                    'warehouse_code': warehouse_code,
                })
                detail_id = c.lastrowid

                if line_type == 'fixed_asset':
                    register_fixed_asset_from_import(
                        c,
                        import_id=import_id,
                        import_detail_id=detail_id,
                        product_id=pid,
                        product_code=p_info['product_code'] or '',
                        product_name=product_name,
                        import_no=import_no,
                        import_date=import_date,
                        warehouse_code=warehouse_code,
                        qty=float(qty_in),
                        buyprice=float(price_original),
                        tax_amount=float(line_vat_vnd),
                        discount_amount=float(line_disc_vnd),
                        line_total=float(line_total_payment_vnd),
                        subtotal=float(line_subtotal_vnd),
                    )
                    fixed_assets_created += 1
                elif line_type == 'tools':
                    register_tool_from_import(
                        c,
                        import_id=import_id,
                        import_detail_id=detail_id,
                        product_id=pid,
                        product_code=p_info['product_code'] or '',
                        product_name=product_name,
                        import_no=import_no,
                        import_date=import_date,
                        warehouse_code=warehouse_code,
                        qty=float(qty_in),
                        buyprice=float(price_original),
                        tax_amount=float(line_vat_vnd),
                        line_total=float(line_total_payment_vnd),
                        subtotal=float(line_subtotal_vnd),
                        discount_amount=float(line_disc_vnd),
                    )
                    tools_created += 1

                if tracks_retail_inventory(line_type):
                    params_detail = (
                        import_id, pid, float(qty_in), float(price_original), float(line_subtotal_vnd),
                        float(line_disc_vnd), float(line_vat_vnd), float(cost_per_retail),
                        1 if is_wholesale else 0, float(tax_p), float(disc_p),
                    )
                    try:
                        c.execute(
                            "INSERT INTO chi_tiet_phieu_nhap_kho "
                            "(import_id, product_id, quantity, buyprice, subtotal, discount_amount, "
                            "tax_amount, cost_price, unit_type, tax_pct, discount_pct) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            params_detail,
                        )
                    except sqlite3.Error:
                        pass

                    apply_wac_inbound(c, pid, float(qty_retail), float(line_inventory_value_vnd))
                    move_note = (
                        f"Nhập kho từ {supplier_name} ({desc_label}: {product_name}) — {warehouse_code}"
                    )
                    _insert_sme_import_stock_move(
                        c,
                        sm_cols,
                        product_id=pid,
                        import_date=import_date,
                        import_id=import_id,
                        qty=qty_retail,
                        cost_per_retail=cost_per_retail,
                        move_note=move_note,
                        import_no=import_no,
                        retail_unit=retail_unit,
                        wholesale_unit=wholesale_unit,
                        ratio=ratio,
                        warehouse_code=warehouse_code,
                    )
                    try:
                        c.execute(
                            """
                            INSERT INTO inventory_transactions
                            (product_id, type, type1, quantity, cost_price, reference_id, reference_type, note, created_at)
                            VALUES (?, 'import', 'Nhập', ?, ?, ?, 'import', ?, ?)
                            """,
                            (
                                pid, float(qty_retail), float(cost_per_retail), import_id,
                                f"Nhập kho - PN#{import_no} ({warehouse_code})", import_date,
                            ),
                        )
                    except sqlite3.Error:
                        pass
                    sync_inventory_quantity_from_moves(c, pid)

            if not items_for_json:
                raise ValueError('Không có dòng hàng hợp lệ để lưu phiếu nhập.')

            total_overall_payment_vnd = sum(
                round_money(x.get('line_total') or 0) for x in items_for_json
            )
            total_final_float = float(total_overall_payment_vnd)
            final_paid = total_final_float if payment_status_input == 'Đã thanh toán' else 0.0
            update_parts = ['total_value = ?', 'paid_amount = ?']
            update_vals = [total_final_float, final_paid]
            if 'import_tax_amount' in import_cols:
                update_parts.append('import_tax_amount = ?')
                update_vals.append(float(total_import_tax_vnd))
            update_vals.append(import_id)
            c.execute(
                f"UPDATE import SET {', '.join(update_parts)} WHERE id = ?",
                update_vals,
            )

            items_json_str = json.dumps(items_for_json, ensure_ascii=False)
            if edit_id:
                c.execute("DELETE FROM phieu_nhap_kho WHERE import_id = ?", (import_id,))
            c.execute(
                """
                INSERT INTO phieu_nhap_kho (
                    import_no, date, bill_no, bill_date, supplier_name, supplier_id,
                    items_json, total_amount, import_id, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    import_no, import_date, bill_no, bill_date, supplier_name, supplier_id,
                    items_json_str, total_final_float, import_id, note,
                ),
            )

            journal_result = sync_import_journals(
                conn,
                import_id,
                accounting_regime='SME',
                created_by=(session.get('user') or {}).get('username'),
                replace_existing=bool(edit_id),
                payment_method=payment_method,
                import_type=import_type,
                import_tax_amount=total_import_tax_vnd,
                exchange_rate=exchange_rate,
            )
            accounting_tx_ids = list(journal_result.get('entry_ids') or [])

            # Cập nhật trạng thái HĐ đầu vào
            if from_invoice_id:
                try:
                    inv_status = 'accounted' if doc_type_input == 'service' else 'imported'
                    c.execute(
                        "UPDATE supplier_invoice SET status = ? WHERE id = ?",
                        (inv_status, int(from_invoice_id)),
                    )
                except sqlite3.Error:
                    pass
            elif bill_no:
                try:
                    inv_status = 'accounted' if doc_type_input == 'service' else 'imported'
                    if tax_code:
                        c.execute(
                            """
                            UPDATE supplier_invoice SET status = ?
                            WHERE TRIM(COALESCE(invoice_no, '')) = ?
                              AND TRIM(COALESCE(seller_tax_code, '')) = ?
                              AND COALESCE(status, '') NOT IN ('imported', 'accounted')
                            """,
                            (inv_status, str(bill_no).strip(), str(tax_code).strip()),
                        )
                    else:
                        c.execute(
                            """
                            UPDATE supplier_invoice SET status = ?
                            WHERE TRIM(COALESCE(invoice_no, '')) = ?
                              AND COALESCE(status, '') NOT IN ('imported', 'accounted')
                            """,
                            (inv_status, str(bill_no).strip()),
                        )
                except sqlite3.Error:
                    pass

            po_result = None
            if po_id and not edit_id:
                from Services.sme.purchase_order import apply_po_receipt
                receipt_lines = []
                for i in items:
                    receipt_lines.append({
                        'product_id': i.get('product_id'),
                        'product_name': i.get('name') or i.get('invoice_name'),
                        'qty': i.get('qty'),
                    })
                po_result = apply_po_receipt(
                    conn, po_id, receipt_lines, import_id=import_id,
                )

            conn.commit()
            return jsonify({
                "success": True,
                "edited": bool(edit_id),
                "import_id": import_id,
                "voucher_no": import_no,
                "total_payment_vnd": total_final_float,
                "journal_entry_ids": accounting_tx_ids,
                "accounting_tx_ids": accounting_tx_ids,
                "journal_sync": journal_result,
                "fixed_assets_created": fixed_assets_created,
                "tools_created": tools_created,
                "purchase_order": po_result,
                "currency": currency,
                "tax_code": tax_code,
            })

        except Exception as e:
            conn.rollback()
            logging.error(f"LỖI import_sme: {str(e)}", exc_info=True)
            return jsonify({"error": f"Lỗi xử lý: {str(e)}"}), 500
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # Phân bổ chi phí mua hàng (landed cost) — SME
    # --------------------------------------------------------------------------
    @app.route('/api/sme/landed-cost/invoice/<int:invoice_id>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_landed_cost_invoice(invoice_id):
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            from Services.sme.landed_cost import get_cost_invoice_summary
            data = get_cost_invoice_summary(conn, invoice_id)
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()

    @app.route('/api/sme/landed-cost/targets', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_landed_cost_targets():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            from Services.sme.landed_cost import list_eligible_target_imports
            from Services.sme.branches import request_branch_filter
            rows = list_eligible_target_imports(
                conn,
                scope=request.args.get('scope') or 'all',
                keyword=request.args.get('q') or request.args.get('keyword'),
                limit=int(request.args.get('limit') or 50),
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': rows})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()

    @app.route('/api/sme/landed-cost/preview', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_landed_cost_preview():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            from Services.sme.branches import request_branch_filter
            from Services.sme.landed_cost import preview_allocation
            data = request.get_json() or {}
            result = preview_allocation(
                conn,
                invoice_id=int(data.get('invoice_id') or 0),
                target_import_ids=data.get('target_import_ids') or [],
                scope=data.get('scope'),
                cost_category=data.get('cost_category'),
                target_detail_ids=data.get('target_detail_ids'),
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': result})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()

    @app.route('/api/sme/landed-cost/allocate', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_landed_cost_allocate():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            from Services.sme.branches import request_branch_filter
            from Services.sme.landed_cost import allocate_landed_cost
            data = request.get_json() or {}
            invoice_id = int(data.get('invoice_id') or data.get('cost_invoice_id') or 0)
            if not invoice_id:
                return jsonify({'success': False, 'error': 'Thiếu invoice_id'}), 400
            result = allocate_landed_cost(
                conn,
                invoice_id=invoice_id,
                target_import_ids=data.get('target_import_ids') or [],
                scope=data.get('scope'),
                cost_category=data.get('cost_category'),
                target_detail_ids=data.get('target_detail_ids'),
                payment_status=data.get('payment_status') or 'Chưa thanh toán',
                payment_method=data.get('payment_method'),
                note=data.get('note'),
                created_by=(session.get('user') or {}).get('username')
                or session.get('user_name')
                or session.get('username'),
                commit=True,
                branch_code=request_branch_filter(),
            )
            return jsonify(result)
        except Exception as e:
            conn.rollback()
            logging.error('LỖI landed_cost allocate: %s', e, exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()

    @app.route('/api/sme/landed-cost/reverse', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_landed_cost_reverse():
        """Hủy phân bổ sai → mở lại HĐ để phân bổ lại."""
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            from Services.sme.landed_cost import reverse_landed_cost
            data = request.get_json() or {}
            invoice_id = data.get('invoice_id') or data.get('cost_invoice_id')
            landed_cost_id = data.get('landed_cost_id')
            result = reverse_landed_cost(
                conn,
                invoice_id=int(invoice_id) if invoice_id not in (None, '', 0, '0') else None,
                landed_cost_id=(
                    int(landed_cost_id) if landed_cost_id not in (None, '', 0, '0') else None
                ),
                created_by=(session.get('user') or {}).get('username')
                or session.get('user_name')
                or session.get('username'),
                reason=data.get('reason') or 'Hủy phân bổ chi phí để sửa lại',
                commit=True,
            )
            return jsonify(result)
        except Exception as e:
            conn.rollback()
            logging.error('LỖI landed_cost reverse: %s', e, exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()

    @app.route('/api/sme/invoices/inward/manual', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_inward_invoice_manual():
        """Nhập tay HĐ CP mua hàng (vận chuyển nước ngoài / không có HĐĐT)."""
        conn = get_db_connection()
        try:
            from Services.inward_invoice_helpers import create_manual_supplier_invoice
            data = request.get_json() or {}
            created = create_manual_supplier_invoice(conn, data)
            conn.commit()
            return jsonify({'success': True, 'data': created})
        except Exception as e:
            conn.rollback()
            logging.error('LỖI tạo HĐ nhập tay: %s', e, exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()

    @app.route('/api/sme/invoices/inward', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_invoices_inward_list():
        """Danh sách HĐ mua — lọc CN (ủy quyền logic /api/invoices/inward)."""
        view = app.view_functions.get('get_inward_invoices')
        if not view:
            return jsonify({'success': False, 'error': 'API HĐ mua chưa sẵn sàng'}), 500
        return view()

    # --------------------------------------------------------------------------
    # Danh mục tài khoản SME (TT99) — tách biệt HKD
    # --------------------------------------------------------------------------
    @app.route('/SME_chart_of_accounts')
    @login_required
    @require_sme_regime
    def SME_chart_of_accounts():
        try:
            _bootstrap_sme_db()
        except Exception:
            logger.exception('SME bootstrap on COA page')
        return render_template('KeToanSME/chart_of_accounts.html')

    @app.route('/SME_journal')
    @login_required
    @require_sme_regime
    def SME_journal():
        try:
            _bootstrap_sme_db()
        except Exception:
            logger.exception('SME bootstrap on journal page')
        return render_template('KeToanSME/journal.html')

    @app.route('/SME_general_ledger')
    @login_required
    @require_sme_regime
    def SME_general_ledger():
        try:
            _bootstrap_sme_db()
        except Exception:
            logger.exception('SME bootstrap on ledger page')
        return render_template('KeToanSME/general_ledger.html')

    @app.route('/api/sme/coa', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_coa_list():
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import account_tree, ensure_sme_coa_ready
            meta = ensure_sme_coa_ready(conn)
            q = (request.args.get('q') or '').strip() or None
            active = request.args.get('active', '1') != '0'
            if q:
                from Services.sme.coa_service import list_accounts
                rows = list_accounts(conn, active_only=active, q=q)
            else:
                rows = account_tree(conn, active_only=active)
            return jsonify({
                'success': True,
                'data': rows,
                'meta': meta,
            })
        except Exception as e:
            logger.exception('api_sme_coa_list')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/coa/<code>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_coa_get(code):
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import get_account, list_children, ensure_sme_coa_ready
            ensure_sme_coa_ready(conn)
            acc = get_account(conn, code)
            if not acc:
                return jsonify({'success': False, 'error': 'Không tìm thấy tài khoản'}), 404
            return jsonify({
                'success': True,
                'data': acc,
                'children': list_children(conn, code),
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/coa/<parent_code>/suggest-child', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_coa_suggest_child(parent_code):
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import suggest_next_child_code, ensure_sme_coa_ready
            ensure_sme_coa_ready(conn)
            return jsonify({
                'success': True,
                'next_code': suggest_next_child_code(conn, parent_code),
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        finally:
            conn.close()

    @app.route('/api/sme/coa/children', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_coa_create_child():
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import create_child_account, ensure_sme_coa_ready
            ensure_sme_coa_ready(conn)
            created = create_child_account(
                conn,
                parent_code=(payload.get('parent_code') or '').strip(),
                code=(payload.get('code') or '').strip() or None,
                name=(payload.get('name') or '').strip(),
                custom_reason=(payload.get('custom_reason') or '').strip(),
                tracks=payload.get('tracks') or None,
                bctc_line_code=payload.get('bctc_line_code'),
                description=(payload.get('description') or '').strip(),
            )
            return jsonify({'success': True, 'data': created})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            logger.exception('api_sme_coa_create_child')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/coa/<code>', methods=['PUT'])
    @login_required
    @require_sme_regime
    def api_sme_coa_update(code):
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import update_account_meta, ensure_sme_coa_ready
            ensure_sme_coa_ready(conn)
            updated = update_account_meta(
                conn,
                code,
                name=payload.get('name'),
                description=payload.get('description'),
                bctc_line_code=payload.get('bctc_line_code'),
                tracks=payload.get('tracks'),
            )
            return jsonify({'success': True, 'data': updated})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/coa/<code>/deactivate', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_coa_deactivate(code):
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import deactivate_account, ensure_sme_coa_ready
            ensure_sme_coa_ready(conn)
            data = deactivate_account(conn, code)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/coa/reseed', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_coa_reseed():
        """Nạp lại seed hệ thống (giữ tài khoản custom của DN)."""
        conn = get_db_connection()
        try:
            from Services.sme.coa_service import ensure_sme_coa_ready
            meta = ensure_sme_coa_ready(conn, force_reseed=True)
            return jsonify({'success': True, 'meta': meta})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # Nhật ký / bút toán SME
    # --------------------------------------------------------------------------
    @app.route('/api/sme/journal/ready', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_journal_ready():
        conn = get_db_connection()
        try:
            from Services.sme.bootstrap import ensure_sme_accounting_ready
            meta = ensure_sme_accounting_ready(conn)
            return jsonify({'success': True, **meta})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/journal', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_journal_list():
        conn = get_db_connection()
        try:
            from Services.sme.journal_engine import list_journal_entries
            doc_type = (request.args.get('document_type') or '').strip() or None
            doc_id = request.args.get('document_id', type=int)
            status = (request.args.get('status') or '').strip() or None
            date_from = (request.args.get('date_from') or '').strip() or None
            date_to = (request.args.get('date_to') or '').strip() or None
            q = (request.args.get('q') or '').strip() or None
            limit = request.args.get('limit', default=50, type=int) or 50
            offset = request.args.get('offset', default=0, type=int) or 0
            branch = (
                request.args.get('branch')
                or session.get('sme_branch_filter')
                or 'ALL'
            )
            rows = list_journal_entries(
                conn,
                document_type=doc_type,
                document_id=doc_id,
                status=status,
                date_from=date_from,
                date_to=date_to,
                q=q,
                branch_code=branch,
                limit=min(max(limit, 1), 200),
                offset=max(offset, 0),
            )
            return jsonify({
                'success': True, 'data': rows, 'count': len(rows),
                'branch_code': branch,
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/journal/<int:entry_id>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_journal_get(entry_id):
        conn = get_db_connection()
        try:
            from Services.sme.journal_engine import get_journal_entry
            data = get_journal_entry(conn, entry_id)
            if not data:
                return jsonify({'success': False, 'error': 'Không tìm thấy bút toán'}), 404
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/journal/<int:entry_id>/reverse', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_journal_reverse(entry_id):
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.branch_filter import assert_row_in_branch
            from Services.sme.journal_engine import reverse_journal_entry
            assert_row_in_branch(
                conn, 'sme_journal_entries', entry_id, label='Bút toán',
            )
            rev = reverse_journal_entry(
                conn,
                entry_id,
                posting_date=(payload.get('posting_date') or None),
                created_by=(session.get('user') or {}).get('username') or session.get('user_name'),
                reason=(payload.get('reason') or 'Đảo bút toán'),
            )
            conn.commit()
            return jsonify({'success': True, 'data': rev})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/posting-rules', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_posting_rules():
        conn = get_db_connection()
        try:
            from Services.sme.journal_engine import ensure_sme_journal_ready
            ensure_sme_journal_ready(conn)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM sme_posting_rules WHERE active = 1 ORDER BY business_type, payment_method"
            ).fetchall()
            return jsonify({'success': True, 'data': [dict(r) for r in rows]})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # Sổ cái / cân đối phát sinh
    # --------------------------------------------------------------------------
    @app.route('/api/sme/trial-balance', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_trial_balance():
        conn = get_db_connection()
        try:
            from Services.sme.general_ledger import trial_balance
            year = request.args.get('year', type=int) or datetime.now().year
            period_from = request.args.get('period_from', type=int) or 1
            period_to = request.args.get('period_to', type=int) or period_from
            include_zero = request.args.get('include_zero', '1') in ('1', 'true', 'True')
            postable_only = request.args.get('postable_only', '0') in ('1', 'true', 'True')
            branch = (
                request.args.get('branch')
                or session.get('sme_branch_filter')
                or 'ALL'
            )
            data = trial_balance(
                conn,
                fiscal_year=year,
                period_from=period_from,
                period_to=period_to,
                postable_only=postable_only,
                include_zero=include_zero,
                branch_code=branch,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/ledger/accounts', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_ledger_accounts():
        """Danh sách Mã Tài Khoản có phát sinh — chọn nhanh sổ cái chi tiết."""
        conn = get_db_connection()
        try:
            from Services.sme.general_ledger import accounts_with_activity
            date_from = (request.args.get('date_from') or '').strip() or None
            date_to = (request.args.get('date_to') or '').strip() or None
            rows = accounts_with_activity(conn, date_from=date_from, date_to=date_to)
            return jsonify({'success': True, 'data': {'accounts': rows, 'count': len(rows)}})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/ledger/<account_code>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_account_ledger(account_code):
        conn = get_db_connection()
        try:
            from Services.sme.general_ledger import account_ledger, period_bounds
            year = request.args.get('year', type=int) or datetime.now().year
            period = request.args.get('period', type=int)
            date_from = (request.args.get('date_from') or '').strip()
            date_to = (request.args.get('date_to') or '').strip()
            if not date_from or not date_to:
                if period:
                    date_from, date_to = period_bounds(year, period)
                else:
                    date_from, date_to = f'{year}-01-01', f'{year}-12-31'
            branch = (
                request.args.get('branch')
                or session.get('sme_branch_filter')
                or 'ALL'
            )
            data = account_ledger(
                conn, account_code,
                date_from=date_from, date_to=date_to,
                branch_code=branch,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # BCTC (B01 / B02)
    # --------------------------------------------------------------------------
    @app.route('/api/sme/bctc/b01', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_bctc_b01():
        conn = get_db_connection()
        try:
            from Services.sme.bctc_report import balance_sheet
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            include_profit = request.args.get('include_current_profit', '1') not in ('0', 'false', 'False')
            data = balance_sheet(
                conn,
                fiscal_year=year,
                period_to=period_to,
                include_current_profit=include_profit,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bctc/b02', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_bctc_b02():
        conn = get_db_connection()
        try:
            from Services.sme.bctc_report import income_statement
            year = request.args.get('year', type=int) or datetime.now().year
            period_from = request.args.get('period_from', type=int) or 1
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = income_statement(
                conn,
                fiscal_year=year,
                period_from=period_from,
                period_to=period_to,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bctc/b03', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_bctc_b03():
        conn = get_db_connection()
        try:
            from Services.sme.bctc_report import cash_flow_statement
            year = request.args.get('year', type=int) or datetime.now().year
            period_from = request.args.get('period_from', type=int) or 1
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = cash_flow_statement(
                conn,
                fiscal_year=year,
                period_from=period_from,
                period_to=period_to,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bctc/b09', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_bctc_b09():
        conn = get_db_connection()
        try:
            from Services.sme.b09_notes import notes_to_financial_statements
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = notes_to_financial_statements(
                conn,
                fiscal_year=year,
                period_to=period_to,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/SME_auto_posting')
    @login_required
    @require_sme_regime
    def SME_auto_posting():
        try:
            _bootstrap_sme_db()
        except Exception:
            logger.exception('SME bootstrap on auto posting page')
        return render_template('KeToanSME/auto_posting.html')

    @app.route('/SME_tax_nsnn')
    @login_required
    @require_sme_regime
    def SME_tax_nsnn():
        try:
            _bootstrap_sme_db()
        except Exception:
            logger.exception('SME bootstrap tax page')
        return render_template('KeToanSME/tax_nsnn.html')

    @app.route('/SME_mgmt_report')
    @login_required
    @require_sme_regime
    def SME_mgmt_report():
        try:
            _bootstrap_sme_db()
        except Exception:
            pass
        return render_template('KeToanSME/mgmt_report.html')

    @app.route('/SME_costing')
    @login_required
    @require_sme_regime
    def SME_costing():
        try:
            _bootstrap_sme_db()
        except Exception:
            pass
        return render_template('KeToanSME/costing.html')

    @app.route('/SME_utilities')
    @login_required
    @require_sme_regime
    def SME_utilities():
        return render_template('KeToanSME/utilities.html')

    @app.route('/api/sme/dashboard-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_dashboard_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import dashboard_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = dashboard_metrics(conn, fiscal_year=year, period_to=period_to, branch_code=_sme_branch_arg())
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/tax-nsnn', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_tax_nsnn():
        conn = get_db_connection()
        try:
            from Services.sme.tax_nsnn import tax_nsnn_summary
            from Services.tenant_profile import get_current_tenant_profile, normalize_vat_filing_period
            profile = get_current_tenant_profile()
            features = profile.get('features') or {}
            default_mode = normalize_vat_filing_period(
                request.args.get('filing_mode')
                or profile.get('vat_filing_period')
                or features.get('vat_filing_period')
                or features.get('filing_period'),
                default='monthly' if features.get('monthly_vat_filing') else 'quarterly',
            )
            year = request.args.get('year', type=int) or datetime.now().year
            now = datetime.now()
            period = request.args.get('period', type=int)
            quarter = request.args.get('quarter', type=int)
            if default_mode == 'quarterly' and quarter is None and period is None:
                quarter = (now.month - 1) // 3 + 1
            elif default_mode == 'monthly' and period is None:
                period = now.month
            data = tax_nsnn_summary(
                conn,
                fiscal_year=year,
                period=period,
                quarter=quarter,
                filing_mode=default_mode,
            )
            data['configured_vat_filing_period'] = normalize_vat_filing_period(
                profile.get('vat_filing_period')
                or features.get('vat_filing_period')
                or features.get('filing_period'),
                default=default_mode,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/settings/vat-filing-period', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_vat_filing_period():
        from Services.tenant_profile import (
            get_current_tenant_profile,
            normalize_vat_filing_period,
            default_vat_filing_period_for_regime,
            update_registry_settings,
        )
        profile = get_current_tenant_profile()
        features = profile.get('features') or {}
        current = normalize_vat_filing_period(
            profile.get('vat_filing_period')
            or features.get('vat_filing_period')
            or features.get('filing_period'),
            default=default_vat_filing_period_for_regime(profile.get('accounting_regime')),
        )
        if request.method == 'GET':
            return jsonify({
                'success': True,
                'data': {
                    'vat_filing_period': current,
                    'monthly_vat_filing': current == 'monthly',
                    'accounting_regime': profile.get('accounting_regime'),
                    'default_for_regime': default_vat_filing_period_for_regime(
                        profile.get('accounting_regime'),
                    ),
                },
            })

        payload = request.get_json(silent=True) or {}
        period = normalize_vat_filing_period(
            payload.get('vat_filing_period') or payload.get('filing_period'),
            default=current,
        )
        tenant_id = (
            getattr(g, 'tenant_id', None)
            or session.get('last_tenant_id')
            or profile.get('tenant_id')
        )
        if not tenant_id:
            return jsonify({'success': False, 'error': 'Không xác định được tenant'}), 400
        ok = update_registry_settings(tenant_id, {
            'vat_filing_period': period,
            'filing_period': period,
        })
        if not ok:
            return jsonify({'success': False, 'error': 'Không lưu được cấu hình'}), 500
        try:
            from Services.tenant_profile import load_tenant_profile
            g.tenant_profile = load_tenant_profile(tenant_id)
        except Exception:
            pass
        return jsonify({
            'success': True,
            'data': {
                'vat_filing_period': period,
                'monthly_vat_filing': period == 'monthly',
                'message': 'Đã lưu kỳ kê khai GTGT',
            },
        })

    @app.route('/api/sme/mgmt-report', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_mgmt_report():
        conn = get_db_connection()
        try:
            from Services.sme.mgmt_report import management_report
            from Services.sme.branches import request_branch_filter
            year = request.args.get('year', type=int) or datetime.now().year
            period_from = request.args.get('period_from', type=int) or 1
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = management_report(
                conn,
                fiscal_year=year,
                period_from=period_from,
                period_to=period_to,
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/costing', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_costing():
        conn = get_db_connection()
        try:
            from Services.sme.costing import costing_summary
            from Services.sme.branches import request_branch_filter
            year = request.args.get('year', type=int) or datetime.now().year
            period = request.args.get('period', type=int) or datetime.now().month
            data = costing_summary(
                conn, fiscal_year=year, period=period,
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bctc/b09/narrative', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_b09_narrative_save():
        conn = get_db_connection()
        try:
            from Services.sme.b09_notes import save_b09_narrative_items
            payload = request.get_json(silent=True) or {}
            items = payload.get('items') or []
            n = save_b09_narrative_items(
                conn,
                items,
                updated_by=session.get('user_name') or session.get('username'),
            )
            conn.commit()
            return jsonify({'success': True, 'saved': n})
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchase-orders', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_po_list():
        conn = get_db_connection()
        try:
            from Services.sme.purchase_order import list_purchase_orders
            from Services.sme.branches import request_branch_filter
            rows = list_purchase_orders(
                conn,
                status=request.args.get('status') or None,
                keyword=request.args.get('keyword') or None,
                date_from=request.args.get('date_from') or None,
                date_to=request.args.get('date_to') or None,
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': rows})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchase-orders', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_po_create():
        conn = get_db_connection()
        try:
            from Services.sme.purchase_order import create_purchase_order
            payload = request.get_json(silent=True) or {}
            data = create_purchase_order(
                conn,
                po_date=payload.get('po_date') or datetime.now().strftime('%Y-%m-%d'),
                expected_date=payload.get('expected_date'),
                supplier_name=payload.get('supplier_name') or '',
                supplier_id=payload.get('supplier_id'),
                supplier_code=payload.get('supplier_code'),
                supplier_tax_code=payload.get('supplier_tax_code'),
                note=payload.get('note'),
                lines=payload.get('lines') or [],
                created_by=session.get('user_name') or session.get('username'),
            )
            conn.commit()
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchase-orders/<int:po_id>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_po_get(po_id):
        conn = get_db_connection()
        try:
            from Services.sme.branch_filter import assert_row_in_branch
            from Services.sme.purchase_order import get_purchase_order
            assert_row_in_branch(conn, 'sme_purchase_orders', po_id, label='Đơn mua hàng')
            data = get_purchase_order(conn, po_id)
            if not data:
                return jsonify({'success': False, 'error': 'Không tìm thấy đơn'}), 404
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 403
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchase-orders/<int:po_id>', methods=['PUT'])
    @login_required
    @require_sme_regime
    def api_sme_po_update(po_id):
        conn = get_db_connection()
        try:
            from Services.sme.branch_filter import assert_row_in_branch
            from Services.sme.purchase_order import update_purchase_order
            assert_row_in_branch(conn, 'sme_purchase_orders', po_id, label='Đơn mua hàng')
            payload = request.get_json(silent=True) or {}
            data = update_purchase_order(
                conn,
                po_id,
                po_date=payload.get('po_date'),
                expected_date=payload.get('expected_date'),
                supplier_name=payload.get('supplier_name'),
                supplier_id=payload.get('supplier_id'),
                supplier_code=payload.get('supplier_code'),
                supplier_tax_code=payload.get('supplier_tax_code'),
                note=payload.get('note'),
                status=payload.get('status'),
                lines=payload.get('lines'),
            )
            conn.commit()
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchase-orders/<int:po_id>/status', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_po_status(po_id):
        conn = get_db_connection()
        try:
            from Services.sme.branch_filter import assert_row_in_branch
            from Services.sme.purchase_order import set_purchase_order_status
            assert_row_in_branch(conn, 'sme_purchase_orders', po_id, label='Đơn mua hàng')
            payload = request.get_json(silent=True) or {}
            data = set_purchase_order_status(conn, po_id, payload.get('status') or '')
            conn.commit()
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchase-orders/<int:po_id>/import-draft', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_po_import_draft(po_id):
        conn = get_db_connection()
        try:
            from Services.sme.branch_filter import assert_row_in_branch
            from Services.sme.purchase_order import build_import_draft_from_po
            assert_row_in_branch(conn, 'sme_purchase_orders', po_id, label='Đơn mua hàng')
            data = build_import_draft_from_po(conn, po_id)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchasing-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_purchasing_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.purchase_order import purchasing_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = purchasing_hub_metrics(
                conn, fiscal_year=year, period_to=period_to,
                branch_code=_sme_branch_arg(),
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/debt-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_debt_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import debt_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = debt_hub_metrics(conn, fiscal_year=year, period_to=period_to, branch_code=_sme_branch_arg())
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/warehouse-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_warehouse_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import warehouse_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = warehouse_hub_metrics(conn, fiscal_year=year, period_to=period_to, branch_code=_sme_branch_arg())
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/fixed-asset-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_fixed_asset_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import fixed_asset_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = fixed_asset_hub_metrics(conn, fiscal_year=year, period_to=period_to, branch_code=_sme_branch_arg())
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/tools-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_tools_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import tools_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = tools_hub_metrics(conn, fiscal_year=year, period_to=period_to, branch_code=_sme_branch_arg())
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/hr-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_hr_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import hr_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = hr_hub_metrics(conn, fiscal_year=year, period_to=period_to, branch_code=_sme_branch_arg())
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/sales-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_sales_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import sales_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = sales_hub_metrics(conn, fiscal_year=year, period_to=period_to, branch_code=_sme_branch_arg())
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/books-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_books_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import books_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = books_hub_metrics(conn, fiscal_year=year, period_to=period_to, branch_code=_sme_branch_arg())
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bctc-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_bctc_metrics():
        conn = get_db_connection()
        try:
            from Services.sme.dashboard_metrics import bctc_hub_metrics
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = bctc_hub_metrics(conn, fiscal_year=year, period_to=period_to)
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/vat-declaration', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_vat_declaration():
        conn = get_db_connection()
        try:
            from Services.sme.vat_declaration import vat_declaration_worksheet
            from Services.tenant_profile import get_current_tenant_profile, normalize_vat_filing_period
            profile = get_current_tenant_profile()
            features = profile.get('features') or {}
            default_mode = normalize_vat_filing_period(
                request.args.get('filing_mode')
                or profile.get('vat_filing_period')
                or features.get('vat_filing_period')
                or features.get('filing_period'),
                default='monthly' if features.get('monthly_vat_filing') else 'quarterly',
            )
            year = request.args.get('year', type=int) or datetime.now().year
            now = datetime.now()
            period = request.args.get('period', type=int)
            quarter = request.args.get('quarter', type=int)
            if default_mode == 'quarterly' and quarter is None and period is None:
                quarter = (now.month - 1) // 3 + 1
            elif default_mode == 'monthly' and period is None:
                period = now.month
            data = vat_declaration_worksheet(
                conn,
                fiscal_year=year,
                period=period,
                quarter=quarter,
                filing_mode=default_mode,
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/vat-declaration/xml', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_vat_declaration_xml():
        conn = get_db_connection()
        try:
            from Services.sme.vat_xml import generate_sme_vat_xml
            from Services.tenant_profile import get_current_tenant_profile, normalize_vat_filing_period
            profile = get_current_tenant_profile()
            features = profile.get('features') or {}
            default_mode = normalize_vat_filing_period(
                request.args.get('filing_mode')
                or profile.get('vat_filing_period')
                or features.get('vat_filing_period')
                or features.get('filing_period'),
                default='monthly' if features.get('monthly_vat_filing') else 'quarterly',
            )
            year = request.args.get('year', type=int) or datetime.now().year
            now = datetime.now()
            period = request.args.get('period', type=int)
            quarter = request.args.get('quarter', type=int)
            if default_mode == 'quarterly' and quarter is None and period is None:
                quarter = (now.month - 1) // 3 + 1
            elif default_mode == 'monthly' and period is None:
                period = now.month
            result = generate_sme_vat_xml(
                conn,
                fiscal_year=year,
                period=period,
                quarter=quarter,
                filing_mode=default_mode,
                loai_tkhai=request.args.get('loai_tkhai') or 'C',
                so_lan=request.args.get('so_lan') or '1',
            )
            return Response(
                result['xml'],
                mimetype='application/xml; charset=utf-8',
                headers={
                    'Content-Disposition': f'attachment; filename="{result["filename"]}"',
                },
            )
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/sale-forms/customers', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_sale_form_customers():
        conn = get_db_connection()
        try:
            from Services.sme.sale_forms import list_sale_customers
            return jsonify({'success': True, 'data': list_sale_customers(conn)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/sale-forms/products', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_sale_form_products():
        conn = get_db_connection()
        try:
            from Services.sme.sale_forms import list_products_brief
            from Services.sme.branches import request_branch_filter
            return jsonify({
                'success': True,
                'data': list_products_brief(conn, branch_code=request_branch_filter()),
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/sale-forms/01-bh', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_form_01_bh():
        conn = get_db_connection()
        try:
            from Services.sme.sale_forms import form_01_bh
            data = form_01_bh(
                conn,
                agent_name=request.args.get('agent_name') or '',
                date_from=request.args.get('date_from') or '',
                date_to=request.args.get('date_to') or '',
                contract_no=request.args.get('contract_no') or '',
                contract_date=request.args.get('contract_date') or '',
                opening_debt=float(request.args.get('opening_debt') or 0),
                commission=float(request.args.get('commission') or 0),
                tax_paid_for=float(request.args.get('tax_paid_for') or 0),
                other_cost=float(request.args.get('other_cost') or 0),
                paid_cash=float(request.args.get('paid_cash') or 0),
                paid_cheque=float(request.args.get('paid_cheque') or 0),
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/sale-forms/02-bh', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_form_02_bh():
        conn = get_db_connection()
        try:
            from Services.sme.sale_forms import form_02_bh
            from Services.sme.branches import request_branch_filter
            data = form_02_bh(
                conn,
                product_id=request.args.get('product_id', type=int) or 0,
                date_from=request.args.get('date_from') or '',
                date_to=request.args.get('date_to') or '',
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/employee-receivable', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_employee_receivable():
        conn = get_db_connection()
        try:
            from Services.sme.employee_receivable import employee_receivable_summary
            from Services.sme.branches import request_branch_filter
            year = request.args.get('year', type=int) or datetime.now().year
            period = request.args.get('period', type=int) or datetime.now().month
            data = employee_receivable_summary(
                conn, fiscal_year=year, period=period,
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/employee-payable', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_employee_payable():
        conn = get_db_connection()
        try:
            from Services.sme.employee_payable import employee_payable_summary
            from Services.sme.branches import request_branch_filter
            year = request.args.get('year', type=int) or datetime.now().year
            period = request.args.get('period', type=int) or datetime.now().month
            data = employee_payable_summary(
                conn, fiscal_year=year, period=period,
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/auto/run-period', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_auto_run_period():
        conn = get_db_connection()
        try:
            from Services.sme.auto_posting import run_period_automation
            from Services.tenant_profile import get_current_tenant_profile
            payload = request.get_json(silent=True) or {}
            now = datetime.now()
            year = int(payload.get('year') or now.year)
            period = int(payload.get('period') or now.month)
            replace_existing = bool(payload.get('replace_existing'))
            auto_activate = payload.get('auto_activate', True)
            profile = get_current_tenant_profile()
            result = run_period_automation(
                conn,
                fiscal_year=year,
                period=period,
                accounting_regime=profile.get('accounting_regime'),
                features=profile.get('features'),
                created_by=session.get('user_name') or session.get('username'),
                replace_existing=replace_existing,
                auto_activate=bool(auto_activate),
            )
            conn.commit()
            return jsonify({'success': True, 'data': result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/auto/period-close', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_auto_period_close():
        """Chỉ chạy kết chuyển KQKD (không KH/PB)."""
        conn = get_db_connection()
        try:
            from Services.sme.period_close import run_period_close
            from Services.tenant_profile import get_current_tenant_profile
            payload = request.get_json(silent=True) or {}
            now = datetime.now()
            year = int(payload.get('year') or now.year)
            period = int(payload.get('period') or now.month)
            profile = get_current_tenant_profile()
            result = run_period_close(
                conn,
                fiscal_year=year,
                period=period,
                accounting_regime=profile.get('accounting_regime'),
                features=profile.get('features'),
                created_by=session.get('user_name') or session.get('username'),
                replace_existing=bool(payload.get('replace_existing')),
            )
            conn.commit()
            return jsonify({'success': True, 'data': result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/auto/vat-settle', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_auto_vat_settle():
        conn = get_db_connection()
        try:
            from Services.sme.vat_settlement import run_vat_settlement
            from Services.tenant_profile import get_current_tenant_profile
            payload = request.get_json(silent=True) or {}
            now = datetime.now()
            year = int(payload.get('year') or now.year)
            period = int(payload.get('period') or now.month)
            profile = get_current_tenant_profile()
            result = run_vat_settlement(
                conn,
                fiscal_year=year,
                period=period,
                accounting_regime=profile.get('accounting_regime'),
                features=profile.get('features'),
                created_by=session.get('user_name') or session.get('username'),
                replace_existing=bool(payload.get('replace_existing')),
            )
            conn.commit()
            return jsonify({'success': True, 'data': result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/period-lock', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_period_lock_list():
        conn = get_db_connection()
        try:
            from Services.sme.period_lock import list_locked_periods
            year = request.args.get('year', type=int)
            rows = list_locked_periods(conn, fiscal_year=year)
            return jsonify({'success': True, 'data': rows})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/period-lock', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_period_lock_set():
        conn = get_db_connection()
        try:
            from Services.sme.period_lock import lock_period, unlock_period
            payload = request.get_json(silent=True) or {}
            year = int(payload.get('year') or datetime.now().year)
            period = int(payload.get('period') or datetime.now().month)
            action = (payload.get('action') or 'lock').strip().lower()
            if action == 'unlock':
                ok = unlock_period(conn, fiscal_year=year, period=period)
                conn.commit()
                return jsonify({'success': True, 'unlocked': ok, 'year': year, 'period': period})
            data = lock_period(
                conn,
                fiscal_year=year,
                period=period,
                locked_by=session.get('user_name') or session.get('username'),
                reason=payload.get('reason') or 'Khóa sổ thủ công',
            )
            conn.commit()
            return jsonify({'success': True, 'data': data})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # Phase P0: tạm ứng 03–05 TT, kiểm kê quỹ, đối chiếu NH, thanh lý TSCĐ, hủy CT
    from routes.ketoan_sme_phase0 import register_sme_phase0_routes
    register_sme_phase0_routes(
        app,
        login_required=login_required,
        require_sme_regime=require_sme_regime,
    )
    # Phase P1: kho VT, TNDN, FX, BHXH/LĐTL
    from routes.ketoan_sme_phase1 import register_sme_phase1_routes
    register_sme_phase1_routes(
        app,
        login_required=login_required,
        require_sme_regime=require_sme_regime,
    )
    # Phase P2: vay nợ, ký quỹ
    from routes.ketoan_sme_phase2 import register_sme_phase2_routes
    register_sme_phase2_routes(
        app,
        login_required=login_required,
        require_sme_regime=require_sme_regime,
    )
    # Tiếp theo: TNDN XML, góp vốn, 08b
    from routes.ketoan_sme_phase3 import register_sme_phase3_routes
    register_sme_phase3_routes(
        app,
        login_required=login_required,
        require_sme_regime=require_sme_regime,
    )
    # Phase P4: giao khoán LĐTL, 02/03/04 LĐTL, 03-VT, biên bản TSCĐ
    from routes.ketoan_sme_phase4 import register_sme_phase4_routes
    register_sme_phase4_routes(
        app,
        login_required=login_required,
        require_sme_regime=require_sme_regime,
    )
    # Phase P5: 06/07/09-TT + in 05-VT
    from routes.ketoan_sme_phase5 import register_sme_phase5_routes
    register_sme_phase5_routes(
        app,
        login_required=login_required,
        require_sme_regime=require_sme_regime,
    )
    # Phase P6: in còn thiếu, year-end, void
    from routes.ketoan_sme_phase6 import register_sme_phase6_routes
    register_sme_phase6_routes(
        app,
        login_required=login_required,
        require_sme_regime=require_sme_regime,
    )
    # Phase P7: bịt lỗ hổng (void, CCDC, số dư quỹ SME)
    from routes.ketoan_sme_phase7 import register_sme_phase7_routes
    register_sme_phase7_routes(
        app,
        login_required=login_required,
        require_sme_regime=require_sme_regime,
    )
    # Phase P8: tối ưu BCTC Excel, 01-BH giao đại lý, TT58, TNCN, void lương
    from routes.ketoan_sme_phase8 import register_sme_phase8_routes
    register_sme_phase8_routes(
        app,
        login_required=login_required,
        require_sme_regime=require_sme_regime,
    )
    # Phase P9: multi-branch
    from routes.ketoan_sme_phase9 import register_sme_phase9_routes
    register_sme_phase9_routes(
        app,
        login_required=login_required,
        require_sme_regime=require_sme_regime,
    )
