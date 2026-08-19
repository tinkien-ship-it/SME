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
        from Services.user_branch import user_allowed_branch_codes
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            code = (data.get('branch_code') or data.get('code') or '').strip().upper()

            allowed = user_allowed_branch_codes(conn, session.get('user_id', 0))

            if code in ('', 'ALL'):
                if allowed is not None:
                    session['sme_branch_code'] = allowed[0] if allowed else get_default_branch_code(conn)
                    session['sme_branch_filter'] = allowed[0] if len(allowed) == 1 else 'ALL'
                else:
                    session['sme_branch_code'] = get_default_branch_code(conn)
                    session['sme_branch_filter'] = 'ALL'
            else:
                if allowed is not None and code not in allowed:
                    return jsonify({'success': False, 'error': 'Bạn không có quyền truy cập chi nhánh này'}), 403
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

    @app.route('/api/sme/warehouses/<code>', methods=['PUT', 'PATCH'])
    @login_required
    @require_sme_regime
    def api_sme_warehouses_update(code):
        """Sửa thông tin kho: name/address/branch_code/is_default/is_active."""
        from Services.import_line_helpers import update_warehouse
        conn = get_db_connection()
        try:
            data = request.get_json(silent=True) or {}
            row = update_warehouse(
                conn,
                code=code,
                name=data.get('name'),
                address=data.get('address'),
                branch_code=data.get('branch_code'),
                is_default=data.get('is_default', None),
                is_active=data.get('is_active', None),
                commit=True,
            )
            return jsonify({'success': True, 'data': row})
        except ValueError as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            conn.rollback()
            logger.exception('api_sme_warehouses_update')
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    # ==================== User-Branch Assignment ====================

    @app.route('/api/sme/user-branches/<int:user_id>', methods=['GET'])
    @login_required
    def api_user_branches_get(user_id):
        from Services.user_branch import get_user_branches
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            return jsonify({'success': True, 'data': get_user_branches(conn, user_id)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/user-branches/<int:user_id>', methods=['PUT'])
    @login_required
    def api_user_branches_set(user_id):
        from Services.user_branch import set_user_branches
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            codes = data.get('branch_codes', [])
            default_code = data.get('default_branch_code')
            if not codes:
                return jsonify({'success': False, 'error': 'Cần ít nhất 1 chi nhánh'}), 400
            set_user_branches(conn, user_id, codes, default_code=default_code)
            return jsonify({'success': True})
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/user-branches/<int:user_id>/add', methods=['POST'])
    @login_required
    def api_user_branch_add(user_id):
        from Services.user_branch import assign_user_branch
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            code = (data.get('branch_code') or '').strip()
            if not code:
                return jsonify({'success': False, 'error': 'Thiếu branch_code'}), 400
            assign_user_branch(conn, user_id, code, is_default=bool(data.get('is_default')))
            return jsonify({'success': True})
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/sme/user-branches/<int:user_id>/remove', methods=['POST'])
    @login_required
    def api_user_branch_remove(user_id):
        from Services.user_branch import remove_user_branch
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            data = request.get_json(silent=True) or {}
            code = (data.get('branch_code') or '').strip()
            if not code:
                return jsonify({'success': False, 'error': 'Thiếu branch_code'}), 400
            remove_user_branch(conn, user_id, code)
            return jsonify({'success': True})
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()
