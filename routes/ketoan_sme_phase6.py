"""Routes SME hoàn thiện — in mẫu còn thiếu, year-end, void."""
from __future__ import annotations

import logging
import sqlite3

from flask import jsonify, render_template, request, session

from db_utils import get_db_connection

logger = logging.getLogger(__name__)


def _user():
    return session.get('user_name') or session.get('username')


def _biz_info(conn):
    info = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
    return dict(info) if info else {}


def register_sme_phase6_routes(app, *, login_required, require_sme_regime):

    # ── Prints ─────────────────────────────────────────────
    @app.route('/SME_purchase_listing/in')
    @login_required
    @require_sme_regime
    def SME_purchase_listing_in():
        from Services.sme.branches import request_branch_filter
        from Services.sme.inventory_ops import purchase_listing
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            df = request.args.get('from') or request.args.get('date_from')
            dt = request.args.get('to') or request.args.get('date_to')
            if not df or not dt:
                return render_template('KeToanSME/purchase_listing.html')
            doc = purchase_listing(
                conn, date_from=df, date_to=dt,
                branch_code=request_branch_filter(),
            )
            return render_template(
                'KeToanSME/purchase_listing_print.html', doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_material_alloc/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_material_alloc_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.inventory_ops import get_material_allocation
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(conn, 'sme_material_allocations', doc_id, label='Phân bổ NVL')
            except ValueError:
                return render_template('KeToanSME/material_alloc.html')
            doc = get_material_allocation(conn, doc_id)
            if not doc:
                return render_template('KeToanSME/material_alloc.html')
            return render_template(
                'KeToanSME/material_alloc_print.html', doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_salary/in')
    @login_required
    @require_sme_regime
    def SME_salary_in():
        from Services.sme.branches import request_branch_filter
        from Services.sme.payroll import salary_sheet_01
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            month = int(request.args.get('month') or 0)
            year = int(request.args.get('year') or 0)
            if not month or not year:
                return render_template('KeToanSME/SME_salary.html')
            doc = salary_sheet_01(
                conn, month=month, year=year,
                branch_code=request_branch_filter(),
            )
            return render_template(
                'KeToanSME/salary_01_print.html', doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_payroll_allocation/in')
    @login_required
    @require_sme_regime
    def SME_payroll_allocation_in():
        from Services.sme.branches import request_branch_filter
        from Services.sme.payroll import payroll_allocation_summary
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            month = int(request.args.get('month') or 0)
            year = int(request.args.get('year') or 0)
            if not month or not year:
                return render_template('KeToanSME/payroll_allocation.html')
            doc = payroll_allocation_summary(
                conn, month=month, year=year,
                branch_code=request_branch_filter(),
            )
            return render_template(
                'KeToanSME/payroll_allocation_print.html', doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_insurance_pay/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_insurance_pay_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.vouchers import get_voucher
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(conn, 'sme_vouchers', doc_id, label='Phiếu chi BH')
            except ValueError:
                return render_template('KeToanSME/insurance_pay.html')
            doc = get_voucher(conn, doc_id)
            if not doc:
                return render_template('KeToanSME/insurance_pay.html')
            doc = {**doc, 'form_code': '07-LĐTL'}
            return render_template(
                'KeToanSME/insurance_pay_print.html', doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_bank_reconcile/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_bank_reconcile_in(doc_id):
        from Services.sme.bank_reconcile import get_reconciliation
        from Services.sme.branch_filter import assert_row_in_branch
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(
                    conn, 'sme_bank_reconciliations', doc_id, label='Đối chiếu NH',
                )
            except ValueError:
                return render_template('KeToanSME/bank_reconcile.html')
            doc = get_reconciliation(conn, doc_id)
            if not doc:
                return render_template('KeToanSME/bank_reconcile.html')
            return render_template(
                'KeToanSME/bank_reconcile_print.html', doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_costing/in')
    @login_required
    @require_sme_regime
    def SME_costing_in():
        from Services.sme.costing import costing_summary
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            year = int(request.args.get('year') or 0)
            period = int(request.args.get('period') or request.args.get('month') or 0)
            if not year or not period:
                return render_template('KeToanSME/costing.html')
            doc = costing_summary(conn, fiscal_year=year, period=period)
            return render_template(
                'KeToanSME/costing_print.html', doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    # ── Material alloc list API ────────────────────────────
    @app.route('/api/sme/material-alloc', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_material_alloc_list():
        from Services.sme.inventory_ops import list_material_allocations
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = (
                request.args.get('branch')
                or session.get('sme_branch_filter')
                or 'ALL'
            )
            return jsonify({
                'success': True,
                'data': list_material_allocations(conn, branch_code=branch),
                'branch_code': branch,
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── Year-end 4212→4211 ─────────────────────────────────
    @app.route('/api/sme/auto/year-end', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_auto_year_end():
        from Services.sme.period_close import run_year_end_close
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            year = int(data.get('year') or 0)
            if year < 2000:
                return jsonify({'success': False, 'error': 'Năm không hợp lệ'}), 400
            result = run_year_end_close(
                conn,
                fiscal_year=year,
                created_by=_user(),
                replace_existing=bool(data.get('replace_existing')),
                lock_after=bool(data.get('lock_after', True)),
            )
            conn.commit()
            return jsonify({'success': True, 'data': result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_auto_year_end')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── Void APIs ──────────────────────────────────────────
    def _do_void(fn, *args, log_name='void'):
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = fn(
                conn, *args,
                reason=data.get('reason') or 'Hủy chứng từ',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception(log_name)
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/stock-count/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_stock_count_void(doc_id):
        from Services.sme.inventory_ops import void_stock_count
        return _do_void(void_stock_count, doc_id, log_name='stock_count_void')

    @app.route('/api/sme/labor-contracts/<int:cid>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_labor_contract_void(cid):
        from Services.sme.labor_contract import void_labor_contract
        return _do_void(void_labor_contract, cid, log_name='labor_contract_void')

    @app.route('/api/sme/labor-sheets/<int:sid>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_labor_sheet_void(sid):
        from Services.sme.labor_sheets import void_labor_sheet
        return _do_void(void_labor_sheet, sid, log_name='labor_sheet_void')

    @app.route('/api/sme/material-alloc/<int:aid>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_material_alloc_void(aid):
        from Services.sme.inventory_ops import void_material_allocation
        return _do_void(void_material_allocation, aid, log_name='material_alloc_void')
