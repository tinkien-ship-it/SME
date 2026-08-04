"""API và trang Nhật ký truy vết."""
from flask import flash, g, jsonify, redirect, render_template, request, session, url_for

from auth import login_required, master_required
from Services.audit_log import (
    ACTION_LABELS,
    MODULE_LABELS,
    get_audit_log_by_id,
    query_audit_logs,
    query_login_history,
)

_AUDIT_VIEW_ROLES = frozenset({
    'master', 'admin', 'admin*', 'adminFB', 'adminSME',
    'manager', 'manager*', 'managerFB', 'managerSME',
    'accountant', 'accountantSME',
})


def _can_view_audit():
    role = str(
        session.get('role') or (session.get('user') or {}).get('role') or ''
    ).strip()
    if role in _AUDIT_VIEW_ROLES:
        return True
    perms = (session.get('user') or {}).get('permissions') or ''
    return 'view_audit_log' in str(perms)


def _audit_page_context():
    tenant_id = getattr(g, 'tenant_id', None) or session.get('last_tenant_id')
    is_master = session.get('role') == 'master'
    return {
        'tenant_id': tenant_id,
        'is_master': is_master,
        'action_labels': ACTION_LABELS,
        'module_labels': MODULE_LABELS,
    }


def _audit_layout_template(*, prefer_sme: bool = False) -> str:
    if prefer_sme:
        return 'KeToanSME/_layout.html'
    try:
        from Services.knowledge_service import is_hkd_regime
        from Services.tenant_profile import get_current_tenant_profile
        profile = get_current_tenant_profile() or {}
        regime = profile.get('accounting_regime')
        if is_hkd_regime(regime):
            return 'KeToanHKD/_layout.html'
        regime_u = str(regime or '').upper()
        if 'TT99' in regime_u or 'TT58' in regime_u or regime_u.startswith('SME'):
            return 'KeToanSME/_layout.html'
    except Exception:
        pass
    return 'base.html'


def _deny_audit_redirect():
    flash('Bạn không có quyền xem nhật ký truy cập.', 'warning')
    try:
        from Services.tenant_profile import get_current_tenant_profile
        regime = str(
            (get_current_tenant_profile() or {}).get('accounting_regime') or ''
        ).upper()
        if 'TT99' in regime or 'TT58' in regime or regime.startswith('SME'):
            return redirect(url_for('SME_utilities'))
    except Exception:
        pass
    try:
        return redirect(url_for('hkd_accounting'))
    except Exception:
        return redirect(url_for('sale'))


def register_audit_routes(app):

    @app.route('/audit-log')
    @login_required
    def audit_log_page():
        if not _can_view_audit():
            return _deny_audit_redirect()
        return render_template(
            'audit_log.html',
            layout_template=_audit_layout_template(),
            **_audit_page_context(),
        )

    @app.route('/api/audit-log', methods=['GET'])
    @login_required
    def api_audit_log_list():
        if not _can_view_audit():
            return jsonify({'success': False, 'error': 'Không có quyền xem nhật ký'}), 403

        log_type = (request.args.get('type') or 'actions').strip().lower()
        tenant_id = request.args.get('tenant_id', '').strip()
        if session.get('role') != 'master':
            tenant_id = getattr(g, 'tenant_id', None) or session.get('last_tenant_id') or ''

        start_date = request.args.get('start_date', '').strip()
        end_date = request.args.get('end_date', '').strip()
        action = request.args.get('action', '').strip()
        module = request.args.get('module', '').strip()
        username = request.args.get('username', '').strip()
        keyword = request.args.get('keyword', '').strip()
        limit = min(int(request.args.get('limit', 300) or 300), 1000)

        if log_type == 'login':
            data = query_login_history(
                tenant_id=tenant_id or None,
                start_date=start_date or None,
                end_date=end_date or None,
                limit=limit,
            )
            return jsonify({'success': True, 'type': 'login', 'data': data})

        is_master = session.get('role') == 'master'
        use_main = is_master and not tenant_id
        tenant_db = tenant_id if is_master and tenant_id else None
        data = query_audit_logs(
            tenant_id=tenant_id or None,
            action=action or None,
            module=module or None,
            username=username or None,
            keyword=keyword or None,
            start_date=start_date or None,
            end_date=end_date or None,
            limit=limit,
            use_main=use_main,
            tenant_id_for_db=tenant_db,
        )
        return jsonify({'success': True, 'type': 'actions', 'data': data})

    @app.route('/api/audit-log/<int:log_id>', methods=['GET'])
    @login_required
    def api_audit_log_detail(log_id):
        if not _can_view_audit():
            return jsonify({'success': False, 'error': 'Không có quyền'}), 403
        tenant_filter = request.args.get('tenant_id', '').strip()
        is_master = session.get('role') == 'master'
        use_main = is_master and not tenant_filter and not getattr(g, 'tenant_id', None)
        tenant_db = tenant_filter if is_master and tenant_filter else None
        row = get_audit_log_by_id(log_id, use_main=use_main, tenant_id_for_db=tenant_db)
        if not row:
            return jsonify({'success': False, 'error': 'Không tìm thấy bản ghi'}), 404
        if session.get('role') != 'master':
            my_tenant = getattr(g, 'tenant_id', None) or session.get('last_tenant_id')
            if row.get('tenant_id') and my_tenant and row['tenant_id'] != my_tenant:
                return jsonify({'success': False, 'error': 'Không có quyền'}), 403
        return jsonify({'success': True, 'data': row})

    @app.route('/api/master/audit-log', methods=['GET'])
    @login_required
    @master_required
    def api_master_audit_log():
        """Master: gộp login + thao tác trên main DB."""
        log_type = (request.args.get('type') or 'actions').strip().lower()
        tenant_id = request.args.get('tenant_id', '').strip() or None
        start_date = request.args.get('start_date', '').strip() or None
        end_date = request.args.get('end_date', '').strip() or None
        limit = min(int(request.args.get('limit', 300) or 300), 1000)

        if log_type == 'login':
            return jsonify({
                'success': True,
                'type': 'login',
                'data': query_login_history(tenant_id, start_date, end_date, limit),
            })

        data = query_audit_logs(
            tenant_id=tenant_id,
            action=request.args.get('action', '').strip() or None,
            module=request.args.get('module', '').strip() or None,
            username=request.args.get('username', '').strip() or None,
            keyword=request.args.get('keyword', '').strip() or None,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            use_main=not tenant_id,
            tenant_id_for_db=tenant_id,
        )
        return jsonify({'success': True, 'type': 'actions', 'data': data})
