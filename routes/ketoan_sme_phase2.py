"""Routes SME Phase P2 — vay nợ, ký quỹ."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from flask import jsonify, render_template, request, session

from db_utils import get_db_connection

logger = logging.getLogger(__name__)


def _user():
    return session.get('user_name') or session.get('username')


def _bootstrap():
    from Services.sme.bootstrap import ensure_sme_accounting_ready
    from Services.sme.loans_deposits import ensure_sme_loans_schema
    from Services.tenant_profile import get_current_tenant_profile

    conn = get_db_connection()
    try:
        profile = get_current_tenant_profile() or {}
        ensure_sme_accounting_ready(
            conn, accounting_regime=profile.get('accounting_regime'), commit=False,
        )
        ensure_sme_loans_schema(conn, commit=True)
    finally:
        conn.close()


def register_sme_phase2_routes(app, *, login_required, require_sme_regime):

    @app.route('/SME_loans')
    @login_required
    @require_sme_regime
    def SME_loans():
        return render_template('KeToanSME/loans.html')

    @app.route('/SME_deposits')
    @login_required
    @require_sme_regime
    def SME_deposits():
        return render_template('KeToanSME/deposits.html')

    @app.route('/api/sme/loans', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_loans():
        from Services.sme.loans_deposits import disburse_loan, list_loans
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                from Services.sme.branches import request_branch_filter
                return jsonify({
                    'success': True,
                    'data': list_loans(conn, branch_code=request_branch_filter()),
                })
            data = request.get_json(silent=True) or {}
            doc = disburse_loan(
                conn,
                start_date=data.get('date') or data.get('start_date'),
                lender_name=data.get('lender_name') or '',
                principal=data.get('principal') or data.get('amount'),
                liability_account=data.get('liability_account') or '3411',
                cash_account=data.get('cash_account') or '1121',
                interest_rate=float(data.get('interest_rate') or 0),
                due_date=data.get('due_date') or '',
                contract_no=data.get('contract_no') or '',
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
            logger.exception('api_sme_loans')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/loans/<int:loan_id>/interest', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_loan_interest(loan_id):
        from Services.sme.loans_deposits import accrue_loan_interest
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            now = datetime.now()
            doc = accrue_loan_interest(
                conn,
                loan_id=loan_id,
                period_year=int(data.get('year') or now.year),
                period_month=int(data.get('period') or now.month),
                amount=data.get('amount'),
                interest_date=data.get('date'),
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/loans/<int:loan_id>/repay', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_loan_repay(loan_id):
        from Services.sme.loans_deposits import repay_loan
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = repay_loan(
                conn,
                loan_id=loan_id,
                amount=data.get('amount') or data.get('principal'),
                pay_date=data.get('date') or datetime.now().strftime('%Y-%m-%d'),
                payment_method=data.get('payment_method') or 'bank',
                include_interest=float(data.get('interest') or 0),
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/deposits', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_deposits():
        from Services.sme.loans_deposits import list_deposits, post_deposit
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                from Services.sme.branches import request_branch_filter
                return jsonify({
                    'success': True,
                    'data': list_deposits(conn, branch_code=request_branch_filter()),
                })
            data = request.get_json(silent=True) or {}
            doc = post_deposit(
                conn,
                doc_date=data.get('date') or data.get('doc_date'),
                direction=data.get('direction') or 'placed',
                party_name=data.get('party_name') or '',
                amount=data.get('amount'),
                payment_method=data.get('payment_method') or 'bank',
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
            logger.exception('api_sme_deposits')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()
