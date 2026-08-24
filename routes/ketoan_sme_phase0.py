"""Routes SME Phase P0 — tạm ứng, kiểm kê quỹ, đối chiếu NH, thanh lý TSCĐ, hủy CT."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from flask import jsonify, redirect, render_template, request, session, url_for

from db_utils import get_db_connection, sqlite_commit

logger = logging.getLogger(__name__)


def _bootstrap():
    from Services.sme.bootstrap import ensure_sme_accounting_ready
    from Services.tenant_profile import get_current_tenant_profile

    conn = get_db_connection()
    try:
        profile = get_current_tenant_profile() or {}
        ensure_sme_accounting_ready(
            conn,
            accounting_regime=profile.get('accounting_regime'),
            commit=True,
        )
    finally:
        conn.close()


def _user():
    return session.get('user_name') or session.get('username')


def register_sme_phase0_routes(app, *, login_required, require_sme_regime):
    """Đăng ký route P0 vào app Flask (gọi từ ketoan_sme)."""

    # ── Pages ──────────────────────────────────────────────
    @app.route('/SME_advances')
    @login_required
    @require_sme_regime
    def SME_advances():
        return render_template('KeToanSME/advances.html')

    @app.route('/SME_cash_count')
    @login_required
    @require_sme_regime
    def SME_cash_count():
        return render_template('KeToanSME/cash_count.html')

    @app.route('/SME_bank_reconcile')
    @login_required
    @require_sme_regime
    def SME_bank_reconcile():
        return render_template('KeToanSME/bank_reconcile.html')

    @app.route('/SME_fa_disposal')
    @login_required
    @require_sme_regime
    def SME_fa_disposal():
        return render_template('KeToanSME/fa_disposal.html')

    @app.route('/SME_fa_depreciation_table')
    @login_required
    @require_sme_regime
    def SME_fa_depreciation_table():
        return render_template('KeToanSME/fa_depreciation_table.html')

    @app.route('/SME_advance/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_advance_in(doc_id):
        from Services.sme.advances import get_advance_doc
        from Services.sme.branch_filter import assert_row_in_branch
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            try:
                assert_row_in_branch(conn, 'sme_advance_docs', doc_id, label='Chứng từ tạm ứng')
            except ValueError:
                return redirect(url_for('SME_advances'))
            doc = get_advance_doc(conn, doc_id)
            if not doc:
                return redirect(url_for('SME_advances'))
            info = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
            return render_template(
                'KeToanSME/advance_print.html',
                doc=doc,
                info=dict(info) if info else {},
            )
        finally:
            conn.close()

    @app.route('/SME_cash_count/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_cash_count_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.cash_count import get_cash_count
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(conn, 'sme_cash_counts', doc_id, label='Biên bản kiểm kê quỹ')
            except ValueError:
                return redirect(url_for('SME_cash_count'))
            doc = get_cash_count(conn, doc_id)
            if not doc:
                return redirect(url_for('SME_cash_count'))
            info = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
            return render_template(
                'KeToanSME/cash_count_print.html',
                doc=doc,
                info=dict(info) if info else {},
            )
        finally:
            conn.close()

    @app.route('/SME_fa_disposal/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_fa_disposal_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.fa_lifecycle import get_disposal
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(conn, 'sme_fa_disposals', doc_id, label='Biên bản thanh lý')
            except ValueError:
                return redirect(url_for('SME_fa_disposal'))
            doc = get_disposal(conn, doc_id)
            if not doc:
                return redirect(url_for('SME_fa_disposal'))
            info = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
            return render_template(
                'KeToanSME/fa_disposal_print.html',
                doc=doc,
                info=dict(info) if info else {},
            )
        finally:
            conn.close()

    @app.route('/SME_fa_depreciation_table/in')
    @login_required
    @require_sme_regime
    def SME_fa_depreciation_table_in():
        from Services.sme.branches import request_branch_filter
        from Services.sme.fa_lifecycle import depreciation_schedule
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            year = request.args.get('year', type=int) or datetime.now().year
            period = request.args.get('period', type=int)
            data = depreciation_schedule(
                conn,
                fiscal_year=year,
                period=period,
                branch_code=request_branch_filter(),
            )
            info = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
            return render_template(
                'KeToanSME/fa_depreciation_print.html',
                data=data,
                info=dict(info) if info else {},
            )
        finally:
            conn.close()

    # ── Advances API ───────────────────────────────────────
    @app.route('/api/sme/advances/employees')
    @login_required
    @require_sme_regime
    def api_sme_advance_employees():
        from Services.sme.advances import list_employees_brief
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            return jsonify({'success': True, 'data': list_employees_brief(conn)})
        finally:
            conn.close()

    @app.route('/api/sme/advances', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_advances_list():
        from Services.sme.advances import list_advance_docs
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            rows = list_advance_docs(
                conn,
                doc_type=request.args.get('doc_type'),
                status=request.args.get('status'),
                date_from=request.args.get('from') or request.args.get('date_from'),
                date_to=request.args.get('to') or request.args.get('date_to'),
                branch_code=request.args.get('branch')
                or session.get('sme_branch_filter')
                or 'ALL',
            )
            return jsonify({'success': True, 'data': rows})
        except Exception as e:
            logger.exception('api_sme_advances_list')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/advances/<int:doc_id>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_advance_get(doc_id):
        from Services.sme.advances import get_advance_doc
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            doc = get_advance_doc(conn, doc_id)
            if not doc:
                return jsonify({'success': False, 'error': 'Not found'}), 404
            return jsonify({'success': True, 'data': doc})
        finally:
            conn.close()

    @app.route('/api/sme/advances/request', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_advance_request():
        from Services.sme.advances import create_advance_request, disburse_advance
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            data = request.get_json(silent=True) or {}
            doc = create_advance_request(
                conn,
                doc_date=data.get('date') or data.get('doc_date'),
                employee_name=data.get('employee_name') or '',
                employee_id=data.get('employee_id'),
                amount=data.get('amount'),
                purpose=data.get('purpose') or '',
                payment_method=data.get('payment_method') or 'cash',
                created_by=_user(),
                commit=False,
            )
            if data.get('disburse', True):
                doc = disburse_advance(
                    conn, int(doc['id']),
                    voucher_date=data.get('date') or data.get('doc_date'),
                    created_by=_user(),
                    commit=False,
                )
            sqlite_commit(conn, label='sme_phase0')
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_advance_request')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/advances/<int:doc_id>/disburse', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_advance_disburse(doc_id):
        from Services.sme.advances import disburse_advance
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = disburse_advance(
                conn, doc_id,
                voucher_date=data.get('date'),
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_advance_disburse')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/advances/settle', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_advance_settle():
        from Services.sme.advances import settle_advance
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            data = request.get_json(silent=True) or {}
            doc = settle_advance(
                conn,
                advance_doc_id=int(data.get('advance_doc_id') or 0),
                doc_date=data.get('date') or data.get('doc_date'),
                expense_amount=data.get('expense_amount') or 0,
                cash_return_amount=data.get('cash_return_amount') or 0,
                additional_payment=data.get('additional_payment') or 0,
                expense_account=data.get('expense_account') or '642',
                purpose=data.get('purpose') or '',
                lines=data.get('lines'),
                payment_method=data.get('payment_method') or 'cash',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_advance_settle')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/advances/payment-request', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_payment_request():
        from Services.sme.advances import create_payment_request, pay_payment_request
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            data = request.get_json(silent=True) or {}
            doc = create_payment_request(
                conn,
                doc_date=data.get('date') or data.get('doc_date'),
                employee_name=data.get('employee_name') or data.get('party_name') or '',
                employee_id=data.get('employee_id'),
                amount=data.get('amount'),
                purpose=data.get('purpose') or '',
                expense_account=data.get('expense_account') or '642',
                payment_method=data.get('payment_method') or 'cash',
                lines=data.get('lines'),
                created_by=_user(),
                commit=False,
            )
            if data.get('pay', True):
                doc = pay_payment_request(
                    conn, int(doc['id']),
                    voucher_date=data.get('date') or data.get('doc_date'),
                    created_by=_user(),
                    commit=False,
                )
            sqlite_commit(conn, label='sme_phase0')
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_payment_request')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/advances/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_advance_void(doc_id):
        from Services.sme.advances import void_advance_doc
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = void_advance_doc(
                conn, doc_id,
                reason=data.get('reason') or 'Hủy chứng từ tạm ứng',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_advance_void')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── Cash count ─────────────────────────────────────────
    @app.route('/api/sme/cash-count/book-balance')
    @login_required
    @require_sme_regime
    def api_sme_cash_book_balance():
        from Services.sme.cash_count import book_cash_balance
        conn = get_db_connection()
        try:
            as_of = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
            acc = request.args.get('account') or '1111'
            from Services.sme.branches import request_branch_filter
            branch = request_branch_filter()
            bal = book_cash_balance(
                conn, as_of=as_of, account_code=acc, branch_code=branch,
            )
            return jsonify({
                'success': True, 'book_balance': bal, 'account_code': acc,
                'as_of': as_of[:10], 'branch_code': branch,
            })
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/cash-count', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_cash_count():
        from Services.sme.cash_count import create_cash_count, list_cash_counts
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                from Services.sme.branches import request_branch_filter
                branch = request_branch_filter()
                rows = list_cash_counts(
                    conn,
                    date_from=request.args.get('from'),
                    date_to=request.args.get('to'),
                    branch_code=branch,
                )
                return jsonify({'success': True, 'data': rows, 'branch_code': branch})
            data = request.get_json(silent=True) or {}
            from Services.sme.branches import request_branch_filter
            doc = create_cash_count(
                conn,
                count_date=data.get('date') or data.get('count_date'),
                counted_amount=data.get('counted_amount'),
                account_code=data.get('account_code') or '1111',
                denominations=data.get('denominations'),
                committee=data.get('committee') or '',
                notes=data.get('notes') or '',
                post_difference=bool(data.get('post_difference', True)),
                surplus_account=data.get('surplus_account') or '711',
                shortage_account=data.get('shortage_account') or '811',
                branch_code=request_branch_filter(),
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_cash_count')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/cash-count/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_cash_count_void(doc_id):
        from Services.sme.cash_count import void_cash_count
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = void_cash_count(
                conn, doc_id,
                reason=data.get('reason') or 'Hủy kiểm kê quỹ',
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

    # ── Bank reconcile ─────────────────────────────────────
    @app.route('/api/sme/bank-reconcile/workspace')
    @login_required
    @require_sme_regime
    def api_sme_bank_reconcile_workspace():
        from Services.sme.bank_reconcile import workspace
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            stmt = request.args.get('statement_balance', type=float)
            from Services.sme.branches import request_branch_filter
            data = workspace(
                conn,
                date_from=request.args.get('from') or datetime.now().strftime('%Y-%m-01'),
                date_to=request.args.get('to') or datetime.now().strftime('%Y-%m-%d'),
                account_code=request.args.get('account') or '1121',
                statement_balance=stmt,
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            logger.exception('api_sme_bank_reconcile_workspace')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bank-reconcile', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_bank_reconcile():
        from Services.sme.bank_reconcile import create_reconciliation, list_reconciliations
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                from Services.sme.branches import request_branch_filter
                return jsonify({
                    'success': True,
                    'data': list_reconciliations(conn, branch_code=request_branch_filter()),
                })
            data = request.get_json(silent=True) or {}
            rec = create_reconciliation(
                conn,
                reconcile_date=data.get('reconcile_date') or data.get('date_to'),
                date_from=data.get('date_from') or data.get('from'),
                date_to=data.get('date_to') or data.get('to'),
                statement_balance=data.get('statement_balance'),
                account_code=data.get('account_code') or '1121',
                notes=data.get('notes') or '',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': rec})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_bank_reconcile')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bank-reconcile/<int:rid>/match', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_bank_reconcile_match(rid):
        from Services.sme.bank_reconcile import match_lines
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            rec = match_lines(
                conn, rid,
                journal_line_id=int(data.get('journal_line_id') or 0),
                bank_txn_id=int(data.get('bank_txn_id') or 0),
                note=data.get('note') or '',
                commit=True,
            )
            return jsonify({'success': True, 'data': rec})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bank-reconcile/unmatch/<int:match_id>', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_bank_reconcile_unmatch(match_id):
        from Services.sme.bank_reconcile import unmatch
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            rec = unmatch(conn, match_id, commit=True)
            return jsonify({'success': True, 'data': rec})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bank-reconcile/<int:rid>/close', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_bank_reconcile_close(rid):
        from Services.sme.bank_reconcile import close_reconciliation
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            rec = close_reconciliation(
                conn, rid, force=bool(data.get('force')), commit=True,
            )
            return jsonify({'success': True, 'data': rec})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bank-txn/<int:txn_id>/create-receipt', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_bank_txn_create_receipt(txn_id):
        """Tạo phiếu thu từ giao dịch NH chưa vào sổ."""
        from Services.sme.bank_reconcile import create_receipt_from_bank_txn
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            sale_id = data.get('sale_id')
            try:
                sale_id = int(sale_id) if sale_id not in (None, '', 0, '0') else None
            except (TypeError, ValueError):
                sale_id = None
            result = create_receipt_from_bank_txn(
                conn,
                txn_id,
                sale_id=sale_id,
                party_name=data.get('party_name') or '',
                credit_account=data.get('credit_account') or '131',
                reason=data.get('reason') or '',
                created_by=session.get('user_name') or session.get('username'),
                commit=True,
            )
            return jsonify({'success': True, 'data': result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_bank_txn_create_receipt')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── FA disposal + 06-TSCĐ ───────────────────────────────
    @app.route('/api/sme/fixed-assets/list')
    @login_required
    @require_sme_regime
    def api_sme_fa_list():
        from Services.sme.fa_lifecycle import list_active_assets, asset_book_values
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            as_of = request.args.get('as_of') or datetime.now().strftime('%Y-%m-%d')
            branch = (
                request.args.get('branch')
                or session.get('sme_branch_filter')
                or 'ALL'
            )
            status = request.args.get('status') or None
            assets = list_active_assets(conn, branch_code=branch, status=status)
            enriched = []
            for a in assets:
                try:
                    vals = asset_book_values(conn, int(a['id']), as_of=as_of)
                    a['accum_dep'] = vals['accum_dep']
                    a['net_book'] = vals['net_book']
                except Exception:
                    a['accum_dep'] = 0
                    a['net_book'] = a.get('original_cost') or 0
                enriched.append(a)
            return jsonify({'success': True, 'data': enriched, 'branch_code': branch})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/SME_fixed_assets')
    @login_required
    @require_sme_regime
    def SME_fixed_assets():
        return render_template('KeToanSME/fixed_assets.html')

    @app.route('/api/sme/fixed-assets/<int:asset_id>/period', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_fa_set_period(asset_id):
        from Services.sme.fa_lifecycle import update_asset_depreciation_period
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            data = request.get_json(silent=True) or {}
            months = data.get('so_thang_khau_hao') or data.get('months')
            doc = update_asset_depreciation_period(
                conn,
                asset_id,
                so_thang_khau_hao=int(months or 0),
                start_date=data.get('ngay_bat_dau_su_dung') or data.get('start_date'),
                expense_account=data.get('expense_account'),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_fa_set_period')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/fa-disposal', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_fa_disposal():
        from Services.sme.fa_lifecycle import dispose_fixed_asset, list_disposals
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                return jsonify({
                    'success': True,
                    'data': list_disposals(
                        conn,
                        date_from=request.args.get('from'),
                        date_to=request.args.get('to'),
                        branch_code=request.args.get('branch')
                        or session.get('sme_branch_filter')
                        or 'ALL',
                    ),
                })
            data = request.get_json(silent=True) or {}
            doc = dispose_fixed_asset(
                conn,
                asset_id=int(data.get('asset_id') or 0),
                disposal_date=data.get('date') or data.get('disposal_date'),
                disposal_type=data.get('disposal_type') or 'scrap',
                proceeds=data.get('proceeds') or 0,
                payment_method=data.get('payment_method') or 'cash',
                counterparty=data.get('counterparty') or '',
                reason=data.get('reason') or '',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_fa_disposal')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/fa-disposal/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_fa_disposal_void(doc_id):
        from Services.sme.fa_lifecycle import void_disposal
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = void_disposal(
                conn, doc_id,
                reason=data.get('reason') or 'Hủy thanh lý',
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

    @app.route('/api/sme/fa-depreciation-table')
    @login_required
    @require_sme_regime
    def api_sme_fa_dep_table():
        from Services.sme.branches import request_branch_filter
        from Services.sme.fa_lifecycle import depreciation_schedule
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            year = request.args.get('year', type=int) or datetime.now().year
            period = request.args.get('period', type=int)
            data = depreciation_schedule(
                conn,
                fiscal_year=year,
                period=period,
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── Void vouchers (01/02-TT) ────────────────────────────
    @app.route('/api/sme/vouchers/<int:voucher_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_voucher_void(voucher_id):
        from Services.sme.vouchers import void_voucher
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            data = request.get_json(silent=True) or {}
            doc = void_voucher(
                conn, voucher_id,
                reason=data.get('reason') or 'Hủy chứng từ thu/chi',
                created_by=_user(),
                posting_date=data.get('date'),
                commit=True,
            )
            return jsonify({
                'success': True,
                'data': doc,
                'message': doc.get('message') or (
                    'Đã xóa bút toán' if doc.get('mode') == 'hard_delete' else 'Đã hủy chứng từ'
                ),
            })
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_voucher_void')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()
