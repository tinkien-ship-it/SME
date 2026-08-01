"""Routes SME — LĐTL giao khoán/thưởng/OT/thuê ngoài, 03-VT, biên bản TSCĐ."""
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
    from Services.sme.labor_contract import ensure_sme_labor_contract_schema
    from Services.sme.labor_sheets import ensure_sme_labor_sheets_schema
    from Services.sme.stock_inspection import ensure_sme_stock_inspection_schema
    from Services.sme.fa_lifecycle import ensure_sme_fa_docs_schema
    from Services.sme.material_remaining import ensure_sme_material_remaining_schema
    from Services.tenant_profile import get_current_tenant_profile

    conn = get_db_connection()
    try:
        profile = get_current_tenant_profile() or {}
        ensure_sme_accounting_ready(
            conn, accounting_regime=profile.get('accounting_regime'), commit=False,
        )
        ensure_sme_labor_contract_schema(conn, commit=False)
        ensure_sme_labor_sheets_schema(conn, commit=False)
        ensure_sme_stock_inspection_schema(conn, commit=False)
        ensure_sme_fa_docs_schema(conn, commit=False)
        ensure_sme_material_remaining_schema(conn, commit=True)
    finally:
        conn.close()


def _biz_info(conn):
    info = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
    return dict(info) if info else {}


