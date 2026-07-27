"""API thanh toán chuyển khoản — webhook SePay/Casso, poll, hoàn tất đơn."""
import logging

from flask import jsonify, render_template, request, url_for

from auth import login_required, require_permission
from Services.payment_bank import (
    complete_pending_bank_payment,
    get_bank_transaction_detail,
    get_payment_config,
    get_sale_payment_status,
    list_bank_transactions,
    parse_casso_webhook,
    parse_sepay_webhook,
    process_bank_transaction,
    sync_bank_transactions_from_provider,
)

logger = logging.getLogger(__name__)


def register_payment_routes(app):
    @app.route('/bank-transactions')
    @login_required
    @require_permission('view_order')
    def bank_transactions_page():
        return render_template('bank_transactions.html')

    @app.route('/api/bank-transactions', methods=['GET'])
    @login_required
    @require_permission('view_order')
    def api_bank_transactions_list():
        start_date = (request.args.get('start_date') or '').strip() or None
        end_date = (request.args.get('end_date') or '').strip() or None
        match_status = (request.args.get('match_status') or '').strip() or None
        q = (request.args.get('q') or '').strip() or None
        try:
            limit = min(int(request.args.get('limit', 200)), 500)
            offset = max(int(request.args.get('offset', 0)), 0)
        except (TypeError, ValueError):
            limit, offset = 200, 0
        return jsonify(list_bank_transactions(
            start_date=start_date, end_date=end_date,
            match_status=match_status, q=q, limit=limit, offset=offset,
        ))

    @app.route('/api/bank-transactions/<int:txn_id>', methods=['GET'])
    @login_required
    @require_permission('view_order')
    def api_bank_transaction_detail(txn_id):
        result = get_bank_transaction_detail(txn_id)
        code = 200 if result.get('success') else 404
        return jsonify(result), code

    @app.route('/api/bank-transactions/sync', methods=['POST'])
    @login_required
    @require_permission('view_order')
    def api_bank_transactions_sync():
        try:
            limit = min(int(request.get_json(silent=True, force=True) or {}.get('limit', 50)), 100)
        except (TypeError, ValueError):
            limit = 50
        result = sync_bank_transactions_from_provider(limit=limit)
        code = 200 if result.get('success') else 400
        return jsonify(result), code

    @app.route('/api/payment/status/<int:sale_id>', methods=['GET'])
    @login_required
    def api_payment_status(sale_id):
        return jsonify(get_sale_payment_status(sale_id))

    @app.route('/api/payment/complete/<int:sale_id>', methods=['POST'])
    @login_required
    def api_payment_complete(sale_id):
        result = complete_pending_bank_payment(sale_id, source='manual')
        code = 200 if result.get('success') else 400
        return jsonify(result), code

    @app.route('/api/payment/webhook/sepay', methods=['POST'])
    def webhook_sepay():
        payload = request.get_json(silent=True) or {}
        cfg = get_payment_config()
        if cfg['provider'] != 'sepay':
            return jsonify({'success': False, 'error': 'Provider không phải SePay'}), 400

        api_key = (request.headers.get('Authorization') or '').replace('Bearer', '').replace('Apikey', '').strip()
        expected = (cfg.get('sepay_api_key') or '').strip()
        if expected and api_key and api_key != expected:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401

        matched = []
        for txn in parse_sepay_webhook(payload):
            r = process_bank_transaction('sepay', txn, source='webhook')
            if r.get('matched') and r.get('completed'):
                matched.append(r)

        return jsonify({'success': True, 'matched': len(matched), 'details': matched})

    @app.route('/api/payment/webhook/casso', methods=['POST'])
    def webhook_casso():
        payload = request.get_json(silent=True) or {}
        cfg = get_payment_config()
        if cfg['provider'] != 'casso':
            return jsonify({'success': False, 'error': 'Provider không phải Casso'}), 400

        secure_token = request.headers.get('Secure-Token') or request.headers.get('X-Casso-Signature') or ''
        expected = (cfg.get('casso_webhook_token') or cfg.get('casso_api_key') or '').strip()
        if expected and secure_token and secure_token != expected:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401

        matched = []
        for txn in parse_casso_webhook(payload):
            r = process_bank_transaction('casso', txn, source='webhook')
            if r.get('matched') and r.get('completed'):
                matched.append(r)

        return jsonify({'success': True, 'matched': len(matched), 'details': matched})

    @app.route('/api/payment/config', methods=['GET'])
    @login_required
    def api_payment_config():
        from Services.payment_bank import get_full_payment_setup
        data = get_full_payment_setup()
        try:
            data['webhook_sepay'] = url_for('webhook_sepay', _external=True)
            data['webhook_casso'] = url_for('webhook_casso', _external=True)
        except Exception:
            data['webhook_sepay'] = '/api/payment/webhook/sepay'
            data['webhook_casso'] = '/api/payment/webhook/casso'
        return jsonify(data)
