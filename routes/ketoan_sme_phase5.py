"""Routes SME phase5 — 06/07/09-TT + in 05-VT."""
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


def _bootstrap():
    from Services.sme.bootstrap import ensure_sme_accounting_ready
    from Services.sme.cash_extras import ensure_sme_cash_extras_schema
    from Services.tenant_profile import get_current_tenant_profile

    conn = get_db_connection()
    try:
        profile = get_current_tenant_profile() or {}
        ensure_sme_accounting_ready(
            conn, accounting_regime=profile.get('accounting_regime'), commit=False,
        )
        ensure_sme_cash_extras_schema(conn, commit=True)
    finally:
        conn.close()


def register_sme_phase5_routes(app, *, login_required, require_sme_regime):

    @app.route('/SME_temp_receipts')
    @login_required
    @require_sme_regime
    def SME_temp_receipts():
        return render_template('KeToanSME/temp_receipts.html')

    @app.route('/SME_payment_listing')
    @login_required
    @require_sme_regime
    def SME_payment_listing():
        return render_template('KeToanSME/payment_listing.html')

    @app.route('/SME_gold_sheet')
    @login_required
    @require_sme_regime
    def SME_gold_sheet():
        return render_template('KeToanSME/gold_sheet.html')

    @app.route('/SME_temp_receipt/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_temp_receipt_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.cash_extras import get_temp_receipt
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(conn, 'sme_vouchers', doc_id, label='Biên lai 06-TT')
            except ValueError:
                return render_template('KeToanSME/temp_receipts.html')
            doc = get_temp_receipt(conn, doc_id)
            if not doc:
                return render_template('KeToanSME/temp_receipts.html')
            return render_template(
                'KeToanSME/temp_receipt_print.html', doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_payment_listing/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_payment_listing_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.cash_extras import get_cash_listing
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(conn, 'sme_cash_listings', doc_id, label='Bảng kê chi')
            except ValueError:
                return render_template('KeToanSME/payment_listing.html')
            doc = get_cash_listing(conn, doc_id)
            if not doc:
                return render_template('KeToanSME/payment_listing.html')
            return render_template(
                'KeToanSME/payment_listing_print.html', doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_gold_sheet/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_gold_sheet_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.cash_extras import get_gold_sheet
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(conn, 'sme_gold_sheets', doc_id, label='Bảng kê vàng')
            except ValueError:
                return render_template('KeToanSME/gold_sheet.html')
            doc = get_gold_sheet(conn, doc_id)
            if not doc:
                return render_template('KeToanSME/gold_sheet.html')
            return render_template(
                'KeToanSME/gold_sheet_print.html', doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_stock_count/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_stock_count_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.inventory_ops import get_stock_count
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(conn, 'sme_stock_counts', doc_id, label='Kiểm kê kho')
            except ValueError:
                return render_template('KeToanSME/stock_count.html')
            doc = get_stock_count(conn, doc_id)
            if not doc:
                return render_template('KeToanSME/stock_count.html')
            return render_template(
                'KeToanSME/stock_count_print.html', doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/api/sme/temp-receipts', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_temp_receipts():
        from Services.sme.cash_extras import create_temp_receipt, list_temp_receipts
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                from Services.sme.branches import request_branch_filter
                return jsonify({
                    'success': True,
                    'data': list_temp_receipts(conn, branch_code=request_branch_filter()),
                })
            data = request.get_json(silent=True) or {}
            doc = create_temp_receipt(
                conn,
                voucher_date=data.get('date') or data.get('voucher_date'),
                party_name=data.get('party_name') or '',
                amount=data.get('amount'),
                payment_method=data.get('payment_method') or 'cash',
                credit_account=data.get('credit_account') or '131',
                reason=data.get('reason') or '',
                party_address=data.get('party_address') or '',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_temp_receipts')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/payment-listing', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_payment_listing():
        from Services.sme.cash_extras import build_payment_listing, list_cash_listings
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            from Services.sme.branches import request_branch_filter
            branch = request_branch_filter()
            if request.method == 'GET':
                return jsonify({'success': True, 'data': list_cash_listings(conn, branch_code=branch)})
            data = request.get_json(silent=True) or {}
            doc = build_payment_listing(
                conn,
                date_from=data.get('date_from') or data.get('from'),
                date_to=data.get('date_to') or data.get('to'),
                listing_date=data.get('date'),
                notes=data.get('notes') or '',
                created_by=_user(),
                branch_code=branch,
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_payment_listing')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/gold-sheets', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_gold_sheets():
        from Services.sme.cash_extras import create_gold_sheet, list_gold_sheets
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            from Services.sme.branches import request_branch_filter
            branch = request_branch_filter()
            if request.method == 'GET':
                return jsonify({'success': True, 'data': list_gold_sheets(conn, branch_code=branch)})
            data = request.get_json(silent=True) or {}
            doc = create_gold_sheet(
                conn,
                sheet_date=data.get('date') or data.get('sheet_date'),
                lines=data.get('lines') or [],
                notes=data.get('notes') or '',
                created_by=_user(),
                branch_code=branch,
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_gold_sheets')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()
