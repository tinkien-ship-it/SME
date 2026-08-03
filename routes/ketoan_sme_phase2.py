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

    @app.route('/api/sme/loans/<int:loan_id>', methods=['GET', 'PUT'])
    @login_required
    @require_sme_regime
    def api_sme_loan_detail(loan_id):
        from Services.sme.loans_deposits import get_loan, update_loan
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                doc = get_loan(conn, loan_id)
                if not doc or doc.get('status') == 'void':
                    return jsonify({'success': False, 'error': 'Không tìm thấy khoản vay'}), 404
                return jsonify({'success': True, 'data': doc})
            data = request.get_json(silent=True) or {}
            if 'principal' in data:
                principal_val = data.get('principal')
            elif 'amount' in data:
                principal_val = data.get('amount')
            else:
                principal_val = None
            start_val = data.get('date') if 'date' in data else data.get('start_date')
            doc = update_loan(
                conn,
                loan_id,
                lender_name=data.get('lender_name'),
                contract_no=data.get('contract_no'),
                due_date=data.get('due_date'),
                interest_rate=data.get('interest_rate'),
                notes=data.get('notes'),
                principal=principal_val,
                start_date=start_val,
                liability_account=data.get('liability_account'),
                cash_account=data.get('cash_account'),
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_loan_detail')
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

    @app.route('/SME_letter_of_credit')
    @login_required
    @require_sme_regime
    def SME_letter_of_credit():
        return render_template('KeToanSME/letter_of_credit.html')

    @app.route('/api/sme/lc', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_lc():
        from Services.sme.letter_of_credit import list_lc_docs, open_letter_of_credit, ensure_sme_lc_schema
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            ensure_sme_lc_schema(conn, commit=True)
            if request.method == 'GET':
                from Services.sme.branches import request_branch_filter
                return jsonify({
                    'success': True,
                    'data': list_lc_docs(
                        conn,
                        status=request.args.get('status'),
                        branch_code=request_branch_filter(),
                    ),
                })
            data = request.get_json(silent=True) or {}
            doc = open_letter_of_credit(
                conn,
                open_date=data.get('date') or data.get('open_date'),
                bank_name=data.get('bank_name') or '',
                beneficiary_name=data.get('beneficiary_name') or data.get('party_name') or '',
                amount_fc=data.get('amount_fc') or data.get('amount'),
                exchange_rate=data.get('exchange_rate') or data.get('fx_rate') or 1,
                currency=data.get('currency') or 'USD',
                funding_mode=data.get('funding_mode') or 'full_margin',
                margin_pct=data.get('margin_pct') if data.get('margin_pct') is not None else 100,
                interest_rate=data.get('interest_rate') if data.get('interest_rate') is not None else (
                    data.get('loan_interest_rate') if data.get('loan_interest_rate') is not None else 0
                ),
                loan_term_months=data.get('loan_term_months') if data.get('loan_term_months') is not None else (
                    data.get('term_months') if data.get('term_months') is not None else 0
                ),
                lc_no=data.get('lc_no'),
                cash_account=data.get('cash_account') or '1122',
                liability_account=data.get('liability_account') or '3411',
                lender_name=data.get('lender_name') or '',
                import_id=data.get('import_id'),
                po_id=data.get('po_id'),
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
            logger.exception('api_sme_lc')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/lc/<int:lc_id>/settle', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_lc_settle(lc_id):
        from Services.sme.letter_of_credit import settle_lc
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            result = settle_lc(
                conn,
                lc_id,
                import_id=data.get('import_id'),
                settle_date=data.get('date') or data.get('settle_date'),
                shortfall_exchange_rate=data.get('exchange_rate') or data.get('fx_rate'),
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, **result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_lc_settle')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/lc/<int:lc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_lc_void(lc_id):
        from Services.sme.letter_of_credit import void_lc
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            result = void_lc(
                conn,
                lc_id,
                reason=data.get('reason') or 'Hủy mở L/C',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, **result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_lc_void')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()
