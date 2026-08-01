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
        try:
            rows = conn.execute(
                """
                SELECT p.id, p.name, p.unit, COALESCE(p.product_type,'goods') AS product_type,
                       COALESCE(p.barcode, '') AS code,
                       COALESCE(i.quantity,0) AS quantity, COALESCE(i.avg_cost,0) AS avg_cost
                FROM products p
                LEFT JOIN inventory i ON i.product_id = p.id
                ORDER BY p.name LIMIT 800
                """
            ).fetchall()
            data = [dict(r) for r in rows]
            if warehouse:
                sm_cols = {r[1] for r in conn.execute('PRAGMA table_info(stock_moves)').fetchall()}
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
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            # barcode column may be missing on older DBs
            try:
                rows = conn.execute(
                    """
                    SELECT p.id, p.name, p.unit, COALESCE(p.product_type,'goods') AS product_type,
                           '' AS code,
                           COALESCE(i.quantity,0) AS quantity, COALESCE(i.avg_cost,0) AS avg_cost
                    FROM products p
                    LEFT JOIN inventory i ON i.product_id = p.id
                    ORDER BY p.name LIMIT 800
                    """
                ).fetchall()
                return jsonify({'success': True, 'data': [dict(r) for r in rows]})
            except Exception:
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
    @app.route('/api/sme/insurance/pay', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_insurance_pay():
        from Services.sme.payroll import pay_insurance
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = pay_insurance(
                conn,
                amount=data.get('amount'),
                pay_date=data.get('date'),
                payment_method=data.get('payment_method') or 'bank',
                account_code=data.get('account_code') or '3383',
                receiver_name=data.get('receiver_name') or 'Cơ quan BHXH',
                reference=data.get('reference') or '',
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
