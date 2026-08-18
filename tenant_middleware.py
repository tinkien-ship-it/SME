from flask import g, request, current_app, redirect, url_for, session, flash, jsonify
import sqlite3
import os
import shutil
import time
from datetime import datetime
from flask_bcrypt import generate_password_hash
import pyotp
import hashlib
import uuid

from db_utils import (
    BASE_DIR,
    MAIN_DB_PATH,
    REGISTRY_PATH,
    get_db_connection,
    get_main_db_connection,
    open_sqlite,
    sqlite_write_retry,
)

_tenant_schema_migrated = set()
# Cache kiểm tra single-session — tránh mở SQLite users mỗi request HTML/API
_session_token_cache: dict[tuple, tuple[float, str | None]] = {}
_SESSION_TOKEN_CACHE_TTL_SEC = 20.0


def _maybe_migrate_tenant_db(db_path):
    """Migrate schema tenant DB một lần / process (products.product_type, import.doc_type, …)."""
    if not db_path:
        return
    normalized = os.path.abspath(db_path)
    if normalized in _tenant_schema_migrated:
        return
    if normalized == os.path.abspath(MAIN_DB_PATH):
        _tenant_schema_migrated.add(normalized)
        return
    try:
        with open_sqlite(normalized) as conn:
            from db.init import ensure_tenant_db_schema
            ensure_tenant_db_schema(conn)
        _tenant_schema_migrated.add(normalized)
    except Exception as e:
        try:
            current_app.logger.error('Tenant schema migrate failed (%s): %s', normalized, e)
        except Exception:
            print(f'[MIGRATE] tenant DB {normalized}: {e}')

def ensure_tenants_dir():
    tenants_dir = os.path.join(BASE_DIR, 'tenants')
    os.makedirs(tenants_dir, exist_ok=True)
    # Đảm bảo quyền (nếu cần)
    # os.chmod(tenants_dir, 0o755)  # tùy theo user chạy
    return tenants_dir

def _ensure_users_extra_columns(cursor):
    cursor.execute("PRAGMA table_info(users)")
    cols = {col[1] for col in cursor.fetchall()}
    if 'must_change_password' not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER DEFAULT 0")
    if 'is_support_account' not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN is_support_account INTEGER DEFAULT 0")
    if 'email' not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")


