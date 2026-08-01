"""Phase P8 — tối ưu: BCTC Excel, 01-BH giao đại lý, TT58, TNCN, void lương."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from flask import Response, jsonify, render_template, request, session

from db_utils import get_db_connection

logger = logging.getLogger(__name__)


def _user():
    return session.get('user_name') or session.get('username')


def register_sme_phase8_routes(app, *, login_required, require_sme_regime):

    @app.route('/api/sme/bctc/export.xlsx')
    @login_required
    @require_sme_regime
    def api_sme_bctc_export_xlsx():
        from Services.sme.bctc_export import export_bctc_workbook, export_meta
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            year = request.args.get('year', type=int) or datetime.now().year
            p_from = request.args.get('period_from', type=int) or 1
            p_to = request.args.get('period_to', type=int) or 12
            profit = request.args.get('include_current_profit', '1') not in ('0', 'false', 'False')
            data = export_bctc_workbook(
                conn, fiscal_year=year, period_from=p_from, period_to=p_to,
                include_current_profit=profit,
            )
            meta = export_meta(fiscal_year=year, period_from=p_from, period_to=p_to)
            return Response(
                data,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                headers={
                    'Content-Disposition': f'attachment; filename="{meta["filename"]}"',
                },
            )
        except Exception as e:
            logger.exception('api_sme_bctc_export_xlsx')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/regime-profile')
    @login_required
    @require_sme_regime
    def api_sme_regime_profile():
        from Services.sme.regime_profile import get_ledger_profile
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            return jsonify({'success': True, 'data': get_ledger_profile(conn)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── 01-BH agent deliveries ─────────────────────────────
    @app.route('/api/sme/agent-deliveries', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_agent_deliveries():
        from Services.sme.sale_forms import create_agent_delivery, list_agent_deliveries
        from Services.sme.branches import request_branch_filter
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = request_branch_filter()
            if request.method == 'GET':
                return jsonify({
                    'success': True,
                    'data': list_agent_deliveries(
                        conn, agent_name=request.args.get('agent'),
                        branch_code=branch,
                    ),
                })
            data = request.get_json(silent=True) or {}
            doc = create_agent_delivery(
                conn,
                agent_name=data.get('agent_name') or data.get('agent') or '',
                delivery_date=data.get('date') or data.get('delivery_date'),
                items=data.get('items') or data.get('lines') or [],
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
            logger.exception('api_sme_agent_deliveries')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/agent-deliveries/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_agent_delivery_void(doc_id):
        from Services.sme.sale_forms import void_agent_delivery
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = void_agent_delivery(
                conn, doc_id,
                reason=data.get('reason') or 'Hủy phiếu giao đại lý',
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_agent_delivery_void')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── Payroll void ───────────────────────────────────────
    @app.route('/api/sme/payroll/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_payroll_void():
        from Services.sme.payroll import void_payroll_run
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            month = int(data.get('month') or 0)
            year = int(data.get('year') or 0)
            from Services.sme.branches import request_branch_filter
            doc = void_payroll_run(
                conn, month=month, year=year,
                reason=data.get('reason') or 'Hủy bảng lương',
                created_by=_user(), commit=True,
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': doc, 'message': f'Đã hủy lương T{month}/{year}'})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_payroll_void')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── PIT / TNCN ─────────────────────────────────────────
    @app.route('/SME_pit_declaration')
    @login_required
    @require_sme_regime
    def SME_pit_declaration():
        return render_template('KeToanSME/pit_declaration.html')

    @app.route('/api/sme/pit/declaration')
    @login_required
    @require_sme_regime
    def api_sme_pit_declaration():
        from Services.sme.pit_declaration import pit_withholding_worksheet
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            year = request.args.get('year', type=int) or datetime.now().year
            p_from = request.args.get('period_from', type=int) or 1
            p_to = request.args.get('period_to', type=int) or 12
            data = pit_withholding_worksheet(
                conn, fiscal_year=year, period_from=p_from, period_to=p_to,
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            logger.exception('api_sme_pit_declaration')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()