def register_sme_phase4_routes(app, *, login_required, require_sme_regime):

    # ── Pages ──────────────────────────────────────────────
    @app.route('/SME_labor_contracts')
    @login_required
    @require_sme_regime
    def SME_labor_contracts():
        return render_template('KeToanSME/labor_contracts.html')

    @app.route('/SME_labor_sheets')
    @login_required
    @require_sme_regime
    def SME_labor_sheets():
        return render_template('KeToanSME/labor_sheets.html')

    @app.route('/SME_stock_inspection')
    @login_required
    @require_sme_regime
    def SME_stock_inspection():
        return render_template('KeToanSME/stock_inspection.html')

    @app.route('/SME_fa_docs')
    @login_required
    @require_sme_regime
    def SME_fa_docs():
        return render_template('KeToanSME/fa_docs.html')

    @app.route('/SME_material_remaining')
    @login_required
    @require_sme_regime
    def SME_material_remaining():
        return render_template('KeToanSME/material_remaining.html')

    @app.route('/SME_labor_contract/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_labor_contract_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.labor_contract import get_labor_contract
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(conn, 'sme_labor_contracts', doc_id, label='HĐ giao khoán')
            except ValueError:
                return render_template('KeToanSME/labor_contracts.html')
            doc = get_labor_contract(conn, doc_id)
            if not doc:
                return render_template('KeToanSME/labor_contracts.html')
            return render_template(
                'KeToanSME/labor_contract_print.html',
                doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_labor_settlement/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_labor_settlement_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.labor_contract import get_settlement, get_labor_contract
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            st = get_settlement(conn, doc_id)
            if not st:
                return render_template('KeToanSME/labor_contracts.html')
            try:
                assert_row_in_branch(
                    conn, 'sme_labor_contracts', int(st['contract_id']),
                    label='HĐ giao khoán',
                )
            except ValueError:
                return render_template('KeToanSME/labor_contracts.html')
            contract = get_labor_contract(conn, int(st['contract_id']))
            return render_template(
                'KeToanSME/labor_settlement_print.html',
                st=st, contract=contract or {}, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_labor_sheet/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_labor_sheet_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.labor_sheets import get_labor_sheet
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(conn, 'sme_labor_sheets', doc_id, label='Bảng LĐTL')
            except ValueError:
                return render_template('KeToanSME/labor_sheets.html')
            doc = get_labor_sheet(conn, doc_id)
            if not doc:
                return render_template('KeToanSME/labor_sheets.html')
            return render_template(
                'KeToanSME/labor_sheet_print.html',
                doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_stock_inspection/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_stock_inspection_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.stock_inspection import get_stock_inspection
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(conn, 'sme_stock_inspections', doc_id, label='Kiểm nghiệm')
            except ValueError:
                return render_template('KeToanSME/stock_inspection.html')
            doc = get_stock_inspection(conn, doc_id)
            if not doc:
                return render_template('KeToanSME/stock_inspection.html')
            return render_template(
                'KeToanSME/stock_inspection_print.html',
                doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_fa_doc/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_fa_doc_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.fa_lifecycle import get_fa_doc
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(conn, 'sme_fa_docs', doc_id, label='Biên bản TSCĐ')
            except ValueError:
                return render_template('KeToanSME/fa_docs.html')
            doc = get_fa_doc(conn, doc_id)
            if not doc:
                return render_template('KeToanSME/fa_docs.html')
            return render_template(
                'KeToanSME/fa_doc_print.html',
                doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    @app.route('/SME_material_remaining/in/<int:doc_id>')
    @login_required
    @require_sme_regime
    def SME_material_remaining_in(doc_id):
        from Services.sme.branch_filter import assert_row_in_branch
        from Services.sme.material_remaining import get_material_remaining
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            try:
                assert_row_in_branch(conn, 'sme_material_remaining', doc_id, label='VT còn lại')
            except ValueError:
                return render_template('KeToanSME/material_remaining.html')
            doc = get_material_remaining(conn, doc_id)
            if not doc:
                return render_template('KeToanSME/material_remaining.html')
            return render_template(
                'KeToanSME/material_remaining_print.html',
                doc=doc, info=_biz_info(conn),
            )
        finally:
            conn.close()

    # ── Labor contracts API ────────────────────────────────
    @app.route('/api/sme/labor-contracts', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_labor_contracts():
        from Services.sme.labor_contract import create_labor_contract, list_labor_contracts
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                from Services.sme.branches import request_branch_filter
                return jsonify({
                    'success': True,
                    'data': list_labor_contracts(
                        conn,
                        status=request.args.get('status'),
                        branch_code=request_branch_filter(),
                    ),
                })
            data = request.get_json(silent=True) or {}
            doc = create_labor_contract(
                conn,
                contract_date=data.get('date') or data.get('contract_date'),
                contractor_name=data.get('contractor_name') or '',
                contract_amount=data.get('contract_amount') or data.get('amount'),
                work_content=data.get('work_content') or '',
                start_date=data.get('start_date') or '',
                end_date=data.get('end_date') or '',
                employer_rep_name=data.get('employer_rep_name') or '',
                employer_rep_title=data.get('employer_rep_title') or '',
                contractor_title=data.get('contractor_title') or '',
                contractor_address=data.get('contractor_address') or '',
                contractor_id_no=data.get('contractor_id_no') or '',
                method=data.get('method') or '',
                conditions=data.get('conditions') or '',
                other_terms=data.get('other_terms') or '',
                expense_account=data.get('expense_account') or '622',
                liability_account=data.get('liability_account') or '331',
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
            logger.exception('api_sme_labor_contracts')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/labor-contracts/<int:cid>/settle', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_labor_contract_settle(cid):
        from Services.sme.labor_contract import settle_labor_contract
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            doc = settle_labor_contract(
                conn,
                contract_id=cid,
                settlement_date=data.get('date') or data.get('settlement_date'),
                accepted_amount=data.get('accepted_amount'),
                paid_amount=data.get('paid_amount') or 0,
                penalty_amount=data.get('penalty_amount') or 0,
                quality_note=data.get('quality_note') or '',
                conclusion=data.get('conclusion') or '',
                pay_now=bool(data.get('pay_now', True)),
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
            logger.exception('api_sme_labor_contract_settle')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── Labor sheets 02/03/04 ──────────────────────────────
    @app.route('/api/sme/labor-sheets', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_labor_sheets():
        from Services.sme.labor_sheets import create_labor_sheet, list_labor_sheets
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                from Services.sme.branches import request_branch_filter
                return jsonify({
                    'success': True,
                    'data': list_labor_sheets(
                        conn,
                        sheet_type=request.args.get('type'),
                        branch_code=request_branch_filter(),
                    ),
                })
            data = request.get_json(silent=True) or {}
            doc = create_labor_sheet(
                conn,
                sheet_type=data.get('sheet_type') or data.get('type'),
                sheet_date=data.get('date') or data.get('sheet_date'),
                lines=data.get('lines') or [],
                department=data.get('department') or '',
                expense_account=data.get('expense_account'),
                liability_account=data.get('liability_account'),
                pay_now=bool(data.get('pay_now', True)),
                payment_method=data.get('payment_method') or 'cash',
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
            logger.exception('api_sme_labor_sheets')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── 03-VT ──────────────────────────────────────────────
    @app.route('/api/sme/stock-inspection', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_stock_inspection():
        from Services.sme.stock_inspection import create_stock_inspection, list_stock_inspections
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                from Services.sme.branches import request_branch_filter
                return jsonify({
                    'success': True,
                    'data': list_stock_inspections(
                        conn, branch_code=request_branch_filter(),
                    ),
                })
            data = request.get_json(silent=True) or {}
            doc = create_stock_inspection(
                conn,
                inspect_date=data.get('date') or data.get('inspect_date'),
                lines=data.get('lines') or [],
                import_id=data.get('import_id'),
                import_no=data.get('import_no') or '',
                supplier_name=data.get('supplier_name') or '',
                method=data.get('method') or 'Toàn diện',
                committee=data.get('committee') or '',
                opinion=data.get('opinion') or '',
                status=data.get('status') or 'accepted',
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
            logger.exception('api_sme_stock_inspection')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── FA docs 01/03/04/05 ────────────────────────────────
    @app.route('/api/sme/fa-docs', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_fa_docs():
        from Services.sme.fa_lifecycle import (
            create_fa_handover,
            create_fa_upgrade,
            create_fa_revaluation,
            create_fa_inventory,
            list_fa_docs,
        )
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                from Services.sme.branches import request_branch_filter
                branch = request_branch_filter()
                return jsonify({
                    'success': True,
                    'data': list_fa_docs(
                        conn,
                        doc_type=request.args.get('type'),
                        branch_code=branch,
                    ),
                    'branch_code': branch,
                })
            data = request.get_json(silent=True) or {}
            dtype = (data.get('doc_type') or data.get('type') or '').strip().lower()
            if dtype == 'handover':
                doc = create_fa_handover(
                    conn, asset_id=int(data.get('asset_id') or 0),
                    doc_date=data.get('date'), from_dept=data.get('from_dept') or '',
                    to_dept=data.get('to_dept') or '', partner_name=data.get('partner_name') or '',
                    content=data.get('content') or '', created_by=_user(), commit=True,
                )
            elif dtype == 'upgrade':
                doc = create_fa_upgrade(
                    conn, asset_id=int(data.get('asset_id') or 0),
                    doc_date=data.get('date'), amount=data.get('amount'),
                    content=data.get('content') or '',
                    cash_account=data.get('cash_account') or '1121',
                    created_by=_user(), commit=True,
                )
            elif dtype == 'revaluation':
                doc = create_fa_revaluation(
                    conn, asset_id=int(data.get('asset_id') or 0),
                    doc_date=data.get('date'), new_cost=data.get('new_cost'),
                    content=data.get('content') or '', created_by=_user(), commit=True,
                )
            elif dtype == 'inventory':
                doc = create_fa_inventory(
                    conn, doc_date=data.get('date'), lines=data.get('lines'),
                    content=data.get('content') or '', created_by=_user(), commit=True,
                )
            else:
                return jsonify({'success': False, 'error': 'doc_type không hợp lệ'}), 400
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_fa_docs')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ── 04-VT vật tư còn lại cuối kỳ ───────────────────────
    @app.route('/api/sme/material-remaining', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_material_remaining():
        from Services.sme.material_remaining import (
            create_material_remaining,
            list_material_remaining,
        )
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            _bootstrap()
            if request.method == 'GET':
                branch = (
                    request.args.get('branch')
                    or session.get('sme_branch_filter')
                    or 'ALL'
                )
                return jsonify({
                    'success': True,
                    'data': list_material_remaining(conn, branch_code=branch),
                    'branch_code': branch,
                })
            data = request.get_json(silent=True) or {}
            doc = create_material_remaining(
                conn,
                as_of_date=data.get('date') or data.get('as_of_date'),
                lines=data.get('lines') or [],
                department=data.get('department') or '',
                notes=data.get('notes') or '',
                warehouse_code=data.get('warehouse_code') or data.get('warehouse') or '',
                created_by=_user(),
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_material_remaining')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()
