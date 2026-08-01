"""Phase P9 — multi-branch: danh mục CN, session, gắn kho."""
from __future__ import annotations

import logging
import sqlite3

from flask import jsonify, render_template, request, session

from db_utils import get_db_connection

logger = logging.getLogger(__name__)


def register_sme_phase9_routes(app, *, login_required, require_sme_regime):

    @app.route('/SME_branches')
    @login_required
    @require_sme_regime
    def SME_branches():
        return render_template('KeToanSME/branches.html')

    @app.route('/api/sme/branches', methods=['GET', 'POST'])
    @login_required
    @require_sme_regime
    def api_sme_branches():
        from Services.sme.branches import create_branch, list_branches
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            if request.method == 'GET':
                active = request.args.get('active')
                active_only = active != '0'
                return jsonify({
                    'success': True,
                    'data': list_branches(conn, active_only=active_only),
                    'current': session.get('sme_branch_code'),
                })
            data = request.get_json(silent=True) or {}
            doc = create_branch(
                conn,
                code=data.get('code') or '',
                name=data.get('name') or '',
                address=data.get('address') or '',
                phone=data.get('phone') or '',
                is_default=bool(data.get('is_default')),
                notes=data.get('notes') or '',
                commit=True,
            )
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_branches')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/branches/<code>', methods=['PUT', 'PATCH'])
    @login_required
    @require_sme_regime
    def api_sme_branch_update(code):
        from Services.sme.branches import update_branch
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            kwargs = {}
            for key in ('name', 'address', 'phone', 'notes'):
                if key in data:
                    kwargs[key] = data[key]
            if 'is_default' in data:
                kwargs['is_default'] = bool(data['is_default'])
            if 'is_active' in data:
                kwargs['is_active'] = bool(data['is_active'])
            doc = update_branch(conn, code, commit=True, **kwargs)
            return jsonify({'success': True, 'data': doc})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_branch_update')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/branches/select', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_branch_select():
        from Services.sme.branches import get_branch, get_default_branch_code
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            code = (data.get('branch_code') or data.get('code') or '').strip().upper()
            if code in ('', 'ALL'):
                session['sme_branch_code'] = get_default_branch_code(conn)
                session['sme_branch_filter'] = 'ALL'
            else:
                br = get_branch(conn, code)
                if not br or not br.get('is_active'):
                    return jsonify({'success': False, 'error': 'Chi nhánh không hợp lệ'}), 400
                session['sme_branch_code'] = code
                session['sme_branch_filter'] = code
            return jsonify({
                'success': True,
                'branch_code': session.get('sme_branch_code'),
                'filter': session.get('sme_branch_filter', session.get('sme_branch_code')),
            })
        except Exception as e:
            logger.exception('api_sme_branch_select')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/branches/context')
    @login_required
    @require_sme_regime
    def api_sme_branch_context():
        from Services.sme.branches import branch_context
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            ctx = branch_context(conn)
            ctx['filter'] = session.get('sme_branch_filter') or ctx['current_branch_code']
            return jsonify({'success': True, 'data': ctx})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/warehouses', methods=['GET'])
    @login_required
    @require_sme_regime
    def api_sme_warehouses():
        from Services.import_line_helpers import list_active_warehouses, next_warehouse_code
        conn = get_db_connection()
        try:
            branch = (
                request.args.get('branch')
                or session.get('sme_branch_filter')
                or 'ALL'
            )
            # Trang quản trị CN: lấy tất cả kho (không lọc)
            if request.args.get('all') == '1':
                branch = 'ALL'
            rows = list_active_warehouses(conn, branch_code=branch)
            return jsonify({
                'success': True,
                'data': rows,
                'branch_code': branch,
                'next_code': next_warehouse_code(conn),
            })
        except Exception as e:
            logger.exception('api_sme_warehouses')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/warehouses', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_warehouses_create():
        from Services.import_line_helpers import create_warehouse, next_warehouse_code
        conn = get_db_connection()
        try:
            data = request.get_json(silent=True) or {}
            code = (data.get('code') or '').strip() or next_warehouse_code(conn)
            row = create_warehouse(
                conn,
                code=code,
                name=data.get('name') or '',
                address=data.get('address') or '',
                branch_code=data.get('branch_code') or 'HQ',
                is_default=bool(data.get('is_default')),
                commit=True,
            )
            return jsonify({'success': True, 'data': row})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_warehouses_create')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/warehouses/<code>/branch', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_sme_warehouse_set_branch(code):
        from Services.sme.branches import set_warehouse_branch
        conn = get_db_connection()
        try:
            data = request.get_json(silent=True) or {}
            set_warehouse_branch(
                conn, code, data.get('branch_code') or '',
                commit=True,
            )
            return jsonify({'success': True})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_warehouse_set_branch')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()
