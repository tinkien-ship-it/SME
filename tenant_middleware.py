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
    paths_same_db,
    sqlite_write_retry,
    sqlite_commit,
    begin_immediate,
)

from Services.firm_tenant import is_firm_tenant  # noqa: E402 — dùng trong middleware onboarding

_tenant_schema_migrated = set()
# Cache kiểm tra single-session — tránh mở SQLite users mỗi request HTML/API
_session_token_cache: dict[tuple, tuple[float, str | None]] = {}
_SESSION_TOKEN_CACHE_TTL_SEC = 20.0


def _maybe_migrate_tenant_db(db_path):
    """Migrate schema tenant DB một lần / process (products.product_type, import.doc_type, …)."""
    if not db_path:
        return
    if os.environ.get('SME_SKIP_RUNTIME_MIGRATE', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        return
    from db.dialect import is_postgres, pg_schema_from_db_path
    if is_postgres():
        schema = pg_schema_from_db_path(db_path)
        cache_key = f'pg:{schema}'
        if cache_key in _tenant_schema_migrated:
            return
        try:
            with open_sqlite(db_path) as conn:
                from db.init import ensure_tenant_db_schema
                ensure_tenant_db_schema(conn)
            _tenant_schema_migrated.add(cache_key)
        except Exception as e:
            try:
                current_app.logger.error('Tenant schema migrate failed (pg %s): %s', schema, e)
            except Exception:
                print(f'[MIGRATE] pg schema {schema}: {e}')
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
        msg = str(e).lower()
        # Đang locked — không đánh dấu done; request sau thử lại (tránh 504 chờ lâu)
        if 'locked' in msg:
            try:
                current_app.logger.warning('Tenant schema migrate deferred (locked): %s', normalized)
            except Exception:
                pass
            return
        try:
            current_app.logger.error('Tenant schema migrate failed (%s): %s', normalized, e)
        except Exception:
            print(f'[MIGRATE] tenant DB {normalized}: {e}')
        # Lỗi khác: đánh dấu để không spam mỗi request
        _tenant_schema_migrated.add(normalized)
def ensure_tenants_dir():
    tenants_dir = os.path.join(BASE_DIR, 'tenants')
    os.makedirs(tenants_dir, exist_ok=True)
    # Đảm bảo quyền (nếu cần)
    # os.chmod(tenants_dir, 0o755)  # tùy theo user chạy
    return tenants_dir

def _ensure_users_extra_columns(cursor):
    from db.schema_helpers import add_column_if_missing
    conn = getattr(cursor, 'connection', None) or cursor
    add_column_if_missing(conn, 'users', 'must_change_password', 'INTEGER DEFAULT 0', cursor=cursor)
    add_column_if_missing(conn, 'users', 'is_support_account', 'INTEGER DEFAULT 0', cursor=cursor)
    add_column_if_missing(conn, 'users', 'email', 'TEXT', cursor=cursor)


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
        from db.dialect import is_postgres
        if not is_postgres():
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

        sqlite_commit(conn_tenant, label='init_tenant_schema')

        from Services.audit_log import ensure_audit_table
        ensure_audit_table(conn_tenant)
        sqlite_commit(conn_tenant, label='init_tenant_audit')
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
        from db.dialect import is_postgres
        with get_main_db_connection() as conn_registry:
            c_reg = conn_registry.cursor()
            if is_postgres():
                c_reg.execute("""
                    INSERT INTO tenants
                    (tenant_id, db_path, business_name, phone, address, email, expiry_date, created_at, is_active, settings, business_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s)
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        db_path = EXCLUDED.db_path,
                        business_name = EXCLUDED.business_name,
                        phone = EXCLUDED.phone,
                        address = EXCLUDED.address,
                        email = EXCLUDED.email,
                        expiry_date = EXCLUDED.expiry_date,
                        settings = EXCLUDED.settings,
                        business_type = EXCLUDED.business_type,
                        is_active = 1
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
                for uname, em, btype in (
                    (phone, email or contact_email, business_line),
                    (support_username, '', business_line),
                ):
                    c_reg.execute("""
                        INSERT INTO user_tenant_mapping
                        (username, email, tenant_id, twofa_type, is_active, business_type)
                        VALUES (%s, %s, %s, 1, 1, %s)
                        ON CONFLICT (username) DO UPDATE SET
                            email = EXCLUDED.email,
                            tenant_id = EXCLUDED.tenant_id,
                            twofa_type = 1,
                            is_active = 1,
                            business_type = EXCLUDED.business_type
                    """, (uname, em, tenant_id, btype))
            else:
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
            sqlite_commit(conn_registry, label='init_tenant_registry')

    try:
        sqlite_write_retry(_write_registry, label='init_tenant_registry')
    except Exception as e:
        if os.path.exists(tenant_db_path):
            os.remove(tenant_db_path)
        raise Exception(f"Lỗi Registry: {str(e)}")

    try:
        from db.dialect import is_postgres, pg_schema_from_db_path
        if is_postgres():
            from db.pg_migrate import import_sqlite_file
            schema = pg_schema_from_db_path(rel_db_path, tenant_id=tenant_id)
            import_sqlite_file(tenant_db_path, schema)
    except Exception as e:
        try:
            current_app.logger.warning('PostgreSQL tenant import (%s): %s', tenant_id, e)
        except Exception:
            print(f'[PG] tenant import {tenant_id}: {e}')

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

    # Firm đang làm sổ DN thuê — db_path đã set trong session
    if session.get('firm_viewing_client') and session.get('db_path'):
        raw_path = session['db_path']
        g.db_path = os.path.join(BASE_DIR, raw_path) if not os.path.isabs(raw_path) else raw_path
        g.tenant_id = session.get('last_tenant_id')
        g.tenant_info = {
            'business_name': session.get('firm_client_name') or '',
            'phone': session.get('firm_client_tax_code') or '',
        }
        g.is_main_tenant = False
        return

    # Firm đang làm sổ Kế toán SME nội bộ (sổ DVKT)
    if session.get('firm_viewing_own_books') and session.get('firm_tenant_id'):
        fid = session['firm_tenant_id']
        tenant_data = get_tenant_db_path(fid)
        if tenant_data:
            g.tenant_id = fid
            raw_path = session.get('db_path') or tenant_data['db_path']
            g.db_path = os.path.join(BASE_DIR, raw_path) if not os.path.isabs(raw_path) else raw_path
            g.tenant_info = tenant_data
            g.is_main_tenant = False
            return

    # Firm đã login, chưa chọn sổ — meta DB firm
    if session.get('firm_tenant_id') and session.get('db_path') and not session.get('firm_viewing_client') and not session.get('firm_viewing_own_books'):
        fid = session['firm_tenant_id']
        tenant_data = get_tenant_db_path(fid)
        if tenant_data:
            g.tenant_id = fid
            raw_path = session.get('db_path') or tenant_data['db_path']
            g.db_path = os.path.join(BASE_DIR, raw_path) if not os.path.isabs(raw_path) else raw_path
            g.tenant_info = tenant_data
            g.is_main_tenant = False
            return

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
                sqlite_commit(conn, label='add_user_to_mapping')

        sqlite_write_retry(_write, label='add_user_to_mapping', retries=4)
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
                sqlite_commit(conn, label='update_user_email_mapping')

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
        from auth import build_template_user, user_can_access_tenant_settings, is_master_configuring_firm_tenant, is_master_configuring_firm_client
        from Services.tenant_profile import is_sme_regime
        from Services.hkd_menu import user_can_access_hub, user_can_see_sme_nav
        profile = getattr(g, 'tenant_profile', None) or {}
        user = build_template_user()
        cu = {
            'role': user.get('role') or session.get('role') or '',
            'permissions': user.get('permissions') or '',
        }
        from Services.sme_roles import is_sme_admin_role, is_sme_role
        from Services.firm_tenant import is_firm_session, is_firm_using_accounting, is_firm_viewing_client, is_firm_viewing_own_books
        sme = is_sme_regime(profile.get('accounting_regime'))
        return {
            'current_tenant': getattr(g, 'tenant_id', None),
            'is_main_tenant': getattr(g, 'is_main_tenant', True),
            'tenant_info': getattr(g, 'tenant_info', {}),
            'tenant_profile': profile,
            'master_viewing_tenant': session.get('master_viewing_tenant'),
            'master_viewing_firm': is_master_configuring_firm_tenant(),
            'master_viewing_firm_client': is_master_configuring_firm_client(),
            'master_firm_client_name': session.get('master_viewing_firm_client_name') or '',
            'firm_viewing_client': is_firm_viewing_client(),
            'firm_viewing_own_books': is_firm_viewing_own_books(),
            'firm_viewing_accounting': is_firm_using_accounting(),
            'firm_session': is_firm_session(),
            'firm_name': session.get('firm_name') or '',
            'firm_client_name': session.get('firm_client_name') or '',
            'firm_client_tax_code': session.get('firm_client_tax_code') or '',
            'current_user': user,
            'tenant_is_sme': sme,
            'user_has_hub': user_can_access_hub(cu, profile),
            'user_has_sme_nav': user_can_see_sme_nav(cu, profile),
            'is_sme_admin': is_sme_admin_role(cu.get('role')),
            'is_sme_user': is_sme_role(cu.get('role')),
            'user_can_tenant_settings': user_can_access_tenant_settings(),
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
            'verify_totp_page',
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
            'api_google_setup_hint',
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
            'api_crm_inbound_lead',
            'api_crm_inbound_channel',
            'crm_public_lead_form',
            'api_crm_public_lead',
            'keto_pos_intro',
        ]
        
        # Phòng hờ request.endpoint bị None khi truy cập file tĩnh lỗi hoặc các route không tồn tại
        if not request.endpoint or request.endpoint in public_endpoints:
            return None  # Cho phép đi tiếp (Bỏ qua kiểm tra bảo mật)

        # Đường dẫn auth công khai (phòng endpoint đổi tên / 404 có path)
        path = request.path or ''
        if path.startswith((
            '/login', '/send-otp', '/verify-otp', '/verify-totp',
            '/authorize-google',
            '/trial/google', '/api/trial', '/api/auth/google',
            '/forgot', '/reset', '/static/', '/favicon',
            '/gioi-thieu-keto-pos',
            '/lead',
            '/api/crm/inbound-lead',
            '/api/crm/public-lead',
            '/api/crm/inbound/',
        )):
            return None
        # Multi-tenant public lead / webhook: /{tenant}/lead , /{tenant}/api/crm/...
        if (
            '/api/crm/inbound-lead' in path
            or '/api/crm/public-lead' in path
            or '/api/crm/inbound/' in path
            or path.rstrip('/').endswith('/lead')
        ):
            # Cho phép /lead và /{tenant_id}/lead (không nhầm /crm/...)
            parts = [p for p in path.split('/') if p]
            if parts == ['lead'] or (len(parts) == 2 and parts[1] == 'lead'):
                return None
            if 'api' in parts and 'crm' in parts:
                if parts[-1] in ('inbound-lead', 'public-lead'):
                    return None
                # /api/crm/inbound/<channel>
                try:
                    i = parts.index('inbound')
                    if i >= 2 and parts[i - 1] == 'crm' and i + 1 < len(parts):
                        return None
                except ValueError:
                    pass

        # ==================== 2. Kiểm tra trạng thái Đăng nhập ====================
        user_data = session.get('user')
        session_token = session.get('session_token')
        db_path = session.get('db_path')

        # NẾU CHƯA ĐĂNG NHẬP: Chặn đứng ngay lập tức, dọn rác và đá về trang login chính
        if not user_data or not session_token or not db_path:
            # Đang chờ OTP/2FA/TOTP — giữ pending_auth; Master → /verify-totp
            if session.get('pending_auth'):
                auth = session.get('pending_auth') or {}
                role = str((auth.get('user') or {}).get('role') or '').strip()
                if role == 'master' or auth.get('auth_method') == 'totp':
                    try:
                        return redirect(url_for('verify_totp_page'))
                    except Exception:
                        return redirect('/verify-totp')
                try:
                    return redirect(url_for('login_2fa'))
                except Exception:
                    return redirect('/login-2fa')
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

        from Services.firm_tenant import is_firm_session, is_firm_using_accounting
        _firm_portal_endpoints = {
            'firm_portal', 'api_firm_clients', 'api_firm_add_client',
            'api_firm_get_client', 'api_firm_update_client', 'api_firm_delete_client',
            'api_firm_set_client_access', 'api_firm_list_users',
            'api_firm_enter_client', 'api_firm_enter_own_books', 'api_firm_leave_client', 'api_firm_add_user',
            'logout', 'static',
        }
        _firm_portal_path = path == '/firm' or path.startswith('/api/firm/')
        if is_firm_session() and not is_firm_using_accounting():
            ep = request.endpoint or ''
            if ep not in _firm_portal_endpoints and not _firm_portal_path and not path.startswith('/static/'):
                if request.path.startswith('/api/'):
                    return jsonify({
                        'success': False,
                        'error': 'Chọn sổ tại cổng DVKT (sổ nội bộ hoặc doanh nghiệp thuê) trước khi thao tác',
                    }), 403
                return redirect(url_for('firm_portal'))

        from auth import (
            is_master_configuring_firm_tenant,
            is_master_configuring_firm_client,
            MASTER_FIRM_SETTINGS_ENDPOINTS,
            MASTER_FIRM_CLIENT_EINVOICE_ENDPOINTS,
        )
        if is_master_configuring_firm_client():
            ep = request.endpoint or ''
            if ep not in MASTER_FIRM_CLIENT_EINVOICE_ENDPOINTS and not path.startswith('/static/'):
                if request.path.startswith('/api/'):
                    return jsonify({
                        'success': False,
                        'error': 'Chế độ cấu hình HĐĐT DN thuê — chỉ được dùng trang Cài đặt (HĐĐT & MST)',
                    }), 403
                return redirect(url_for('settings_page'))
        if is_master_configuring_firm_tenant():
            ep = request.endpoint or ''
            if ep not in MASTER_FIRM_SETTINGS_ENDPOINTS and not path.startswith('/static/'):
                if request.path.startswith('/api/'):
                    return jsonify({
                        'success': False,
                        'error': 'Chế độ cấu hình DVKT — chỉ được dùng trang Cài đặt tenant',
                    }), 403
                return redirect(url_for('settings_page'))

        tenant_id = session.get('last_tenant_id')
        onboarding_ok = {
            'onboarding_page', 'api_onboarding_status', 'api_onboarding_complete',
            'api_onboarding_skip', 'logout', 'static',
            'firm_portal', 'api_firm_clients', 'api_firm_add_client',
            'api_firm_get_client', 'api_firm_update_client', 'api_firm_delete_client',
            'api_firm_set_client_access', 'api_firm_list_users',
            'api_firm_enter_client', 'api_firm_enter_own_books', 'api_firm_leave_client', 'api_firm_add_user',
        }
        if tenant_id:
            try:
                if (session.get('firm_viewing_client') or session.get('firm_viewing_own_books')) and getattr(g, 'db_path', None):
                    from Services.tenant_profile import load_profile_from_tenant_db
                    g.tenant_profile = load_profile_from_tenant_db(g.db_path)
                    g.tenant_settings = g.tenant_profile.get('settings') or {}
                elif is_firm_tenant(tenant_id):
                    from Services.tenant_profile import load_tenant_profile
                    g.tenant_profile = load_tenant_profile(tenant_id)
                    g.tenant_settings = g.tenant_profile.get('settings') or {}
                else:
                    from Services.tenant_profile import load_tenant_profile
                    g.tenant_profile = load_tenant_profile(tenant_id)
                    g.tenant_settings = g.tenant_profile.get('settings') or {}
            except Exception:
                g.tenant_profile = {}
                g.tenant_settings = {}

        if tenant_id and request.endpoint not in onboarding_ok:
            from Services.hrm.ess_access import is_ess_portal_only_user
            skip_onboarding = (
                (session.get('master_viewing_tenant') and session.get('role') == 'master')
                or session.get('firm_viewing_client')
                or session.get('firm_viewing_own_books')
                or is_firm_tenant(tenant_id)
                or is_ess_portal_only_user(session.get('role') or (user_data or {}).get('role'))
            )
            if not skip_onboarding:
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

        # Role employee (Cổng ESS) — không được vào POS / sale / dashboard
        from Services.hrm.ess_access import ess_portal_path_allowed, is_ess_portal_only_user
        ess_role = str(session.get('role') or (user_data or {}).get('role') or '').strip()
        if is_ess_portal_only_user(ess_role):
            req_path = request.path or ''
            if req_path.startswith('/api/'):
                if not ess_portal_path_allowed(req_path, ess_role):
                    return jsonify({'success': False, 'error': 'Forbidden'}), 403
            elif not ess_portal_path_allowed(req_path, ess_role):
                try:
                    return redirect(url_for('hrm_ess_portal'))
                except Exception:
                    return redirect('/hrm/ess')

        # ==================== 3. Kiểm tra Single Session (Đá thiết bị cũ) ====================
        try:
            # Master / Firm xem client — token lưu ở registry (firm_users) hoặc DB gốc
            validate_db_path = g.db_path
            if session.get('master_viewing_tenant') and session.get('role') == 'master':
                validate_db_path = session.get('master_home_db_path') or MAIN_DB_PATH
                if not os.path.isabs(validate_db_path):
                    validate_db_path = os.path.join(BASE_DIR, validate_db_path)
            elif session.get('firm_user_id') and session.get('firm_tenant_id'):
                validate_db_path = MAIN_DB_PATH

            user_id = user_data['id']
            cache_key = (os.path.abspath(validate_db_path), user_id, session_token)
            now_ts = time.time()
            cached = _session_token_cache.get(cache_key)
            if cached and (now_ts - cached[0]) < _SESSION_TOKEN_CACHE_TTL_SEC:
                db_token = cached[1]
            else:
                db_token = None
                if session.get('firm_user_id') and session.get('firm_tenant_id'):
                    conn = get_main_db_connection()
                    try:
                        row = conn.execute(
                            "SELECT last_session_id FROM firm_users WHERE id = ?",
                            (user_id,),
                        ).fetchone()
                        db_token = row[0] if row else None
                    finally:
                        conn.close()
                else:
                    if hasattr(g, 'db_path') and g.db_path and paths_same_db(validate_db_path, g.db_path):
                        conn = get_db_connection()
                        row = conn.execute(
                            "SELECT last_session_id FROM users WHERE id = ?",
                            (user_id,),
                        ).fetchone()
                        db_token = row[0] if row else None
                    else:
                        conn = open_sqlite(validate_db_path)
                        try:
                            row = conn.execute(
                                "SELECT last_session_id FROM users WHERE id = ?",
                                (user_id,),
                            ).fetchone()
                            db_token = row[0] if row else None
                        finally:
                            conn.close()
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
            try:
                response = redirect(url_for('login'))
            except Exception:
                response = redirect('/login')
            return response

        # ==================== 4. Cache user-branch context ====================
        try:
            if session.get('user_id') and hasattr(g, 'db_path') and g.db_path:
                from Services.user_branch import cache_user_branch_context
                cache_user_branch_context(get_db_connection())
        except Exception:
            pass

    print("--- ĐÃ CẬP NHẬT VÀ KHỞI TẠO TENANT MIDDLEWARE AN TOÀN THÀNH CÔNG ---")