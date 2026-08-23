"""Auth decorators, User model, Flask-Login — tách từ app.py."""
import os
import sqlite3
from functools import wraps

from flask import current_app, flash, jsonify, redirect, request, session, url_for
from flask_login import LoginManager, UserMixin

from db_utils import BASE_DIR

login_manager = LoginManager()


class User(UserMixin):
    def __init__(self, id, username, role, db_path, tenant_id, full_name, permissions=""):
        self.id = id
        self.username = username
        self.role = role
        self.db_path = db_path
        self.tenant_id = tenant_id
        self.full_name = full_name
        self.permissions = permissions

    def get_id(self):
        return str(self.id)


def normalize_permissions(raw):
    """Chuẩn hóa permissions → list str (session có thể lưu CSV hoặc list)."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(p).strip() for p in raw if str(p).strip()]
    text = str(raw).strip()
    if not text:
        return []
    return [p.strip() for p in text.split(',') if p.strip()]


def build_template_user():
    """Dict current_user cho template — luôn có permissions dạng list."""
    user = session.get('user') or {}
    role = session.get('role') or user.get('role') or 'guest'
    logged_in = bool(user.get('username')) or session.get('user_id') is not None or _session_user_id() is not None
    return {
        'id': user.get('id'),
        'username': user.get('username'),
        'role': str(role).strip(),
        'full_name': user.get('full_name') or user.get('username') or 'Khách',
        'permissions': normalize_permissions(user.get('permissions')),
        'is_authenticated': logged_in,
    }


def init_auth(app):
    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.login_message = "Vui lòng đăng nhập để tiếp tục."
    login_manager.login_message_category = "info"

    @app.context_processor
    def inject_template_user():
        return {'current_user': build_template_user()}

    @login_manager.user_loader
    def load_user(user_id):
        try:
            # Master đang xem tenant — không tra users trong DB tenant (id/token khác DB gốc)
            if session.get('master_viewing_tenant') and session.get('role') == 'master':
                u = session.get('user') or {}
                if str(u.get('id')) != str(user_id):
                    return None
                db_path_raw = session.get('db_path')
                if not db_path_raw:
                    db_path = os.path.join(BASE_DIR, "database.db")
                elif os.path.isabs(db_path_raw):
                    db_path = os.path.abspath(db_path_raw)
                else:
                    db_path = os.path.join(BASE_DIR, db_path_raw)
                return User(
                    id=u["id"],
                    username=u["username"],
                    role='master',
                    db_path=db_path,
                    tenant_id=session.get('last_tenant_id'),
                    full_name=u.get('full_name') or u['username'],
                    permissions=u.get('permissions', ''),
                )

            # Firm user — id lưu trên registry (firm_users), không có trong DB tenant/client
            if session.get('firm_user_id') and (session.get('user') or {}).get('is_firm_user'):
                u = session.get('user') or {}
                if str(u.get('id')) != str(user_id):
                    return None
                db_path_raw = session.get('db_path')
                if not db_path_raw:
                    return None
                if os.path.isabs(db_path_raw):
                    db_path = os.path.abspath(db_path_raw)
                else:
                    db_path = os.path.join(BASE_DIR, db_path_raw)
                return User(
                    id=u['id'],
                    username=u['username'],
                    role=session.get('role') or u.get('role') or 'accountant',
                    db_path=db_path,
                    tenant_id=session.get('last_tenant_id'),
                    full_name=u.get('full_name') or u['username'],
                    permissions=u.get('permissions', ''),
                )

            db_path_raw = session.get("db_path")
            if not db_path_raw:
                db_path = os.path.join(BASE_DIR, "database.db")
            elif os.path.isabs(db_path_raw):
                db_path = os.path.abspath(db_path_raw)
            else:
                db_path = os.path.join(BASE_DIR, db_path_raw)

            if not os.path.exists(db_path):
                current_app.logger.error(f"Database path không tồn tại: {db_path}")
                return None

            from db_utils import open_sqlite
            with open_sqlite(db_path) as conn:
                user_row = conn.execute(
                    "SELECT * FROM users WHERE id = ?", (int(user_id),)
                ).fetchone()

            if user_row:
                u = dict(user_row)
                return User(
                    id=u["id"],
                    username=u["username"],
                    role=str(u.get("role", "")).strip(),
                    db_path=db_path,
                    tenant_id=session.get("last_tenant_id"),
                    full_name=u.get("full_name") or u["username"],
                    permissions=u.get("permissions", ""),
                )
        except Exception as e:
            current_app.logger.error(f"Lỗi nghiêm trọng tại load_user: {e}")
        return None


def _session_user_id():
    if 'user_id' in session:
        return session['user_id']
    user = session.get('user') or {}
    uid = user.get('id')
    if uid is not None:
        session['user_id'] = int(uid)
        return session['user_id']
    return None


def _session_role():
    role = session.get('role')
    if role:
        return str(role).strip()
    role = (session.get('user') or {}).get('role', '')
    role = str(role).strip()
    if role:
        session['role'] = role
    return role


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if _session_user_id() is None:
            # Với API → trả JSON, không redirect
            if request.path.startswith('/api/'):
                return jsonify({"success": False, "error": "Unauthorized"}), 401
            # Với trang web → redirect login
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def master_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if _session_role() != 'master':
            if request.path.startswith('/api/'):
                return jsonify({"success": False, "error": "Forbidden"}), 403
            return redirect(url_for('sale'))
        return f(*args, **kwargs)
    return decorated_function

def admin_or_master_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        role = _session_role()
        from Services.sme_roles import ADMIN_OR_MASTER_ROLES
        if role not in ADMIN_OR_MASTER_ROLES:
            if request.path.startswith('/api/'):
                return jsonify({"success": False, "error": "Forbidden"}), 403
            return _settings_forbidden_redirect()
        return f(*args, **kwargs)
    return decorated_function


def _settings_forbidden_redirect():
    try:
        if session.get('firm_tenant_id'):
            flash(
                'Cài đặt HĐĐT và thông tin đơn vị DVKT do Master thiết lập từ «Thiết lập tổng quản trị».',
                'info',
            )
            return redirect(url_for('firm_portal'))
    except Exception:
        pass
    try:
        return redirect(url_for('sale'))
    except Exception:
        return redirect('/')


def user_can_access_tenant_settings() -> bool:
    """Cài đặt tenant: admin/master, quản lý SME độc lập, hoặc Master đang cấu hình DVKT."""
    from Services.sme_roles import ADMIN_OR_MASTER_ROLES, SME_MANAGER_ROLES
    role = _session_role()
    if role in ADMIN_OR_MASTER_ROLES:
        return True
    if role not in SME_MANAGER_ROLES:
        return False
    # Phiên DVKT (owner/KTV/…) không tự cấu hình — chỉ Master switch từ /master/settings
    if session.get('firm_tenant_id') or session.get('firm_user_id'):
        return False
    return True


def is_master_configuring_firm_tenant() -> bool:
    """Master đang vào tenant DVKT từ Thiết lập tổng quản trị — chỉ được dùng /settings."""
    if _session_role() != 'master':
        return False
    tid = (session.get('master_viewing_tenant') or '').strip()
    if not tid:
        return False
    from Services.firm_tenant import is_firm_tenant
    return is_firm_tenant(tid)


MASTER_FIRM_SETTINGS_ENDPOINTS = frozenset({
    'settings_page',
    'logout',
    'static',
    'api_master_leave_tenant',
    'api_save_business',
    'api_upload_business_logo',
    'api_delete_business_logo',
    'api_get_payment_bank',
    'api_save_payment_bank',
    'api_test_payment_connection',
    'api_save_system',
    'list_backups',
    'backup_now',
    'download_backup',
    'save_esign_settings',
    'get_esign_settings',
    'test_invoice_connection',
    'list_users',
    'save_user',
    'delete_user_api',
    'api_invoice_schedule_status',
    'api_invoice_schedule_toggle',
    'api_sme_tt58_tax_method',
    'api_database_reset',
})


def tenant_settings_required(f):
    """Trang /settings và API HĐĐT — không cho phiên DVKT tự cấu hình."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not user_can_access_tenant_settings():
            if request.path.startswith('/api/'):
                return jsonify({"success": False, "error": "Forbidden"}), 403
            return _settings_forbidden_redirect()
        return f(*args, **kwargs)
    return decorated_function


