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

    @app.route('/api/sme/bctc/opening', methods=['GET', 'POST', 'DELETE'])
    @login_required
    @require_sme_regime
    def api_sme_bctc_opening():
        from Services.sme.bctc_opening import (
            clear_opening_lines,
            list_opening_lines,
            opening_meta,
            save_opening_lines,
        )
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            if request.method == 'GET':
                year = request.args.get('year', type=int) or datetime.now().year
                report = (request.args.get('report') or 'B01').strip().upper()
                lines = list_opening_lines(conn, year, report)
                return jsonify({
                    'success': True,
                    'data': {
                        'fiscal_year': year,
                        'report': report,
                        'lines': {k: float(v) for k, v in lines.items()},
                        'meta': opening_meta(conn, year, report),
                    },
                })
            if request.method == 'DELETE':
                year = request.args.get('year', type=int) or datetime.now().year
                report = request.args.get('report')
                n = clear_opening_lines(conn, fiscal_year=year, report=report)
                return jsonify({'success': True, 'deleted': n})
            payload = request.get_json(silent=True) or {}
            year = int(payload.get('year') or payload.get('fiscal_year') or datetime.now().year)
            report = str(payload.get('report') or 'B01').strip().upper()
            info = save_opening_lines(
                conn,
                fiscal_year=year,
                report=report,
                lines=payload.get('lines') or [],
                source=str(payload.get('source') or 'manual'),
                updated_by=_user(),
            )
            return jsonify({'success': True, **info})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            logger.exception('api_sme_bctc_opening')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bctc/opening/template.xlsx')
    @login_required
    @require_sme_regime
    def api_sme_bctc_opening_template():
        from Services.sme.bctc_opening import build_opening_template
        conn = get_db_connection()
        try:
            data = build_opening_template(conn)
            year = request.args.get('year', type=int) or datetime.now().year
            return Response(
                data,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                headers={
                    'Content-Disposition': (
                        f'attachment; filename="Mau_BCTC_so_dau_nam_{year}.xlsx"'
                    ),
                },
            )
        except Exception as e:
            logger.exception('api_sme_bctc_opening_template')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/bctc/opening/import', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_bctc_opening_import():
        from Services.sme.bctc_opening import apply_excel_import, parse_opening_excel
        file = request.files.get('file')
        if not file or not (file.filename or '').lower().endswith(('.xlsx', '.xlsm')):
            return jsonify({
                'success': False,
                'error': 'Chọn file Excel .xlsx (xuất từ phần mềm khác hoặc mẫu hệ thống).',
            }), 400
        year = request.form.get('year', type=int) or request.args.get('year', type=int)
        year = year or datetime.now().year
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            parsed = parse_opening_excel(file)
            result = apply_excel_import(
                conn, fiscal_year=int(year), parsed=parsed, created_by=_user(),
            )
            return jsonify({'success': True, 'data': result})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_bctc_opening_import')
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

    @app.route('/api/sme/tt58-tax-method', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_tt58_tax_method():
        from Services.sme.regime_profile import get_ledger_profile, set_tt58_tax_method
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            profile = get_ledger_profile(conn)
            if not profile.get('is_tt58_micro'):
                return jsonify({
                    'success': False,
                    'error': 'Chỉ áp dụng cho doanh nghiệp siêu nhỏ (TT58).',
                }), 400
            if request.method == 'GET':
                return jsonify({'success': True, 'data': profile})
            data = request.get_json(silent=True) or {}
            method = data.get('method') or data.get('tt58_tax_method') or request.form.get('method')
            if not method:
                return jsonify({'success': False, 'error': 'Thiếu phương pháp thuế'}), 400
            tax_def = set_tt58_tax_method(conn, method, commit=True)
            profile = get_ledger_profile(conn)
            return jsonify({
                'success': True,
                'data': profile,
                'tax_method': tax_def,
                'message': f"Đã chọn {tax_def.get('short_label')}",
            })
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/tt58-tax-rates', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_tt58_tax_rates():
        from Services.sme.regime_profile import get_ledger_profile
        from Services.sme.tt58_tax_rates import (
            get_tt58_tax_rates,
            list_tt58_tax_rate_history,
            rates_ui_context_for_method,
            save_tt58_tax_rates,
        )
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            profile = get_ledger_profile(conn)
            if not profile.get('is_tt58_micro'):
                return jsonify({
                    'success': False,
                    'error': 'Chỉ áp dụng cho doanh nghiệp siêu nhỏ (TT58).',
                }), 400
            as_of = request.args.get('as_of') or (request.get_json(silent=True) or {}).get('as_of')
            if request.method == 'GET':
                rates = get_tt58_tax_rates(conn, as_of=as_of)
                return jsonify({
                    'success': True,
                    'data': rates,
                    'ui': rates_ui_context_for_method(profile.get('tt58_tax_method')),
                    'history': list_tt58_tax_rate_history(conn, limit=30),
                    'tax_method': profile.get('tt58_tax_method'),
                })
            data = request.get_json(silent=True) or {}
            saved = save_tt58_tax_rates(
                conn,
                sectors=data.get('sectors') or [],
                cit_pct_income=data.get('cit_pct_income'),
                cit_income_brackets=data.get('cit_income_brackets'),
                effective_from=data.get('effective_from'),
                note=data.get('note'),
                created_by=session.get('username') or session.get('user') or 'user',
                commit=True,
            )
            return jsonify({
                'success': True,
                'data': saved,
                'ui': rates_ui_context_for_method(profile.get('tt58_tax_method')),
                'message': 'Đã lưu thuế suất (có hiệu lực từ ngày đã chọn).',
            })
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── Hàng gửi đi bán (TK 157) + 01-BH deliveries ─────────
    @app.route('/SME_consignment')
    @login_required
    @require_sme_regime
    def SME_consignment():
        return render_template('KeToanSME/consignment.html')

    @app.route('/api/sme/consignments', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_consignments():
        from Services.sme.consignment import list_consignments, ship_consignment
        from Services.sme.branches import request_branch_filter
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = request_branch_filter()
            if request.method == 'GET':
                return jsonify({
                    'success': True,
                    'data': list_consignments(
                        conn,
                        agent_name=request.args.get('agent'),
                        status=request.args.get('status'),
                        branch_code=branch,
                    ),
                })
            data = request.get_json(silent=True) or {}
            doc = ship_consignment(
                conn,
                agent_name=data.get('agent_name') or data.get('agent') or '',
                delivery_date=data.get('date') or data.get('delivery_date'),
                warehouse_code=data.get('warehouse_code') or data.get('warehouse') or '',
                items=data.get('items') or data.get('lines') or [],
                notes=data.get('notes') or '',
                created_by=_user(),
                branch_code=branch,
                customer_id=data.get('customer_id'),
                agent_address=data.get('agent_address') or data.get('address') or '',
                agent_tax_code=data.get('agent_tax_code') or data.get('tax_code') or '',
                agent_phone=data.get('agent_phone') or data.get('phone') or '',
                agent_email=data.get('agent_email') or data.get('email') or '',
                send_email_to_agent=data.get('send_email', True) not in (False, 0, '0', 'false'),
                commit=True,
            )
            return jsonify({
                'success': True,
                'data': doc,
                'email_sent': bool(doc.get('email_sent')),
                'email_error': doc.get('email_error'),
            })
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_consignments')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/consignments/<int:doc_id>', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_consignment_detail(doc_id):
        from Services.sme.consignment import get_consignment
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            doc = get_consignment(conn, doc_id)
            if not doc:
                return jsonify({'success': False, 'error': 'Không tìm thấy'}), 404
            return jsonify({'success': True, 'data': doc})
        finally:
            conn.close()

    @app.route('/api/sme/consignments/<int:doc_id>/confirm-sale', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_consignment_confirm_sale(doc_id):
        from Services.sme.consignment import confirm_consignment_sale
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            loai = data.get('loai_hdon', 1)
            try:
                loai = int(loai)
            except (TypeError, ValueError):
                loai = 1
            doc = confirm_consignment_sale(
                conn, doc_id,
                event_date=data.get('date') or data.get('event_date'),
                lines=data.get('lines') or data.get('items') or [],
                payment_method=data.get('payment_method') or '131',
                tax_pct=float(data.get('tax_pct') if data.get('tax_pct') is not None else 10),
                notes=data.get('notes') or '',
                created_by=_user(),
                issue_einvoice=data.get('issue_einvoice', True) not in (False, 0, '0', 'false'),
                loai_hdon=loai,
                commit=True,
            )
            last = (doc or {}).get('last_event') or {}
            return jsonify({
                'success': True,
                'data': doc,
                'invoice_number': last.get('invoice_number'),
                'sale_id': last.get('sale_id'),
                'invoice': last.get('invoice'),
            })
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_consignment_confirm_sale')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/consignments/<int:doc_id>/return', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_consignment_return(doc_id):
        from Services.sme.consignment import return_consignment
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = return_consignment(
                conn, doc_id,
                event_date=data.get('date') or data.get('event_date'),
                lines=data.get('lines') or data.get('items') or [],
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
            logger.exception('api_sme_consignment_return')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/consignments/<int:doc_id>/send-email', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_consignment_send_email(doc_id):
        from Services.sme.consignment import send_consignment_voucher_email
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            info = send_consignment_voucher_email(conn, doc_id, commit=True)
            return jsonify({'success': True, 'data': info})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_consignment_send_email')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/consignments/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_consignment_void(doc_id):
        from Services.sme.consignment import void_consignment
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = void_consignment(
                conn, doc_id,
                reason=data.get('reason') or 'Hủy phiếu gửi đại lý',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_consignment_void')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/agent-deliveries', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_agent_deliveries():
        from Services.sme.sale_forms import create_agent_delivery
        from Services.sme.consignment import list_consignments, ship_consignment
        from Services.sme.branches import request_branch_filter
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            branch = request_branch_filter()
            if request.method == 'GET':
                return jsonify({
                    'success': True,
                    'data': list_consignments(
                        conn, agent_name=request.args.get('agent'),
                        branch_code=branch,
                    ),
                })
            data = request.get_json(silent=True) or {}
            wh = (data.get('warehouse_code') or data.get('warehouse') or '').strip()
            if wh:
                doc = ship_consignment(
                    conn,
                    agent_name=data.get('agent_name') or data.get('agent') or '',
                    delivery_date=data.get('date') or data.get('delivery_date'),
                    warehouse_code=wh,
                    items=data.get('items') or data.get('lines') or [],
                    notes=data.get('notes') or '',
                    created_by=_user(),
                    branch_code=branch,
                    customer_id=data.get('customer_id'),
                    agent_address=data.get('agent_address') or data.get('address') or '',
                    agent_tax_code=data.get('agent_tax_code') or data.get('tax_code') or '',
                    agent_phone=data.get('agent_phone') or data.get('phone') or '',
                    agent_email=data.get('agent_email') or data.get('email') or '',
                    send_email_to_agent=data.get('send_email', True) not in (False, 0, '0', 'false'),
                    commit=True,
                )
                return jsonify({
                    'success': True,
                    'data': doc,
                    'email_sent': bool(doc.get('email_sent')),
                    'email_error': doc.get('email_error'),
                })
            else:
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
        from Services.sme.consignment import void_consignment, get_consignment
        from Services.sme.sale_forms import void_agent_delivery
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = get_consignment(conn, doc_id)
            if doc and doc.get('journal_ship_id'):
                out = void_consignment(
                    conn, doc_id,
                    reason=data.get('reason') or 'Hủy phiếu giao đại lý',
                    created_by=_user(),
                    commit=True,
                )
            else:
                out = void_agent_delivery(
                    conn, doc_id,
                    reason=data.get('reason') or 'Hủy phiếu giao đại lý',
                    commit=True,
                )
            return jsonify({'success': True, 'data': out})
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
            return jsonify({
                'success': True,
                'data': doc,
                'message': doc.get('message') or f'Đã hủy lương T{month}/{year}',
            })
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

    def _ledger_err(conn, e, name):
        try:
            conn.rollback()
        except Exception:
            pass
        if isinstance(e, ValueError):
            return jsonify({'success': False, 'error': str(e)}), 400
        logger.exception(name)
        return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/sme/ledger-ops', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_ledger_ops_list():
        from Services.sme.ledger_ops import list_ledger_ops
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            op = (request.args.get('op_type') or '').strip() or None
            rows = list_ledger_ops(conn, op_type=op, limit=200)
            return jsonify({'success': True, 'data': rows})
        except Exception as e:
            logger.exception('api_sme_ledger_ops_list')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/sales-allowance', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_sales_allowance():
        from Services.sme.ledger_ops import post_sales_allowance
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            out = post_sales_allowance(
                conn,
                doc_date=payload.get('date') or payload.get('doc_date'),
                amount=payload.get('amount'),
                vat_amount=payload.get('vat_amount') or 0,
                kind=payload.get('kind') or 'discount',
                customer_name=payload.get('customer_name') or '',
                settle_account=payload.get('settle_account') or '131',
                notes=payload.get('notes') or '',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': out})
        except Exception as e:
            return _ledger_err(conn, e, 'api_sme_sales_allowance')
        finally:
            conn.close()

    @app.route('/api/sme/provisions', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_provisions():
        from Services.sme.ledger_ops import post_provision
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            out = post_provision(
                conn,
                doc_date=payload.get('date') or payload.get('doc_date'),
                amount=payload.get('amount'),
                kind=payload.get('kind') or 'ar',
                action=payload.get('action') or 'accrue',
                notes=payload.get('notes') or '',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': out})
        except Exception as e:
            return _ledger_err(conn, e, 'api_sme_provisions')
        finally:
            conn.close()

    @app.route('/api/sme/other-tax', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_other_tax():
        from Services.sme.ledger_ops import OTHER_TAX_DEFS, accrue_other_tax, pay_other_tax
        payload = request.get_json(silent=True) or {}
        action = (payload.get('action') or 'accrue').strip().lower()
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            if action in ('pay', 'nop'):
                out = pay_other_tax(
                    conn,
                    doc_date=payload.get('date') or payload.get('doc_date'),
                    amount=payload.get('amount'),
                    tax_account=payload.get('tax_account') or '3339',
                    payment_method=payload.get('payment_method') or 'bank',
                    notes=payload.get('notes') or '',
                    created_by=_user(),
                    commit=True,
                )
            else:
                out = accrue_other_tax(
                    conn,
                    doc_date=payload.get('date') or payload.get('doc_date'),
                    amount=payload.get('amount'),
                    tax_account=payload.get('tax_account') or '3339',
                    expense_account=payload.get('expense_account'),
                    notes=payload.get('notes') or '',
                    created_by=_user(),
                    commit=True,
                )
            return jsonify({
                'success': True, 'data': out,
                'tax_defs': [{'code': c, 'label': n, 'expense': e} for c, n, e in OTHER_TAX_DEFS],
            })
        except Exception as e:
            return _ledger_err(conn, e, 'api_sme_other_tax')
        finally:
            conn.close()

    @app.route('/SME_prepaid')
    @login_required
    @require_sme_regime
    def SME_prepaid():
        return render_template('KeToanSME/prepaid.html')

    @app.route('/api/sme/prepaid', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_prepaid_list():
        from Services.sme.prepaid import list_prepaid
        from Services.sme.branches import request_branch_filter
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            rows = list_prepaid(
                conn,
                status=request.args.get('status') or None,
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': rows})
        except Exception as e:
            logger.exception('api_sme_prepaid_list')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/prepaid', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_prepaid_create():
        from Services.sme.prepaid import create_prepaid
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = create_prepaid(
                conn,
                name=payload.get('name') or payload.get('party_name') or '',
                start_date=payload.get('date') or payload.get('start_date'),
                amount=payload.get('amount'),
                months=int(payload.get('months') or 12),
                vat_amount=payload.get('vat_amount') or 0,
                expense_account=payload.get('expense_account') or '642',
                payment_method=payload.get('payment_method') or 'bank',
                notes=payload.get('notes') or '',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            return _ledger_err(conn, e, 'api_sme_prepaid_create')
        finally:
            conn.close()

    @app.route('/api/sme/prepaid/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_prepaid_void(doc_id):
        from Services.sme.prepaid import void_prepaid
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = void_prepaid(
                conn, doc_id,
                reason=payload.get('reason') or 'Hủy chi phí trả trước',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            return _ledger_err(conn, e, 'api_sme_prepaid_void')
        finally:
            conn.close()

    @app.route('/SME_accruals')
    @login_required
    @require_sme_regime
    def SME_accruals():
        return render_template('KeToanSME/accruals.html')

    @app.route('/api/sme/accruals', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_accruals_list():
        from Services.sme.accruals import list_accruals
        from Services.sme.branches import request_branch_filter
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            rows = list_accruals(
                conn,
                kind=request.args.get('kind') or None,
                status=request.args.get('status') or None,
                branch_code=request_branch_filter(),
            )
            return jsonify({'success': True, 'data': rows})
        except Exception as e:
            logger.exception('api_sme_accruals_list')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/accruals', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_accruals_create():
        from Services.sme.accruals import create_accrual
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = create_accrual(
                conn,
                kind=payload.get('kind') or 'expense',
                name=payload.get('name') or '',
                doc_date=payload.get('date') or payload.get('doc_date'),
                amount=payload.get('amount'),
                contra_account=payload.get('contra_account'),
                payment_method=payload.get('payment_method') or 'bank',
                notes=payload.get('notes') or '',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            return _ledger_err(conn, e, 'api_sme_accruals_create')
        finally:
            conn.close()

    @app.route('/api/sme/accruals/<int:doc_id>/settle', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_accruals_settle(doc_id):
        from Services.sme.accruals import settle_accrual
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = settle_accrual(
                conn, doc_id,
                settle_date=payload.get('date') or payload.get('settle_date'),
                payment_method=payload.get('payment_method'),
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            return _ledger_err(conn, e, 'api_sme_accruals_settle')
        finally:
            conn.close()

    @app.route('/api/sme/accruals/<int:doc_id>/void', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_accruals_void(doc_id):
        from Services.sme.accruals import void_accrual
        payload = request.get_json(silent=True) or {}
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = void_accrual(
                conn, doc_id,
                reason=payload.get('reason') or 'Hủy chứng từ',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            return _ledger_err(conn, e, 'api_sme_accruals_void')
        finally:
            conn.close()
