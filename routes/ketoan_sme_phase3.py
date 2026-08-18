"""Routes SME tiếp theo — giá thành UI phụ, TNDN worksheet/XML, góp vốn, 08b."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from flask import Response, jsonify, render_template, request, session

from db_utils import get_db_connection

logger = logging.getLogger(__name__)


def _user():
    return session.get('user_name') or session.get('username')


def _bootstrap():
    from Services.sme.bootstrap import ensure_sme_accounting_ready
    from Services.sme.capital import ensure_sme_capital_schema
    from Services.tenant_profile import get_current_tenant_profile

    conn = get_db_connection()
    try:
        profile = get_current_tenant_profile() or {}
        ensure_sme_accounting_ready(
            conn, accounting_regime=profile.get('accounting_regime'), commit=False,
        )
        ensure_sme_capital_schema(conn, commit=True)
    finally:
        conn.close()


def register_sme_phase3_routes(app, *, login_required, require_sme_regime):

    @app.route('/SME_capital')
    @login_required
    @require_sme_regime
    def SME_capital():
        return render_template('KeToanSME/capital.html')

    @app.route('/SME_cit_declaration')
    @login_required
    @require_sme_regime
    def SME_cit_declaration():
        return render_template('KeToanSME/cit_declaration.html')

    @app.route('/SME_cash_count_fx')
    @login_required
    @require_sme_regime
    def SME_cash_count_fx():
        return render_template('KeToanSME/cash_count_fx.html')

    # ── CIT worksheet + XML ────────────────────────────────
    @app.route('/api/sme/cit/declaration')
    @login_required
    @require_sme_regime
    def api_sme_cit_declaration():
        from Services.sme.cit_declaration import cit_declaration_worksheet
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or 12
            rate = request.args.get('tax_rate', type=float)
            adj = {
                'non_deductible': request.args.get('non_deductible', type=float) or 0,
                'exempt_income': request.args.get('exempt_income', type=float) or 0,
                'other_increase': request.args.get('other_increase', type=float) or 0,
                'other_decrease': request.args.get('other_decrease', type=float) or 0,
                'loss_carry_forward': request.args.get('loss_carry_forward', type=float) or 0,
            }
            data = cit_declaration_worksheet(
                conn, fiscal_year=year, period_to=period_to,
                tax_rate=rate, adjustments=adj,
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            logger.exception('api_sme_cit_declaration')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/cit/xml')
    @login_required
    @require_sme_regime
    def api_sme_cit_xml():
        from Services.sme.cit_xml import generate_sme_cit_xml
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            year = request.args.get('year', type=int) or datetime.now().year
            period_to = request.args.get('period_to', type=int) or 12
            rate = request.args.get('tax_rate', type=float)
            adj = {
                'non_deductible': request.args.get('non_deductible', type=float) or 0,
                'exempt_income': request.args.get('exempt_income', type=float) or 0,
                'other_increase': request.args.get('other_increase', type=float) or 0,
                'other_decrease': request.args.get('other_decrease', type=float) or 0,
                'loss_carry_forward': request.args.get('loss_carry_forward', type=float) or 0,
            }
            result = generate_sme_cit_xml(
                conn, fiscal_year=year, period_to=period_to,
                tax_rate=rate, adjustments=adj,
            )
            return Response(
                result['xml'],
                mimetype='application/xml',
                headers={
                    'Content-Disposition': f'attachment; filename="{result["filename"]}"',
                },
            )
        except Exception as e:
            logger.exception('api_sme_cit_xml')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── Capital ────────────────────────────────────────────
    @app.route('/api/sme/capital', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_capital():
        from Services.sme.capital import (
            contribute_capital,
            declare_dividend,
            distribute_profit,
            list_capital_docs,
            pay_dividend,
        )
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                from Services.sme.branches import request_branch_filter
                return jsonify({'success': True, 'data': list_capital_docs(
                    conn, branch_code=request_branch_filter(),
                )})
            data = request.get_json(silent=True) or {}
            dtype = (data.get('doc_type') or 'contribute').strip().lower()
            if dtype == 'contribute':
                doc = contribute_capital(
                    conn,
                    doc_date=data.get('date') or data.get('doc_date'),
                    amount=data.get('amount'),
                    party_name=data.get('party_name') or '',
                    equity_account=data.get('equity_account') or '4111',
                    cash_account=data.get('cash_account') or '1121',
                    notes=data.get('notes') or '',
                    created_by=_user(),
                    commit=True,
                )
            elif dtype == 'dividend':
                doc = declare_dividend(
                    conn,
                    doc_date=data.get('date') or data.get('doc_date'),
                    amount=data.get('amount'),
                    party_name=data.get('party_name') or 'Cổ đông',
                    equity_account=data.get('equity_account') or '4212',
                    payable_account=data.get('payable_account') or '3388',
                    notes=data.get('notes') or '',
                    created_by=_user(),
                    commit=True,
                )
            elif dtype == 'dividend_pay':
                doc = pay_dividend(
                    conn,
                    doc_date=data.get('date') or data.get('doc_date'),
                    amount=data.get('amount'),
                    party_name=data.get('party_name') or 'Cổ đông',
                    payable_account=data.get('payable_account') or '3388',
                    cash_account=data.get('cash_account') or '1121',
                    notes=data.get('notes') or '',
                    created_by=_user(),
                    commit=True,
                )
            elif dtype in ('distribute', 'phan_phoi', 'ppln'):
                doc = distribute_profit(
                    conn,
                    doc_date=data.get('date') or data.get('doc_date'),
                    amount=data.get('amount'),
                    party_name=data.get('party_name') or '',
                    equity_account=data.get('equity_account') or '4212',
                    dest_account=data.get('dest_account') or data.get('cash_account') or '418',
                    notes=data.get('notes') or '',
                    created_by=_user(),
                    commit=True,
                )
            else:
                return jsonify({'success': False, 'error': 'doc_type không hợp lệ'}), 400
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_capital')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/capital/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_capital_void(doc_id):
        from Services.sme.capital import void_capital_doc
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = void_capital_doc(
                conn, doc_id,
                reason=data.get('reason') or 'Hủy chứng từ vốn',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_capital_void')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── 08b kiểm kê quỹ ngoại tệ ───────────────────────────
    @app.route('/api/sme/cash-count-fx', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_cash_count_fx():
        """Kiểm kê quỹ ngoại tệ 08b-TT — ghi chênh lệch tỷ giá lên 515/635."""
        from Services.sme.cash_count import create_cash_count, ensure_sme_cash_count_schema, list_cash_counts
        from Services.sme.branches import request_branch_filter
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            ensure_sme_cash_count_schema(conn, commit=False)
            branch = request_branch_filter()
            if request.method == 'GET':
                return jsonify({
                    'success': True,
                    'data': list_cash_counts(conn, form_code='08b-TT', branch_code=branch),
                })
            data = request.get_json(silent=True) or {}
            # Đếm theo VND quy đổi: counted_fc * rate
            counted_fc = float(data.get('counted_fc') or 0)
            rate = float(data.get('rate') or 0)
            if counted_fc < 0 or rate <= 0:
                raise ValueError('Số ngoại tệ / tỷ giá không hợp lệ')
            counted_vnd = round(counted_fc * rate, 2)
            account = (data.get('account_code') or '1112').strip() or '1112'
            doc = create_cash_count(
                conn,
                count_date=data.get('date') or data.get('count_date'),
                counted_amount=counted_vnd,
                account_code=account,
                branch_code=branch,
                denominations={
                    'currency': data.get('currency') or 'USD',
                    'counted_fc': counted_fc,
                    'rate': rate,
                    'form': '08b-TT',
                },
                committee=data.get('committee') or '',
                notes=(data.get('notes') or '') + f' | 08b {data.get("currency") or "USD"} @ {rate}',
                post_difference=bool(data.get('post_difference', True)),
                surplus_account=data.get('surplus_account') or '515',
                shortage_account=data.get('shortage_account') or '635',
                created_by=_user(),
                commit=False,
            )
            # Ghi đè form_code thành 08b-TT
            conn.execute(
                "UPDATE sme_cash_counts SET form_code = '08b-TT' WHERE id = ?",
                (doc['id'],),
            )
            conn.commit()
            doc = {**doc, 'form_code': '08b-TT', 'counted_fc': counted_fc, 'rate': rate}
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_cash_count_fx')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()
