"""Routes SME Phase P1 — kho VT, TNDN, FX, BHXH/LĐTL."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from flask import jsonify, render_template, request, session

from db_utils import get_db_connection

logger = logging.getLogger(__name__)


def _bootstrap():
    from Services.sme.bootstrap import ensure_sme_accounting_ready
    from Services.tenant_profile import get_current_tenant_profile

    conn = get_db_connection()
    try:
        profile = get_current_tenant_profile() or {}
        ensure_sme_accounting_ready(
            conn, accounting_regime=profile.get('accounting_regime'), commit=True,
        )
    finally:
        conn.close()


def _user():
    return session.get('user_name') or session.get('username')


_GOODS_TYPES = frozenset({
    '', 'goods', 'hang_hoa', 'hanghoa', 'ready_made', 'readymade',
})
_FINISHED_TYPES = frozenset({
    'finished_goods', 'finished', 'thanh_pham', 'thanhpham',
})
_BLOCKED_PRODUCT_TYPES = frozenset({
    'materials', 'material', 'raw_materials', 'nvl',
    'service', 'services', 'dich_vu', 'dichvu',
    'fixed_asset', 'fixed_assets', 'tscd',
    'tools', 'tool', 'ccdc',
})
_BLOCKED_CODE_PREFIXES = ('VT', 'TSCD', 'CCDC', 'DV')


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    except sqlite3.Error:
        return set()


def _like_needles(q: str) -> list[str]:
    raw = (q or '').strip()
    if not raw:
        return []
    variants = {raw, raw.lower(), raw.upper(), raw.casefold()}
    swapped = raw.replace('Đ', '\0').replace('đ', 'Đ').replace('\0', 'đ')
    variants.update({swapped, swapped.lower(), swapped.upper()})
    return [f'%{v}%' for v in variants if v]


def _text_matches_q(value, q: str) -> bool:
    if not q:
        return True
    text = str(value or '')
    if q.casefold() in text.casefold():
        return True
    return q.upper() in text.upper()


def _brief_matches_prefixes(row: dict, prefixes: list[str]) -> bool:
    """HH/SP/TP = hàng hóa + thành phẩm. Không bắt buộc mã bắt đầu bằng HH/SP/TP."""
    if not prefixes:
        return True
    code = str(row.get('product_code') or row.get('code') or '').strip().upper()
    pt = str(row.get('product_type') or '').strip().lower()
    if pt in _BLOCKED_PRODUCT_TYPES:
        return False
    if any(code.startswith(px) for px in _BLOCKED_CODE_PREFIXES):
        return False
    if any(code.startswith(px) for px in prefixes):
        return True
    want_goods = any(px in prefixes for px in ('HH', 'SP'))
    want_fg = any(px in prefixes for px in ('TP', 'SP'))
    if want_fg and pt in _FINISHED_TYPES:
        return True
    if want_goods and pt in _GOODS_TYPES:
        return True
    if want_goods and pt not in _FINISHED_TYPES:
        return True
    return False


def register_sme_phase1_routes(app, *, login_required, require_sme_regime):

    @app.route('/SME_stock_count')
    @login_required
    @require_sme_regime
    def SME_stock_count():
        return render_template('KeToanSME/stock_count.html')

    @app.route('/SME_stock_transfer')
    @login_required
    @require_sme_regime
    def SME_stock_transfer():
        return render_template('KeToanSME/stock_transfer.html')

    @app.route('/SME_material_alloc')
    @login_required
    @require_sme_regime
    def SME_material_alloc():
        return render_template('KeToanSME/material_alloc.html')

    @app.route('/SME_purchase_listing')
    @login_required
    @require_sme_regime
    def SME_purchase_listing():
        return render_template('KeToanSME/purchase_listing.html')

    @app.route('/SME_cit')
    @login_required
    @require_sme_regime
    def SME_cit():
        return render_template('KeToanSME/cit.html')

    @app.route('/SME_fx_revaluation')
    @login_required
    @require_sme_regime
    def SME_fx_revaluation():
        return render_template('KeToanSME/fx_revaluation.html')

    @app.route('/SME_insurance_pay')
    @login_required
    @require_sme_regime
    def SME_insurance_pay():
        return render_template('KeToanSME/insurance_pay.html')

    @app.route('/SME_payroll_allocation')
    @login_required
    @require_sme_regime
    def SME_payroll_allocation():
        return render_template('KeToanSME/payroll_allocation.html')

    # ── Inventory APIs ─────────────────────────────────────
    @app.route('/api/sme/stock-count', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_stock_count():
        from Services.sme.inventory_ops import list_stock_counts, post_stock_count
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                branch = (
                    request.args.get('branch')
                    or session.get('sme_branch_filter')
                    or 'ALL'
                )
                return jsonify({
                    'success': True,
                    'data': list_stock_counts(conn, branch_code=branch),
                    'branch_code': branch,
                })
            data = request.get_json(silent=True) or {}
            doc = post_stock_count(
                conn,
                count_date=data.get('date') or data.get('count_date'),
                items=data.get('items') or [],
                warehouse_code=data.get('warehouse_code') or '',
                notes=data.get('notes') or '',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_stock_count')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/stock-transfer', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_stock_transfer():
        from Services.sme.inventory_ops import create_stock_transfer, list_stock_transfers
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                branch = (
                    request.args.get('branch')
                    or session.get('sme_branch_filter')
                    or 'ALL'
                )
                return jsonify({
                    'success': True,
                    'data': list_stock_transfers(conn, branch_code=branch),
                    'branch_code': branch,
                })
            data = request.get_json(silent=True) or {}
            doc = create_stock_transfer(
                conn,
                transfer_date=data.get('date') or data.get('transfer_date'),
                from_warehouse=data.get('from_warehouse') or '',
                to_warehouse=data.get('to_warehouse') or '',
                items=data.get('items') or [],
                notes=data.get('notes') or '',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_stock_transfer')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/material-alloc', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_material_alloc():
        from Services.sme.inventory_ops import allocate_materials
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            data = request.get_json(silent=True) or {}
            doc = allocate_materials(
                conn,
                alloc_date=data.get('date') or data.get('alloc_date'),
                items=data.get('items') or [],
                expense_account=data.get('expense_account') or '621',
                notes=data.get('notes') or '',
                warehouse_code=data.get('warehouse_code') or data.get('warehouse') or '',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_material_alloc')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/purchase-listing')
    @login_required
    @require_sme_regime
    def api_sme_purchase_listing():
        from Services.sme.inventory_ops import purchase_listing
        from Services.sme.branches import request_branch_filter
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = purchase_listing(
                conn,
                date_from=request.args.get('from') or datetime.now().strftime('%Y-%m-01'),
                date_to=request.args.get('to') or datetime.now().strftime('%Y-%m-%d'),
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/products/brief')
    @login_required
    @require_sme_regime
    def api_sme_products_brief():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        warehouse = (request.args.get('warehouse') or request.args.get('warehouse_code') or '').strip()
        stock_only = (request.args.get('stock_only') or '').strip().lower() in ('1', 'true', 'yes')
        prefixes_raw = (request.args.get('prefixes') or '').strip().upper()
        prefixes = [p.strip() for p in prefixes_raw.split(',') if p.strip()] if prefixes_raw else []
        q = (request.args.get('q') or request.args.get('search') or '').strip()
        try:
            limit = int(request.args.get('limit') or (40 if q else 800))
        except (TypeError, ValueError):
            limit = 40 if q else 800
        limit = min(max(limit, 1), 800)
        try:
            pcols = _table_cols(conn, 'products')
            icols = _table_cols(conn, 'inventory')
            has_code = 'product_code' in pcols
            has_barcode = 'barcode' in pcols
            has_type = 'product_type' in pcols
            has_price = 'price' in pcols
            has_base = 'base_price' in pcols
            has_inv = bool(icols)
            code_expr = "COALESCE(p.product_code, '')" if has_code else "''"
            barcode_expr = "COALESCE(p.barcode, '')" if has_barcode else "''"
            type_expr = (
                "COALESCE(NULLIF(TRIM(p.product_type), ''), 'goods')"
                if has_type else "'goods'"
            )
            price_expr = "COALESCE(p.price, 0)" if has_price else "0"
            base_expr = "COALESCE(p.base_price, 0)" if has_base else "0"
            qty_expr = "COALESCE(i.quantity, 0)" if has_inv else "0"
            cost_expr = "COALESCE(i.avg_cost, 0)" if has_inv else "0"
            join_sql = "LEFT JOIN inventory i ON i.product_id = p.id" if has_inv else ""

            where = ["1=1"]
            params: list = []
            needles = _like_needles(q) if q else []
            if needles:
                ors = []
                for n in needles:
                    ors.append("p.name LIKE ?")
                    params.append(n)
                    if has_code:
                        ors.append("IFNULL(p.product_code, '') LIKE ?")
                        params.append(n)
                    if has_barcode:
                        ors.append("IFNULL(p.barcode, '') LIKE ?")
                        params.append(n)
                where.append('(' + ' OR '.join(ors) + ')')

            # Tìm theo q trên toàn bộ danh mục — không cắt 800 dòng trước khi lọc tên.
            sql_limit = 2000 if q else 800
            sql = f"""
                SELECT p.id, p.name, p.unit, {type_expr} AS product_type,
                       {code_expr} AS product_code,
                       COALESCE(NULLIF(TRIM({code_expr}), ''), {barcode_expr}) AS code,
                       {barcode_expr} AS barcode,
                       {qty_expr} AS quantity, {cost_expr} AS avg_cost,
                       {price_expr} AS wholesale_price,
                       {base_expr} AS base_price
                FROM products p
                {join_sql}
                WHERE {' AND '.join(where)}
                ORDER BY p.name
                LIMIT {int(sql_limit)}
            """
            data = [dict(r) for r in conn.execute(sql, params).fetchall()]
            if warehouse:
                sm_cols = _table_cols(conn, 'stock_moves')
                if 'warehouse_code' in sm_cols:
                    qty_map = {
                        int(r[0]): float(r[1] or 0)
                        for r in conn.execute(
                            """
                            SELECT product_id, COALESCE(SUM(quantity),0)
                            FROM stock_moves WHERE warehouse_code = ?
                            GROUP BY product_id
                            """,
                            (warehouse,),
                        ).fetchall()
                    }
                    for row in data:
                        row['quantity'] = qty_map.get(int(row['id']), 0.0)
                        row['warehouse_code'] = warehouse
            if stock_only:
                data = [r for r in data if float(r.get('quantity') or 0) > 1e-9]
            if prefixes:
                data = [r for r in data if _brief_matches_prefixes(r, prefixes)]
            if q:
                data = [
                    r for r in data
                    if _text_matches_q(r.get('name'), q)
                    or _text_matches_q(r.get('product_code'), q)
                    or _text_matches_q(r.get('code'), q)
                    or _text_matches_q(r.get('barcode'), q)
                ]
            data = data[:limit]
            for row in data:
                row['price'] = (
                    float(row.get('wholesale_price') or 0)
                    or float(row.get('base_price') or 0)
                    or float(row.get('avg_cost') or 0)
                )
            resp = jsonify({'success': True, 'data': data})
            resp.headers['Cache-Control'] = 'no-store' if q else 'private, max-age=60'
            return resp
        except Exception as e:
            logger.exception('api_sme_products_brief')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── CIT ────────────────────────────────────────────────
    @app.route('/api/sme/cit', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_cit():
        from Services.sme.cit import accrue_cit_provisional, list_cit_provisions
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                year = request.args.get('year', type=int)
                return jsonify({'success': True, 'data': list_cit_provisions(conn, fiscal_year=year)})
            data = request.get_json(silent=True) or {}
            doc = accrue_cit_provisional(
                conn,
                fiscal_year=int(data.get('year') or datetime.now().year),
                period=int(data.get('period') or datetime.now().month),
                tax_amount=data.get('tax_amount'),
                taxable_income=data.get('taxable_income') or 0,
                tax_rate=data.get('tax_rate') or 0.20,
                provision_date=data.get('date'),
                notes=data.get('notes') or '',
                created_by=_user(),
                replace_existing=bool(data.get('replace_existing')),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_cit')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/cit/pay', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_cit_pay():
        from Services.sme.cit import pay_cit
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = pay_cit(
                conn,
                fiscal_year=int(data.get('year') or datetime.now().year),
                period=int(data.get('period') or datetime.now().month),
                amount=data.get('amount'),
                pay_date=data.get('date'),
                payment_method=data.get('payment_method') or 'bank',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_cit_pay')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── FX ─────────────────────────────────────────────────
    @app.route('/api/sme/fx-revaluation', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_fx_revaluation():
        from Services.sme.fx_revaluation import list_fx_revaluations, revalue_foreign_currency
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                year = request.args.get('year', type=int)
                from Services.sme.branches import request_branch_filter
                return jsonify({
                    'success': True,
                    'data': list_fx_revaluations(
                        conn, fiscal_year=year, branch_code=request_branch_filter(),
                    ),
                })
            data = request.get_json(silent=True) or {}
            doc = revalue_foreign_currency(
                conn,
                fiscal_year=int(data.get('year') or datetime.now().year),
                period=int(data.get('period') or datetime.now().month),
                currency=data.get('currency') or 'USD',
                rate=data.get('rate'),
                lines=data.get('lines') or [],
                reval_date=data.get('date'),
                equity_mode=bool(data.get('equity_mode')),
                notes=data.get('notes') or '',
                created_by=_user(),
                replace_existing=bool(data.get('replace_existing')),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_fx_revaluation')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── Insurance / LĐTL ───────────────────────────────────
    @app.route('/api/sme/insurance/periods', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_insurance_periods():
        """Danh sách kỳ công nợ BHXH/BHYT/BHTN (giống HKD, chuẩn SME)."""
        from Services.sme.insurance_debt import get_insurance_debt_list
        year = request.args.get('year', type=int)
        include_paid = request.args.get('include_paid', '1') == '1'
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = get_insurance_debt_list(conn, year=year, include_paid=include_paid)
            return jsonify({'success': True, **data})
        except Exception as e:
            logger.exception('api_sme_insurance_periods')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/insurance/period-detail', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_insurance_period_detail():
        from Services.sme.insurance_debt import compute_period_insurance
        month = request.args.get('month', type=int)
        year = request.args.get('year', type=int)
        if not month or not year:
            return jsonify({'success': False, 'error': 'Thiếu tháng/năm'}), 400
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = compute_period_insurance(conn, month, year)
            if not data:
                return jsonify({'success': False, 'error': 'Không có bảng lương kỳ này'}), 404
            return jsonify({'success': True, **data})
        except Exception as e:
            logger.exception('api_sme_insurance_period_detail')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/insurance/pay', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_insurance_pay():
        """Nộp 1 khoản BH (BHXH/BHYT/BHTN × NLĐ|DN) hoặc lập phiếu thủ công (legacy)."""
        from Services.sme.insurance_debt import pay_insurance_item
        from Services.sme.payroll import pay_insurance
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            # Luồng công nợ theo kỳ (giống HKD)
            if data.get('ins_type') and data.get('month') and data.get('year'):
                method = data.get('payment_method') or data.get('pay_method') or 'bank'
                if str(method) in ('111', '112'):
                    method = 'cash' if str(method) == '111' else 'bank'
                doc = pay_insurance_item(
                    conn,
                    month=int(data['month']),
                    year=int(data['year']),
                    ins_type=data.get('ins_type'),
                    payer=data.get('payer') or 'NLD',
                    amount=data.get('amount'),
                    pay_date=data.get('pay_date') or data.get('date'),
                    payment_method=method,
                    receiver_name=data.get('receiver') or data.get('receiver_name') or 'Cơ quan BHXH',
                    reason=data.get('reason') or '',
                    created_by=_user(),
                    commit=True,
                )
                return jsonify({
                    'success': True,
                    'data': doc,
                    'voucher': doc.get('voucher_no'),
                    'message': doc.get('message'),
                })

            # Legacy: nhập tay TK + số tiền
            doc = pay_insurance(
                conn,
                amount=data.get('amount'),
                pay_date=data.get('date') or data.get('pay_date'),
                payment_method=data.get('payment_method') or 'bank',
                account_code=data.get('account_code') or '3383',
                receiver_name=data.get('receiver_name') or 'Cơ quan BHXH',
                reference=data.get('reference') or '',
                reason=data.get('reason'),
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_insurance_pay')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/insurance/pay-all', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_insurance_pay_all():
        """Nộp tất cả khoản còn nợ kỳ — mỗi mục BH một phiếu + bút toán 338x."""
        from Services.sme.insurance_debt import pay_insurance_period
        data = request.get_json(silent=True) or {}
        try:
            month = int(data.get('month'))
            year = int(data.get('year'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Tháng/năm không hợp lệ'}), 400
        method = data.get('payment_method') or data.get('pay_method') or 'bank'
        if str(method) in ('111', '112'):
            method = 'cash' if str(method) == '111' else 'bank'
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            result = pay_insurance_period(
                conn,
                month=month,
                year=year,
                pay_date=data.get('pay_date') or data.get('date'),
                payment_method=method,
                receiver_name=data.get('receiver') or data.get('receiver_name') or 'Cơ quan BHXH',
                scope=data.get('scope') or 'ALL',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, **result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_insurance_pay_all')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/payroll/allocation', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_payroll_allocation():
        from Services.sme.payroll import payroll_allocation_summary, post_payroll_allocation
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            if request.method == 'GET':
                month = request.args.get('month', type=int) or datetime.now().month
                year = request.args.get('year', type=int) or datetime.now().year
                data = payroll_allocation_summary(conn, month=month, year=year)
                return jsonify({'success': True, 'data': data})
            data = request.get_json(silent=True) or {}
            month = int(data.get('month') or datetime.now().month)
            year = int(data.get('year') or datetime.now().year)
            doc = post_payroll_allocation(
                conn,
                month=month,
                year=year,
                allocations=data.get('allocations'),
                posting_date=data.get('posting_date'),
                source_account=data.get('source_account') or '642',
                created_by=session.get('user_name'),
                replace_existing=bool(data.get('replace_existing', True)),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_payroll_allocation')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()