# Owner SME (managerSME / managerSME58 / managerSME99) được vào /thiet-lap
from Services.sme_roles import STORE_SETUP_ALLOWED_ROLES  # noqa: E402


def admin_or_store_setup_required(f):
    """Admin/master hoặc Quản lý SME — trang Thiết lập (/thiet-lap)."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        role = _session_role()
        from Services.sme_roles import STORE_SETUP_ALLOWED_ROLES as allowed
        if role not in allowed:
            if request.path.startswith('/api/'):
                return jsonify({"success": False, "error": "Forbidden"}), 403
            return redirect(url_for('sale'))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('role') != 'admin':
            if request.path.startswith('/api/'):
                return jsonify({"success": False, "error": "Forbidden"}), 403
            return redirect(url_for('sale'))
        return f(*args, **kwargs)
    return decorated_function

from functools import wraps
from flask import session, request, jsonify, flash, redirect, url_for, current_app

from functools import wraps
from flask import session, request, jsonify, flash, redirect, url_for, current_app

def require_permission(target_perm):
    """
    Decorator kiểm tra quyền truy cập.
    - Master và Admin có toàn quyền (bypass tất cả permission)
    - Admin* có quyền theo permission và mặc định điều hướng về /rental
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            # 1. Kiểm tra đã đăng nhập chưa
            if 'user' not in session:
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({"success": False, "error": "Unauthorized"}), 401
                flash("Vui lòng đăng nhập", "warning")
                return redirect(url_for('login'))

            user = session['user']
            role = user.get('role', 'guest')
            # Chuyển chuỗi permissions thành list để kiểm tra cho chính xác
            perms = [p.strip() for p in user.get('permissions', '').split(',') if p.strip()]

            # ==================== QUYỀN SIÊU CAO (BYPASS) ====================
            # Master và Admin (không dấu *) có toàn quyền
            from Services.sme_roles import PERMISSION_BYPASS_ROLES
            if role in PERMISSION_BYPASS_ROLES:
                return f(*args, **kwargs)

            # ==================== KIỂM TRA QUYỀN THỰC TẾ ====================
            # admin* hoặc các role khác phải có target_perm trong danh sách perms
            if target_perm in perms:
                return f(*args, **kwargs)

            # ==================== TỪ CHỐI TRUY CẬP ====================
            msg = f"Không có quyền thực hiện: {target_perm}"
            current_app.logger.warning(f"Access Denied: {user.get('username')} (Role: {role}) tried to access {target_perm}")

            # Trả về JSON nếu là API
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({"success": False, "error": msg}), 403

            # Điều hướng trang chủ mặc định tùy theo Role
            flash(msg, "danger")
            
            # Fix: Nếu user là admin* thì mặc định về trang rental
            if role == 'admin*':
                return redirect(url_for('rental_service'))
            
            # Các trường hợp khác về trang sale
            return redirect(url_for('sale'))

        return decorated
    return decorator
