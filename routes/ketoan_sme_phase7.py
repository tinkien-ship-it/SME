"""Phase P7 — bịt lỗ hổng nghiệp vụ: void, CCDC master, số dư quỹ SME."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from flask import jsonify, render_template, request, session

from db_utils import get_db_connection, sqlite_commit

logger = logging.getLogger(__name__)


def _dispatch_named_view(app, *names: str):
    """Gọi view đã đăng ký theo tên (ổn định hơn lookup đơn)."""
    for name in names:
        view = app.view_functions.get(name)
        if view:
            return view()
    needles = [n.lower() for n in names]
    for name, fn in app.view_functions.items():
        low = name.lower()
        if any(low == n or low.endswith('.' + n) or low.endswith(n) for n in needles):
            return fn()
    return None


def _user():
    return session.get('user_name') or session.get('username')


def register_sme_phase7_routes(app, *, login_required, require_sme_regime):

    @app.route('/SME_fx_cash')
    @login_required
    @require_sme_regime
    def SME_fx_cash():
        return render_template('KeToanSME/fx_cash.html')

    @app.route('/api/sme/fx-cash/positions')
    @login_required
    @require_sme_regime
    def api_sme_fx_cash_positions():
        from Services.sme.fx_cash import fx_cash_positions
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = (
                request.args.get('branch')
                or session.get('sme_branch_filter')
                or 'ALL'
            )
            as_of = (request.args.get('as_of') or request.args.get('date') or '').strip()[:10]
            data = fx_cash_positions(conn, as_of=as_of or None, branch_code=branch)
            return jsonify({'success': True, **data})
        except Exception as e:
            logger.exception('api_sme_fx_cash_positions')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/fx-cash/ledger')
    @login_required
    @require_sme_regime
    def api_sme_fx_cash_ledger():
        from Services.sme.fx_cash import fx_cash_ledger
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = (
                request.args.get('branch')
                or session.get('sme_branch_filter')
                or 'ALL'
            )
            data = fx_cash_ledger(
                conn,
                account_code=request.args.get('account') or request.args.get('account_code') or '1122',
                date_from=request.args.get('from') or request.args.get('date_from'),
                date_to=request.args.get('to') or request.args.get('date_to'),
                branch_code=branch,
            )
            return jsonify({'success': True, **data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            logger.exception('api_sme_fx_cash_ledger')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/fx-cash/sell/preview')
    @login_required
    @require_sme_regime
    def api_sme_fx_cash_sell_preview():
        from Services.sme.fx_cash import preview_sell_fx
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = (
                request.args.get('branch')
                or session.get('sme_branch_filter')
                or 'ALL'
            )
            data = preview_sell_fx(
                conn,
                fx_account=request.args.get('fx_account') or '1122',
                amount_fc=request.args.get('amount_fc'),
                sell_rate=request.args.get('sell_rate'),
                as_of=request.args.get('as_of') or request.args.get('date'),
                branch_code=branch,
            )
            return jsonify({'success': True, 'data': data, **data})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            logger.exception('api_sme_fx_cash_sell_preview')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/fx-cash/sell', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_fx_cash_sell():
        from Services.sme.fx_cash import sell_foreign_currency
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            branch = (
                data.get('branch_code')
                or session.get('sme_branch_filter')
                or 'ALL'
            )
            result = sell_foreign_currency(
                conn,
                voucher_date=data.get('date') or data.get('voucher_date'),
                amount_fc=data.get('amount_fc'),
                sell_rate=data.get('sell_rate') or data.get('exchange_rate') or data.get('fx_rate'),
                fx_account=data.get('fx_account') or '1122',
                vnd_account=data.get('vnd_account'),
                payment_method=data.get('payment_method'),
                currency=data.get('currency') or 'USD',
                party_name=data.get('party_name') or data.get('receiver_name') or '',
                reason=data.get('reason') or '',
                created_by=_user(),
                branch_code=branch,
                commit=True,
            )
            return jsonify({'success': True, **result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_fx_cash_sell')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/cash-balances')
    @login_required
    @require_sme_regime
    def api_sme_cash_balances():
        from Services.sme.cash_books import (
            cash_account_balance_as_of,
            cash_fund_balances,
        )
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            year = request.args.get('year', type=int) or datetime.now().year
            branch = (
                request.args.get('branch')
                or session.get('sme_branch_filter')
                or 'ALL'
            )
            as_of = (request.args.get('as_of') or request.args.get('date') or '').strip()[:10]
            account = (request.args.get('account') or request.args.get('account_code') or '').strip()
            data = cash_fund_balances(
                conn, fiscal_year=year, branch_code=branch, as_of=as_of or None,
            )
            if account and as_of:
                data['account_code'] = account
                data['account_balance'] = float(
                    cash_account_balance_as_of(
                        conn, account_code=account, as_of=as_of, branch_code=branch,
                    )
                )
            return jsonify({'success': True, **data})
        except Exception as e:
            logger.exception('api_sme_cash_balances')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bank-payment-accounts')
    @login_required
    @require_sme_regime
    def api_sme_bank_payment_accounts():
        """Danh sách TK NH ghi sổ + mặc định (= VietQR); needs_choice khi có nhiều TK."""
        from Services.sme.bank_accounts import list_bank_payment_accounts
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = list_bank_payment_accounts(conn)
            sqlite_commit(conn, label='sme_phase7')
            return jsonify({'success': True, **data})
        except Exception as e:
            logger.exception('api_sme_bank_payment_accounts')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── FA docs void ───────────────────────────────────────
    @app.route('/api/sme/fa-docs/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_fa_docs_void(doc_id):
        from Services.sme.fa_lifecycle import void_fa_doc
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = void_fa_doc(
                conn, doc_id,
                reason=data.get('reason') or 'Hủy biên bản TSCĐ',
                created_by=_user(), commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_fa_docs_void')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── CCDC master ────────────────────────────────────────
    @app.route('/SME_tools')
    @login_required
    @require_sme_regime
    def SME_tools():
        return render_template('KeToanSME/tools.html')

    @app.route('/api/sme/tools', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_tools_list():
        from Services.sme.tools_ops import list_tools
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
                'data': list_tools(
                    conn,
                    status=request.args.get('status'),
                    branch_code=branch,
                ),
                'branch_code': branch,
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/tools/<int:tool_id>/activate', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_tools_activate(tool_id):
        from Services.sme.tools_ops import activate_tool
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = activate_tool(
                conn, tool_id,
                start_date=data.get('date') or data.get('start_date'),
                so_thang_phan_bo=data.get('so_thang_phan_bo') or data.get('months'),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_tools_activate')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/tools/<int:tool_id>/period', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_tools_set_period(tool_id):
        from Services.sme.tools_ops import update_tool_allocation_period
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            months = data.get('so_thang_phan_bo') or data.get('months')
            doc = update_tool_allocation_period(
                conn,
                tool_id,
                so_thang_phan_bo=int(months or 0),
                start_date=data.get('ngay_bat_dau_su_dung') or data.get('start_date') or data.get('date'),
                expense_account=data.get('expense_account'),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_tools_set_period')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/tools/<int:tool_id>/scrap', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_tools_scrap(tool_id):
        from Services.sme.tools_ops import scrap_tool
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = scrap_tool(
                conn, tool_id,
                reason=data.get('reason') or 'Thanh lý CCDC',
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_tools_scrap')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── Voids batch ────────────────────────────────────────
    @app.route('/api/sme/stock-transfer/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_stock_transfer_void(doc_id):
        from Services.sme.inventory_ops import void_stock_transfer
        return _void_helper(void_stock_transfer, doc_id, 'api_sme_stock_transfer_void')

    @app.route('/api/sme/stock-inspection/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_stock_inspection_void(doc_id):
        from Services.sme.stock_inspection import void_stock_inspection
        return _void_helper(void_stock_inspection, doc_id, 'api_sme_stock_inspection_void', with_user=False)

    @app.route('/api/sme/material-remaining/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_material_remaining_void(doc_id):
        from Services.sme.material_remaining import void_material_remaining
        return _void_helper(void_material_remaining, doc_id, 'api_sme_material_remaining_void', with_user=False)

    @app.route('/api/sme/gold-sheets/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_gold_sheet_void(doc_id):
        from Services.sme.cash_extras import void_gold_sheet
        return _void_helper(void_gold_sheet, doc_id, 'api_sme_gold_sheet_void', with_user=False)

    @app.route('/api/sme/payment-listing/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_payment_listing_void(doc_id):
        from Services.sme.cash_extras import void_cash_listing
        return _void_helper(void_cash_listing, doc_id, 'api_sme_payment_listing_void', with_user=False)

    @app.route('/api/sme/loans/<int:loan_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_loan_void(loan_id):
        from Services.sme.loans_deposits import void_loan
        return _void_helper(void_loan, loan_id, 'api_sme_loan_void')

    @app.route('/api/sme/deposits/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_deposit_void(doc_id):
        from Services.sme.loans_deposits import void_deposit
        return _void_helper(void_deposit, doc_id, 'api_sme_deposit_void')

    @app.route('/api/sme/insurance/payments', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_insurance_payments():
        from Services.sme.vouchers import list_vouchers
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            rows = list_vouchers(
                conn,
                voucher_type='payment',
                branch_code=session.get('sme_branch_filter') or 'ALL',
                limit=300,
            )
            out = [
                r for r in rows
                if str(r.get('debit_account') or '').startswith('338')
                or 'BHXH' in (r.get('reason') or '').upper()
                or 'BHYT' in (r.get('reason') or '').upper()
                or 'BHTN' in (r.get('reason') or '').upper()
                or '07-L' in (r.get('form_code') or '').upper()
                or str(r.get('source_type') or '') == 'insurance'
                or str(r.get('reference_document') or '').startswith('INSURANCE|')
            ]
            return jsonify({'success': True, 'data': out})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()


    @app.route('/api/sme/return-import/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_return_import_void(doc_id):
        from Services.sme.returns_ops import void_return_import
        return _void_helper(void_return_import, doc_id, 'api_sme_return_import_void')

    @app.route('/api/sme/import/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_import_void(doc_id):
        from Services.sme.import_ops import void_import
        return _void_helper(void_import, doc_id, 'api_sme_import_void')

    @app.route('/api/sme/return-sale/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_return_sale_void(doc_id):
        from Services.sme.returns_ops import void_return_sale
        return _void_helper(void_return_sale, doc_id, 'api_sme_return_sale_void')

    @app.route('/api/sme/returns/import', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_returns_import_list():
        from Services.sme.branches import DEFAULT_BRANCH_CODE, request_branch_filter
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = request_branch_filter()
            cols = {r[1] for r in conn.execute('PRAGMA table_info(return_import)').fetchall()}
            status_sql = ''
            if 'status' in cols:
                status_sql = " AND COALESCE(ri.status,'posted') != 'void'"
            branch_sql = ''
            branch_params: list = []
            imp_cols = {r[1] for r in conn.execute('PRAGMA table_info(import)').fetchall()}
            code = (branch or '').strip().upper()
            if code and code != 'ALL' and 'warehouse_code' in imp_cols:
                if code == DEFAULT_BRANCH_CODE:
                    branch_sql = """
                        AND (
                            i.warehouse_code IS NULL OR i.warehouse_code = ''
                            OR i.warehouse_code IN (
                                SELECT code FROM warehouses
                                WHERE branch_code IS NULL OR branch_code = '' OR branch_code = ?
                            )
                        )
                    """
                    branch_params.append(DEFAULT_BRANCH_CODE)
                else:
                    branch_sql = """
                        AND i.warehouse_code IN (
                            SELECT code FROM warehouses WHERE branch_code = ?
                        )
                    """
                    branch_params.append(code)
            rows = conn.execute(
                f"""
                SELECT ri.*, p.name AS product_name, i.import_no
                FROM return_import ri
                LEFT JOIN products p ON p.id = ri.product_id
                LEFT JOIN import i ON i.id = ri.import_id
                WHERE 1=1 {status_sql} {branch_sql}
                ORDER BY ri.id DESC LIMIT 200
                """,
                branch_params,
            ).fetchall()
            return jsonify({'success': True, 'data': [dict(r) for r in rows]})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/returns/import', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_returns_import_create():
        """Tạo trả NCC — kiểm tra CN rồi ủy quyền logic legacy."""
        from Services.sme.branches import import_branch_filter_sql, request_branch_filter

        data = request.get_json(silent=True) or {}
        try:
            import_id = int(data.get('import_id') or 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'import_id không hợp lệ'}), 400
        if not import_id:
            return jsonify({'success': False, 'error': 'Thiếu import_id'}), 400

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = request_branch_filter()
            bf, bp = import_branch_filter_sql(conn, branch, alias='i')
            row = conn.execute(
                f'SELECT i.id FROM import i WHERE i.id = ? {bf}',
                [import_id, *bp],
            ).fetchone()
            if not row:
                return jsonify({
                    'success': False,
                    'error': 'Phiếu nhập không thuộc chi nhánh đang chọn',
                }), 403
        finally:
            conn.close()

        view_result = _dispatch_named_view(app, 'api_return_import_post')
        if view_result is None:
            return jsonify({'success': False, 'error': 'API trả NCC chưa sẵn sàng'}), 500
        return view_result

    @app.route('/api/sme/returns/sale', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_returns_sale_list():
        from Services.sme.branches import DEFAULT_BRANCH_CODE, branch_sql_filter, request_branch_filter
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = request_branch_filter()
            cols = {r[1] for r in conn.execute('PRAGMA table_info(return_sales)').fetchall()}
            status_sql = ''
            if 'status' in cols:
                status_sql = " AND COALESCE(rs.status,'posted') != 'void'"
            branch_sql = ''
            branch_params: list = []
            code = (branch or '').strip().upper()
            if code and code != 'ALL':
                sale_cols = {r[1] for r in conn.execute('PRAGMA table_info(sale)').fetchall()}
                if 'warehouse_code' in sale_cols:
                    if code == DEFAULT_BRANCH_CODE:
                        branch_sql = """
                            AND (
                                s.warehouse_code IS NULL OR s.warehouse_code = ''
                                OR s.warehouse_code IN (
                                    SELECT code FROM warehouses
                                    WHERE branch_code IS NULL OR branch_code = '' OR branch_code = ?
                                )
                            )
                        """
                        branch_params.append(DEFAULT_BRANCH_CODE)
                    else:
                        branch_sql = """
                            AND s.warehouse_code IN (
                                SELECT code FROM warehouses WHERE branch_code = ?
                            )
                        """
                        branch_params.append(code)
                else:
                    bf, bp = branch_sql_filter(branch, alias='je')
                    branch_sql = f"""
                        AND rs.sale_id IN (
                            SELECT je.document_id FROM sme_journal_entries je
                            WHERE je.document_type = 'SALE_REVENUE'
                              AND je.status IN ('posted', 'reversed')
                              {bf}
                        )
                    """
                    branch_params.extend(bp)
            rows = conn.execute(
                f"""
                SELECT rs.*, p.name AS product_name, s.sale_no, s.customer_name
                FROM return_sales rs
                LEFT JOIN products p ON p.id = rs.product_id
                LEFT JOIN sale s ON s.id = rs.sale_id
                WHERE 1=1 {status_sql} {branch_sql}
                ORDER BY rs.id DESC LIMIT 200
                """,
                branch_params,
            ).fetchall()
            return jsonify({'success': True, 'data': [dict(r) for r in rows]})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/returns/sale', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_returns_sale_create():
        """Tạo khách trả hàng — kiểm tra CN rồi ủy quyền logic legacy."""
        from Services.sme.branches import request_branch_filter, sale_branch_filter_sql

        data = request.get_json(silent=True) or {}
        try:
            sale_id = int(data.get('sale_id') or 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'sale_id không hợp lệ'}), 400
        if not sale_id:
            return jsonify({'success': False, 'error': 'Thiếu sale_id'}), 400

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = request_branch_filter()
            bf, bp = sale_branch_filter_sql(conn, branch, alias='s')
            row = conn.execute(
                f'SELECT s.id FROM sale s WHERE s.id = ? {bf}',
                [sale_id, *bp],
            ).fetchone()
            if not row:
                return jsonify({
                    'success': False,
                    'error': 'Đơn bán không thuộc chi nhánh đang chọn',
                }), 403
        finally:
            conn.close()

        view_result = _dispatch_named_view(app, 'api_return_sale')
        if view_result is None:
            return jsonify({'success': False, 'error': 'API trả hàng bán chưa sẵn sàng'}), 500
        return view_result

    @app.route('/api/sme/temp-receipts/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_temp_receipt_void(doc_id):
        from Services.sme.cash_extras import void_temp_receipt
        return _void_helper(void_temp_receipt, doc_id, 'api_sme_temp_receipt_void')

    @app.route('/api/sme/fx-revaluation/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_fx_revaluation_void(doc_id):
        from Services.sme.fx_revaluation import void_fx_revaluation
        return _void_helper(void_fx_revaluation, doc_id, 'api_sme_fx_revaluation_void')


def _void_helper(fn, doc_id, log_name, *, with_user=True):
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    try:
        data = request.get_json(silent=True) or {}
        kwargs = {
            'reason': data.get('reason') or 'Hủy chứng từ',
            'commit': True,
        }
        if with_user:
            kwargs['created_by'] = _user()
        doc = fn(conn, doc_id, **kwargs)
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
