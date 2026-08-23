# -*- coding: utf-8 -*-
"""Cổng đơn vị dịch vụ kế toán (DVKT) — quản lý DN thuê, chuyển sổ."""
from flask import flash, jsonify, redirect, render_template, request, session, url_for

from auth import login_required


def register_firm_portal_routes(app):

    def _require_firm_session():
        from Services.firm_tenant import is_firm_session
        if not is_firm_session():
            return jsonify({'success': False, 'error': 'Không phải phiên DVKT'}), 403
        return None

    def _firm_ctx():
        return session.get('firm_tenant_id'), session.get('firm_user_id')

    @app.route('/firm')
    @login_required
    def firm_portal():
        from Services.firm_tenant import (
            FIRM_ROLE_LABELS,
            is_firm_session,
            is_firm_viewing_client,
            is_firm_viewing_own_books,
            list_clients_for_firm_user,
            list_firm_users_portal,
            user_can_manage_firm_clients,
        )
        if not is_firm_session():
            flash('Vui lòng đăng nhập tài khoản DVKT.', 'warning')
            return redirect(url_for('login'))
        if is_firm_viewing_client() or is_firm_viewing_own_books():
            from Services.tenant_profile import is_sme_regime
            from flask import g
            regime = (getattr(g, 'tenant_profile', None) or {}).get('accounting_regime') or 'SME_TT99'
            if is_sme_regime(regime):
                return redirect(url_for('SME_dashboard'))
            return redirect(url_for('HKD_dashboard'))

        firm_tenant_id, firm_user_id = _firm_ctx()
        clients = list_clients_for_firm_user(firm_tenant_id, firm_user_id)
        firm_role = session.get('firm_role') or ''
        can_manage = user_can_manage_firm_clients(firm_tenant_id, firm_user_id)
        firm_users = list_firm_users_portal(firm_tenant_id) if can_manage else []
        return render_template(
            'firm_portal.html',
            clients=clients,
            firm_users=firm_users,
            firm_user_id=firm_user_id,
            firm_name=session.get('firm_name') or firm_tenant_id,
            firm_role=firm_role,
            firm_role_label=FIRM_ROLE_LABELS.get(firm_role, firm_role),
            can_manage_clients=can_manage,
        )

    @app.route('/api/firm/clients', methods=['GET'])
    @login_required
    def api_firm_clients():
        err = _require_firm_session()
        if err:
            return err
        from Services.firm_tenant import list_clients_for_firm_user
        firm_tenant_id, firm_user_id = _firm_ctx()
        clients = list_clients_for_firm_user(firm_tenant_id, firm_user_id)
        return jsonify({'success': True, 'clients': clients})

    @app.route('/api/firm/clients', methods=['POST'])
    @login_required
    def api_firm_add_client():
        err = _require_firm_session()
        if err:
            return err
        firm_tenant_id, firm_user_id = _firm_ctx()
        from Services.firm_tenant import add_firm_client, user_can_manage_firm_clients
        if not user_can_manage_firm_clients(firm_tenant_id, firm_user_id):
            return jsonify({'success': False, 'error': 'Chỉ Chủ đơn vị / Kế Toán Trưởng được thêm DN thuê'}), 403
        data = request.get_json() or {}
        result = add_firm_client(
            firm_tenant_id,
            client_name=(data.get('client_name') or '').strip(),
            tax_code=(data.get('tax_code') or '').strip(),
            address=(data.get('address') or '').strip(),
            phone=(data.get('phone') or '').strip(),
            email=(data.get('email') or '').strip(),
            representative_name=(data.get('representative_name') or '').strip(),
            accounting_regime=(data.get('accounting_regime') or 'SME_TT99').strip(),
            client_id=(data.get('client_id') or '').strip() or None,
            notes=(data.get('notes') or '').strip(),
        )
        status = 200 if result.get('success') else 400
        return jsonify(result), status

    @app.route('/api/firm/clients/<client_id>', methods=['GET'])
    @login_required
    def api_firm_get_client(client_id):
        err = _require_firm_session()
        if err:
            return err
        firm_tenant_id, firm_user_id = _firm_ctx()
        from Services.firm_tenant import get_client_manage_detail, user_can_manage_firm_clients, user_can_access_client
        if user_can_manage_firm_clients(firm_tenant_id, firm_user_id):
            detail = get_client_manage_detail(firm_tenant_id, client_id.strip())
        else:
            ctx = user_can_access_client(firm_tenant_id, firm_user_id, client_id.strip())
            detail = {'client': ctx['client'], 'staff_access': []} if ctx else None
        if not detail:
            return jsonify({'success': False, 'error': 'Không tìm thấy hoặc không có quyền'}), 404
        return jsonify({
            'success': True,
            'can_manage': user_can_manage_firm_clients(firm_tenant_id, firm_user_id),
            **detail,
        })

    @app.route('/api/firm/clients/<client_id>', methods=['PUT'])
    @login_required
    def api_firm_update_client(client_id):
        err = _require_firm_session()
        if err:
            return err
        firm_tenant_id, firm_user_id = _firm_ctx()
        data = request.get_json() or {}
        from Services.firm_tenant import update_firm_client
        result = update_firm_client(
            firm_tenant_id,
            client_id.strip(),
            firm_user_id,
            client_name=data.get('client_name'),
            tax_code=data.get('tax_code'),
            address=data.get('address'),
            phone=data.get('phone'),
            email=data.get('email'),
            representative_name=data.get('representative_name'),
            accounting_regime=data.get('accounting_regime'),
            notes=data.get('notes'),
        )
        status = 200 if result.get('success') else 400
        return jsonify(result), status

    @app.route('/api/firm/clients/<client_id>', methods=['DELETE'])
    @login_required
    def api_firm_delete_client(client_id):
        err = _require_firm_session()
        if err:
            return err
        firm_tenant_id, firm_user_id = _firm_ctx()
        from Services.firm_tenant import delete_firm_client
        result = delete_firm_client(firm_tenant_id, client_id.strip(), firm_user_id)
        status = 200 if result.get('success') else 400
        return jsonify(result), status

    @app.route('/api/firm/clients/<client_id>/access', methods=['PUT'])
    @login_required
    def api_firm_set_client_access(client_id):
        err = _require_firm_session()
        if err:
            return err
        firm_tenant_id, firm_user_id = _firm_ctx()
        data = request.get_json() or {}
        from Services.firm_tenant import set_client_staff_access
        result = set_client_staff_access(
            firm_tenant_id,
            client_id.strip(),
            firm_user_id,
            data.get('assignments') or data.get('staff_access') or [],
        )
        status = 200 if result.get('success') else 400
        return jsonify(result), status

    @app.route('/api/firm/users', methods=['GET'])
    @login_required
    def api_firm_list_users():
        err = _require_firm_session()
        if err:
            return err
        firm_tenant_id, firm_user_id = _firm_ctx()
        from Services.firm_tenant import list_firm_users_portal, user_can_manage_firm_clients
        if not user_can_manage_firm_clients(firm_tenant_id, firm_user_id):
            return jsonify({'success': False, 'error': 'Không có quyền'}), 403
        users = list_firm_users_portal(firm_tenant_id)
        return jsonify({'success': True, 'users': users})

    @app.route('/api/firm/enter-own', methods=['POST'])
    @login_required
    def api_firm_enter_own_books():
        err = _require_firm_session()
        if err:
            return err
        from Services.firm_tenant import enter_firm_own_books_context
        firm_tenant_id, firm_user_id = _firm_ctx()
        result = enter_firm_own_books_context(firm_tenant_id, firm_user_id)
        status = 200 if result.get('success') else 400
        return jsonify(result), status

    @app.route('/api/firm/enter/<client_id>', methods=['POST'])
    @login_required
    def api_firm_enter_client(client_id):
        err = _require_firm_session()
        if err:
            return err
        from Services.firm_tenant import enter_client_context
        firm_tenant_id, firm_user_id = _firm_ctx()
        result = enter_client_context(
            firm_tenant_id,
            firm_user_id,
            client_id.strip(),
        )
        status = 200 if result.get('success') else 400
        return jsonify(result), status

    @app.route('/api/firm/leave', methods=['POST'])
    @login_required
    def api_firm_leave_client():
        err = _require_firm_session()
        if err:
            return err
        from Services.firm_tenant import leave_client_context
        result = leave_client_context()
        status = 200 if result.get('success') else 400
        return jsonify(result), status

    @app.route('/api/firm/users', methods=['POST'])
    @login_required
    def api_firm_add_user():
        err = _require_firm_session()
        if err:
            return err
        firm_tenant_id, firm_user_id = _firm_ctx()
        data = request.get_json() or {}
        from Services.firm_tenant import add_firm_staff_user
        result = add_firm_staff_user(
            firm_tenant_id,
            firm_user_id,
            login_email=(data.get('login_email') or data.get('email') or '').strip(),
            password=data.get('password') or '',
            full_name=(data.get('full_name') or '').strip(),
            firm_role=(data.get('firm_role') or 'accountant').strip(),
        )
        status = 200 if result.get('success') else 400
        return jsonify(result), status