def init_tenant_database(tenant_id: str, business_name: str, phone: str, **kwargs):
    ensure_tenants_dir()
    tenant_db_path = os.path.join(BASE_DIR, 'tenants', f"{tenant_id}.db")
    email = kwargs.get('email', '').strip()
    contact_email = (kwargs.get('contact_email') or email).strip()
    tax_code = (kwargs.get('tax_code') or '').strip()
    business_line = (kwargs.get('business_line') or 'pos').strip()
    enabled_nn_sectors = kwargs.get('enabled_nn_sectors')
    hkd_sector = (kwargs.get('hkd_sector') or '').strip()
    representative_name = (kwargs.get('representative_name') or '').strip()
    revenue_tier = kwargs.get('revenue_tier')
    accounting_regime = kwargs.get('accounting_regime') or 'HKD'
    settings_json = kwargs.get('settings_json') or {}
    empty_business_data = bool(kwargs.get('empty_business_data', False))

    from Services.subscription_service import role_for_business_line, support_role_for_business_line
    from Services.tenant_profile import (
        build_tenant_settings,
        ensure_business_info_profile_columns,
        sync_business_info_profile,
        build_profile_from_registry,
        is_sme_regime,
    )
    from Services.hkd_sector import nn_to_storage_code, normalize_nn_code

    sme = is_sme_regime(accounting_regime)
    if sme:
        hkd_sector = ''
        revenue_tier = None
        if enabled_nn_sectors is None:
            enabled_nn_sectors = []
    else:
        hkd_sector = hkd_sector or 'NN1'
        revenue_tier = revenue_tier or 'DT1'

    if not settings_json.get('accounting_regime'):
        settings_json = build_tenant_settings(
            business_line=business_line,
            hkd_sector=hkd_sector or 'NN1',
            enabled_nn_sectors=enabled_nn_sectors,
            revenue_tier=revenue_tier or 'DT1',
            accounting_regime=accounting_regime,
            subscription_plan=settings_json.get('plan', kwargs.get('subscription_plan', '')),
            onboarding_completed=settings_json.get('onboarding_completed', False),
            extra=settings_json,
        )
    elif sme:
        # Đảm bảo settings SME không còn DT/NN HKD
        settings_json = build_tenant_settings(
            business_line=business_line or settings_json.get('business_line') or 'pos',
            accounting_regime=accounting_regime,
            subscription_plan=settings_json.get('plan', kwargs.get('subscription_plan', '')),
            onboarding_completed=settings_json.get('onboarding_completed', False),
            extra=settings_json,
        )

    primary_raw = settings_json.get('primary_nn_sector') or hkd_sector
    if sme and not primary_raw:
        primary_nn = None
        storage_sector = None
    else:
        primary_nn = normalize_nn_code(primary_raw or 'NN1')
        storage_sector = nn_to_storage_code(primary_nn)

    customer_password = kwargs.get('customer_password') or 'admin'
    support_username = kwargs.get('support_username') or f"{phone}admin"
    support_password = kwargs.get('support_password') or customer_password
    owner_role = role_for_business_line(business_line, accounting_regime)
    support_role = support_role_for_business_line(business_line, accounting_regime)
    # Chủ tenant SME (managerSME*): thiết lập + xem nhật ký; không có quyền admin/settings
    from Services.sme_roles import is_sme_manager_role
    owner_permissions = 'view_audit_log' if is_sme_manager_role(owner_role) else ''

    # 1. Copy Database mẫu
    if os.path.exists(os.path.join(BASE_DIR, 'database.db')):
        shutil.copy2(os.path.join(BASE_DIR, 'database.db'), tenant_db_path)
    else:
        if os.path.exists('database.db'):
            shutil.copy2('database.db', tenant_db_path)
        else:
            raise Exception("Không tìm thấy file database.db mẫu")

    # 2. Xử lý Database con của Tenant
    conn_tenant = open_sqlite(tenant_db_path)
    try:
        cursor_tenant = conn_tenant.cursor()

        tables_to_drop = ['user_tenant_mapping', 'user_trusted_devices', 'tenants']
        for table in tables_to_drop:
            cursor_tenant.execute(f"DROP TABLE IF EXISTS {table}")

        if empty_business_data:
            from Services.tenant_db_bootstrap import clear_trial_business_data
            clear_trial_business_data(conn_tenant)

        cursor_tenant.execute("DELETE FROM users")
        try:
            cursor_tenant.execute("DELETE FROM sqlite_sequence WHERE name='users'")
        except Exception:
            pass

        _ensure_users_extra_columns(cursor_tenant)

        owner_hash = generate_password_hash(customer_password).decode('utf-8')
        support_hash = generate_password_hash(support_password).decode('utf-8')

        cursor_tenant.execute("""
            INSERT INTO users (username, password, full_name, role, email, permissions, must_change_password, is_support_account)
            VALUES (?, ?, ?, ?, ?, ?, 1, 0)
        """, (phone, owner_hash, business_name, owner_role, contact_email, owner_permissions))

        cursor_tenant.execute("""
            INSERT INTO users (username, password, full_name, role, email, must_change_password, is_support_account)
            VALUES (?, ?, ?, ?, ?, 0, 1)
        """, (support_username, support_hash, 'KETO Hỗ trợ', support_role, ''))

        cursor_tenant.execute("DELETE FROM business_info")
        ensure_business_info_profile_columns(cursor_tenant)
        cursor_tenant.execute("""
            INSERT INTO business_info (
                business_name, representative_name, address, phone, email, tax_code,
                accounting_regime, revenue_tier_declared, revenue_tier_effective,
                default_hkd_sector, filing_period
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            business_name,
            representative_name or business_name,
            kwargs.get('address', ''),
            phone,
            contact_email,
            tax_code,
            settings_json.get('accounting_regime', 'HKD'),
            settings_json.get('revenue_tier', revenue_tier),
            settings_json.get('revenue_tier_effective', revenue_tier),
            storage_sector,
            settings_json.get('filing_period', 'quarterly'),
        ))

        profile = build_profile_from_registry({
            'tenant_id': tenant_id,
            'settings': settings_json,
            'business_type': business_line,
        })
        sync_business_info_profile(cursor_tenant, profile)

        from Services.inward_invoice_helpers import ensure_import_service_schema
        from db.init import ensure_tenant_db_schema
        ensure_tenant_db_schema(conn_tenant)

        conn_tenant.commit()

        from Services.audit_log import ensure_audit_table
        ensure_audit_table(conn_tenant)
        conn_tenant.commit()
    except Exception as e:
        conn_tenant.rollback()
        if os.path.exists(tenant_db_path):
            os.remove(tenant_db_path)
        raise Exception(f"Lỗi xử lý DB Tenant: {str(e)}")
    finally:
        conn_tenant.close()

    # 3. Cập nhật Registry (Main Database)
    import json
    rel_db_path = os.path.join('tenants', f"{tenant_id}.db")
    settings_payload = dict(settings_json)
    settings_payload.setdefault('business_line', business_line)
    settings_payload.setdefault('default_hkd_sector', storage_sector)
    if settings_json.get('enabled_nn_sectors'):
        settings_payload.setdefault('enabled_nn_sectors', settings_json['enabled_nn_sectors'])
    created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _write_registry():
        with get_main_db_connection() as conn_registry:
            c_reg = conn_registry.cursor()
            c_reg.execute("""
                INSERT OR REPLACE INTO tenants
                (tenant_id, db_path, business_name, phone, address, email, expiry_date, created_at, is_active, settings, business_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """, (
                tenant_id,
                rel_db_path,
                business_name,
                phone,
                kwargs.get('address', ''),
                contact_email,
                kwargs.get('expiry_date'),
                created_at,
                json.dumps(settings_payload, ensure_ascii=False),
                business_line,
            ))
            c_reg.execute("""
                INSERT OR REPLACE INTO user_tenant_mapping
                (username, email, tenant_id, twofa_type, is_active, business_type)
                VALUES (?, ?, ?, 1, 1, ?)
            """, (phone, email or contact_email, tenant_id, business_line))
            c_reg.execute("""
                INSERT OR REPLACE INTO user_tenant_mapping
                (username, email, tenant_id, twofa_type, is_active, business_type)
                VALUES (?, ?, ?, 1, 1, ?)
            """, (support_username, '', tenant_id, business_line))
            conn_registry.commit()

    try:
        sqlite_write_retry(_write_registry, label='init_tenant_registry')
    except Exception as e:
        if os.path.exists(tenant_db_path):
            os.remove(tenant_db_path)
        raise Exception(f"Lỗi Registry: {str(e)}")

    return tenant_db_path

def get_tenant_db_path(tenant_id: str):
    if not tenant_id: return None
    with get_main_db_connection() as conn:
        row = conn.execute(
            "SELECT db_path, business_name, phone FROM tenants WHERE tenant_id = ? AND is_active = 1",
            (tenant_id,),
        ).fetchone()
        return dict(row) if row else None

def load_tenant():
    path = request.path.strip('/')
    parts = path.split('/')
    first_part = parts[0] if parts else ""
    excluded = [
        'static', 'api', 'logout', 'master', 'favicon.ico',
        'F&B_service', 'sale', 'login', 'hkd_accounting', 'onboarding',
    ]

    # Trang / API Master luôn dùng database.db (registry). Không lấy
    # last_tenant_id từ session — nếu không, get_db_connection() trỏ nhầm
    # sang DB tenant (không có bảng tenants) → list/toggle 2FA hỏng.
    if first_part == 'master' or path.startswith('api/master'):
        g.tenant_id = None
        g.db_path = MAIN_DB_PATH
        g.tenant_info = None
        g.is_main_tenant = True
        return

    tenant_data = None

    # Bước 1: Kiểm tra trên URL
    if first_part and first_part not in excluded:
        tenant_data = get_tenant_db_path(first_part)
        if tenant_data:
            g.tenant_id = first_part
            # SỬA TẠI ĐÂY: Ép về đường dẫn tuyệt đối
            raw_path = tenant_data['db_path']
            g.db_path = os.path.join(BASE_DIR, raw_path) if not os.path.isabs(raw_path) else raw_path
            
            g.tenant_info = tenant_data
            g.is_main_tenant = False
            session['last_tenant_id'] = first_part
            return

    # Bước 2: Kiểm tra trong Session
    stored_id = session.get('last_tenant_id')
    if stored_id:
        tenant_data = get_tenant_db_path(stored_id)
        if tenant_data:
            g.tenant_id = stored_id
            # SỬA TẠI ĐÂY: Ép về đường dẫn tuyệt đối
            raw_path = tenant_data['db_path']
            g.db_path = os.path.join(BASE_DIR, raw_path) if not os.path.isabs(raw_path) else raw_path
            
            g.tenant_info = tenant_data
            g.is_main_tenant = False
            return

    # Bước 3: Mặc định (Main System)
    g.tenant_id = None
    g.db_path = os.path.join(BASE_DIR, 'database.db')
    g.tenant_info = None
    g.is_main_tenant = True

def add_user_to_mapping(username: str, email: str, tenant_id: str):
    """
    Thêm hoặc cập nhật mapping giữa user và tenant trong Master DB.
    - username: thường là số điện thoại
    - email: email của user (có thể rỗng)
    """
    if not username or not tenant_id:
        print(f"WARNING: add_user_to_mapping - username hoặc tenant_id bị thiếu")
        return False

    try:
        def _write():
            with get_main_db_connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO user_tenant_mapping
                    (username, email, tenant_id)
                    VALUES (?, ?, ?)
                """, (username.strip(), email.strip() if email else None, tenant_id))
                conn.commit()

        sqlite_write_retry(_write, label='add_user_to_mapping')
        print(f"DEBUG: Đã thêm/cập nhật mapping → username='{username}' | email='{email}' | tenant='{tenant_id}'")
        return True
    except Exception as e:
        print(f"ERROR: add_user_to_mapping thất bại: {e}")
        return False

def update_user_email_in_mapping(old_email: str, new_email: str, username: str, tenant_id: str):
    """
    Cập nhật email khi user thay đổi email.
    - Nếu username đã tồn tại trong tenant → chỉ UPDATE email (không thay đổi username)
    - Nếu chưa tồn tại → INSERT bình thường
    """
    if not username or not tenant_id:
        print("WARNING: update_user_email_in_mapping - username hoặc tenant_id bị thiếu")
        return False

    if old_email and old_email.strip().lower() == new_email.strip().lower():
        return True  # Email không thay đổi

    try:
        def _write():
            with get_main_db_connection() as conn:
                c = conn.cursor()
                c.execute("""
                    UPDATE user_tenant_mapping
                    SET email = ?
                    WHERE username = ? AND tenant_id = ?
                """, (new_email.strip(), username.strip(), tenant_id))
                if c.rowcount == 0:
                    c.execute("""
                        INSERT INTO user_tenant_mapping (username, email, tenant_id)
                        VALUES (?, ?, ?)
                    """, (username.strip(), new_email.strip(), tenant_id))
                conn.commit()

        sqlite_write_retry(_write, label='update_user_email_in_mapping')
        print(f"DEBUG: Cập nhật email mapping thành công → username='{username}' | email='{new_email}' | tenant={tenant_id}")
        return True
    except Exception as e:
        print(f"ERROR: update_user_email_in_mapping thất bại: {e}")
        return False

def get_tenant_by_username(username: str, active_only=True):
    if not username:
        return None
    with get_main_db_connection() as conn:
        active_clause = " AND t.is_active = 1" if active_only else ""
        query = f"""
            SELECT t.db_path, t.tenant_id, t.is_2fa_enabled,
                   m.email, t.is_active, t.expiry_date, t.business_name, t.settings
            FROM tenants t
            JOIN user_tenant_mapping m ON t.tenant_id = m.tenant_id
            WHERE m.username = ? AND m.is_active = 1
              {active_clause}
            LIMIT 1
        """
        row = conn.execute(query, (username,)).fetchone()
        return dict(row) if row else None

def init_tenant(app):
    @app.before_request
    def before_request():
        load_tenant()

    @app.context_processor
    def inject_tenant():
        from flask import session
        from auth import build_template_user
        from Services.tenant_profile import is_sme_regime
        from Services.hkd_menu import user_can_access_hub, user_can_see_sme_nav
        profile = getattr(g, 'tenant_profile', None) or {}
        user = build_template_user()
        cu = {
            'role': user.get('role') or session.get('role') or '',
            'permissions': user.get('permissions') or '',
        }
        from Services.sme_roles import is_sme_admin_role, is_sme_role
        sme = is_sme_regime(profile.get('accounting_regime'))
        return {
            'current_tenant': getattr(g, 'tenant_id', None),
            'is_main_tenant': getattr(g, 'is_main_tenant', True),
            'tenant_info': getattr(g, 'tenant_info', {}),
            'tenant_profile': profile,
            'master_viewing_tenant': session.get('master_viewing_tenant'),
            'current_user': user,
            'tenant_is_sme': sme,
            'user_has_hub': user_can_access_hub(cu, profile),
            'user_has_sme_nav': user_can_see_sme_nav(cu, profile),
            'is_sme_admin': is_sme_admin_role(cu.get('role')),
            'is_sme_user': is_sme_role(cu.get('role')),
        }

def init_tenant_middleware(app, get_db_connection_fn=None):
    """Middleware session — luôn dùng get_db_connection từ db_utils."""
    connect_db = get_db_connection_fn or get_db_connection
    @app.before_request
    def check_session_and_device():
        # ==================== 1. Loại trừ các trang công khai ====================
        # Đã cập nhật chính xác tên các hàm xử lý luồng 2FA để tránh bị chặn nhầm trên VPS
        public_endpoints = [
            'login',
            'static',
            'login_2fa',
            'send_otp_email',
            'send_otp_sms_route',
            'verify_otp_page',
            'verify_otp_code',
            'login_google',
            'login_google_callback',
            'login_google_credential',
            'login_google_2fa_credential',
            'login_google_2fa_start',
            'trial_google_start',
            'trial_google_callback',
            'authorize_google_2fa',
            'logout',
            'forgot_password',
            'reset_password',
            'renewal_page',
            'api_trial_google_check',
            'api_trial_register',
            'api_tenant_profile_options',
            'api_subscription_plans',
            'api_renewal_checkout',
            'api_renewal_status',
            'qr_payment',
            'onboarding_page',
            'api_onboarding_status',
            'api_onboarding_complete',
            'api_onboarding_skip',
            'webhook_sepay',
            'webhook_casso',
        ]
        
        # Phòng hờ request.endpoint bị None khi truy cập file tĩnh lỗi hoặc các route không tồn tại
        if not request.endpoint or request.endpoint in public_endpoints:
            return None  # Cho phép đi tiếp (Bỏ qua kiểm tra bảo mật)

        # ==================== 2. Kiểm tra trạng thái Đăng nhập ====================
        user_data = session.get('user')
        session_token = session.get('session_token')
        db_path = session.get('db_path')

        # NẾU CHƯA ĐĂNG NHẬP: Chặn đứng ngay lập tức, dọn rác và đá về trang login chính
        if not user_data or not session_token or not db_path:
            session.clear()  # Dọn sạch session tạm hoặc session lỗi nếu có
            # API phải trả JSON — không redirect HTML (tránh fetch().json() vỡ)
            if request.path.startswith('/api/') or request.accept_mimetypes.best == 'application/json':
                return jsonify({"success": False, "error": "Unauthorized — vui lòng đăng nhập lại"}), 401
            try:
                response = redirect(url_for('login'))
            except Exception:
                response = redirect('/login')
            response.delete_cookie('session') # Xóa triệt để cookie để trình duyệt reset trạng thái sạch
            return response

        # 🌟 ĐỒNG BỘ ĐƯỜNG DẪN: Bảo đảm g.db_path luôn là đường dẫn tuyệt đối ổn định trên VPS Linux
        if not os.path.isabs(db_path):
            BASE_DIR = os.path.abspath(os.path.dirname(__file__))
            g.db_path = os.path.join(BASE_DIR, db_path)
        else:
            g.db_path = db_path

        _maybe_migrate_tenant_db(g.db_path)

        tenant_id = session.get('last_tenant_id')
        onboarding_ok = {
            'onboarding_page', 'api_onboarding_status', 'api_onboarding_complete',
            'api_onboarding_skip', 'logout', 'static',
        }
        if tenant_id:
            try:
                from Services.tenant_profile import load_tenant_profile
                g.tenant_profile = load_tenant_profile(tenant_id)
                g.tenant_settings = g.tenant_profile.get('settings') or {}
            except Exception:
                g.tenant_profile = {}
                g.tenant_settings = {}

        if tenant_id and request.endpoint not in onboarding_ok:
            if not (session.get('master_viewing_tenant') and session.get('role') == 'master'):
                try:
                    from Services.subscription_service import parse_tenant_settings
                    settings = getattr(g, 'tenant_settings', None) or {}
                    if not settings:
                        from Services.subscription_service import get_tenant_record
                        rec = get_tenant_record(tenant_id, include_inactive=True)
                        settings = parse_tenant_settings(rec.get('settings') if rec else {})
                    if not settings.get('onboarding_completed'):
                        return redirect(url_for('onboarding_page'))
                except Exception:
                    pass

        # ==================== 3. Kiểm tra Single Session (Đá thiết bị cũ) ====================
        try:
            # Master xem tenant: token lưu ở DB gốc (database.db), không phải DB tenant
            validate_db_path = g.db_path
            if session.get('master_viewing_tenant') and session.get('role') == 'master':
                validate_db_path = session.get('master_home_db_path') or MAIN_DB_PATH
                if not os.path.isabs(validate_db_path):
                    validate_db_path = os.path.join(BASE_DIR, validate_db_path)

            user_id = user_data['id']
            cache_key = (os.path.abspath(validate_db_path), user_id, session_token)
            now_ts = time.time()
            cached = _session_token_cache.get(cache_key)
            if cached and (now_ts - cached[0]) < _SESSION_TOKEN_CACHE_TTL_SEC:
                db_token = cached[1]
            else:
                conn = open_sqlite(validate_db_path)
                try:
                    row = conn.execute(
                        "SELECT last_session_id FROM users WHERE id = ?",
                        (user_id,),
                    ).fetchone()
                finally:
                    conn.close()
                db_token = row[0] if row else None
                _session_token_cache[cache_key] = (now_ts, db_token)
                # Giữ cache nhỏ — tránh phình khi nhiều user
                if len(_session_token_cache) > 256:
                    cutoff = now_ts - _SESSION_TOKEN_CACHE_TTL_SEC
                    for k, (ts, _) in list(_session_token_cache.items()):
                        if ts < cutoff:
                            _session_token_cache.pop(k, None)

            # Nếu Token trong DB đã đổi (do một thiết bị khác đăng nhập sau và chiếm quyền sở hữu)
            if db_token is not None and db_token != session_token:
                session.clear()  # Xóa sạch dữ liệu phiên làm việc hiện tại của trình duyệt này
                if request.path.startswith('/api/'):
                    return jsonify({
                        "success": False,
                        "error": "Phiên đăng nhập đã bị thay thế trên thiết bị khác — vui lòng đăng nhập lại",
                    }), 401
                
                # Tạo phản hồi chuyển hướng an toàn cứng chống lặp vòng lặp (ERR_TOO_MANY_REDIRECTS)
                try:
                    response = redirect(url_for('login'))
                except Exception:
                    response = redirect('/login')
                
                # Cưỡng bức trình duyệt xóa cookie session cũ ngay lập tức
                response.delete_cookie('session')
                
                # Sử dụng flash để thông báo trực quan cho người dùng biết lý do bị đẩy ra ngoài
                flash("Tài khoản của bạn đã được đăng nhập ở một thiết bị hoặc trình duyệt khác!", "danger")
                return response

        except Exception as e:
            current_app.logger.error("Tenant middleware error: %s", e, exc_info=True)
            if request.path.startswith('/api/'):
                return jsonify({"success": False, "error": "Lỗi phiên làm việc — thử đăng nhập lại"}), 500
            # Nếu lỗi kết nối DB trên VPS (như nghẽn file), đẩy về trang đăng nhập bằng URL tĩnh để cứu vớt hệ thống
            try:
                response = redirect(url_for('login'))
            except Exception:
                response = redirect('/login')
            return response

    print("--- ĐÃ CẬP NHẬT VÀ KHỞI TẠO TENANT MIDDLEWARE AN TOÀN THÀNH CÔNG ---")