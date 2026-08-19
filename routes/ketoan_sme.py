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

    @app.before_request
    def _guard_sme_regime_pages():
        """Chặn URL/API TT99 khi tenant là TT58 (và ngược lại với sổ DNSN)."""
        from flask import jsonify, flash, redirect, url_for
        from Services.sme_roles import sme_request_allowed
        from Services.tenant_profile import (
            get_current_tenant_profile,
            is_master_session,
            is_sme_regime,
        )
        ep = request.endpoint or ''
        path = request.path or ''
        if not (
            ep.startswith('SME_')
            or ep.startswith('api_sme')
            or path.startswith('/SME_')
            or path.startswith('/api/sme/')
        ):
            return None
        if is_master_session():
            return None
        profile = get_current_tenant_profile() or {}
        regime = profile.get('accounting_regime')
        if not is_sme_regime(regime):
            return None
        allowed, msg = sme_request_allowed(ep, path, regime)
        if allowed:
            return None
        if path.startswith('/api/') or request.is_json:
            return jsonify({'success': False, 'error': msg}), 403
        flash(msg, 'warning')
        try:
            return redirect(url_for('SME_dashboard'))
        except Exception:
            return redirect('/SME_dashboard')

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
        ep = request.endpoint or ''
        # API JSON không render sidebar — bỏ mở DB chi nhánh
        needs_branch = (
            (is_sme_endpoint(ep) or ep.startswith('SME_'))
            and not request.path.startswith('/api/')
        )
        if needs_branch:
            cached = getattr(g, '_sme_branch_ctx', None)
            if cached is not None:
                sme_branch = cached
            else:
                try:
                    from Services.sme.branches import branch_context
                    import time as _time
                    db_key = getattr(g, 'db_path', None) or session.get('db_path') or ''
                    proc = getattr(app, '_sme_branch_proc_cache', None)
                    if proc is None:
                        app._sme_branch_proc_cache = {}
                        proc = app._sme_branch_proc_cache
                    hit = proc.get(db_key) if db_key else None
                    now = _time.time()
                    if hit and (now - hit[0]) < 60:
                        sme_branch = dict(hit[1])
                    else:
                        conn = get_db_connection()
                        conn.row_factory = sqlite3.Row
                        sme_branch = branch_context(conn)
                        if db_key:
                            proc[db_key] = (now, dict(sme_branch))
                    sme_branch['filter'] = (
                        session.get('sme_branch_filter')
                        or sme_branch.get('current_branch_code')
                        or 'ALL'
                    )
                except Exception:
                    pass
                g._sme_branch_ctx = sme_branch
        # Cảnh báo layout: đọc từ profile đã có sẵn — không mở DB / không gọi API nền
        vat_alert = None
        micro_alert = None
        try:
            profile = getattr(g, 'tenant_profile', None) or {}
            settings = dict(profile.get('settings') or {})
            if not settings.get('accounting_regime') and profile.get('accounting_regime'):
                settings['accounting_regime'] = profile.get('accounting_regime')
            from Services.sme.vat_filing_alert import get_vat_filing_alert
            from Services.sme.micro_enterprise import get_tt99_switch_alert
            vat_alert = get_vat_filing_alert(settings)
            micro_alert = get_tt99_switch_alert(settings)
        except Exception:
            pass
        return {
            'sme_hub_title': SME_HUB_TITLE,
            'sme_menu_groups': get_sme_menu_groups(),
            'sme_dashboard_quick': get_sme_quick_links(),
            'sme_dashboard_featured': get_sme_featured_links(),
            'sme_hub_active': is_sme_endpoint(ep),
            'sme_current_group': current_group,
            'sme_branch': sme_branch,
            'sme_vat_filing_alert': vat_alert,
            'sme_micro_tt99_alert': micro_alert,
            'sme_is_tt58': 'TT58' in str(
                (getattr(g, 'tenant_profile', None) or {}).get('accounting_regime') or ''
            ).upper(),
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
        # Không bootstrap schema trên mỗi lần mở dashboard — warm path chỉ render HTML.
        # Schema được đảm bảo khi gọi API ghi sổ / lần đầu dùng chức năng kế toán.
        return render_template('KeToanSME/main_dashboard.html')

    @app.route('/SME_hub/<group_id>')
    @login_required
    @require_sme_regime
    def SME_hub_group(group_id):
        from Services.sme_menu import get_sme_menu_groups
        group = None
        for g in get_sme_menu_groups():
            if g.get('id') == group_id and g.get('_type') != 'section_header':
                group = g
                break
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

    @app.route('/SME_sale_export')
    @login_required
    @require_sme_regime
    def SME_sale_export():
        # Không mở DB khi render form (DDL/warehouse lock). Kho + hàng load qua API.
        return render_template(
            'KeToanSME/sale_export.html',
            products_json='[]',
            warehouses=[{'code': 'KHO_001', 'name': 'Kho mặc định', 'is_default': 1}],
            edit_id=request.args.get('sale_id') or request.args.get('id'),
        )

    @app.route('/SME_sale_export_list')
    @login_required
    @require_sme_regime
    def SME_sale_export_list():
        return render_template('KeToanSME/sale_export_list.html')

    @app.route('/SME_customs_declarations')
    @login_required
    @require_sme_regime
    def SME_customs_declarations():
        from Services.sme.customs_declaration import OFFICIAL_EDECLARATION_URL
        return render_template(
            'KeToanSME/customs_declarations.html',
            edeclaration_url=OFFICIAL_EDECLARATION_URL,
        )

    @app.route('/api/sme/customs-declarations', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_customs_declarations():
        from Services.sme.customs_declaration import (
            ensure_customs_declaration_schema,
            list_declarations,
            upsert_declaration,
        )
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            ensure_customs_declaration_schema(conn, commit=False)
            if request.method == 'GET':
                items = list_declarations(
                    conn,
                    direction=request.args.get('direction'),
                    q=request.args.get('q'),
                    limit=int(request.args.get('limit') or 100),
                )
                return jsonify({'success': True, 'items': items})
            data = request.get_json(silent=True) or {}
            doc = upsert_declaration(
                conn, data,
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_customs_declarations')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/customs-declarations/import', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_customs_declarations_import():
        from Services.sme.customs_declaration import (
            import_declarations_bulk, parse_import_text,
        )
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            payload = request.get_json(silent=True) or {}
            raw = payload.get('raw') or ''
            items = payload.get('items')
            if items is None:
                items = parse_import_text(raw)
            result = import_declarations_bulk(
                conn, items,
                created_by=session.get('user_name') or session.get('username'),
                default_source=payload.get('source') or 'json_import',
                commit=True,
            )
            return jsonify(result)
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_customs_declarations_import')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/customs-declarations/<int:decl_id>/apply-export-sale', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_customs_apply_export_sale(decl_id):
        from Services.sme.customs_declaration import apply_declaration_to_export_sale
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            sale_id = int(data.get('sale_id') or 0)
            if not sale_id:
                return jsonify({'success': False, 'error': 'Thiếu sale_id'}), 400
            result = apply_declaration_to_export_sale(
                conn, declaration_id=decl_id, sale_id=sale_id, commit=True,
            )
            return jsonify(result)
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_customs_apply_export_sale')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/customs-declarations/<int:decl_id>/apply-import', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_customs_apply_import(decl_id):
        from Services.sme.customs_declaration import apply_declaration_to_import
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            import_id = int(data.get('import_id') or 0)
            if not import_id:
                return jsonify({'success': False, 'error': 'Thiếu import_id'}), 400
            result = apply_declaration_to_import(
                conn, declaration_id=decl_id, import_id=import_id, commit=True,
            )
            return jsonify(result)
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_customs_apply_import')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/export-sale', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_export_sale():
        from Services.sme.export_sale import (
            create_or_update_export_sale, list_export_sales,
        )
        from Services.sme.export_payment import ensure_export_sale_schema
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            if request.method == 'GET':
                ensure_export_sale_schema(conn, commit=False)
                items = list_export_sales(
                    conn,
                    limit=int(request.args.get('limit') or 100),
                    q=request.args.get('q'),
                )
                return jsonify({'success': True, 'items': items})
            _bootstrap_sme_db()
            data = request.get_json(silent=True) or {}
            result = create_or_update_export_sale(
                conn, data,
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify(result)
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_export_sale')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/export-sale/<int:sale_id>/clearance', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_export_sale_clearance(sale_id):
        """Bước 2: thông quan — Nợ 632/Có 157 + Nợ 131/Có 511·3333."""
        from Services.sme.export_sale import confirm_export_clearance
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            data = request.get_json(silent=True) or {}
            result = confirm_export_clearance(
                conn, sale_id, data,
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify(result)
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_export_sale_clearance')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/export-sale/<int:sale_id>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_export_sale_detail(sale_id):
        from Services.sme.export_sale import get_export_sale
        from Services.sme.export_payment import ensure_export_sale_schema
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            ensure_export_sale_schema(conn, commit=False)
            data = get_export_sale(conn, sale_id)
            if not data:
                return jsonify({'success': False, 'error': 'Không tìm thấy'}), 404
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            logger.exception('api_sme_export_sale_detail')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/export-sale/customer-advances', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_export_customer_advances():
        from Services.sme.export_payment import list_customer_advances, ensure_export_sale_schema
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            ensure_export_sale_schema(conn, commit=False)
            include_id = request.args.get('include_sale_id')
            try:
                include_id = int(include_id) if include_id not in (None, '', '0') else None
            except (TypeError, ValueError):
                include_id = None
            unused = str(request.args.get('unused_only', '1')).lower() not in ('0', 'false', 'no')
            items = list_customer_advances(
                conn,
                customer_name=request.args.get('customer_name'),
                currency=request.args.get('currency'),
                unused_only=unused,
                include_sale_id=include_id,
            )
            return jsonify({'success': True, 'items': items})
        except Exception as e:
            logger.exception('api_sme_export_customer_advances')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/export-sale/open-lcs', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_export_open_lcs():
        from Services.sme.letter_of_credit import list_lc_docs
        from Services.sme.export_payment import ensure_export_sale_schema
        from Services.sme.branches import request_branch_filter
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            ensure_export_sale_schema(conn, commit=False)
            items = list_lc_docs(
                conn,
                status='open',
                direction='export',
                branch_code=request_branch_filter(),
                with_balance=True,
                only_linkable=True,
            )
            return jsonify({'success': True, 'items': items})
        except Exception as e:
            logger.exception('api_sme_export_open_lcs')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/export-sale/open-lc', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_export_open_lc():
        from Services.sme.letter_of_credit import open_export_letter_of_credit
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            data = request.get_json(silent=True) or {}
            doc = open_export_letter_of_credit(
                conn,
                open_date=data.get('open_date') or data.get('date'),
                bank_name=data.get('bank_name') or '',
                amount_fc=data.get('amount_fc'),
                exchange_rate=data.get('exchange_rate') or 1,
                currency=data.get('currency') or 'USD',
                beneficiary_name=data.get('beneficiary_name') or '',
                applicant_name=data.get('applicant_name') or '',
                lc_no=data.get('lc_no'),
                notes=data.get('notes') or '',
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_export_open_lc')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/export-sale/<int:sale_id>/settle-ar', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_export_settle_ar(sale_id):
        from Services.sme.export_settle import settle_export_ar
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            data = request.get_json(silent=True) or {}
            result = settle_export_ar(
                conn, sale_id,
                settle_date=data.get('settle_date') or data.get('date'),
                amount_fc=data.get('amount_fc'),
                exchange_rate=data.get('exchange_rate') or data.get('fx_rate'),
                payment_method=data.get('payment_method') or 'bank',
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify(result)
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_export_settle_ar')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/export-sale/<int:sale_id>/doc-discount', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_export_doc_discount(sale_id):
        from Services.sme.export_settle import create_doc_discount
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            data = request.get_json(silent=True) or {}
            # mặc định chiết khấu toàn bộ amount_fc còn lại
            sale = conn.execute(
                'SELECT amount_fc, advance_fc, settle_amount_fc FROM sale WHERE id = ?',
                (sale_id,),
            ).fetchone()
            if not sale:
                return jsonify({'success': False, 'error': 'Không tìm thấy phiếu'}), 404
            remain = float(sale['amount_fc'] or 0) - float(sale['advance_fc'] or 0) - float(sale['settle_amount_fc'] or 0)
            result = create_doc_discount(
                conn, sale_id,
                discount_date=data.get('discount_date') or data.get('date'),
                amount_fc=data.get('amount_fc') if data.get('amount_fc') is not None else remain,
                exchange_rate=data.get('exchange_rate') or 1,
                fee_vnd=data.get('fee_vnd') or 0,
                cash_account=data.get('cash_account') or '1122',
                loan_account=data.get('loan_account') or '3411',
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify(result)
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_export_doc_discount')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/export-sale/<int:sale_id>/costs', methods=['POST', 'GET'])
    @login_required
    @require_sme_regime
    def api_sme_export_costs(sale_id):
        from Services.sme.export_settle import post_export_cost, list_export_costs
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            if request.method == 'GET':
                return jsonify({'success': True, 'items': list_export_costs(conn, sale_id)})
            data = request.get_json(silent=True) or {}
            result = post_export_cost(
                conn, sale_id,
                cost_date=data.get('cost_date') or data.get('date'),
                description=data.get('description') or '',
                amount_vnd=data.get('amount_vnd') or data.get('amount'),
                vat_vnd=data.get('vat_vnd') or 0,
                payment_method=data.get('payment_method') or 'bank',
                credit_account=data.get('credit_account'),
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify(result)
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_export_costs')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

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
        vat_in_inventory_cost = False
        vat_in_cost_label = ''
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
                from Services.sme.regime_profile import get_ledger_profile
                profile = get_ledger_profile(conn)
                vat_in_inventory_cost = bool(profile.get('vat_in_inventory_cost'))
                td = profile.get('tt58_tax_method_def') or {}
                if vat_in_inventory_cost:
                    vat_in_cost_label = (
                        f"Trường hợp {td.get('case_no') or td.get('method_no') or ''} "
                        f"— GTGT theo % doanh thu: thuế GTGT đầu vào không khấu trừ, "
                        f"đã cộng vào giá vốn hàng hóa / nguyên giá TSCĐ-CCDC. "
                        f"Không hạch toán Nợ 133."
                    ).strip()
            finally:
                conn.close()
        except Exception:
            logger.exception('SME_import load warehouses / tax method')
        try:
            next_import_no = peek_next_import_no(import_mode) or next_import_no
        except Exception:
            logger.exception('SME_import next_import_no')
        return render_template(
            'KeToanSME/import_sme.html',
            warehouses=warehouses,
            next_import_no=next_import_no,
            import_mode=import_mode,
            vat_in_inventory_cost=vat_in_inventory_cost,
            vat_in_cost_label=vat_in_cost_label,
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

    @app.route('/SME_import/view/<int:import_id>')
    @login_required
    @require_sme_regime
    def SME_import_view(import_id):
        """Xem chi tiết phiếu nhập SME — template import_view."""
        from Services.import_line_helpers import load_import_for_edit, prepare_import_edit_json
        from Services.sme.branches import assert_import_in_branch

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_import_in_branch(conn, import_id)
            except ValueError:
                abort(404, description='Phiếu nhập không tồn tại hoặc ngoài chi nhánh.')

            imp = load_import_for_edit(conn, import_id)
            if not imp:
                abort(404, description='Phiếu nhập không tồn tại.')

            view_items = []
            for raw in imp.get('items') or []:
                item = dict(raw)
                item['display_unit'] = str(
                    item.get('unit') or item.get('invoice_unit') or item.get('base_unit') or 'Cái'
                ).strip() or 'Cái'
                item['discount_amount'] = float(
                    item.get('discount_amount') or item.get('discount') or 0
                )
                item['tax_amount'] = float(item.get('tax_amount') or item.get('tax') or 0)
                qty = float(item.get('qty') or 0)
                price = float(item.get('buyprice') or 0)
                line_total = float(item.get('line_total') or (qty * price))
                subtotal = float(item.get('subtotal') or (line_total - item['discount_amount']))
                item['line_total'] = line_total
                item['subtotal'] = subtotal
                item['payment_amount'] = float(
                    item.get('payment_amount') or (subtotal + item['tax_amount'])
                )
                view_items.append(item)

            payload = prepare_import_edit_json({**imp, 'items': view_items})
            return render_template(
                'import_view.html',
                imp=payload,
                items=payload.get('items') or [],
                is_service_import=bool(imp.get('is_service_import')),
                back_url=url_for('SME_DanhSachPhieuNhapKho'),
            )
        except Exception as exc:
            from werkzeug.exceptions import HTTPException
            if isinstance(exc, HTTPException):
                raise
            logger.exception('SME_import_view id=%s', import_id)
            abort(500, description='Lỗi xử lý dữ liệu phiếu nhập.')
        finally:
            conn.close()

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
                currency=data.get('currency') or 'VND',
                exchange_rate=data.get('exchange_rate') or data.get('fx_rate') or 1,
                amount_fc=data.get('amount_fc'),
                purpose=data.get('purpose'),
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
                currency=data.get('currency') or 'VND',
                exchange_rate=data.get('exchange_rate') or data.get('fx_rate') or 1,
                amount_fc=data.get('amount_fc'),
                purpose=data.get('purpose'),
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

    @app.route('/api/sme/vouchers/receipts/renumber', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_vouchers_receipts_renumber():
        from Services.sme.vouchers import renumber_vouchers
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            result = renumber_vouchers(conn, 'receipt', commit=True)
            return jsonify({'success': True, **result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e), 'message': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_vouchers_receipts_renumber')
            return jsonify({'success': False, 'error': str(e), 'message': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/vouchers/payments/renumber', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_vouchers_payments_renumber():
        from Services.sme.vouchers import renumber_vouchers
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            result = renumber_vouchers(conn, 'payment', commit=True)
            return jsonify({'success': True, **result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e), 'message': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_vouchers_payments_renumber')
            return jsonify({'success': False, 'error': str(e), 'message': str(e)}), 500
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
                    'payment_mode': r.get('payment_mode') or '',
                    'linked_lc_id': r.get('linked_lc_id'),
                    'settle_journal_id': r.get('settle_journal_id'),
                    'settle_amount_fc': float(r.get('settle_amount_fc') or 0),
                    'amount_fc': float(r.get('amount_fc') or 0),
                    'advance_fc': float(r.get('advance_fc') or 0),
                    'import_type': r.get('import_type') or 'DOMESTIC',
                    'receipt_stage': r.get('receipt_stage') or 'RECEIVED',
                    'tax_payment_voucher_id': r.get('tax_payment_voucher_id'),
                    'receive_journal_id': r.get('receive_journal_id'),
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

    @app.route('/api/sme/stock-out-vouchers/<int:voucher_id>', methods=['DELETE'])
    @login_required
    @require_sme_regime
    def api_sme_stock_out_voucher_delete(voucher_id):
        """Xóa phiếu xuất kho 02-VT + bút toán / kho liên quan (kỳ mở)."""
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.audit_log import write_audit
            from Services.sme.branches import assert_sale_in_branch, request_branch_filter
            from Services.sme.journal_cascade import delete_stock_out_voucher

            row = conn.execute(
                'SELECT id, sale_id, voucher_no FROM phieu_xuat_kho WHERE id = ?',
                (voucher_id,),
            ).fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Không tìm thấy phiếu xuất kho'}), 404
            sale_id = row['sale_id'] if hasattr(row, 'keys') else row[1]
            branch = request_branch_filter()
            if sale_id and branch and str(branch).upper() not in ('', 'ALL'):
                assert_sale_in_branch(conn, int(sale_id))

            actor = (
                (session.get('user') or {}).get('username')
                or session.get('user_name')
                or session.get('username')
            )
            result = delete_stock_out_voucher(
                conn,
                voucher_id,
                reason=(payload.get('reason') or 'Xóa phiếu xuất kho 02-VT'),
                deleted_by=actor,
                commit=True,
            )
            write_audit(
                'delete',
                'phieu_xuat_kho',
                result.get('message') or f'Xóa phiếu xuất #{voucher_id}',
                entity_type='phieu_xuat_kho',
                entity_id=voucher_id,
                entity_label=result.get('voucher_no'),
                old_data={'voucher_id': voucher_id, 'sale_id': sale_id},
                new_data=None,
            )
            return jsonify({'success': True, 'data': result, 'message': result.get('message')})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
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
        from Services.sme.payroll import get_salary_insurance_config
        month = request.args.get('month', datetime.now().month, type=int)
        year = request.args.get('year', datetime.now().year, type=int)
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            cfg = get_salary_insurance_config(conn)
        except Exception:
            cfg = {
                'salary_region': '',
                'base_salary_insurance': 5210000,
                'rate_bhxh': 8, 'rate_bhyt': 1.5, 'rate_bhtn': 1,
                'rate_bhxh_chu': 17.5, 'rate_bhyt_chu': 3, 'rate_bhtn_chu': 1,
                'regions': [],
            }
        finally:
            conn.close()
        return render_template(
            'KeToanSME/SME_salary.html',
            month=month, year=year, focus=None,
            info=cfg,
            vung_data=cfg.get('regions') or [],
        )

    @app.route('/SME_salary/update_config', methods=['POST'])
    @login_required
    @require_sme_regime
    def SME_salary_update_config():
        """Cấu hình lương & tỷ lệ BH (cùng nguồn business_info như HKD)."""
        from Services.sme.payroll import update_salary_insurance_config
        conn = get_db_connection()
        try:
            _bootstrap_sme_db()
            update_salary_insurance_config(
                conn,
                region=request.form.get('region'),
                base_salary=request.form.get('base_salary'),
                rate_bhxh=request.form.get('rate_bhxh'),
                rate_bhyt=request.form.get('rate_bhyt'),
                rate_bhtn=request.form.get('rate_bhtn'),
                rate_bhxh_chu=request.form.get('rate_bhxh_chu'),
                rate_bhyt_chu=request.form.get('rate_bhyt_chu'),
                rate_bhtn_chu=request.form.get('rate_bhtn_chu'),
                commit=True,
            )
            flash('Cấu hình lương và tỷ lệ bảo hiểm đã được cập nhật thành công!', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'Lỗi khi cập nhật: {e}', 'danger')
        finally:
            conn.close()
        month = request.form.get('month') or request.args.get('month')
        year = request.form.get('year') or request.args.get('year')
        kwargs = {}
        if month:
            kwargs['month'] = month
        if year:
            kwargs['year'] = year
        return redirect(url_for('SME_salary_create', **kwargs))

    @app.route('/api/sme/payroll/config', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_payroll_config():
        from Services.sme.payroll import get_salary_insurance_config, update_salary_insurance_config
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            if request.method == 'GET':
                return jsonify({'success': True, 'data': get_salary_insurance_config(conn)})
            data = request.get_json(silent=True) or {}
            cfg = update_salary_insurance_config(
                conn,
                region=data.get('region') or data.get('salary_region'),
                base_salary=data.get('base_salary') or data.get('base_salary_insurance'),
                rate_bhxh=data.get('rate_bhxh'),
                rate_bhyt=data.get('rate_bhyt'),
                rate_bhtn=data.get('rate_bhtn'),
                rate_bhxh_chu=data.get('rate_bhxh_chu'),
                rate_bhyt_chu=data.get('rate_bhyt_chu'),
                rate_bhtn_chu=data.get('rate_bhtn_chu'),
                commit=True,
            )
            return jsonify({'success': True, 'data': cfg, 'message': 'Đã lưu cấu hình lương & BH'})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_payroll_config')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

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

    @app.route('/api/sme/payroll/debt-periods', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_payroll_debt_periods():
        """Danh sách kỳ lương còn công nợ (giống HKD /api/debt/salary-periods)."""
        from Services.sme.employee_payable import get_period_debt_list
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            include_paid = str(request.args.get('include_paid') or '').lower() in (
                '1', 'true', 'yes',
            )
            data = get_period_debt_list(conn, include_paid=include_paid)
            return jsonify({'success': True, **data})
        except Exception as e:
            logger.exception('api_sme_payroll_debt_periods')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/payroll/debt-period-detail', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_payroll_debt_period_detail():
        """Chi tiết công nợ lương theo kỳ (giống HKD)."""
        from Services.sme.employee_payable import get_period_debt_detail
        try:
            month = int(request.args.get('month'))
            year = int(request.args.get('year'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Tháng/năm không hợp lệ'}), 400
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            detail = get_period_debt_detail(conn, month, year)
            if not detail:
                return jsonify({'success': False, 'error': 'Không có bảng lương kỳ này'}), 404
            return jsonify({'success': True, **detail})
        except Exception as e:
            logger.exception('api_sme_payroll_debt_period_detail')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/payroll/pay', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_payroll_pay():
        """Trả lương cả kỳ — 1 phiếu chi 02-TT (luồng mặc định, giống HKD)."""
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
                reason=data.get('reason'),
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify({
                'success': True,
                **result,
                'voucher': result.get('voucher') or result.get('voucher_no'),
                'message': result.get('message') or (
                    f"Đã lập phiếu chi trả lương {result.get('voucher_no')}"
                ),
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

    @app.route('/api/sme/payroll/pay-employee', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_payroll_pay_employee():
        """Trả lương lẻ 1 NV — phiếu chi 02-TT (trường hợp đặc biệt)."""
        from Services.sme.payroll import pay_payroll_employee
        data = request.get_json(silent=True) or {}
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            result = pay_payroll_employee(
                conn,
                employee_id=int(data.get('employee_id')),
                month=int(data.get('month')),
                year=int(data.get('year')),
                amount=data.get('amount'),
                pay_date=data.get('pay_date') or data.get('date'),
                payment_method=data.get('payment_method') or data.get('pay_method') or 'bank',
                receiver_name=data.get('receiver'),
                reason=data.get('reason'),
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify({
                'success': True,
                **result,
                'voucher': result.get('voucher') or result.get('voucher_no'),
                'message': result.get('message') or (
                    f"Đã lập phiếu chi {result.get('voucher_no')}"
                ),
            })
        except (TypeError, ValueError) as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_payroll_pay_employee')
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
                'records': result['data'],
                'standard_days': result['standard_days'],
                'config': result.get('config'),
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
        """Nhật ký truy cập — layout SME (không redirect sang POS sale)."""
        from routes.audit import (
            _audit_page_context,
            _can_view_audit,
            _deny_audit_redirect,
        )
        if not _can_view_audit():
            return _deny_audit_redirect()
        return render_template(
            'audit_log.html',
            layout_template='KeToanSME/_layout.html',
            **_audit_page_context(),
        )

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

    @app.route('/SME_chung_tu')
    @login_required
    @require_sme_regime
    def SME_chung_tu():
        from Services.sme_menu import get_sme_group_by_id
        group = get_sme_group_by_id('vouchers')
        if not group:
            abort(404)
        return render_template(
            'KeToanSME/group_dashboard.html',
            group=group,
            group_id='vouchers',
        )

    @app.route('/SME_san_xuat_gia_thanh')
    @login_required
    @require_sme_regime
    def SME_san_xuat_gia_thanh():
        from Services.sme_menu import get_sme_group_by_id
        group = get_sme_group_by_id('production')
        if not group:
            abort(404)
        return render_template(
            'KeToanSME/group_dashboard.html',
            group=group,
            group_id='production',
        )

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
        from Services.sme.regime_profile import get_ledger_profile
        conn = get_db_connection()
        try:
            profile = get_ledger_profile(conn)
        finally:
            conn.close()
        if profile.get('is_tt58_micro') and not profile.get('show_bctc'):
            flash(
                'Trường hợp thuế hiện tại không bắt buộc lập BCTC. '
                'Chỉ dùng sổ DNSN theo Trường hợp 1 hoặc 3.',
                'info',
            )
            return redirect(url_for('SME_dnsn_books'))
        return render_template('KeToanSME/dashboard_BCTC.html')

    @app.route('/SME_BCTC/reports')
    @login_required
    @require_sme_regime
    def SME_BCTC_reports():
        from Services.sme.regime_profile import get_ledger_profile
        conn = get_db_connection()
        try:
            profile = get_ledger_profile(conn)
        finally:
            conn.close()
        if profile.get('is_tt58_micro') and not profile.get('show_bctc'):
            flash(
                'Trường hợp đang chọn (TNDN theo % doanh thu) không bắt buộc '
                'lập BCTC. Đổi sang Trường hợp 2 hoặc 4 nếu cần lập B01/B02-DNSN '
                '(nộp trong 90 ngày sau năm tài chính).',
                'warning',
            )
            return redirect(url_for('SME_dnsn_books'))
        return render_template('KeToanSME/bctc_reports.html')

    @app.route('/SME_dnsn_books')
    @login_required
    @require_sme_regime
    def SME_dnsn_books():
        from Services.sme.bctc_lines_tt58 import DNSN_VOUCHER_FORMS
        from Services.sme.dnsn_books import list_dnsn_books
        from Services.sme.regime_profile import get_ledger_profile
        from Services.sme.tt58_tax_rates import (
            get_tt58_tax_rates,
            rates_ui_context_for_method,
        )
        from flask import url_for as _url_for

        conn = get_db_connection()
        try:
            profile = get_ledger_profile(conn)
            if profile.get('is_tt58_micro'):
                try:
                    tax_rates = get_tt58_tax_rates(conn)
                except sqlite3.OperationalError:
                    tax_rates = {}
                rates_ui = rates_ui_context_for_method(profile.get('tt58_tax_method'))
            else:
                tax_rates, rates_ui = {}, {}
        finally:
            conn.close()

        tax_method = profile.get('tt58_tax_method') if profile.get('is_tt58_micro') else None
        books = list_dnsn_books(tax_method=tax_method, include_optional=True)
        required = [b for b in books if b.get('is_required')]
        optional = [b for b in books if b.get('is_optional')]

        vouchers = []
        if profile.get('show_vouchers', True):
            for v in DNSN_VOUCHER_FORMS:
                item = dict(v)
                ep = v.get('endpoint')
                try:
                    item['url'] = _url_for(ep) if ep else None
                except Exception:
                    item['url'] = None
                vouchers.append(item)

        return render_template(
            'KeToanSME/dnsn_books.html',
            books=books,
            required_books=required,
            optional_books=optional,
            book_map={b['code']: b for b in books},
            vouchers=vouchers,
            regime_profile=profile,
            tax_methods=profile.get('tt58_tax_methods') or [],
            tax_method=tax_method,
            tax_rates=tax_rates,
            rates_ui=rates_ui,
            year=request.args.get('year', default=datetime.today().year, type=int),
        )

    def _load_dnsn_book(code: str):
        from Services.sme.dnsn_books import get_dnsn_book

        year = request.args.get('year', default=datetime.today().year, type=int)
        partner = (request.args.get('partner') or '').strip() or None
        product_id = request.args.get('product_id', type=int)
        branch = (
            request.args.get('branch')
            or session.get('sme_branch_filter')
            or 'ALL'
        )
        conn = get_db_connection()
        try:
            return get_dnsn_book(
                conn,
                code,
                fiscal_year=year,
                partner_key=partner,
                product_id=product_id,
                branch_code=branch,
            )
        finally:
            conn.close()

    @app.route('/SME_dnsn_book/<path:code>')
    @login_required
    @require_sme_regime
    def SME_dnsn_book(code):
        try:
            book = _load_dnsn_book(code)
        except ValueError as exc:
            abort(404, description=str(exc))
        return render_template('KeToanSME/dnsn_book_view.html', book=book)

    @app.route('/SME_dnsn_book/<path:code>/print')
    @login_required
    @require_sme_regime
    def SME_dnsn_book_print(code):
        try:
            book = _load_dnsn_book(code)
        except ValueError as exc:
            abort(404, description=str(exc))
        return render_template('KeToanSME/dnsn_book_print.html', book=book)

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
            from Services.sme.import_transit import (
                STAGE_IN_TRANSIT,
                STAGE_RECEIVED,
                STAGE_TAX_PAID,
                default_receipt_stage,
                ensure_import_transit_schema,
            )
            from Services.sme.import_payment import (
                PAYMENT_LC,
                PAYMENT_PREPAID_FULL,
                PAYMENT_PREPAID_PARTIAL,
                PAYMENT_UNPAID,
                build_advance_payloads_from_request,
                compute_split_fx_goods_vnd,
                ensure_import_payment_schema,
                normalize_payment_mode,
                payment_status_label,
                replace_import_advances,
                validate_import_payment,
            )

            ensure_import_service_schema(conn)
            ensure_import_transit_schema(conn, commit=False)
            ensure_import_payment_schema(conn, commit=False)
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

            customs_decl_no = (data.get('customs_decl_no') or data.get('declaration_no') or '').strip() or None
            customs_decl_date = (data.get('customs_decl_date') or data.get('declaration_date') or '')[:10] or None
            try:
                customs_fx = data.get('customs_fx_rate')
                customs_fx_rate = float(customs_fx) if customs_fx not in (None, '') else float(exchange_rate)
            except (TypeError, ValueError):
                customs_fx_rate = float(exchange_rate)

            # IMPORT mặc định hàng đi đường (151); trong nước = nhập kho ngay
            receipt_stage = default_receipt_stage(import_type)
            raw_stage = str(data.get('receipt_stage') or '').strip().upper()
            if raw_stage in (STAGE_IN_TRANSIT, STAGE_TAX_PAID, STAGE_RECEIVED):
                receipt_stage = raw_stage
            skip_physical_stock = (
                import_type == 'IMPORT'
                and receipt_stage in (STAGE_IN_TRANSIT, STAGE_TAX_PAID)
            )

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

            payment_mode = normalize_payment_mode(
                data.get('payment_mode') or data.get('import_payment_mode'),
                import_type=import_type,
            )
            if import_type == 'IMPORT':
                # Legacy: map payment_status text → mode nếu client chưa gửi payment_mode
                if not data.get('payment_mode') and not data.get('import_payment_mode'):
                    st = str(payment_status_input or '')
                    st_l = st.lower()
                    if 'L/C' in st or 'tín dụng' in st_l:
                        payment_mode = PAYMENT_LC
                    elif 'một phần' in st_l:
                        payment_mode = PAYMENT_PREPAID_PARTIAL
                    elif 'trước đủ' in st_l:
                        payment_mode = PAYMENT_PREPAID_FULL
                    else:
                        payment_mode = PAYMENT_UNPAID
                payment_status_input = payment_status_label(payment_mode)
                payment_method = 'CREDIT'
            else:
                payment_method_raw = str(data.get('payment_method', 'cash')).strip().upper()
                if payment_status_input == 'Chưa thanh toán':
                    payment_method = 'CREDIT'
                    payment_mode = PAYMENT_UNPAID
                else:
                    payment_method = 'CASH' if payment_method_raw == 'CASH' else 'BANK_TRANSFER'
                    payment_mode = 'paid'

            linked_lc_id = data.get('linked_lc_id') or data.get('lc_id')
            try:
                linked_lc_id = int(linked_lc_id) if linked_lc_id not in (None, '', 0, '0') else None
            except (TypeError, ValueError):
                linked_lc_id = None

            try:
                advance_payloads = build_advance_payloads_from_request(
                    conn, data,
                    exchange_rate=exchange_rate,
                    exclude_import_id=edit_id,
                )
            except ValueError as e:
                return jsonify({'error': str(e), 'success': False}), 400

            po_id = data.get('po_id')
            try:
                po_id = int(po_id) if po_id not in (None, '', 0, '0') else None
            except (TypeError, ValueError):
                po_id = None

            c.execute("SELECT name, address FROM suppliers WHERE id = ?", (supplier_id,))
            sup_row = c.fetchone()
            supplier_name = sup_row['name'] if sup_row else f"NCC ID {supplier_id}"
            form_address = (data.get('address') or '').strip()
            supplier_address = form_address or (
                (sup_row['address'] or '') if (sup_row and sup_row['address']) else ''
            )
            if supplier_id and (form_address or (tax_code or '').strip()):
                # Đồng bộ địa chỉ/MST nhập tay vào master NCC
                c.execute(
                    """UPDATE suppliers
                       SET address = COALESCE(NULLIF(?, ''), address),
                           tax_code = COALESCE(NULLIF(?, ''), tax_code)
                       WHERE id = ?""",
                    (form_address, (tax_code or '').strip(), supplier_id),
                )

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
            total_fc = Decimal('0.00')
            for i in items:
                qty = round_money(i.get('qty', 0))
                price_fc = round_money(i.get('buyprice', 0))
                disc_p = Decimal(str(i.get('discount_pct', 0) or i.get('discountPct', 0) or 0))
                line_fc = round_money(qty * price_fc)
                total_fc += line_fc - round_money(line_fc * (disc_p / Decimal('100.00')))
                price_vnd = round_money(price_fc * exchange_rate)
                total_base_vnd += round_money(qty * price_vnd)
            total_base_safe = total_base_vnd if total_base_vnd > 0 else Decimal('1.00')

            split_info = None
            fx_scale = Decimal('1')
            advance_fc_total = Decimal('0.00')
            advance_vnd_total = Decimal('0.00')
            if import_type == 'IMPORT':
                try:
                    validate_import_payment(
                        payment_mode=payment_mode,
                        total_fc=total_fc,
                        advances=advance_payloads,
                        lc_id=linked_lc_id,
                    )
                except ValueError as e:
                    return jsonify({'error': str(e), 'success': False}), 400
                if linked_lc_id:
                    from Services.sme.letter_of_credit import get_lc, get_lc_balance
                    lc_doc = get_lc(conn, linked_lc_id)
                    if not lc_doc or lc_doc.get('status') != 'open':
                        return jsonify({
                            'error': 'L/C không tồn tại hoặc không còn hiệu lực (cần status=open)',
                            'success': False,
                        }), 400
                    if payment_mode == PAYMENT_LC:
                        try:
                            from Services.sme.import_settle import ensure_import_settle_schema
                            ensure_import_settle_schema(conn, commit=False)
                        except Exception:
                            pass
                        try:
                            lc_bal = get_lc_balance(conn, linked_lc_id)
                        except ValueError as e:
                            return jsonify({'error': str(e), 'success': False}), 400
                        remain_lc = Decimal(str(lc_bal.get('remaining_fc') or 0))
                        # Trừ các phiếu nhập khác đã gắn L/C này nhưng chưa tất toán
                        reserved_fc = Decimal('0')
                        try:
                            imp_cols_chk = {
                                r[1] for r in conn.execute('PRAGMA table_info("import")').fetchall()
                            }
                            if 'amount_fc' in imp_cols_chk and 'linked_lc_id' in imp_cols_chk:
                                reserved_sql = """
                                    SELECT COALESCE(SUM(COALESCE(amount_fc, 0)), 0)
                                    FROM "import"
                                    WHERE linked_lc_id = ?
                                """
                                reserved_params: list = [linked_lc_id]
                                if 'settle_journal_id' in imp_cols_chk:
                                    reserved_sql += ' AND COALESCE(settle_journal_id, 0) = 0'
                                if edit_id:
                                    reserved_sql += ' AND id != ?'
                                    reserved_params.append(int(edit_id))
                                reserved_row = conn.execute(reserved_sql, reserved_params).fetchone()
                                reserved_fc = Decimal(str(
                                    reserved_row[0] if reserved_row else 0
                                ))
                        except sqlite3.OperationalError:
                            reserved_fc = Decimal('0')
                        avail_lc = remain_lc - reserved_fc
                        if avail_lc < 0:
                            avail_lc = Decimal('0')
                        if total_fc > avail_lc + Decimal('0.0001'):
                            return jsonify({
                                'error': (
                                    f'L/C {lc_doc.get("lc_no") or linked_lc_id} còn khả dụng '
                                    f'{float(avail_lc):g} NT (số dư sau tất toán {float(remain_lc):g}'
                                    f'{f", đã gắn đợt khác {float(reserved_fc):g}" if reserved_fc > 0 else ""}) '
                                    f'— không đủ cho đợt này ({float(total_fc):g} NT).'
                                ),
                                'success': False,
                            }), 400
                if payment_mode in (PAYMENT_PREPAID_FULL, PAYMENT_PREPAID_PARTIAL) and advance_payloads:
                    split_info = compute_split_fx_goods_vnd(
                        total_fc=total_fc,
                        customs_rate=exchange_rate,
                        advances=advance_payloads,
                    )
                    advance_fc_total = Decimal(str(split_info.get('advance_fc') or 0))
                    advance_vnd_total = Decimal(str(split_info.get('advance_vnd') or 0))
                    customs_only = Decimal(str(split_info.get('customs_only_vnd') or 0))
                    goods_vnd = Decimal(str(split_info.get('goods_vnd') or 0))
                    if customs_only > 0 and goods_vnd > 0:
                        fx_scale = goods_vnd / customs_only
                    # Đồng bộ amount_fc/rate đã scale (nếu ứng vượt)
                    advance_payloads = split_info.get('advances') or advance_payloads

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
                old_stage = ''
                if 'receipt_stage' in old_imp.keys() and old_imp['receipt_stage']:
                    old_stage = str(old_imp['receipt_stage']).strip().upper()
                if old_stage == STAGE_RECEIVED or (
                    'receive_journal_id' in old_imp.keys() and old_imp['receive_journal_id']
                ):
                    receipt_stage = STAGE_RECEIVED
                    skip_physical_stock = False
                elif old_stage == STAGE_TAX_PAID:
                    receipt_stage = STAGE_TAX_PAID
                    skip_physical_stock = True
                elif import_type == 'IMPORT':
                    receipt_stage = STAGE_IN_TRANSIT
                    skip_physical_stock = True

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
                if 'excise_tax_amount' in import_cols:
                    set_parts.append('excise_tax_amount = ?')
                    set_vals.append(0)
                if 'doc_type' in import_cols:
                    set_parts.append('doc_type = ?')
                    set_vals.append(doc_type_input)
                if 'receipt_stage' in import_cols:
                    set_parts.append('receipt_stage = ?')
                    set_vals.append(receipt_stage)
                if 'customs_decl_no' in import_cols:
                    set_parts.append('customs_decl_no = ?')
                    set_vals.append(customs_decl_no)
                if 'customs_decl_date' in import_cols:
                    set_parts.append('customs_decl_date = ?')
                    set_vals.append(customs_decl_date)
                if 'customs_fx_rate' in import_cols:
                    set_parts.append('customs_fx_rate = ?')
                    set_vals.append(customs_fx_rate)
                if 'payment_mode' in import_cols:
                    set_parts.append('payment_mode = ?')
                    set_vals.append(payment_mode)
                if 'amount_fc' in import_cols:
                    set_parts.append('amount_fc = ?')
                    set_vals.append(float(total_fc))
                if 'advance_fc' in import_cols:
                    set_parts.append('advance_fc = ?')
                    set_vals.append(float(advance_fc_total))
                if 'advance_vnd' in import_cols:
                    set_parts.append('advance_vnd = ?')
                    set_vals.append(float(advance_vnd_total))
                if 'linked_lc_id' in import_cols:
                    set_parts.append('linked_lc_id = ?')
                    set_vals.append(linked_lc_id)
                if 'po_id' in import_cols and po_id:
                    set_parts.append('po_id = ?')
                    set_vals.append(po_id)
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
                if 'excise_tax_amount' in import_cols:
                    import_fields.append('excise_tax_amount')
                    import_values.append(0)
                if 'doc_type' in import_cols:
                    import_fields.append('doc_type')
                    import_values.append(doc_type_input)
                if 'receipt_stage' in import_cols:
                    import_fields.append('receipt_stage')
                    import_values.append(receipt_stage)
                if 'customs_decl_no' in import_cols:
                    import_fields.append('customs_decl_no')
                    import_values.append(customs_decl_no)
                if 'customs_decl_date' in import_cols:
                    import_fields.append('customs_decl_date')
                    import_values.append(customs_decl_date)
                if 'customs_fx_rate' in import_cols:
                    import_fields.append('customs_fx_rate')
                    import_values.append(customs_fx_rate)
                if 'payment_mode' in import_cols:
                    import_fields.append('payment_mode')
                    import_values.append(payment_mode)
                if 'amount_fc' in import_cols:
                    import_fields.append('amount_fc')
                    import_values.append(float(total_fc))
                if 'advance_fc' in import_cols:
                    import_fields.append('advance_fc')
                    import_values.append(float(advance_fc_total))
                if 'advance_vnd' in import_cols:
                    import_fields.append('advance_vnd')
                    import_values.append(float(advance_vnd_total))
                if 'linked_lc_id' in import_cols:
                    import_fields.append('linked_lc_id')
                    import_values.append(linked_lc_id)
                if 'po_id' in import_cols and po_id:
                    import_fields.append('po_id')
                    import_values.append(po_id)

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
            total_excise_tax_vnd = Decimal('0.00')
            from Services.sme.tt58_tax_methods import tt58_input_vat_in_inventory_cost
            vat_in_inventory_cost = tt58_input_vat_in_inventory_cost(conn)

            for item in items:
                line_type = _normalize_line_type(
                    item.get('line_type')
                    or item.get('invoice_product_type')
                    or item.get('product_type')
                )
                warehouse_code = (item.get('warehouse_code') or default_warehouse or 'KHO_001').strip()
                if not tracks_retail_inventory(line_type):
                    warehouse_code = warehouse_code or 'KHO_001'
                exp_acct = (item.get('expense_account') or '').strip()
                if line_type == 'service' and not exp_acct:
                    exp_acct = '642'

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
                excise_tax_p = (
                    Decimal(str(
                        item.get('excise_tax_pct', 0)
                        or item.get('ttdb_pct', 0)
                        or 0
                    ))
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
                # Tách tỷ giá tạm ứng / tờ khai → điều chỉnh nguyên giá CIF (VND)
                if import_type == 'IMPORT' and fx_scale != Decimal('1'):
                    line_net_vnd = round_money(line_net_vnd * fx_scale)
                    line_subtotal_vnd = round_money(line_subtotal_vnd * fx_scale)
                    line_disc_vnd = round_money(line_subtotal_vnd - line_net_vnd)
                line_import_tax_vnd = Decimal('0.00')
                line_excise_tax_vnd = Decimal('0.00')
                if import_type == 'IMPORT' and line_type != 'service':
                    # Ưu tiên số tiền tuyệt đối từ tờ khai (nếu gửi); không thì tính theo %
                    raw_nk_amt = item.get('import_tax_amount')
                    if raw_nk_amt not in (None, '') and Decimal(str(raw_nk_amt or 0)) > 0 and import_tax_p <= 0:
                        line_import_tax_vnd = round_money(raw_nk_amt)
                    elif import_tax_p > 0:
                        line_import_tax_vnd = round_money(
                            line_net_vnd * (import_tax_p / Decimal('100.00'))
                        )
                    elif raw_nk_amt not in (None, ''):
                        line_import_tax_vnd = round_money(raw_nk_amt)

                    raw_excise_amt = item.get('excise_tax_amount') or item.get('ttdb_amount')
                    # TTĐB tính trên (CIF sau CK + thuế NK)
                    excise_base = line_net_vnd + line_import_tax_vnd
                    if (
                        raw_excise_amt not in (None, '')
                        and Decimal(str(raw_excise_amt or 0)) > 0
                        and excise_tax_p <= 0
                    ):
                        line_excise_tax_vnd = round_money(raw_excise_amt)
                    elif excise_tax_p > 0:
                        line_excise_tax_vnd = round_money(
                            excise_base * (excise_tax_p / Decimal('100.00'))
                        )
                    elif raw_excise_amt not in (None, ''):
                        line_excise_tax_vnd = round_money(raw_excise_amt)

                total_import_tax_vnd += line_import_tax_vnd
                total_excise_tax_vnd += line_excise_tax_vnd
                line_extra_vnd = round_money((line_subtotal_vnd / total_base_safe) * extra_cost)
                # Cơ sở GTGT hàng NK = CIF + NK + TTĐB
                if import_type == 'IMPORT':
                    tax_base_vnd = line_net_vnd + line_import_tax_vnd + line_excise_tax_vnd
                else:
                    tax_base_vnd = line_net_vnd
                line_vat_vnd = round_money(tax_base_vnd * (tax_p / Decimal('100.00')))
                # Giá vốn hóa HH/NVL/TSCĐ/CCDC (TH3/TH4: không gồm VAT; TH1/TH2: gồm VAT)
                line_inventory_value_vnd = (
                    line_net_vnd + line_import_tax_vnd + line_excise_tax_vnd + line_extra_vnd
                )
                if vat_in_inventory_cost:
                    line_inventory_value_vnd += line_vat_vnd
                # Tổng hiển thị: hàng + thuế NK/TTĐB + VAT + CP khác
                if import_type == 'IMPORT':
                    line_total_payment_vnd = (
                        line_net_vnd + line_import_tax_vnd + line_excise_tax_vnd
                        + line_vat_vnd + line_extra_vnd
                    )
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
                        'expense_account': exp_acct or '642',
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
                        'expense_account': exp_acct or '642',
                        'line_total': float(line_total_payment_vnd),
                    })
                    continue

                pid = item.get('product_id')
                p_info = p_map.get(pid)
                if not p_info:
                    continue

                if inv_name:
                    try:
                        from Services.product_match import save_product_alias
                        save_product_alias(
                            conn,
                            product_id=int(pid),
                            invoice_name=inv_name,
                            supplier_id=supplier_id,
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
                    'import_tax_amount': float(line_import_tax_vnd),
                    'excise_tax_pct': float(excise_tax_p),
                    'excise_tax_amount': float(line_excise_tax_vnd),
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
                    'import_tax_pct': float(import_tax_p),
                    'import_tax_amount': float(line_import_tax_vnd),
                    'excise_tax_pct': float(excise_tax_p),
                    'excise_tax_amount': float(line_excise_tax_vnd),
                    'payment_amt': float(line_total_payment_vnd),
                    'product_name': product_name,
                    'unit': unit_in,
                    'line_type': line_type,
                    'warehouse_code': warehouse_code,
                    'expense_account': exp_acct or None,
                })
                detail_id = c.lastrowid

                if line_type == 'fixed_asset' and not skip_physical_stock:
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
                        capitalized_cost=float(line_inventory_value_vnd),
                        so_thang_khau_hao=item.get('so_thang_khau_hao') or item.get('depreciation_months'),
                        ngay_bat_dau_su_dung=item.get('ngay_bat_dau_su_dung') or item.get('start_date') or import_date,
                    )
                    fixed_assets_created += 1
                elif line_type == 'tools' and not skip_physical_stock:
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
                        capitalized_cost=float(line_inventory_value_vnd),
                    )
                    tools_created += 1

                if tracks_retail_inventory(line_type) and not skip_physical_stock:
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
            if import_type == 'IMPORT':
                # paid_amount = phần đã ứng (VND theo tỷ giá ngày ứng) — không ghi tiền mặt trên PN
                if payment_mode in (PAYMENT_PREPAID_FULL, PAYMENT_PREPAID_PARTIAL):
                    final_paid = float(advance_vnd_total)
                else:
                    final_paid = 0.0
            else:
                final_paid = total_final_float if payment_status_input == 'Đã thanh toán' else 0.0
            update_parts = ['total_value = ?', 'paid_amount = ?']
            update_vals = [total_final_float, final_paid]
            if 'import_tax_amount' in import_cols:
                update_parts.append('import_tax_amount = ?')
                update_vals.append(float(total_import_tax_vnd))
            if 'excise_tax_amount' in import_cols:
                update_parts.append('excise_tax_amount = ?')
                update_vals.append(float(total_excise_tax_vnd))
            if 'payment_mode' in import_cols:
                update_parts.append('payment_mode = ?')
                update_vals.append(payment_mode)
            if 'amount_fc' in import_cols:
                update_parts.append('amount_fc = ?')
                update_vals.append(float(total_fc))
            if 'advance_fc' in import_cols:
                update_parts.append('advance_fc = ?')
                update_vals.append(float(advance_fc_total))
            if 'advance_vnd' in import_cols:
                update_parts.append('advance_vnd = ?')
                update_vals.append(float(advance_vnd_total))
            if 'linked_lc_id' in import_cols:
                update_parts.append('linked_lc_id = ?')
                update_vals.append(linked_lc_id)
            update_vals.append(import_id)
            c.execute(
                f"UPDATE import SET {', '.join(update_parts)} WHERE id = ?",
                update_vals,
            )

            if import_type == 'IMPORT':
                replace_import_advances(
                    conn,
                    import_id,
                    advance_payloads if payment_mode in (
                        PAYMENT_PREPAID_FULL, PAYMENT_PREPAID_PARTIAL,
                    ) else [],
                    commit=False,
                )
                if linked_lc_id:
                    try:
                        conn.execute(
                            """
                            UPDATE sme_lc_docs
                            SET import_id = ?, updated_at = datetime('now','localtime')
                            WHERE id = ? AND status = 'open'
                            """,
                            (import_id, linked_lc_id),
                        )
                    except sqlite3.OperationalError:
                        pass
            else:
                replace_import_advances(conn, import_id, [], commit=False)

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
                excise_tax_amount=total_excise_tax_vnd,
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
                "import_type": import_type,
                "payment_mode": payment_mode,
                "payment_status": payment_status_input,
                "linked_lc_id": linked_lc_id,
                "advance_fc": float(advance_fc_total),
                "advance_vnd": float(advance_vnd_total),
                "amount_fc": float(total_fc),
                "split_fx": split_info,
                "receipt_stage": receipt_stage,
                "in_transit": bool(skip_physical_stock),
                "message": (
                    'Đã ghi nhận hàng mua đang đi đường (TK 151). '
                    'Dùng «Nộp thuế HQ» rồi «Nhập kho thực tế» khi hàng về kho.'
                    if skip_physical_stock else
                    ('Đã cập nhật phiếu nhập.' if edit_id else 'Đã lập phiếu nhập kho.')
                ),
            })

        except Exception as e:
            conn.rollback()
            logging.error(f"LỖI import_sme: {str(e)}", exc_info=True)
            return jsonify({"error": f"Lỗi xử lý: {str(e)}"}), 500
        finally:
            conn.close()

    @app.route('/api/sme/import-payment/supplier-advances', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_import_supplier_advances():
        """PC tạm ứng NCC còn số dư (nhiều đợt chứng từ) — chọn trên form NK."""
        from Services.sme.import_payment import list_supplier_advances
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            unused = str(request.args.get('unused_only', '1')).lower() not in ('0', 'false', 'no')
            include_id = request.args.get('include_import_id') or request.args.get('import_id')
            try:
                include_id = int(include_id) if include_id not in (None, '', '0') else None
            except (TypeError, ValueError):
                include_id = None
            items = list_supplier_advances(
                conn,
                supplier_name=request.args.get('supplier_name'),
                currency=request.args.get('currency'),
                unused_only=unused,
                include_import_id=include_id,
            )
            return jsonify({'success': True, 'items': items})
        except Exception as e:
            logger.exception('api_sme_import_supplier_advances')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/import-payment/open-lcs', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_import_open_lcs():
        """L/C còn số dư để gắn vào phiếu nhập khẩu (nhiều đợt chứng từ)."""
        from Services.sme.letter_of_credit import list_lc_docs, get_lc
        from Services.sme.branches import request_branch_filter
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            items = list_lc_docs(
                conn,
                status='open',
                branch_code=request_branch_filter(),
                with_balance=True,
                only_linkable=True,
            )
            include_id = request.args.get('include_lc_id', type=int)
            if include_id and not any(int(x.get('id') or 0) == include_id for x in items):
                extra = get_lc(conn, include_id)
                if extra and extra.get('status') != 'void':
                    items = [extra] + items
            return jsonify({'success': True, 'items': items})
        except Exception as e:
            logger.exception('api_sme_import_open_lcs')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/import/<int:import_id>/ap-summary', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_import_ap_summary(import_id):
        """Tóm tắt công nợ 331 còn lại (CIF) để quyết toán / tất toán L/C."""
        from Services.sme.import_settle import get_import_ap_summary, ensure_import_settle_schema
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            ensure_import_settle_schema(conn, commit=False)
            data = get_import_ap_summary(conn, import_id)
            return jsonify({'success': True, **data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            logger.exception('api_sme_import_ap_summary')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/import/<int:import_id>/settle-ap', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_import_settle_ap(import_id):
        """Trả phần còn lại NCC NK — Nợ 331 / Có 1122 + CLTG 635/515."""
        from Services.sme.import_settle import settle_import_supplier_ap
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            result = settle_import_supplier_ap(
                conn,
                import_id,
                settle_date=data.get('date') or data.get('settle_date'),
                amount_fc=data.get('amount_fc'),
                exchange_rate=data.get('exchange_rate') or data.get('fx_rate'),
                payment_method=data.get('payment_method') or 'bank_fx',
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify({'success': True, **result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_import_settle_ap')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/import/<int:import_id>/settle-lc', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_import_settle_lc(import_id):
        """Tất toán L/C — Nợ 331 / Có 244 (+ 1122 nếu thiếu)."""
        from Services.sme.import_settle import settle_import_by_lc
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            result = settle_import_by_lc(
                conn,
                import_id,
                settle_date=data.get('date') or data.get('settle_date'),
                shortfall_exchange_rate=data.get('exchange_rate') or data.get('fx_rate'),
                payment_method=data.get('payment_method') or 'bank_fx',
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify({'success': True, **result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_import_settle_lc')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/import/<int:import_id>/pay-customs-tax', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_import_pay_customs_tax(import_id):
        """G2 — Nộp thuế HQ: Nợ 3333/3332/33312 / Có 112."""
        from Services.sme.import_transit import pay_customs_taxes
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            result = pay_customs_taxes(
                conn,
                import_id,
                pay_date=data.get('date') or data.get('pay_date'),
                payment_method=data.get('payment_method') or 'bank',
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify({'success': True, **result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_import_pay_customs_tax')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/import/<int:import_id>/receive-warehouse', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_import_receive_warehouse(import_id):
        """G3 — Nhập kho thực tế: Nợ 156/152 / Có 151 + tăng tồn."""
        from Services.sme.import_transit import receive_import_to_warehouse
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            result = receive_import_to_warehouse(
                conn,
                import_id,
                receive_date=data.get('date') or data.get('receive_date'),
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify({'success': True, **result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_import_receive_warehouse')
            return jsonify({'success': False, 'error': str(e)}), 500
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
            from Services.sme.coa_service import (
                account_tree,
                ensure_sme_coa_ready,
                list_accounts,
                list_counterpart_accounts,
            )
            meta = ensure_sme_coa_ready(conn)
            q = (request.args.get('q') or '').strip() or None
            active = request.args.get('active', '1') != '0'
            postable_only = str(request.args.get('postable') or '').lower() in (
                '1', 'true', 'yes',
            )
            counterpart = str(request.args.get('counterpart') or '').lower() in (
                '1', 'true', 'yes',
            )
            levels_raw = (request.args.get('levels') or '').strip()
            levels = None
            if levels_raw:
                levels = []
                for part in levels_raw.replace(';', ',').split(','):
                    part = part.strip()
                    if part.isdigit():
                        levels.append(int(part))
                if not levels:
                    levels = None
            # Lọc theo cấp / ghi sổ / tìm kiếm → danh sách phẳng; ngược lại trả cây UI
            if counterpart:
                rows = list_counterpart_accounts(conn, active_only=active)
                if q:
                    ql = q.lower()
                    rows = [
                        r for r in rows
                        if ql in str(r.get('code') or '').lower()
                        or ql in str(r.get('name') or '').lower()
                    ]
                if postable_only:
                    rows = [r for r in rows if int(r.get('is_postable') or 0) == 1]
            elif q or levels is not None or postable_only:
                rows = list_accounts(
                    conn,
                    active_only=active,
                    q=q,
                    postable_only=postable_only,
                    levels=levels,
                )
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
            from Services.sme.bank_accounts import preview_bank_split
            ensure_sme_coa_ready(conn)
            return jsonify({
                'success': True,
                'next_code': suggest_next_child_code(conn, parent_code),
                'bank_split_preview': preview_bank_split(conn, parent_code),
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
                set_as_default=bool(payload.get('set_as_default')),
            )
            return jsonify({
                'success': True,
                'data': created,
                'automation_message': created.get('automation_message') if isinstance(created, dict) else None,
            })
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

    @app.route('/api/sme/coa/<code>/set-default', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_coa_set_default(code):
        """Đặt TK leaf làm mặc định ghi sổ cho các vai trò thuộc nhóm đó."""
        conn = get_db_connection()
        try:
            from Services.sme.account_roles import set_default_posting_flag
            from Services.sme.coa_service import ensure_sme_coa_ready
            ensure_sme_coa_ready(conn)
            payload = request.get_json(silent=True) or {}
            is_default = payload.get('is_default', True)
            if isinstance(is_default, str):
                is_default = is_default.strip().lower() not in ('0', 'false', 'no')
            data = set_default_posting_flag(conn, code, is_default=bool(is_default), commit=True)
            roles = []
            try:
                from Services.sme.account_roles import roles_for_account
                roles = roles_for_account(conn, code)
            except Exception:
                pass
            return jsonify({'success': True, 'data': data, 'roles': roles})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            logger.exception('api_sme_coa_set_default')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/coa/roles', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_coa_roles():
        conn = get_db_connection()
        try:
            from Services.sme.account_roles import list_roles
            from Services.sme.coa_service import ensure_sme_coa_ready
            ensure_sme_coa_ready(conn)
            category = (request.args.get('category') or '').strip() or None
            return jsonify({'success': True, 'data': list_roles(conn, category=category)})
        except Exception as e:
            logger.exception('api_sme_coa_roles')
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
    @app.route('/api/sme/journal/renumber', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_journal_renumber():
        """Đánh lại số BT liên tục theo ngày ghi sổ."""
        from Services.sme.journal_engine import renumber_journal_entries
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap_sme_db()
            result = renumber_journal_entries(conn, commit=True)
            return jsonify({'success': True, **result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e), 'message': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_journal_renumber')
            return jsonify({'success': False, 'error': str(e), 'message': str(e)}), 500
        finally:
            conn.close()

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

    @app.route('/api/sme/journal', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_journal_create():
        """Lập bút toán thủ công (nhật ký)."""
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.sme.journal_engine import post_journal_entry
            raw_lines = payload.get('lines') or []
            lines = []
            for i, ln in enumerate(raw_lines, start=1):
                acct = str(ln.get('account_code') or ln.get('account') or '').strip()
                debit = float(ln.get('debit') or 0)
                credit = float(ln.get('credit') or 0)
                if not acct or (debit <= 0 and credit <= 0):
                    continue
                lines.append({
                    'sequence': i,
                    'account_code': acct,
                    'debit': debit,
                    'credit': credit,
                    'description': ln.get('description') or payload.get('description') or '',
                })
            if not lines:
                return jsonify({'success': False, 'error': 'Thiếu dòng hạch toán'}), 400
            date_s = (payload.get('posting_date') or payload.get('date') or '')[:10]
            if not date_s:
                return jsonify({'success': False, 'error': 'Thiếu ngày ghi sổ'}), 400
            actor = (
                (session.get('user') or {}).get('username')
                or session.get('user_name') or session.get('username')
            )
            entry = post_journal_entry(
                conn,
                posting_date=date_s,
                document_date=(payload.get('document_date') or date_s)[:10],
                document_type=(payload.get('document_type') or 'BT').strip() or 'BT',
                document_no=(payload.get('document_no') or '').strip() or None,
                business_type=(payload.get('business_type') or 'THU_CONG').strip() or 'THU_CONG',
                description=payload.get('description') or 'Bút toán thủ công',
                created_by=actor,
                lines=lines,
            )
            conn.commit()
            return jsonify({'success': True, 'data': entry})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
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

    @app.route('/api/sme/journal/<int:entry_id>', methods=['PUT', 'PATCH'])
    @login_required
    @require_sme_regime
    def api_sme_journal_update(entry_id):
        """Sửa tại chỗ khi kỳ chưa khóa — không ghi đảo; có audit log."""
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.audit_log import write_audit
            from Services.sme.branch_filter import assert_row_in_branch
            from Services.sme.journal_engine import update_journal_entry
            assert_row_in_branch(
                conn, 'sme_journal_entries', entry_id, label='Bút toán',
            )
            actor = (session.get('user') or {}).get('username') or session.get('user_name') or session.get('username')
            result = update_journal_entry(
                conn,
                entry_id,
                lines=payload.get('lines'),
                description=payload.get('description'),
                document_no=payload.get('document_no'),
                document_date=payload.get('document_date'),
                reference_document=payload.get('reference_document'),
                posting_date=payload.get('posting_date'),
                updated_by=actor,
                reason=(payload.get('reason') or 'Sửa bút toán từ nhật ký'),
            )
            conn.commit()
            write_audit(
                'update',
                'sme_journal',
                f"Sửa bút toán {result.get('entry_no') or entry_id}: {result.get('reason')}",
                entity_type='sme_journal_entry',
                entity_id=entry_id,
                entity_label=result.get('entry_no'),
                old_data=result.get('old'),
                new_data=result.get('new'),
            )
            return jsonify({'success': True, 'data': result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/journal/<int:entry_id>', methods=['DELETE'])
    @login_required
    @require_sme_regime
    def api_sme_journal_delete(entry_id):
        """Xóa hoàn toàn khi kỳ chưa khóa — không ghi đảo; có audit log."""
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.audit_log import write_audit
            from Services.sme.branch_filter import assert_row_in_branch
            from Services.sme.journal_engine import delete_journal_entry
            assert_row_in_branch(
                conn, 'sme_journal_entries', entry_id, label='Bút toán',
            )
            actor = (session.get('user') or {}).get('username') or session.get('user_name') or session.get('username')
            result = delete_journal_entry(
                conn,
                entry_id,
                reason=(payload.get('reason') or 'Xóa bút toán từ nhật ký'),
                deleted_by=actor,
            )
            conn.commit()
            snap = result.get('snapshot') or {}
            write_audit(
                'delete',
                'sme_journal',
                f"Xóa bút toán {snap.get('entry_no') or entry_id}: {snap.get('reason')}",
                entity_type='sme_journal_entry',
                entity_id=entry_id,
                entity_label=snap.get('entry_no'),
                old_data=snap,
                new_data=None,
            )
            return jsonify({'success': True, 'data': result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/journal/<int:entry_id>/reverse', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_journal_reverse(entry_id):
        """Đảo bút toán — chỉ khi kỳ đã khóa (điều chỉnh sau kê khai / khóa sổ)."""
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        try:
            from Services.audit_log import write_audit
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
                require_locked=True,
            )
            conn.commit()
            write_audit(
                'reverse',
                'sme_journal',
                f"Đảo bút toán #{entry_id} → {rev.get('entry_no') or rev.get('id')}",
                entity_type='sme_journal_entry',
                entity_id=entry_id,
                entity_label=rev.get('reference_document') or str(entry_id),
                old_data={'entry_id': entry_id},
                new_data=rev,
            )
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
            from Services.sme.regime_profile import get_ledger_profile
            if get_ledger_profile(conn).get('is_tt58_micro'):
                return jsonify({
                    'success': False,
                    'error': 'TT58 siêu nhỏ không lập thuyết minh B09-DN. Chỉ lập B01-DNSN và B02-DNSN.',
                }), 400
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

    @app.route('/SME_cap-nhat-kien-thuc')
    @login_required
    @require_sme_regime
    def SME_cap_nhat_kien_thuc():
        """Cập nhật kiến thức riêng SME — chỉ tin/pháp luật doanh nghiệp."""
        from Services.knowledge_service import (
            KNOWLEDGE_AUDIENCES,
            SME_KNOWLEDGE_CATEGORIES,
            count_drafts,
            seed_default_articles,
        )
        seed_default_articles()
        can_manage = session.get('role') == 'master'
        return render_template(
            'KeToanSME/cap_nhat_kien_thuc.html',
            categories=SME_KNOWLEDGE_CATEGORIES,
            audiences={
                k: v for k, v in KNOWLEDGE_AUDIENCES.items() if k in ('all', 'dn')
            },
            can_manage=can_manage,
            tenant_audience='dn',
            draft_count=count_drafts() if can_manage else 0,
        )

    @app.route('/api/sme/knowledge/articles', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_knowledge_articles():
        """API bản tin chỉ lọc audience DN / chung phù hợp doanh nghiệp."""
        from Services.knowledge_service import list_sme_articles
        category = (request.args.get('category') or '').strip() or None
        keyword = (request.args.get('keyword') or '').strip() or None
        for_mgmt = session.get('role') == 'master'
        status_filter = (request.args.get('status') or '').strip() or None
        if for_mgmt and request.args.get('all_status') == '1':
            status_filter = status_filter or 'all'
        data = list_sme_articles(
            category=category,
            keyword=keyword,
            for_management=for_mgmt,
            status_filter=status_filter,
        )
        return jsonify({
            'success': True,
            'data': data,
            'tenant_audience': 'dn',
        })

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
            resolve_vat_filing_policy,
            update_registry_settings,
            VAT_MONTHLY_REVENUE_THRESHOLD,
        )
        profile = get_current_tenant_profile()
        features = profile.get('features') or {}
        settings = dict(profile.get('settings') or {})
        policy = resolve_vat_filing_policy(profile.get('accounting_regime'), settings)
        current = normalize_vat_filing_period(
            profile.get('vat_filing_period')
            or features.get('vat_filing_period')
            or features.get('filing_period'),
            default=policy['default_period'],
        )
        if policy['must_monthly']:
            current = 'monthly'
        elif current == 'monthly' and not policy['can_select_monthly']:
            current = 'quarterly'

        if request.method == 'GET':
            return jsonify({
                'success': True,
                'data': {
                    'vat_filing_period': current,
                    'monthly_vat_filing': current == 'monthly',
                    'accounting_regime': profile.get('accounting_regime'),
                    'default_for_regime': policy['default_period'],
                    'prior_year_revenue': policy['prior_year_revenue'],
                    'revenue_threshold': VAT_MONTHLY_REVENUE_THRESHOLD,
                    'revenue_over_50b': policy['revenue_over_50b'],
                    'can_select_monthly': policy['can_select_monthly'],
                    'must_monthly': policy['must_monthly'],
                    'allowed_periods': policy['allowed_periods'],
                    'hint': policy['hint'],
                },
            })

        payload = request.get_json(silent=True) or {}
        # Cập nhật doanh thu năm trước (nếu gửi kèm)
        if 'prior_year_revenue' in payload or 'vat_revenue_over_50b' in payload:
            if 'prior_year_revenue' in payload and payload.get('prior_year_revenue') not in (None, ''):
                try:
                    settings['prior_year_revenue'] = float(
                        str(payload.get('prior_year_revenue')).replace(',', '').replace(' ', '')
                    )
                except (TypeError, ValueError):
                    return jsonify({'success': False, 'error': 'Doanh thu năm trước không hợp lệ'}), 400
            if 'vat_revenue_over_50b' in payload:
                settings['vat_revenue_over_50b'] = bool(payload.get('vat_revenue_over_50b'))
                if payload.get('vat_revenue_over_50b') is True and 'prior_year_revenue' not in payload:
                    settings['prior_year_revenue'] = float(VAT_MONTHLY_REVENUE_THRESHOLD) + 1
                if payload.get('vat_revenue_over_50b') is False and 'prior_year_revenue' not in payload:
                    settings['prior_year_revenue'] = 0

        policy = resolve_vat_filing_policy(profile.get('accounting_regime'), settings)
        period = normalize_vat_filing_period(
            payload.get('vat_filing_period') or payload.get('filing_period'),
            default=policy['default_period'],
        )
        if period == 'monthly' and not policy['can_select_monthly']:
            return jsonify({
                'success': False,
                'error': (
                    'Doanh thu năm trước ≤ 50 tỷ — chỉ kê khai theo quý. '
                    'Đánh dấu doanh thu > 50 tỷ nếu đơn vị thuộc diện kê khai tháng.'
                ),
            }), 400
        if policy['must_monthly']:
            period = 'monthly'

        tenant_id = (
            getattr(g, 'tenant_id', None)
            or session.get('last_tenant_id')
            or profile.get('tenant_id')
        )
        if not tenant_id:
            return jsonify({'success': False, 'error': 'Không xác định được tenant'}), 400

        # User chủ động chọn tháng → xác nhận hết cảnh báo cuối năm
        if period == 'monthly' and (
            policy['revenue_over_50b']
            or settings.get('vat_revenue_over_50b')
            or (settings.get('vat_filing_year_end_alert') or {}).get('active')
        ):
            from Services.sme.vat_filing_alert import confirm_vat_filing_monthly
            conf = confirm_vat_filing_monthly(
                tenant_id=tenant_id,
                settings=settings,
                confirmed_by=session.get('user_name') or session.get('username'),
            )
            try:
                from Services.tenant_profile import load_tenant_profile
                g.tenant_profile = load_tenant_profile(tenant_id)
            except Exception:
                pass
            return jsonify({
                'success': True,
                'data': {
                    'vat_filing_period': 'monthly',
                    'monthly_vat_filing': True,
                    'revenue_over_50b': True,
                    'can_select_monthly': True,
                    'alert_cleared': conf.get('alert_cleared'),
                    'hint': 'Đã xác nhận kê khai theo tháng — cảnh báo cuối năm đã tắt.',
                    'message': 'Đã lưu kỳ kê khai GTGT theo tháng',
                },
            })

        patch = {
            'vat_filing_period': period,
            'filing_period': period,
        }
        if 'prior_year_revenue' in settings:
            patch['prior_year_revenue'] = settings['prior_year_revenue']
        if 'vat_revenue_over_50b' in settings:
            patch['vat_revenue_over_50b'] = settings['vat_revenue_over_50b']
        ok = update_registry_settings(tenant_id, patch)
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
                'revenue_over_50b': policy['revenue_over_50b'],
                'can_select_monthly': policy['can_select_monthly'],
                'hint': policy['hint'],
                'message': 'Đã lưu kỳ kê khai GTGT',
            },
        })

    @app.route('/api/sme/micro-enterprise', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_micro_enterprise():
        """Tiêu chí DN siêu nhỏ (NĐ 80) + cảnh báo chuyển TT58 → TT99."""
        from Services.sme.micro_enterprise import (
            SECTOR_AGRI_INDUSTRY,
            SECTOR_TRADE_SERVICE,
            SECTOR_LABELS,
            evaluate_micro_criteria,
            evaluate_tt58_to_tt99_alert,
            get_tt99_switch_alert,
            normalize_enterprise_sector,
            resolve_enterprise_sector,
        )
        from Services.tenant_profile import (
            get_current_tenant_profile,
            load_tenant_profile,
            update_registry_settings,
        )
        profile = get_current_tenant_profile()
        settings = dict(profile.get('settings') or {})
        tenant_id = (
            getattr(g, 'tenant_id', None)
            or session.get('last_tenant_id')
            or profile.get('tenant_id')
        )
        year = request.args.get('year', type=int) or datetime.now().year
        if datetime.now().month < 12 and request.method == 'GET' and not request.args.get('year'):
            # Mặc định đánh giá năm trước liền kề khi chưa hết năm
            year = datetime.now().year - 1 if datetime.now().month == 1 else datetime.now().year

        if request.method == 'POST':
            payload = request.get_json(silent=True) or {}
            action = (payload.get('action') or 'save_sector').strip().lower()
            if not tenant_id:
                return jsonify({'success': False, 'error': 'Không xác định tenant'}), 400
            if action == 'save_sector':
                sector = normalize_enterprise_sector(payload.get('enterprise_sector'))
                patch = {'enterprise_sector': sector}
                if payload.get('avg_bhxh_headcount') not in (None, ''):
                    try:
                        patch['avg_bhxh_headcount'] = float(payload.get('avg_bhxh_headcount'))
                    except (TypeError, ValueError):
                        return jsonify({'success': False, 'error': 'Số LĐ BHXH không hợp lệ'}), 400
                ok = update_registry_settings(tenant_id, patch)
                if not ok:
                    return jsonify({'success': False, 'error': 'Không lưu được'}), 500
                try:
                    g.tenant_profile = load_tenant_profile(tenant_id)
                    settings = dict((g.tenant_profile or {}).get('settings') or {})
                except Exception:
                    settings.update(patch)
            elif action == 'evaluate':
                year = int(payload.get('year') or year)
            else:
                return jsonify({'success': False, 'error': 'action không hợp lệ'}), 400

        conn = get_db_connection()
        try:
            criteria = evaluate_micro_criteria(conn, fiscal_year=year, settings=settings)
            alert_eval = None
            # Chỉ persist khi trang thuế/NSNN chủ động (?persist=1) — tránh ghi registry mỗi lần GET
            do_persist = str(request.args.get('persist', '')).lower() in ('1', 'true', 'yes')
            if (profile.get('accounting_regime') or '').upper() == 'SME_MICRO_TT58':
                alert_eval = evaluate_tt58_to_tt99_alert(
                    conn, tenant_id=str(tenant_id or ''), fiscal_year=year,
                    settings=settings, persist=bool(tenant_id) and do_persist,
                )
                if do_persist:
                    try:
                        if tenant_id:
                            g.tenant_profile = load_tenant_profile(tenant_id)
                            settings = dict((g.tenant_profile or {}).get('settings') or {})
                    except Exception:
                        pass
            alert = get_tt99_switch_alert(settings)
            return jsonify({
                'success': True,
                'data': {
                    'criteria': criteria,
                    'alert': alert,
                    'evaluation': alert_eval,
                    'enterprise_sector': resolve_enterprise_sector(settings),
                    'sector_options': [
                        {'value': SECTOR_AGRI_INDUSTRY, 'label': SECTOR_LABELS[SECTOR_AGRI_INDUSTRY]},
                        {'value': SECTOR_TRADE_SERVICE, 'label': SECTOR_LABELS[SECTOR_TRADE_SERVICE]},
                    ],
                    'accounting_regime': profile.get('accounting_regime'),
                    'hint': (
                        'DN siêu nhỏ: LĐ BHXH BQ năm ≤ 10 và (DT ≤ trần lĩnh vực HOẶC vốn ≤ 3 tỷ). '
                        'Hết diện → chuyển TT99 (Master đổi chế độ kế toán).'
                    ),
                },
            })
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/sync-tt99', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_sync_tt99():
        """Đồng bộ / kiểm tra toàn vẹn dữ liệu theo TT99 (sau khi chuyển chế độ)."""
        from Services.sme.migrate_tt58_to_tt99 import (
            migrate_tenant_to_tt99,
            verify_balance_sheet,
            verify_journal_integrity,
            verify_required_accounts,
        )
        from Services.tenant_profile import get_current_tenant_profile, normalize_accounting_regime
        from flask import session

        profile = get_current_tenant_profile()
        tenant_id = profile.get('tenant_id') or getattr(g, 'tenant_id', None)
        settings = dict(profile.get('settings') or {})
        regime = normalize_accounting_regime(
            profile.get('accounting_regime') or settings.get('accounting_regime')
        )

        conn = get_db_connection()
        try:
            if request.method == 'GET':
                checks = {
                    'required_accounts': verify_required_accounts(conn),
                    'journals': verify_journal_integrity(conn),
                    'balance_sheet': verify_balance_sheet(conn),
                    'accounting_regime': regime,
                    'ledger_profile': None,
                }
                try:
                    from Services.sme.regime_profile import get_ledger_profile
                    checks['ledger_profile'] = get_ledger_profile(conn)
                except Exception:
                    pass
                ok = all(
                    (checks[k].get('ok', True) if isinstance(checks[k], dict) else True)
                    for k in ('required_accounts', 'journals', 'balance_sheet')
                )
                return jsonify({'success': True, 'data': {'ok': ok, 'checks': checks}})

            if regime not in ('SME_TT99', 'SME_MICRO_TT58', 'SME'):
                return jsonify({
                    'success': False,
                    'error': f'Chế độ hiện tại ({regime}) không đồng bộ sang TT99 tại đây. Dùng Master đổi chế độ.',
                }), 400
            payload = request.get_json(silent=True) or {}
            # Tenant chỉ đồng bộ khi đã / đang chuyển TT99; TT58 → buộc Master đổi regime trước
            if regime == 'SME_MICRO_TT58' and not payload.get('allow_from_tt58'):
                return jsonify({
                    'success': False,
                    'error': 'Cần Master đổi accounting_regime sang SME_TT99 trước, hệ thống sẽ tự đồng bộ.',
                    'hint': 'Hoặc gọi POST /api/master/tenants/<id>/sync-tt99',
                }), 400
            result = migrate_tenant_to_tt99(
                conn,
                tenant_id=str(tenant_id or ''),
                settings=settings,
                update_registry=True,
                migrated_by=session.get('username') or session.get('user'),
                force_coa_refresh=bool(payload.get('force_coa_refresh')),
            )
            conn.commit()
            return jsonify({
                'success': bool(result.get('synced') or result.get('ok')),
                'integrity_ok': bool(result.get('integrity_ok')),
                'data': result,
            })
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/vat-filing-alert', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_vat_filing_alert():
        """Cảnh báo DT > 50 tỷ → phải kê khai tháng từ năm sau; POST confirm/evaluate."""
        from Services.sme.vat_filing_alert import (
            confirm_vat_filing_monthly,
            evaluate_year_end_vat_filing,
            get_vat_filing_alert,
        )
        from Services.tenant_profile import get_current_tenant_profile, load_tenant_profile
        profile = get_current_tenant_profile()
        settings = dict(profile.get('settings') or {})
        tenant_id = (
            getattr(g, 'tenant_id', None)
            or session.get('last_tenant_id')
            or profile.get('tenant_id')
        )
        if not tenant_id:
            return jsonify({'success': False, 'error': 'Không xác định tenant'}), 400

        if request.method == 'GET':
            # Không auto-apply / ghi DB trên GET — tránh làm chậm mọi trang;
            # chuyển kỳ tự động do cron năm mới hoặc trang Thuế NSNN.
            alert = get_vat_filing_alert(settings)
            return jsonify({
                'success': True,
                'data': {
                    'alert': alert,
                    'auto_applied': None,
                    'vat_filing_period': (
                        (g.tenant_profile or profile).get('vat_filing_period')
                        or settings.get('vat_filing_period')
                    ),
                },
            })

        payload = request.get_json(silent=True) or {}
        action = (payload.get('action') or 'confirm').strip().lower()
        conn = get_db_connection()
        try:
            if action == 'evaluate':
                year = int(payload.get('year') or datetime.now().year)
                out = evaluate_year_end_vat_filing(
                    conn, tenant_id=tenant_id, fiscal_year=year,
                    settings=settings, persist=True,
                )
                conn.commit()
                try:
                    g.tenant_profile = load_tenant_profile(tenant_id)
                except Exception:
                    pass
                return jsonify({'success': True, 'data': out})
            if action == 'confirm':
                out = confirm_vat_filing_monthly(
                    tenant_id=tenant_id,
                    settings=settings,
                    confirmed_by=session.get('user_name') or session.get('username'),
                )
                try:
                    g.tenant_profile = load_tenant_profile(tenant_id)
                except Exception:
                    pass
                return jsonify({'success': True, 'data': out})
            return jsonify({'success': False, 'error': 'action không hợp lệ'}), 400
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

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

    @app.route('/api/sme/hub-group-metrics', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_hub_group_metrics():
        from Services.sme_menu import get_sme_group_by_id
        from Services.sme.hub_group_metrics import fetch_hub_group_metrics
        group_id = (request.args.get('group_id') or '').strip()
        group = get_sme_group_by_id(group_id)
        if not group:
            return jsonify({'success': False, 'error': 'Không tìm thấy nhóm menu'}), 404
        conn = get_db_connection()
        try:
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or datetime.now().month
            data = fetch_hub_group_metrics(
                conn, group,
                fiscal_year=year,
                period_to=period_to,
                branch_code=_sme_branch_arg(),
            )
            resp = jsonify({'success': True, 'data': data})
            resp.headers['Cache-Control'] = 'private, max-age=30'
            return resp
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            logger.exception('api_sme_hub_group_metrics')
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

    @app.route('/api/sme/debt/aging')
    @login_required
    @require_sme_regime
    def api_sme_debt_aging():
        from Services.sme.debt_aging import debt_aging_summary
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            as_of = (request.args.get('as_of') or '').strip() or None
            return jsonify({'success': True, 'data': debt_aging_summary(conn, as_of=as_of)})
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
            from Services.sme.period_lock import (
                get_filing_close_for_month,
                is_year_locked,
                list_filing_closes,
                list_locked_periods,
            )
            year = request.args.get('year', type=int) or datetime.now().year
            period = request.args.get('period', type=int)
            rows = list_locked_periods(conn, fiscal_year=year)
            filing = list_filing_closes(conn, fiscal_year=year)
            payload = {
                'success': True,
                'data': rows,
                'year': year,
                'year_locked': is_year_locked(conn, year),
                'filing_closes': filing,
            }
            if period:
                payload['filing_close_for_period'] = get_filing_close_for_month(
                    conn, year, period,
                )
            return jsonify(payload)
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/period-lock', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_period_lock_set():
        """
        action:
          - lock / lock_year: khóa cả năm (chỉ sau 31/12)
          - unlock / unlock_year: mở lại sổ năm (cần lý do)
          - unlock_filing: gỡ chốt kỳ kê khai của cửa sổ chứa ``period``
          - unlock_period: mở một tháng (tương thích dữ liệu khóa cũ)
        """
        conn = get_db_connection()
        try:
            from Services.audit_log import write_audit
            from Services.sme.period_lock import (
                clear_filing_close,
                is_year_locked,
                list_filing_closes,
                list_locked_periods,
                lock_year,
                unlock_period,
                unlock_year,
            )
            from Services.tenant_profile import get_current_tenant_profile
            payload = request.get_json(silent=True) or {}
            year = int(payload.get('year') or datetime.now().year)
            period = int(payload.get('period') or datetime.now().month)
            action = (payload.get('action') or 'lock').strip().lower()
            actor = session.get('user_name') or session.get('username')
            reason = (payload.get('reason') or '').strip()
            profile = get_current_tenant_profile()
            features = profile.get('features') or {}

            if action in ('unlock', 'unlock_year'):
                if not reason:
                    raise ValueError('Cần nhập lý do mở lại sổ năm (để truy vết).')
                data = unlock_year(
                    conn, fiscal_year=year, unlocked_by=actor, reason=reason,
                    clear_filing=True,
                )
                conn.commit()
                write_audit(
                    'unlock', 'sme_period_lock',
                    f'Mở lại sổ năm {year} (gỡ khóa + chốt kê khai): {reason}',
                    entity_type='sme_year_lock', entity_id=year,
                    old_data={'year_locked': True}, new_data=data,
                )
                return jsonify({
                    'success': True,
                    'data': data,
                    'message': data.get('hint'),
                })

            if action == 'unlock_period':
                if not reason:
                    raise ValueError('Cần nhập lý do mở khóa kỳ.')
                ok = unlock_period(conn, fiscal_year=year, period=period)
                conn.commit()
                write_audit(
                    'unlock', 'sme_period_lock',
                    f'Mở khóa kỳ {period:02d}/{year}: {reason}',
                    entity_type='sme_period_lock', entity_id=f'{year}-{period}',
                    old_data={'locked': True}, new_data={'unlocked': ok},
                )
                return jsonify({'success': True, 'unlocked': ok, 'year': year, 'period': period})

            if action == 'unlock_filing':
                if not reason:
                    raise ValueError('Cần nhập lý do mở lại kỳ kê khai.')
                data = clear_filing_close(
                    conn,
                    fiscal_year=year,
                    period=period,
                    features=features,
                    cleared_by=actor,
                    reason=reason,
                )
                conn.commit()
                write_audit(
                    'unlock', 'sme_filing_close',
                    f'Mở lại kỳ kê khai (tháng {period}/{year}): {reason}',
                    entity_type='sme_filing_close', entity_id=f'{year}-{period}',
                    old_data={'filing_closed': True},
                    new_data=data,
                )
                return jsonify({
                    'success': True,
                    'cleared': bool(data.get('cleared')),
                    'year': year,
                    'period': period,
                    'reason': reason,
                    'data': data,
                    'message': data.get('hint'),
                })

            # lock / lock_year
            data = lock_year(
                conn,
                fiscal_year=year,
                locked_by=actor,
                reason=reason or 'Khóa sổ năm thủ công',
            )
            conn.commit()
            write_audit(
                'lock', 'sme_period_lock',
                f'Khóa sổ năm {year}',
                entity_type='sme_year_lock', entity_id=year,
                new_data=data,
            )
            return jsonify({
                'success': True,
                'data': data,
                'year_locked': is_year_locked(conn, year),
                'locked_periods': list_locked_periods(conn, fiscal_year=year),
                'filing_closes': list_filing_closes(conn, fiscal_year=year),
            })
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
