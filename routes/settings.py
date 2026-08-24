"""Routes đăng nhập, cài đặt, master admin — tách từ app.py."""
import json
import logging
import os
import random
import re
import secrets
import smtplib
import sqlite3
import string
import threading
import traceback
import uuid
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from email.message import EmailMessage
from io import BytesIO

import pandas as pd
import requests
from flask import (
    Response,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from db_utils import (
    BASE_DIR,
    MAIN_DB_PATH,
    db_path_available,
    get_db_connection,
    get_main_db_connection,
    open_sqlite,
    resolve_db_path,
    sqlite_commit,
    begin_immediate,
    locked_user_message,
    _is_locked_error,
)
from Services.email_service import get_smtp_config, send_email
from Services.subscription_service import get_subscription_plans
from Services.einvoice_registry import get_provider_meta, list_providers_for_ui

logger = logging.getLogger(__name__)

RECEIVER_EMAIL = os.getenv('RECEIVER_EMAIL', 'sales@ketoshop.pro.vn')



# --- UTILS: NHẬN DIỆN THIẾT BỊ ĐÃ THỰC XÁC THỰC KHI ĐĂNG NHẬP ---#
# Kiểm tra thiết bị trong quá trình đăng nhập
def is_device_trusted(username, fingerprint):
    from db_utils import get_main_db_connection, sqlite_write_retry

    def _read():
        with get_main_db_connection() as conn:
            row = conn.execute("""
                SELECT 1 FROM user_trusted_devices
                WHERE username = ? AND device_fingerprint = ?
            """, (username, fingerprint)).fetchone()
            return True if row else False

    try:
        return bool(sqlite_write_retry(_read, label='is_device_trusted'))
    except Exception as exc:
        try:
            current_app.logger.error('is_device_trusted: %s', exc)
        except Exception:
            pass
        return False

# Sau khi User nhập OTP thành công, lưu thiết bị này vào danh sách "Quen"
def _client_host0() -> str:
    return (request.host or '').split(':')[0].lower()


def _otp_onscreen_allowed() -> bool:
    """
    Hiện mã OTP trên UI chỉ khi đang dev localhost.
    VPS / production: luôn False — OTP chỉ qua email/SMS.
    Chặn spoof Host: localhost bằng cách yêu cầu app.debug (gunicorn VPS = False).
    Ép tắt: SME_SHOW_OTP_ON_SCREEN=0 | ép bật (vẫn cần debug+local): =1
    """
    raw = (os.environ.get('SME_SHOW_OTP_ON_SCREEN') or '').strip().lower()
    if raw in ('0', 'false', 'no', 'off'):
        return False
    # Production (gunicorn/waitress, debug=False) — không bao giờ hiện mã
    try:
        if not current_app.debug:
            return False
    except Exception:
        return False
    if _client_host0() not in ('127.0.0.1', 'localhost'):
        return False
    public = (
        os.environ.get('SME_PUBLIC_BASE_URL')
        or os.environ.get('PUBLIC_BASE_URL')
        or ''
    ).strip().lower()
    if public.startswith('https://') and 'localhost' not in public and '127.0.0.1' not in public:
        return False
    if raw in ('1', 'true', 'yes', 'on'):
        return True
    # Mặc định localhost + debug: cho hiện (tiện dev khi SMTP Zoho chặn)
    return True


def _mask_email(email: str) -> str:
    email = (email or '').strip()
    if '@' not in email:
        return '***'
    local, _, domain = email.partition('@')
    if len(local) <= 2:
        shown = local[:1] + '***'
    else:
        shown = local[:2] + '***'
    return f'{shown}@{domain}'


def add_trusted_device(username, fingerprint):
    from db_utils import get_main_db_connection, sqlite_write_retry

    def _write():
        with get_main_db_connection() as conn:
            from db_utils import begin_immediate
            begin_immediate(conn, label='add_trusted_device')
            conn.execute("""
                INSERT OR IGNORE INTO user_trusted_devices (username, device_fingerprint, last_login)
                VALUES (?, ?, ?)
            """, (username, fingerprint, datetime.now()))
            sqlite_commit(conn, label='settings')

    sqlite_write_retry(_write, label='add_trusted_device')

def get_device_fingerprint():
    """Vân tay trình duyệt (UA + IP) — dùng cho tenant / legacy OTP email."""
    ua = request.headers.get('User-Agent', '')
    ip = get_client_ip()
    return hashlib.sha256(f"{ua}{ip}".encode()).hexdigest()


def get_master_device_fingerprint(signal: dict | None = None):
    """Vân tay máy cho Master — ổn định giữa các trình duyệt trên cùng thiết bị."""
    from Services.totp_auth import fingerprint_from_request, parse_device_signal

    if signal is None:
        raw = request.form.get('device_signal') or request.args.get('device_signal')
        if not raw and request.is_json:
            body = request.get_json(silent=True) or {}
            raw = body.get('device_signal')
        signal = parse_device_signal(raw)
    return fingerprint_from_request(request, signal)

def finalize_login_process(user_row, tenant_id, db_path):
    new_session_token = str(uuid.uuid4())
    
    # Cập nhật token mới vào DB của Tenant để duy trì Single Session
    g.db_path = db_path
    conn = get_db_connection()
    conn.execute("UPDATE users SET last_session_id = ? WHERE id = ?", (new_session_token, user_row['id']))
    sqlite_commit(conn, label='settings')
    conn.close()

    # Thiết lập Session Flask
    session.clear()
    session['user'] = {
        'id': user_row['id'],
        'username': user_row['username'],
        'role': user_row['role'],
        'full_name': user_row.get('full_name') or user_row['username']
    }
    session['session_token'] = new_session_token
    session['db_path'] = db_path # Lưu để Middleware dùng
    session['last_tenant_id'] = tenant_id

def get_client_ip():
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0].split(',')[0]
    return request.remote_addr

def _defer_login_db_write(fn, *, label='login_deferred'):
    """Ghi DB phụ (lịch sử, audit) ngoài request — không chặn redirect login."""
    def _run():
        try:
            from db_utils import sqlite_write_retry
            retries = int(os.environ.get('SME_LOGIN_DEFER_RETRIES', '6') or 6)
            sqlite_write_retry(fn, label=label, retries=retries)
        except Exception as exc:
            try:
                current_app.logger.warning('%s failed: %s', label, exc)
            except Exception:
                print(f'{label} failed: {exc}')

    threading.Thread(target=_run, daemon=True, name=label).start()


def get_location(ip):
    if ip in ['127.0.0.1', 'localhost']:
        return "Nội bộ (Localhost)"
    # VPS: tắt gọi API ngoài mặc định — tránh treo login khi ip-api chậm/chặn
    if os.environ.get('SME_GEOIP', '').strip().lower() not in ('1', 'true', 'yes', 'on'):
        return ip or "Không xác định"
    try:
        res = requests.get(
            f'http://ip-api.com/json/{ip}?fields=city,country',
            timeout=float(os.environ.get('SME_GEOIP_TIMEOUT', '0.8') or 0.8),
        ).json()
        if res.get('status') == 'success':
            return f"{res.get('city')}, {res.get('country')}"
    except Exception:
        pass
    return ip or "Không xác định"

def log_login_attempt(user_id, username, tenant_id, status='Thành công'):
    """Ghi lịch sử vào Main Database (best-effort — không chặn đăng nhập)."""
    ip = get_client_ip()
    loc = get_location(ip)
    ua = request.headers.get('User-Agent')

    def _write():
        with get_main_db_connection() as conn:
            conn.execute("""
                INSERT INTO login_history (tenant_id, user_id, username, ip_address, location, device_info, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (tenant_id, user_id, username, ip, loc, ua, status))
            sqlite_commit(conn, label='settings')

    _defer_login_db_write(_write, label='log_login_attempt')


def _persist_successful_login(user_id, username, tenant_id, db_to_open, new_session_id, fingerprint, status='Thành công'):
    """Ghi session (đồng bộ) + trusted device / lịch sử / audit (nền).

    Chỉ cập nhật last_session_id là bắt buộc cho single-session; phần còn lại
    chạy thread nền để tránh 504 khi database.db bị locked trên VPS.
    """
    from db_utils import sqlite_write_retry

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    ip = get_client_ip()
    loc = get_location(ip)
    ua = request.headers.get('User-Agent')
    login_retries = int(os.environ.get('SME_LOGIN_WRITE_RETRIES', '3') or 3)

    def _write_session_critical():
        with open_sqlite(db_to_open) as conn_target:
            from db_utils import begin_immediate, sqlite_commit
            begin_immediate(conn_target, label='login_session')
            conn_target.execute(
                "UPDATE users SET last_session_id = ? WHERE id = ?",
                (new_session_id, user_id),
            )
            sqlite_commit(conn_target, label='login_session')

    def _write_trusted_device_sync():
        """Ghi thiết bị tin cậy đồng bộ — tránh OTP/TOTP lặp do thread nền thất bại."""
        if not fingerprint:
            return
        with get_main_db_connection() as conn_m:
            from db_utils import begin_immediate, sqlite_commit
            from Services.totp_auth import remember_trusted_device
            begin_immediate(conn_m, label='login_trusted_device')
            remember_trusted_device(conn_m, username, fingerprint)
            sqlite_commit(conn_m, label='login_trusted_device')

    def _write_main_extras():
        with get_main_db_connection() as conn_m:
            from db_utils import begin_immediate, sqlite_commit
            begin_immediate(conn_m, label='login_main_extras')
            try:
                conn_m.execute(
                    """INSERT INTO login_history
                       (tenant_id, user_id, username, ip_address, location, device_info, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (tenant_id, user_id, username, ip, loc, ua, status),
                )
            except Exception:
                pass
            sqlite_commit(conn_m, label='login_main_extras')

    def _write_audit_deferred():
        from Services.audit_log import write_audit
        write_audit(
            'login', 'auth',
            f"Đăng nhập: {status}",
            tenant_id=tenant_id,
            user_id=user_id,
            username=username,
            status='success' if 'Thành công' in str(status) else 'failed',
            use_main=True,
        )

    try:
        sqlite_write_retry(_write_session_critical, label='login_session', retries=login_retries)
    except Exception as e:
        try:
            current_app.logger.error('login session write: %s', e)
        except Exception:
            print(f'login session write: {e}')

    try:
        sqlite_write_retry(_write_trusted_device_sync, label='login_trusted_device', retries=login_retries)
    except Exception as e:
        try:
            current_app.logger.error('login trusted device: %s', e)
        except Exception:
            print(f'login trusted device: {e}')

    _defer_login_db_write(_write_main_extras, label='login_main_writes')
    _defer_login_db_write(_write_audit_deferred, label='login_audit')


def register_settings_routes(app):
    """Đăng ký route settings/login (giữ nguyên URL/endpoint)."""
    from auth import (
        User,
        admin_or_master_required,
        admin_or_store_setup_required,
        login_required,
        master_required,
        require_permission,
        tenant_settings_required,
    )
    from flask_login import login_user
    from app import bcrypt, google
    from Services.login_service import (
        configure_google_oauth,
        fetch_google_user_info,
        verify_google_credential,
        get_google_client_id,
        get_public_base_url,
        google_client_id_error,
        google_oauth_redirect_ready,
        google_oauth_setup_hints,
        oauth_redirect_uri,
        find_user_by_email,
        get_auth_settings,
        get_auth_settings_db,
        google_login_enabled,
        google_login_visible,
        google_oauth_callback_error_message,
        login_redirect_target,
        normalize_vn_phone,
        resolve_user_phone,
        save_auth_settings,
        send_otp_sms,
        sms_otp_enabled,
        sms_otp_visible,
    )
    from tenant_middleware import (
        add_user_to_mapping,
        get_tenant_by_username,
        update_user_email_in_mapping,
    )

    BACKUP_ROOT = os.path.join(BASE_DIR, 'backups')

    # --- ROUTE LOGIN CHÍNH ---

    from flask import render_template, request, flash, redirect, url_for, session, current_app

    def _login_page_context():
        """Context trang login — mọi phần phụ đều fail-safe, không được làm sập trang."""
        def _safe(fn, default=None, label=''):
            try:
                return fn()
            except Exception as exc:
                current_app.logger.warning("login context %s: %s", label or fn, exc)
                return default

        trial_google = None
        if request.args.get('trial_google') == '1':
            # Giữ lại trong session để /api/trial/register xác thực được email Google
            trial_google = session.get('trial_google')
        return dict(
            google_visible=_safe(google_login_visible, False, 'google_login_visible'),
            google_ready=_safe(google_login_enabled, False, 'google_login_enabled'),
            google_client_id=_safe(get_google_client_id, '', 'get_google_client_id'),
            google_redirect_ready=_safe(
                google_oauth_redirect_ready, False, 'google_oauth_redirect_ready',
            ),
            trial_google_pending=trial_google,
            subscription_plans=_safe(get_subscription_plans, [], 'get_subscription_plans'),
        )

    # Form dự phòng — HTML thuần, không qua Jinja/context processor nên không thể lỗi
    _LOGIN_FALLBACK_HTML = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Đăng Nhập - KETO POS ALL IN ONE - Hệ Thống Bán Hàng kiêm Kế Toán cho Hộ Kinh Doanh và Doanh Nghiệp.</title>
<meta name="description" content="KETO POS ALL IN ONE - Hệ Thống Bán Hàng kiêm Kế Toán cho Hộ Kinh Doanh và Doanh Nghiệp.">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
</head><body class="bg-light">
<div class="container" style="max-width:420px">
  <div class="card shadow-sm mt-5">
    <div class="card-body p-4">
      <h4 class="fw-bold text-primary text-center mb-4">ĐĂNG NHẬP</h4>
      <form method="POST" action="/login">
        <div class="mb-3">
          <label class="form-label fw-semibold">Tên đăng nhập</label>
          <input type="text" name="username" class="form-control form-control-lg" required autofocus>
        </div>
        <div class="mb-3">
          <label class="form-label fw-semibold">Mật khẩu</label>
          <input type="password" name="password" class="form-control form-control-lg" required>
        </div>
        <button type="submit" class="btn btn-primary btn-lg w-100 fw-bold">ĐĂNG NHẬP</button>
      </form>
      <div class="text-end mt-3"><a href="/forgot-password" class="small">Quên mật khẩu?</a></div>
    </div>
  </div>
</div>
</body></html>"""

    def _render_login_page():
        try:
            html = render_template('login.html', **_login_page_context())
        except Exception as exc:
            # Không để trang đăng nhập trả 500 — hiển thị form tối giản để user vẫn vào được
            current_app.logger.exception("render login.html: %s", exc)
            return _LOGIN_FALLBACK_HTML, 200, {'Content-Type': 'text/html; charset=utf-8'}
        resp = make_response(html)
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        return resp

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')

            if not username or not password:
                flash("Vui lòng nhập đầy đủ tên đăng nhập và mật khẩu.", "danger")
                return _render_login_page()

            # ==================== 0. Đăng nhập DVKT (firm_users trên registry) ====================
            from Services.firm_tenant import (
                get_firm_user_by_login,
                verify_firm_password,
                finalize_firm_login,
            )
            firm_user = get_firm_user_by_login(username)
            if firm_user:
                if not firm_user.get('firm_active', 1):
                    flash('Tài khoản đơn vị DVKT đã bị khóa. Liên hệ quản trị.', 'danger')
                    return _render_login_page()
                exp = (firm_user.get('expiry_date') or '').strip()
                if exp:
                    try:
                        from datetime import datetime as _dt
                        if _dt.strptime(exp[:10], '%Y-%m-%d').date() < _dt.now().date():
                            flash('Gói dịch vụ DVKT đã hết hạn. Liên hệ quản trị để gia hạn.', 'warning')
                            return _render_login_page()
                    except ValueError:
                        pass
                if not verify_firm_password(firm_user, password):
                    flash('Mật khẩu không chính xác!', 'danger')
                    return _render_login_page()
                token = finalize_firm_login(firm_user)
                session.permanent = True
                user_obj = User(
                    id=firm_user['id'],
                    username=firm_user['login_email'],
                    role=session.get('role') or 'managerSME99',
                    db_path=session.get('db_path'),
                    tenant_id=session.get('last_tenant_id'),
                    full_name=firm_user.get('full_name') or firm_user['login_email'],
                    permissions='view_audit_log',
                )
                session['user_id'] = int(firm_user['id'])
                login_user(user_obj, remember=True)
                flash(f"Chào mừng {user_obj.full_name} — cổng DVKT", 'success')
                return redirect(url_for('firm_portal'))

            # ==================== 1. Xác định Tenant và Database ====================
            tenant_record = get_tenant_by_username(username, active_only=True)

            if not tenant_record:
                inactive = get_tenant_by_username(username, active_only=False)
                if inactive and not inactive.get('is_active'):
                    session['renewal_context'] = {
                        'tenant_id': inactive['tenant_id'],
                        'email': inactive.get('email') or '',
                        'business_name': inactive.get('business_name') or '',
                    }
                    flash('Tài khoản đã hết hạn. Vui lòng gia hạn để tiếp tục.', 'warning')
                    return redirect(url_for('renewal_page'))

            if tenant_record:
                tenant_data = dict(tenant_record)
                db_path_raw = tenant_data.get('db_path')
                current_tenant_id = tenant_data.get('tenant_id')
                tenant_2fa_enabled = bool(tenant_data.get('is_2fa_enabled', 0))

                if db_path_raw and not os.path.isabs(db_path_raw):
                    db_to_open = os.path.join(BASE_DIR, db_path_raw)
                else:
                    db_to_open = db_path_raw
            else:
                db_to_open = os.path.join(BASE_DIR, 'database.db')
                current_tenant_id = None
                tenant_2fa_enabled = False

            # Kiểm tra DB đích có sẵn (file SQLite hoặc schema Postgres)
            if not db_path_available(db_to_open):
                flash("Cơ sở dữ liệu của chi nhánh không tồn tại hoặc đường dẫn sai cấu hình!", "danger")
                current_app.logger.error(f"Đăng nhập thất bại: Không tìm thấy file DB tại {db_to_open}")
                return _render_login_page()

            # ==================== 2. Lấy thông tin User từ DB tương ứng ====================
            try:
                conn = open_sqlite(db_to_open)
                user_row = conn.execute(
                    "SELECT * FROM users WHERE username = ?",
                    (username,)
                ).fetchone()
                conn.close()
            except Exception as e:
                current_app.logger.error(f"Lỗi kết nối cơ sở dữ liệu tenant ({db_to_open}): {e}")
                flash(f"Lỗi hệ thống khi truy cập cơ sở dữ liệu.", "danger")
                return _render_login_page()

            if not user_row:
                flash("Tài khoản không tồn tại trên hệ thống!", "danger")
                return _render_login_page()

            user = dict(user_row)
            stored_password = user.get('password')
            if isinstance(stored_password, bytes):
                stored_password = stored_password.decode('utf-8')

            # ==================== 3. Kiểm tra mật khẩu ====================
            if not stored_password:
                flash("Tài khoản chưa có mật khẩu. Liên hệ quản trị viên.", "danger")
                return _render_login_page()
            try:
                password_ok = bcrypt.check_password_hash(stored_password, password)
            except (TypeError, ValueError) as exc:
                current_app.logger.error("Lỗi bcrypt user %s: %s", username, exc)
                flash("Mật khẩu tài khoản trên hệ thống không hợp lệ. Liên hệ quản trị viên.", "danger")
                return _render_login_page()
            if not password_ok:
                log_login_attempt(user.get('id'), username, current_tenant_id, status='Thất bại (Sai MK)')
                flash("Mật khẩu không chính xác!", "danger")
                return _render_login_page()

            # ==================== 4. Quyết định có yêu cầu 2FA hay không ====================
            if current_tenant_id is not None:
                is_2fa_enabled = tenant_2fa_enabled
            else:
                is_2fa_enabled = bool(user.get('is_2fa_enabled', 0))

            user_role = str(user.get('role', '')).strip()
            is_master_login = user_role == 'master' and current_tenant_id is None

            # ==================== 5. Xử lý 2FA ====================
            if is_2fa_enabled:
                from Services.totp_auth import (
                    MASTER_TRUST_DAYS,
                    get_user_totp_secret,
                    is_device_trusted as totp_device_trusted,
                    parse_device_signal,
                )

                if is_master_login:
                    device_signal = parse_device_signal(request.form.get('device_signal'))
                    fingerprint = get_master_device_fingerprint(device_signal)
                    trusted = False
                    totp_ready = False
                    try:
                        conn_m = get_main_db_connection()
                        try:
                            trusted = totp_device_trusted(
                                conn_m, username, fingerprint, trust_days=MASTER_TRUST_DAYS,
                            )
                            totp_ready = bool(get_user_totp_secret(conn_m, int(user['id'])))
                        finally:
                            conn_m.close()
                    except Exception as e:
                        current_app.logger.error(f"Lỗi thiết bị tin cậy master: {e}")
                        trusted = False

                    if not trusted:
                        session.permanent = True
                        session['pending_auth'] = {
                            'user': {
                                'id': int(user['id']),
                                'username': str(user['username']),
                                'role': user_role,
                                'full_name': str(user.get('full_name') or username),
                                'permissions': str(user.get('permissions') or ''),
                            },
                            'last_tenant_id': current_tenant_id,
                            'db_path': db_to_open,
                            'email': user.get('email'),
                            'phone': user.get('phone') or username,
                            'fingerprint': fingerprint,
                            'device_signal': device_signal,
                            'auth_method': 'totp',
                            'totp_ready': totp_ready,
                            'google_allowed': google_login_enabled(),
                        }
                        session.modified = True
                        try:
                            return redirect(url_for('verify_totp_page'))
                        except Exception:
                            return redirect('/verify-totp')
                else:
                    fingerprint = get_device_fingerprint()
                    try:
                        conn_m = get_main_db_connection()
                        device_record = conn_m.execute(
                            "SELECT last_login FROM user_trusted_devices WHERE username=? AND device_fingerprint=?",
                            (username, fingerprint)
                        ).fetchone()
                        conn_m.close()
                    except Exception as e:
                        current_app.logger.error(f"Lỗi truy vấn thiết bị tin cậy tại DB tổng: {e}")
                        device_record = None

                    should_ask_2fa = False
                    if not device_record:
                        should_ask_2fa = True
                    else:
                        last_login_val = device_record['last_login']
                        if last_login_val:
                            try:
                                last_login_dt = datetime.strptime(last_login_val, '%Y-%m-%d %H:%M:%S')
                                if datetime.now() - last_login_dt > timedelta(days=3):
                                    should_ask_2fa = True
                            except Exception:
                                should_ask_2fa = True
                        else:
                            should_ask_2fa = True

                    if should_ask_2fa:
                        session.permanent = True
                        session['pending_auth'] = {
                            'user': {
                                'id': int(user['id']),
                                'username': str(user['username']),
                                'role': str(user.get('role', '')).strip(),
                                'full_name': str(user.get('full_name') or username),
                                'permissions': str(user.get('permissions') or '')
                            },
                            'last_tenant_id': current_tenant_id,
                            'db_path': db_to_open,
                            'email': user.get('email'),
                            'phone': user.get('phone') or username,
                            'fingerprint': fingerprint,
                            'auth_method': 'email_otp',
                            'google_allowed': google_login_enabled(),
                        }
                        session.modified = True
                        try:
                            return redirect(url_for('login_2fa'))
                        except Exception:
                            return redirect('/login_2fa')

            # ====================== ĐĂNG NHẬP THÀNH CÔNG ======================
        
            # Làm sạch chuỗi quyền phòng hờ khoảng trắng trong DB phá vỡ cấu hình điều hướng
            user_role = str(user.get('role', '')).strip()
            new_session_id = str(uuid.uuid4())
            if is_master_login:
                from Services.totp_auth import parse_device_signal as _parse_ds
                login_fp = get_master_device_fingerprint(_parse_ds(request.form.get('device_signal')))
            else:
                login_fp = get_device_fingerprint()

            _persist_successful_login(
                user['id'], username, current_tenant_id, db_to_open,
                new_session_id, login_fp, status='Thành công',
            )

            # Khởi tạo và thiết lập Flask Session thuần sạch sẽ
            session.clear()
            session.permanent = True
            session['user'] = {
                'id': int(user['id']),
                'username': str(user['username']),
                'role': user_role,
                'full_name': str(user.get('full_name') or username),
                'permissions': str(user.get('permissions') or '')
            }
            session['last_tenant_id'] = current_tenant_id
            session['user_id'] = int(user['id'])
            session['role'] = user_role
            session['db_path'] = db_to_open
            session['session_token'] = new_session_id 
            session.modified = True

            # 🌟 ĐỒNG BỘ: Tạo thực thể đối tượng User và nạp trực tiếp vào Flask-Login (Kích hoạt current_user)
            user_obj = User(
                id=user['id'],
                username=user['username'],
                role=user_role,
                db_path=db_to_open,
                tenant_id=current_tenant_id,
                full_name=user.get('full_name') or username,
                permissions=user.get('permissions', '')
            )
            login_user(user_obj, remember=True)

            flash(f"Chào mừng bạn quay lại, {user_obj.full_name}!", "success")

            # ==================== LOGIC ĐIỀU HƯỚNG PHÂN QUYỀN AN TOÀN ====================
            from Services.sme_roles import is_sme_role
            try:
                if user_role in ('admin*', 'manager*') and current_tenant_id is not None:
                    return redirect(url_for('rental_service'))
                if user_role in ('adminFB', 'managerFB') and current_tenant_id is not None:
                    return redirect(url_for('F_and_B_service'))
                if is_sme_role(user_role) and current_tenant_id is not None:
                    return redirect(url_for('SME_dashboard'))
                elif user_role == 'master' and current_tenant_id is None:
                    return redirect(url_for('master_settings'))
                return redirect(url_for('sale'))
            
            except Exception as redirect_err:
                current_app.logger.error(f"Lỗi Build URL động thông qua url_for: {redirect_err}")
                if user_role in ('admin*', 'manager*'):
                    return redirect('/rental_service')
                if user_role in ('adminFB', 'managerFB'):
                    return redirect('/F_and_B_service')
                if is_sme_role(user_role):
                    return redirect('/SME_dashboard')
                elif user_role == 'master' and current_tenant_id is None:
                    return redirect('/master_settings')
                return redirect('/sale')

        # Xử lý phương thức GET: Hiển thị giao diện form đăng nhập thông thường
        return _render_login_page()

    #=== LOGIN HISTORY ============#
    @app.route('/api/master/login-history')
    def api_login_history():
        if session.get('role') != 'master':
            return jsonify({'success': False, 'error': 'Quyền truy cập bị từ chối'}), 403

        f_tenant = request.args.get('tenant_id', '').strip()
        f_start = request.args.get('start_date', '')
        f_end = request.args.get('end_date', '')

        query = "SELECT * FROM login_history WHERE 1=1"
        params = []

        if f_tenant:
            query += " AND tenant_id = ?"
            params.append(f_tenant)
        if f_start:
            query += " AND DATE(login_at) >= ?"
            params.append(f_start)
        if f_end:
            query += " AND DATE(login_at) <= ?"
            params.append(f_end)

        query += " ORDER BY login_at DESC LIMIT 500"

        try:
            with get_main_db_connection() as conn:
                rows = conn.execute(query, params).fetchall()
            return jsonify({'success': True, 'data': [dict(r) for r in rows]})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/master/tenant/<tenant_id>', methods=['GET'])
    def get_tenant_detail(tenant_id):
        """Lấy thông tin chi tiết của một tenant (dùng cho toggle 2FA và edit)"""
    
        # Kiểm tra quyền Master
        if 'user' not in session or session.get('role') != 'master':
            return jsonify({
                "success": False,
                "error": "Bạn không có quyền truy cập. Chỉ Master mới được phép."
            }), 403

        try:
            with get_main_db_connection() as conn:
                tenant = conn.execute("""
                    SELECT tenant_id, business_name, phone, email, address,
                           expiry_date, is_active, is_2fa_enabled,
                           created_at, settings, business_type,
                           COALESCE(tenant_type, 'standalone') AS tenant_type,
                           COALESCE(max_clients, 50) AS max_clients
                    FROM tenants
                    WHERE tenant_id = ?
                """, (tenant_id,)).fetchone()

            if not tenant:
                return jsonify({
                    "success": False,
                    "error": f"Tenant '{tenant_id}' không tồn tại."
                }), 404

            # Chuyển sang dict
            tenant_dict = dict(tenant)
            tenant_dict['is_2fa_enabled'] = int(tenant_dict.get('is_2fa_enabled') or 0)

            from Services.subscription_service import parse_tenant_settings
            from Services.tenant_profile import build_profile_from_registry

            profile = build_profile_from_registry(tenant_dict)
            tenant_dict['accounting_regime'] = profile.get('accounting_regime')
            tenant_dict['revenue_tier'] = profile.get('revenue_tier')
            tenant_dict['business_line'] = profile.get('business_line')
            tenant_dict['hkd_sector'] = profile.get('primary_nn_sector')
            tenant_dict['primary_nn_sector'] = profile.get('primary_nn_sector')
            tenant_dict['enabled_nn_sectors'] = profile.get('enabled_nn_sectors') or []
            tenant_dict['vat_filing_period'] = profile.get('vat_filing_period') or profile.get('filing_period')
            tenant_dict['filing_period'] = profile.get('filing_period')
            st = parse_tenant_settings(tenant_dict.get('settings')) or {}
            tenant_dict['enterprise_sector'] = st.get('enterprise_sector')
            tenant_dict['sme_revenue_band'] = st.get('sme_revenue_band')
            tenant_dict['firm_package_id'] = st.get('firm_package_id')
            tenant_dict['settings'] = st
            if tenant_dict.get('tenant_type') == 'firm':
                from Services.firm_tenant import firm_usage_summary, resolve_firm_package
                tenant_dict['firm_usage'] = firm_usage_summary(
                    tenant_id, tenant_dict.get('max_clients'),
                )
                pkg = resolve_firm_package(
                    tenant_dict.get('firm_package_id'),
                    tenant_dict.get('max_clients'),
                )
                tenant_dict['firm_package_label'] = pkg.get('label')

            return jsonify({
                "success": True,
                "tenant": tenant_dict
            })

        except sqlite3.Error as e:
            return jsonify({
                "success": False,
                "error": f"Lỗi database: {str(e)}"
            }), 500
        except Exception as e:
            return jsonify({
                "success": False,
                "error": f"Lỗi hệ thống: {str(e)}"
            }), 500

    # --- ROUTE XÁC THỰC GOOGLE BƯỚC 2 ---
    @app.route('/login-2fa')
    def login_2fa():
        # Kiểm tra nếu không có dữ liệu chờ xác thực thì đẩy về trang login chính
        if 'pending_auth' not in session:
            return redirect(url_for('login'))
    
        # Hiển thị giao diện chọn phương thức xác thực (Email/SMS OTP / Google)
        auth = dict(session['pending_auth'])
        if str((auth.get('user') or {}).get('role') or '').strip() == 'master' or auth.get('auth_method') == 'totp':
            return redirect(url_for('verify_totp_page'))
        auth['google_allowed'] = google_login_visible()
        auth['google_ready'] = google_login_enabled()
        auth['google_client_id'] = get_google_client_id()
        auth['google_config_error'] = google_client_id_error()
        auth['sms_allowed'] = sms_otp_visible()
        auth['sms_ready'] = sms_otp_enabled()
        return render_template('login_2fa.html', auth=auth)


    def send_otp_email_logic(to_email, otp_code):
        from Services.email_service import send_otp_email as _send_otp
        ok, err = _send_otp(to_email, otp_code)
        if not ok:
            current_app.logger.error("Lỗi gửi OTP email: %s", err)
        return ok, err

    @app.route('/send-otp-email', methods=['POST'])
    def send_otp_email():
        if 'pending_auth' not in session:
            flash("Phiên xác thực đã hết hạn. Vui lòng đăng nhập lại.", "warning")
            return redirect(url_for('login'))

        auth_data = session['pending_auth']
        username = auth_data['user']['username']
        # Ưu tiên email đã lưu lúc login — tránh query DB (dễ locked) làm chậm/vỡ phiên
        email_to = (auth_data.get('email') or '').strip() or None

        if not email_to:
            db_path_raw = auth_data.get('db_path')
            if db_path_raw and not os.path.isabs(db_path_raw):
                g.db_path = os.path.join(BASE_DIR, db_path_raw)
            else:
                g.db_path = db_path_raw

            try:
                conn = get_db_connection()
                user_row = conn.execute(
                    "SELECT email FROM users WHERE username = ?", (username,),
                ).fetchone()
                conn.close()
                if user_row and user_row['email']:
                    email_to = user_row['email']
            except Exception as e:
                current_app.logger.error("DEBUG_OTP: Lỗi khi tìm ở Tenant DB: %s", e)

        if not email_to:
            g.db_path = os.path.join(BASE_DIR, 'database.db')
            try:
                conn_main = get_db_connection()
                user_main = conn_main.execute(
                    "SELECT email FROM users WHERE username = ?", (username,),
                ).fetchone()
                conn_main.close()
                if user_main and user_main['email']:
                    email_to = user_main['email']
            except Exception as e:
                current_app.logger.error("DEBUG_OTP: Lỗi khi tìm ở Main DB: %s", e)

        if not email_to:
            flash("Tài khoản chưa cập nhật email. Không thể gửi mã OTP!", "danger")
            return redirect(url_for('login_2fa'))

        otp_code = ''.join(random.choices(string.digits, k=6))
        session.permanent = True
        session['otp_check'] = {
            'code': otp_code,
            'channel': 'email',
            'email': email_to,
            'expires': (datetime.now() + timedelta(minutes=5)).timestamp(),
        }
        session.modified = True

        show_onscreen = _otp_onscreen_allowed()
        masked = _mask_email(email_to)

        if show_onscreen:
            # Dev localhost: gửi nền + hiện mã (SMTP Zoho thường chặn từ máy local)
            app_obj = current_app._get_current_object()

            def _send_bg():
                with app_obj.app_context():
                    ok, err_msg = send_otp_email_logic(email_to, otp_code)
                    if not ok:
                        app_obj.logger.error(
                            "Lỗi gửi OTP email tới %s: %s", masked, err_msg,
                        )

            threading.Thread(target=_send_bg, daemon=True, name='send-otp-email').start()
            flash(
                f"Mã OTP (localhost): {otp_code} — đang thử gửi email tới {masked}.",
                "warning",
            )
            return redirect(url_for('verify_otp_page'))

        # VPS / production: bắt buộc gửi email thành công; không hiện mã trên UI / log
        ok, err_msg = send_otp_email_logic(email_to, otp_code)
        if not ok:
            session.pop('otp_check', None)
            session.modified = True
            current_app.logger.error(
                "Lỗi gửi OTP email tới %s: %s", masked, err_msg,
            )
            flash(
                err_msg
                or "Không gửi được email OTP. Kiểm tra cấu hình SMTP hoặc thử lại sau.",
                "danger",
            )
            return redirect(url_for('login_2fa'))

        flash(f"Mã xác thực đã được gửi tới email {masked}.", "success")
        return redirect(url_for('verify_otp_page'))

    @app.route('/send-otp-sms', methods=['POST'])
    def send_otp_sms_route():
        if 'pending_auth' not in session:
            return redirect(url_for('login'))

        if not sms_otp_visible():
            flash("OTP SMS đang tắt trong Master Settings.", "danger")
            return redirect(url_for('login_2fa'))
        if not sms_otp_enabled():
            flash("Chưa cấu hình SMS API. Vào Master Settings → tab Đăng nhập & OTP.", "danger")
            return redirect(url_for('login_2fa'))

        auth_data = session['pending_auth']
        username = auth_data['user']['username']
        db_path_raw = auth_data.get('db_path')
        db_path = db_path_raw if os.path.isabs(db_path_raw or '') else os.path.join(BASE_DIR, db_path_raw or 'database.db')

        phone = resolve_user_phone(auth_data.get('user'), db_path, username)
        if not phone:
            flash("Tài khoản chưa có số điện thoại. Không thể gửi OTP SMS!", "danger")
            return redirect(url_for('login_2fa'))

        otp_code = ''.join(random.choices(string.digits, k=6))
        ok, result = send_otp_sms(phone, otp_code)
        if not ok:
            flash(f"Không gửi được SMS: {result}", "danger")
            return redirect(url_for('login_2fa'))

        masked = phone[-4:].rjust(len(phone), '*') if len(phone) > 4 else phone
        session['otp_check'] = {
            'code': otp_code,
            'channel': 'sms',
            'phone': phone,
            'display': masked,
            'expires': (datetime.now() + timedelta(minutes=5)).timestamp(),
        }
        session.modified = True
        flash(f"Mã OTP đã gửi tới số điện thoại ***{phone[-4:]}.", "success")
        return redirect(url_for('verify_otp_page'))

    # 🌟 THÊM ROUTE NÀY: Dùng phương thức GET hiển thị trang nhập OTP an toàn không sợ F5 lỗi
    @app.route('/verify-otp', methods=['GET'])
    def verify_otp_page():
        if 'pending_auth' not in session or 'otp_check' not in session:
            flash("Phiên xác thực đã hết hạn. Vui lòng đăng nhập lại.", "warning")
            return redirect(url_for('login'))
        otp = session['otp_check']
        return render_template(
            'verify_2fa.html',
            channel=otp.get('channel', 'email'),
            email=_mask_email(otp.get('email') or session['pending_auth'].get('email') or ''),
            phone_display=otp.get('display'),
            # Chỉ truyền mã khi được phép (localhost + debug); VPS luôn None
            localhost_otp=otp.get('code') if _otp_onscreen_allowed() else None,
        )

    @app.route('/verify-otp-code', methods=['POST'])
    def verify_otp_code():
        entered_code = request.form.get('otp_code', '').strip()
        otp_data = session.get('otp_check')
        auth = session.get('pending_auth')

        # Nếu mất session tạm trong quá trình chờ nhập mã, đẩy thẳng về login
        if not otp_data or not auth:
            flash("Phiên xác thực đã hết hạn hoặc không hợp lệ. Vui lòng đăng nhập lại.", "danger")
            return redirect(url_for('login'))

        # Kiểm tra tính chính xác và thời hạn của mã OTP
        if entered_code == otp_data['code'] and datetime.now().timestamp() < otp_data['expires']:
            try:
                db_to_open = auth.get('db_path')
                if db_to_open and not os.path.isabs(db_to_open):
                    db_to_open = os.path.join(BASE_DIR, db_to_open)
                session.pop('otp_check', None)
                session.pop('pending_auth', None)
                return _finalize_login_from_dict(
                    auth['user'],
                    db_to_open,
                    auth.get('last_tenant_id'),
                    auth.get('fingerprint') or get_device_fingerprint(),
                )
            except Exception as e:
                current_app.logger.error(f"Lỗi hệ thống nghiêm trọng tại verify_otp_code trên VPS: {e}")
                flash("Lỗi cấu hình đồng bộ phiên làm việc an toàn.", "danger")
                return redirect(url_for('login'))
            
        # Xử lý trường hợp nhập sai mã hoặc mã hết hạn hạn
        flash("Mã OTP không đúng hoặc đã hết hạn!", "danger")
        otp = session.get('otp_check') or {}
        return render_template(
            'verify_2fa.html',
            channel=otp.get('channel', 'email'),
            email=_mask_email(otp.get('email') or (auth or {}).get('email') or ''),
            phone_display=otp.get('display'),
            localhost_otp=otp.get('code') if _otp_onscreen_allowed() else None,
        )

    @app.route('/verify-totp', methods=['GET', 'POST'])
    def verify_totp_page():
        """Xác thực TOTP (Google Authenticator) cho tài khoản Master trên thiết bị mới."""
        auth = session.get('pending_auth')
        if not auth:
            flash('Phiên xác thực đã hết hạn. Vui lòng đăng nhập lại.', 'danger')
            return redirect(url_for('login'))

        user = auth.get('user') or {}
        if str(user.get('role') or '').strip() != 'master':
            flash('Luồng TOTP chỉ dành cho tài khoản Master.', 'warning')
            return redirect(url_for('login_2fa'))

        setup_mode = not bool(auth.get('totp_ready'))
        setup_payload = None

        if request.method == 'GET' and setup_mode:
            from Services.totp_auth import (
                generate_totp_secret,
                get_user_totp_secret_any,
                provisioning_uri,
                qr_png_data_url,
                save_totp_draft,
            )
            secret = session.get('pending_totp_secret')
            if not secret:
                try:
                    with get_main_db_connection() as conn:
                        secret = get_user_totp_secret_any(conn, int(user['id']))
                except Exception:
                    secret = None
            if not secret:
                secret = generate_totp_secret()
                try:
                    with get_main_db_connection() as conn:
                        from db_utils import begin_immediate, sqlite_commit
                        begin_immediate(conn, label='totp_draft')
                        save_totp_draft(conn, int(user['id']), secret)
                        sqlite_commit(conn, label='totp_draft')
                except Exception as exc:
                    current_app.logger.warning('totp draft save: %s', exc)
            session['pending_totp_secret'] = secret
            session.modified = True
            uri = provisioning_uri(secret, user.get('username') or 'master')
            setup_payload = {
                'secret': secret,
                'otpauth_uri': uri,
                'qr_data_url': qr_png_data_url(uri),
            }

        if request.method == 'POST':
            code = (request.form.get('totp_code') or '').strip()
            from Services.totp_auth import (
                get_user_totp_secret,
                save_totp_secret,
                verify_totp_code,
            )
            from db_utils import sqlite_commit

            secret = None
            if setup_mode:
                secret = session.get('pending_totp_secret')
                if not secret:
                    try:
                        with get_main_db_connection() as conn:
                            from Services.totp_auth import get_user_totp_secret_any
                            secret = get_user_totp_secret_any(conn, int(user['id']))
                    except Exception:
                        secret = None
            else:
                try:
                    with get_main_db_connection() as conn:
                        secret = get_user_totp_secret(conn, int(user['id']))
                except Exception as exc:
                    current_app.logger.error('verify_totp load secret: %s', exc)

            if not secret or not verify_totp_code(secret, code):
                flash('Mã Authenticator không đúng hoặc đã hết hạn.', 'danger')
                if setup_mode and not setup_payload:
                    from Services.totp_auth import (
                        get_user_totp_secret_any,
                        provisioning_uri,
                        qr_png_data_url,
                    )
                    secret_show = (
                        session.get('pending_totp_secret')
                        or secret
                    )
                    if not secret_show:
                        try:
                            with get_main_db_connection() as conn:
                                secret_show = get_user_totp_secret_any(conn, int(user['id']))
                        except Exception:
                            secret_show = None
                    if not secret_show:
                        from Services.totp_auth import generate_totp_secret
                        secret_show = generate_totp_secret()
                    session['pending_totp_secret'] = secret_show
                    uri = provisioning_uri(secret_show, user.get('username') or 'master')
                    setup_payload = {
                        'secret': secret_show,
                        'otpauth_uri': uri,
                        'qr_data_url': qr_png_data_url(uri),
                    }
                return render_template(
                    'verify_totp.html',
                    auth=auth,
                    setup_mode=setup_mode,
                    setup=setup_payload,
                )

            if setup_mode:
                try:
                    with get_main_db_connection() as conn:
                        from db_utils import begin_immediate
                        begin_immediate(conn, label='save_master_totp')
                        save_totp_secret(conn, int(user['id']), secret)
                        sqlite_commit(conn, label='save_master_totp')
                except Exception as exc:
                    current_app.logger.error('save totp: %s', exc)
                    flash('Không lưu được cấu hình Authenticator.', 'danger')
                    return redirect(url_for('verify_totp_page'))
                session.pop('pending_totp_secret', None)

            db_to_open = auth.get('db_path')
            if db_to_open and not os.path.isabs(db_to_open):
                db_to_open = os.path.join(BASE_DIR, db_to_open)
            fingerprint = auth.get('fingerprint') or get_master_device_fingerprint(
                auth.get('device_signal')
            )
            session.pop('otp_check', None)
            session.pop('pending_auth', None)
            return _finalize_login_from_dict(
                auth['user'],
                db_to_open,
                auth.get('last_tenant_id'),
                fingerprint,
            )

        return render_template(
            'verify_totp.html',
            auth=auth,
            setup_mode=setup_mode,
            setup=setup_payload,
        )

    def _google_login_by_email(email):
        """
        Đăng nhập bằng Google: email đã xác minh bởi Google và khớp email đăng ký → vào thẳng.
        Ưu tiên tài khoản master (cùng email với tenant) rồi mới tới tenant.
        """
        from Services.subscription_service import get_tenant_record, tenant_is_expired

        found = find_user_by_email(email)
        if not found:
            return {
                'success': False,
                'needs_registration': True,
                'email': email,
                'error': f'Email Google "{email}" chưa được đăng ký. Vui lòng đăng ký dùng thử.',
            }

        user = found['user']
        db_to_open = found['db_path']
        current_tenant_id = found.get('tenant_id')

        if current_tenant_id is not None:
            rec = get_tenant_record(current_tenant_id, include_inactive=True) or {}
            if not rec.get('is_active') or tenant_is_expired({
                'is_active': rec.get('is_active'),
                'expiry_date': rec.get('expiry_date'),
            }):
                session['renewal_context'] = {
                    'tenant_id': current_tenant_id,
                    'email': email,
                    'business_name': rec.get('business_name') or '',
                }
                return {
                    'success': False,
                    'needs_renewal': True,
                    'redirect': url_for('renewal_page'),
                    'error': 'Tài khoản đã hết hạn. Vui lòng gia hạn để tiếp tục sử dụng.',
                }

        resp = _finalize_login_from_dict(
            user, db_to_open, current_tenant_id, get_device_fingerprint(),
        )
        redirect_url = getattr(resp, 'location', None)
        if not redirect_url:
            if current_tenant_id is None and str(user.get('role', '')).strip() == 'master':
                redirect_url = url_for('master_settings')
            else:
                redirect_url = url_for('sale')
        return {'success': True, 'redirect': redirect_url}

    @app.route('/login/google/credential', methods=['POST'])
    def login_google_credential():
        """Đăng nhập bằng Gmail đang lưu trên trình duyệt (Google Identity Services)."""
        if not google_login_visible():
            return jsonify({'success': False, 'error': 'Đăng nhập Google đang tắt.'}), 400

        payload = request.get_json(silent=True) or {}
        user_info, err = verify_google_credential(payload.get('credential'))
        if err:
            return jsonify({'success': False, 'error': err}), 400

        result = _google_login_by_email(user_info['email'])
        if not result.get('success'):
            status = 400
            if result.get('needs_registration') or result.get('needs_renewal'):
                status = 200
            return jsonify(result), status
        return jsonify(result)

    @app.route('/login/google/2fa-credential', methods=['POST'])
    def login_google_2fa_credential():
        """Xác thực 2FA bằng Gmail trên trình duyệt."""
        if 'pending_auth' not in session:
            return jsonify({'success': False, 'error': 'Phiên xác thực đã hết hạn.'}), 401

        payload = request.get_json(silent=True) or {}
        user_info, err = verify_google_credential(payload.get('credential'))
        if err:
            return jsonify({'success': False, 'error': err}), 400

        auth = session['pending_auth']
        google_email = user_info['email']
        registered = (auth.get('email') or '').strip().lower()
        if not registered or google_email != registered:
            return jsonify({
                'success': False,
                'error': 'Email Google không trùng khớp với email đã đăng ký.',
            }), 400

        user = auth['user']
        db_path = auth['db_path']
        if db_path and not os.path.isabs(db_path):
            db_path = os.path.join(BASE_DIR, db_path)
        tenant_id = auth.get('last_tenant_id')
        fingerprint = auth.get('fingerprint') or get_device_fingerprint()

        session.pop('pending_auth', None)
        session.pop('otp_check', None)
        resp = _finalize_login_from_dict(user, db_path, tenant_id, fingerprint)
        return jsonify({'success': True, 'redirect': getattr(resp, 'location', None) or url_for('sale')})

    # --- ĐĂNG NHẬP GOOGLE (OAuth redirect — dự phòng) ---
    @app.route('/login/google')
    def login_google():
        if not google_login_visible():
            flash("Đăng nhập Google đang tắt trong Master Settings.", "warning")
            return redirect(url_for('login'))
        if not configure_google_oauth(google):
            flash(
                "Chưa cấu hình Google OAuth. Chọn một trong các cách: "
                "(1) Master Settings → Đăng nhập & OTP, "
                "(2) thêm GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET vào .env, "
                "(3) tạo file config/auth.local.json (xem auth.local.json.example).",
                "warning",
            )
            return redirect(url_for('login'))
        session.permanent = True
        session['oauth_mode'] = 'login'
        session.modified = True
        redirect_uri = oauth_redirect_uri('login_google_callback')
        current_app.logger.info('Google login redirect_uri=%s host=%s', redirect_uri, request.host)
        return google.authorize_redirect(redirect_uri)

    @app.route('/login/google/callback')
    def login_google_callback():
        # Google trả lỗi trên query (hủy / redirect_uri_mismatch)
        if request.args.get('error'):
            flash(google_oauth_callback_error_message(), 'danger')
            session.pop('oauth_mode', None)
            return redirect(url_for('login', google_setup=1))

        if session.get('oauth_mode') != 'login':
            host0 = (request.host or '').split(':')[0].lower()
            if host0 in ('127.0.0.1', 'localhost'):
                flash(
                    'Phiên Google bị mất (cookie). Dùng cố định một địa chỉ '
                    f'http://{host0}:5000/login — xóa cookie site, khởi động lại '
                    'app.py (SESSION_COOKIE_SECURE=False), rồi thử lại.',
                    'danger',
                )
            else:
                flash(
                    'Phiên Google bị mất (cookie). Hãy mở https://ketoshop.pro.vn/login '
                    '(không lẫn www) rồi thử lại.',
                    'danger',
                )
            return redirect(url_for('login'))
        try:
            if not configure_google_oauth(google):
                flash("Chưa cấu hình Google OAuth.", "warning")
                return redirect(url_for('login'))
            user_info = fetch_google_user_info(google)
            email = (user_info.get('email') or '').strip().lower()
            if not email:
                flash("Google không trả về email.", "danger")
                return redirect(url_for('login'))

            account = find_user_by_email(email)
            if not account:
                # Chưa có tài khoản → chuyển sang luồng đăng ký dùng thử
                session['trial_google'] = {
                    'email': email,
                    'name': user_info.get('name') or '',
                    'verified_at': datetime.now().isoformat(timespec='seconds'),
                }
                session.permanent = True
                session.modified = True
                flash(
                    "Email Google chưa có tài khoản. Vui lòng hoàn tất đăng ký dùng thử.",
                    "info",
                )
                return redirect(url_for('login', trial_google=1))

            user = account['user']
            db_to_open = account['db_path']
            current_tenant_id = account['tenant_id']
            username = user['username']
            tenant_2fa_enabled = account.get('tenant_2fa_enabled', False)
            is_2fa_enabled = tenant_2fa_enabled if current_tenant_id else bool(user.get('is_2fa_enabled', 0))
            user_role = str(user.get('role', '')).strip()
            is_master_login = user_role == 'master' and current_tenant_id is None

            if is_2fa_enabled:
                from Services.totp_auth import (
                    MASTER_TRUST_DAYS,
                    get_user_totp_secret,
                    is_device_trusted as totp_device_trusted,
                    parse_device_signal,
                )

                if is_master_login:
                    device_signal = parse_device_signal(request.args.get('device_signal'))
                    fingerprint = get_master_device_fingerprint(device_signal)
                    trusted = False
                    totp_ready = False
                    try:
                        with get_main_db_connection() as conn_m:
                            trusted = totp_device_trusted(
                                conn_m, username, fingerprint, trust_days=MASTER_TRUST_DAYS,
                            )
                            totp_ready = bool(get_user_totp_secret(conn_m, int(user['id'])))
                    except Exception as exc:
                        current_app.logger.error("Lỗi thiết bị tin cậy master (Google): %s", exc)

                    if not trusted:
                        session.permanent = True
                        session['pending_auth'] = {
                            'user': {
                                'id': int(user['id']),
                                'username': str(user['username']),
                                'role': user_role,
                                'full_name': str(user.get('full_name') or username),
                                'permissions': str(user.get('permissions') or ''),
                            },
                            'last_tenant_id': current_tenant_id,
                            'db_path': db_to_open,
                            'email': user.get('email') or email,
                            'phone': user.get('phone') or username,
                            'fingerprint': fingerprint,
                            'device_signal': device_signal,
                            'auth_method': 'totp',
                            'totp_ready': totp_ready,
                            'google_allowed': google_login_enabled(),
                        }
                        session.modified = True
                        session.pop('oauth_mode', None)
                        flash("Thiết bị mới — nhập mã Google Authenticator.", "info")
                        return redirect(url_for('verify_totp_page'))
                else:
                    fingerprint = get_device_fingerprint()
                    device_record = None
                    try:
                        with get_main_db_connection() as conn_m:
                            device_record = conn_m.execute(
                                "SELECT last_login FROM user_trusted_devices WHERE username=? AND device_fingerprint=?",
                                (username, fingerprint),
                            ).fetchone()
                    except Exception as exc:
                        current_app.logger.error("Lỗi thiết bị tin cậy: %s", exc)

                    should_ask_2fa = not device_record
                    if device_record and device_record['last_login']:
                        try:
                            last_login_dt = datetime.strptime(device_record['last_login'], '%Y-%m-%d %H:%M:%S')
                            should_ask_2fa = (datetime.now() - last_login_dt) > timedelta(days=3)
                        except ValueError:
                            should_ask_2fa = True

                    if should_ask_2fa:
                        session.permanent = True
                        session['pending_auth'] = {
                            'user': {
                                'id': int(user['id']),
                                'username': str(user['username']),
                                'role': user_role,
                                'full_name': str(user.get('full_name') or username),
                                'permissions': str(user.get('permissions') or ''),
                            },
                            'last_tenant_id': current_tenant_id,
                            'db_path': db_to_open,
                            'email': user.get('email') or email,
                            'phone': user.get('phone') or username,
                            'fingerprint': fingerprint,
                            'auth_method': 'email_otp',
                            'google_allowed': google_login_enabled(),
                        }
                        session.modified = True
                        session.pop('oauth_mode', None)
                        flash("Thiết bị mới — vui lòng xác minh OTP.", "info")
                        return redirect(url_for('login_2fa'))

            # Đăng nhập trực tiếp (thiết bị tin cậy / không bật 2FA)
            fp_final = (
                get_master_device_fingerprint()
                if is_master_login
                else get_device_fingerprint()
            )
            return _finalize_login_from_dict(user, db_to_open, current_tenant_id, fp_final)

        except Exception as exc:
            current_app.logger.error("Google login lỗi: %s", exc, exc_info=True)
            flash(google_oauth_callback_error_message(exc), "danger")
            return redirect(url_for('login', google_setup=1))
        finally:
            session.pop('oauth_mode', None)

    @app.route('/trial/google')
    def trial_google_start():
        """Đăng ký dùng thử — OAuth redirect (tránh lỗi origin_mismatch của nút JS ẩn)."""
        if not google_login_visible():
            flash("Đăng ký Google đang tắt.", "warning")
            return redirect(url_for('login'))
        if not configure_google_oauth(google):
            flash(
                "Chưa cấu hình đủ Google OAuth (cần Client ID + Client Secret trong Master Settings). "
                "Hoặc thêm JavaScript origin vào Google Cloud Console — xem hướng dẫn trên trang đăng nhập.",
                "warning",
            )
            return redirect(url_for('login', google_setup=1))
        session.permanent = True
        session['oauth_mode'] = 'trial_register'
        session.modified = True
        redirect_uri = oauth_redirect_uri('trial_google_callback')
        current_app.logger.info('Trial Google redirect_uri=%s host=%s', redirect_uri, request.host)
        return google.authorize_redirect(redirect_uri)

    @app.route('/trial/google/callback')
    def trial_google_callback():
        from Services.subscription_service import find_account_by_email, tenant_is_expired

        if request.args.get('error'):
            flash(google_oauth_callback_error_message(), 'danger')
            session.pop('oauth_mode', None)
            return redirect(url_for('login', google_setup=1))

        if session.get('oauth_mode') != 'trial_register':
            flash(
                'Phiên đăng ký Google bị mất (cookie). Hãy dùng https://ketoshop.pro.vn/login '
                '(không lẫn www) rồi nhấn lại «Đăng ký dùng thử».',
                'danger',
            )
            return redirect(url_for('login'))
        try:
            if not configure_google_oauth(google):
                flash("Chưa cấu hình Google OAuth.", "warning")
                return redirect(url_for('login'))
            user_info = fetch_google_user_info(google)
            email = (user_info.get('email') or '').strip().lower()
            if not email:
                flash("Google không trả về email.", "danger")
                return redirect(url_for('login'))

            account = find_account_by_email(email, active_only=False)
            if account:
                if not account.get('tenant_active') or tenant_is_expired({
                    'is_active': account.get('tenant_active'),
                    'expiry_date': account.get('expiry_date'),
                }):
                    session['renewal_context'] = {
                        'tenant_id': account['tenant_id'],
                        'email': email,
                        'business_name': account.get('business_name') or '',
                    }
                    flash("Tài khoản đã hết hạn — vui lòng gia hạn.", "info")
                    return redirect(url_for('renewal_page'))
                flash("Email này đã có tài khoản. Hãy dùng «Đăng nhập bằng Google».", "info")
                return redirect(url_for('login'))

            session['trial_google'] = {
                'email': email,
                'name': user_info.get('name') or '',
                'verified_at': datetime.now().isoformat(timespec='seconds'),
            }
            session.permanent = True
            session.modified = True
            return redirect(url_for('login', trial_google=1))
        except Exception as exc:
            current_app.logger.error("Trial Google OAuth lỗi: %s", exc, exc_info=True)
            flash(google_oauth_callback_error_message(exc), "danger")
            return redirect(url_for('login', google_setup=1))
        finally:
            session.pop('oauth_mode', None)

    @app.route('/api/auth/google-setup-hint', methods=['GET'])
    def api_google_setup_hint():
        base = get_public_base_url(request.url_root)
        return jsonify({'success': True, **google_oauth_setup_hints(base)})

    @app.route('/login/google/2fa')
    def login_google_2fa_start():
        if 'pending_auth' not in session:
            return redirect(url_for('login'))
        if not google_login_visible():
            flash("Xác thực Google đang tắt.", "warning")
            return redirect(url_for('login_2fa'))
        if not configure_google_oauth(google):
            flash("Chưa cấu hình Google OAuth. Vào Master Settings → tab Đăng nhập & OTP.", "warning")
            return redirect(url_for('login_2fa'))
        session['oauth_mode'] = '2fa'
        session.modified = True
        return google.authorize_redirect(oauth_redirect_uri('authorize_google_2fa'))

    def _finalize_login_from_dict(user, db_to_open, current_tenant_id, fingerprint):
        """Hoàn tất đăng nhập sau OTP/Google — dùng chung logic redirect."""
        user_role = str(user.get('role', '')).strip()
        new_session_id = str(uuid.uuid4())
        username = user['username']

        _persist_successful_login(
            user['id'], username, current_tenant_id, db_to_open,
            new_session_id, fingerprint, status='Thành công (Google/OTP)',
        )

        session.clear()
        session.permanent = True
        session['user'] = {
            'id': int(user['id']),
            'username': str(user['username']),
            'role': user_role,
            'full_name': str(user.get('full_name') or username),
            'permissions': str(user.get('permissions') or ''),
        }
        session['last_tenant_id'] = current_tenant_id
        session['user_id'] = int(user['id'])
        session['role'] = user_role
        session['db_path'] = db_to_open
        session['session_token'] = new_session_id
        session.modified = True

        user_obj = User(
            id=user['id'],
            username=user['username'],
            role=user_role,
            db_path=db_to_open,
            tenant_id=current_tenant_id,
            full_name=user.get('full_name') or username,
            permissions=user.get('permissions', ''),
        )
        login_user(user_obj, remember=True)
        flash(f"Chào mừng bạn quay lại, {user_obj.full_name}!", "success")

        if current_tenant_id:
            from Services.subscription_service import get_tenant_record, parse_tenant_settings
            rec = get_tenant_record(current_tenant_id, include_inactive=True)
            settings = parse_tenant_settings(rec.get('settings') if rec else {})
            if not settings.get('onboarding_completed'):
                try:
                    return redirect(url_for('onboarding_page'))
                except Exception:
                    return redirect('/onboarding')

        target = login_redirect_target(user_role, current_tenant_id)
        try:
            return redirect(url_for(target))
        except Exception:
            fallbacks = {
                'rental_service': '/rental_service',
                'master_settings': '/master_settings',
            }
            return redirect(fallbacks.get(target, '/sale'))

    @app.route('/authorize-google-2fa')
    def authorize_google_2fa():
        if 'pending_auth' not in session:
            flash("Vui lòng đăng nhập lại.", "warning")
            return redirect(url_for('login'))

        if session.get('oauth_mode') != '2fa':
            flash("Phiên xác thực Google không hợp lệ.", "warning")
            return redirect(url_for('login_2fa'))

        try:
            if not configure_google_oauth(google):
                flash("Chưa cấu hình Google OAuth.", "warning")
                return redirect(url_for('login_2fa'))
            user_info = fetch_google_user_info(google)
            auth = session['pending_auth']
            google_email = (user_info.get('email') or '').strip().lower()
            registered = (auth.get('email') or '').strip().lower()

            if not google_email or google_email != registered:
                flash("Email Google không trùng khớp với email đã đăng ký!", "danger")
                return redirect(url_for('login_2fa'))

            user = auth['user']
            db_path = auth['db_path']
            if db_path and not os.path.isabs(db_path):
                db_path = os.path.join(BASE_DIR, db_path)
            tenant_id = auth.get('last_tenant_id')
            fingerprint = auth.get('fingerprint') or get_device_fingerprint()

            session.pop('oauth_mode', None)
            session.pop('pending_auth', None)
            session.pop('otp_check', None)
            return _finalize_login_from_dict(user, db_path, tenant_id, fingerprint)

        except Exception as e:
            current_app.logger.error("Lỗi Google Auth 2FA: %s", e)
            flash("Xác thực Google thất bại hoặc bị hủy.", "danger")
            return redirect(url_for('login_2fa'))
        finally:
            session.pop('oauth_mode', None)

    #=== HÀM LƯU THIẾT BỊ ĐÃ ĐĂNG NHẬP ===#
    def mark_device_trusted(username, fingerprint):
        g.db_path = None  # Master
        add_trusted_device(username, fingerprint)

    @app.route('/logout')
    def logout():
        try:
            user_info = session.get('user', {})
            username = user_info.get('username', 'Ẩn danh')
            tenant_id = session.get('last_tenant_id')
            user_id = session.get('user_id')

            if user_id:
                try:
                    log_login_attempt(
                        user_id,
                        username,
                        tenant_id,
                        status='Đăng xuất thành công'
                    )
                except Exception as log_err:
                    current_app.logger.error(log_err)

        except Exception as e:
            current_app.logger.error(e)

        # Xóa session
        session.clear()

        # Nếu muốn hiện thông báo
        flash("Bạn đã đăng xuất thành công.", "success")

        try:
            response = redirect(url_for("login"))
        except Exception:
            response = redirect("/login")

        response.delete_cookie("session")

        return response

    # Hàm helper kiểm tra quyền (Dùng cho Backend)
    def check_permission(required_perm):
        if not g.user: return False
        if g.user['role'] == 'admin': return True
        user_perms = g.user.get('permissions', '').split(',')
        return required_perm in user_perms

    # --- ROUTES QUÊN VÀ KHÔI PHỤC PASSWORD---#

    def send_reset_email(to_email, username, reset_url):
        body = f"""Kính gửi Quý khách,

Chúng tôi nhận được yêu cầu khôi phục mật khẩu cho tài khoản: {username}

Vui lòng nhấn vào liên kết bên dưới để thiết lập mật khẩu mới:
{reset_url}

Lưu ý: Liên kết này có hiệu lực trong 60 phút. Nếu bạn không yêu cầu thay đổi này, vui lòng bỏ qua email này.

Trân trọng,
Đội ngũ hỗ trợ KETO ALL IN ONE"""
        ok, err = send_email(to_email, "Khôi phục mật khẩu - KETO ALL IN ONE", body)
        if not ok:
            current_app.logger.error("Lỗi gửi email khôi phục: %s", err)
        return ok

    @app.route('/forgot-password', methods=['GET', 'POST'])
    def forgot_password():
        if request.method == 'POST':
            email_input = request.form.get('email', '').strip()
        
            # 1. Tìm mapping để xác định Tenant và Username
            g.db_path = None 
            main_conn = get_db_connection()
            mapping = main_conn.execute("""
                SELECT username, tenant_id FROM user_tenant_mapping 
                WHERE email = ? AND is_active = 1
            """, (email_input,)).fetchone()
        
            target_username = None
            if mapping:
                target_username = mapping['username']
                tenant_info = main_conn.execute("SELECT db_path FROM tenants WHERE tenant_id = ?", (mapping['tenant_id'],)).fetchone()
                if tenant_info:
                    g.db_path = tenant_info['db_path']
            else:
                # Kiểm tra Master User ở Main DB
                user_main = main_conn.execute("SELECT username FROM users WHERE email = ?", (email_input,)).fetchone()
                if user_main:
                    target_username = user_main['username']
                    g.db_path = None
            main_conn.close()

            if target_username:
                # 2. Tạo Token và lưu vào DB tương ứng
                token = secrets.token_urlsafe(32)
                expiry = (datetime.now() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
            
                try:
                    conn = get_db_connection()
                    conn.execute("UPDATE users SET reset_token = ?, reset_token_expiry = ? WHERE username = ?", 
                                 (token, expiry, target_username))
                    sqlite_commit(conn, label='settings')
                    conn.close()

                    # 3. Tạo link và GỬI MAIL THẬT
                    reset_url = url_for('reset_password', token=token, user=target_username, _external=True)
                
                    if send_reset_email(email_input, target_username, reset_url):
                        flash("Một liên kết khôi phục mật khẩu đã được gửi đến Email của bạn.", "success")
                    else:
                        flash("Lỗi hệ thống khi gửi Email. Vui lòng thử lại sau.", "danger")
                
                    return redirect(url_for('forgot_password'))

                except Exception as e:
                    flash(f"Lỗi database: {e}", "danger")
            else:
                flash("Email không tồn tại trên hệ thống!", "danger")
            
        return render_template('forgot_password.html')

    @app.route('/reset-password', methods=['GET', 'POST'])
    def reset_password():
        token = request.args.get('token')
        username = request.args.get('user') # Lấy username từ URL
    
        if not token or not username:
            flash("Liên kết không hợp lệ.", "danger")
            return redirect(url_for('forgot_password'))

        # BƯỚC 1: Xác định lại DB Path dựa trên username
        tenant_record = get_tenant_by_username(username)
        if tenant_record:
            g.db_path = tenant_record['db_path']
            g.tenant_id = tenant_record['tenant_id']
        else:
            # Nếu không thấy trong mapping, mặc định là Main DB
            g.db_path = None
            g.tenant_id = 'MAIN'

        try:
            conn = get_db_connection() # Sử dụng hàm của bạn
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
            # Kiểm tra token
            user_row = conn.execute("""
                SELECT id FROM users WHERE username = ? AND reset_token = ? AND reset_token_expiry > ?
            """, (username, token, now)).fetchone()

            if not user_row:
                conn.close()
                flash("Link đã hết hạn hoặc không hợp lệ.", "danger")
                return redirect(url_for('forgot_password'))

            if request.method == 'POST':
                new_pw = request.form.get('password')
                from flask_bcrypt import generate_password_hash
                hashed_pw = generate_password_hash(new_pw).decode('utf-8')
            
                conn.execute("""
                    UPDATE users SET password = ?, reset_token = NULL, reset_token_expiry = NULL 
                    WHERE id = ?
                """, (hashed_pw, user_row['id']))
                sqlite_commit(conn, label='settings')
                conn.close()
            
                flash("Đặt lại mật khẩu thành công!", "success")
                return redirect(url_for('login'))
            
            conn.close()
        except Exception as e:
            flash(f"Lỗi: {e}", "danger")

        return render_template('reset_password.html')

    # API: Lấy danh sách users — chỉ admin / master (không cho manager)
    @app.route('/api/settings/list_users', methods=['GET'])
    @admin_or_master_required
    def list_users():
        from auth import is_master_configuring_firm_tenant
        if is_master_configuring_firm_tenant():
            tid = (session.get('master_viewing_tenant') or '').strip()
            from Services.firm_tenant import list_firm_users_for_settings
            return jsonify(list_firm_users_for_settings(tid))

        from Services.sme.branches import ensure_sme_branches_schema
        from Services.sme_roles import PERMISSION_BYPASS_ROLES
        from Services.user_branch import ensure_user_branch_schema

        db = get_db_connection()
        try:
            ensure_sme_branches_schema(db, commit=False)
        except Exception:
            pass
        try:
            ensure_user_branch_schema(db, commit=False)
        except Exception:
            pass

        cursor = db.execute(
            "SELECT id, username, full_name, role, email, phone, permissions FROM users"
        )
        users = [dict(row) for row in cursor.fetchall()]

        # Preload branch names for display
        branch_rows = []
        try:
            branch_rows = db.execute("SELECT code, name FROM sme_branches").fetchall()
        except Exception:
            branch_rows = []
        branch_name_map = {}
        for r in branch_rows:
            code = (r["code"] if hasattr(r, "keys") else r[0]) or ""
            name = (r["name"] if hasattr(r, "keys") else r[1]) or ""
            branch_name_map[str(code).strip().upper()] = str(name).strip()

        # Preload user -> branch_codes mapping (only for non-bypass users)
        user_branch_map: dict[int, list[str]] = {}
        try:
            ub_rows = db.execute(
                "SELECT user_id, branch_code FROM user_branches"
            ).fetchall()
            for r in ub_rows:
                uid = int(r["user_id"] if hasattr(r, "keys") else r[0])
                bc = str(r["branch_code"] if hasattr(r, "keys") else r[1] or '').strip().upper()
                if not bc:
                    continue
                user_branch_map.setdefault(uid, []).append(bc)
        except Exception:
            user_branch_map = {}

        for u in users:
            uid = int(u.get("id") or 0)
            role = str(u.get("role") or '').strip()
            if role in PERMISSION_BYPASS_ROLES:
                u["branch_names"] = "Tất cả chi nhánh"
            else:
                codes = user_branch_map.get(uid) or []
                if not codes:
                    u["branch_names"] = "Chưa gán"
                else:
                    names = []
                    for c in codes:
                        n = branch_name_map.get(str(c).strip().upper(), '') or str(c)
                        names.append(n)
                    # Giới hạn hiển thị để không dài
                    uniq_names = []
                    for n in names:
                        if n not in uniq_names:
                            uniq_names.append(n)
                    if len(uniq_names) > 3:
                        u["branch_names"] = ', '.join(uniq_names[:3]) + f' + {len(uniq_names) - 3} khác'
                    else:
                        u["branch_names"] = ', '.join(uniq_names)

        return jsonify(users)

    # API: Lưu hoặc Cập nhật User — chỉ admin / master (không cho manager dù có edit_data)
    @app.route('/api/settings/save_user', methods=['POST'])
    @admin_or_master_required
    def save_user():
        data = request.json
   
        user_id     = data.get('user_id')
        username    = data.get('username')          # ← thường là số điện thoại
        email       = data.get('email', '').strip()
        full_name   = data.get('full_name', '').strip()
        phone       = data.get('phone', '').strip()
        password    = data.get('password')
        role        = data.get('role')
        permissions = data.get('permissions')

        if not username:
            return jsonify({"success": False, "error": "Tên đăng nhập không được để trống"}), 400

        from Services.sme_roles import validate_assignable_role
        from Services.tenant_profile import get_current_tenant_profile
        profile = get_current_tenant_profile() or {}
        actor_role = str(session.get('role') or '').strip()
        existing_role = None
        conn = get_db_connection()   # DB của tenant

        try:
            from Services.audit_log import write_audit
            if user_id:
                old_preview = conn.execute(
                    "SELECT role FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
                if old_preview:
                    existing_role = old_preview['role'] if not isinstance(old_preview, tuple) else old_preview[0]
            ok, err = validate_assignable_role(
                role,
                profile.get('accounting_regime'),
                actor_is_master=(actor_role == 'master'),
                existing_role=existing_role,
            )
            if not ok:
                return jsonify({"success": False, "error": err}), 400
            if user_id:  # ================== CẬP NHẬT ==================
                old_row = conn.execute(
                    "SELECT id, username, full_name, email, phone, role, permissions FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
                old_data = dict(old_row) if old_row else None

                old_email = old_data.get('email') if old_data else None

                # ... phần UPDATE users giữ nguyên như cũ của bạn ...

                if password and password.strip() != "":
                    hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
                    conn.execute("""UPDATE users SET username=?, full_name=?, email=?, phone=?, 
                                    password=?, role=?, permissions=? WHERE id=?""",
                                 (username, full_name, email, phone, hashed_pw, role, permissions, user_id))
                else:
                    conn.execute("""UPDATE users SET username=?, full_name=?, email=?, phone=?, 
                                    role=?, permissions=? WHERE id=?""",
                                 (username, full_name, email, phone, role, permissions, user_id))

                # Cập nhật mapping
                if old_email and email and old_email.strip().lower() != email.strip().lower():
                    update_user_email_in_mapping(old_email=old_email, new_email=email, username=username, tenant_id=g.tenant_id)

                # Cập nhật lại mapping với username + email mới nhất
                add_user_to_mapping(username, email, g.tenant_id)
                write_audit(
                    'update', 'users',
                    f"Cập nhật người dùng {username}",
                    entity_type='user', entity_id=user_id, entity_label=username,
                    old_data=old_data,
                    new_data={'username': username, 'full_name': full_name, 'email': email, 'role': role},
                )

            else:  # ================== TẠO MỚI ==================
                if not password or password.strip() == "":
                    return jsonify({"success": False, "error": "Mật khẩu không được để trống khi tạo mới"}), 400

                hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
                conn.execute("""
                    INSERT INTO users (username, full_name, email, phone, password, role, permissions)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (username, full_name, email, phone, hashed_pw, role, permissions))

                # Thêm vào mapping khi tạo mới
                add_user_to_mapping(username, email, g.tenant_id)
                write_audit(
                    'create', 'users',
                    f"Tạo người dùng {username} ({role})",
                    entity_type='user', entity_label=username,
                    new_data={'username': username, 'full_name': full_name, 'email': email, 'role': role},
                )

            from db_utils import begin_immediate, sqlite_write_retry, locked_user_message, _is_locked_error

            def _commit_user():
                begin_immediate(conn, label='save_user')
                sqlite_commit(conn, label='settings')

            sqlite_write_retry(_commit_user, label='save_user')
            return jsonify({"success": True, "message": "Đã lưu thông tin nhân viên thành công"})

        except sqlite3.OperationalError as e:
            if conn:
                conn.rollback()
            if _is_locked_error(e):
                return jsonify({"success": False, "error": locked_user_message()}), 503
            return jsonify({"success": False, "error": "Lỗi hệ thống: " + str(e)}), 500
        except Exception as e:
            if conn:
                conn.rollback()
            error_msg = str(e)
            if "UNIQUE constraint failed" in error_msg:
                if "username" in error_msg:
                    return jsonify({"success": False, "error": "Tên đăng nhập đã tồn tại"}), 409
                elif "email" in error_msg:
                    return jsonify({"success": False, "error": "Email đã được sử dụng"}), 409
            return jsonify({"success": False, "error": "Lỗi hệ thống: " + error_msg}), 500
        finally:
            if conn:
                conn.close()

    # --- API XÓA USER — chỉ admin / master (không cho manager dù có delete_data) ---
    @app.route('/api/settings/delete_user/<int:user_id>', methods=['DELETE'])
    @admin_or_master_required
    def delete_user_api(user_id):
        # Ngăn việc Admin tự xóa chính mình
        if session.get('user', {}).get('id') == user_id:
            return jsonify({"success": False, "error": "Bạn không thể tự xóa chính mình!"}), 400

        conn = get_db_connection()
        try:
            from db_utils import begin_immediate, sqlite_write_retry, locked_user_message, _is_locked_error
            from Services.audit_log import write_audit
            old_row = conn.execute(
                "SELECT id, username, full_name, role, email FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            old_data = dict(old_row) if old_row else None

            def _delete_user():
                begin_immediate(conn, label='delete_user')
                conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                sqlite_commit(conn, label='settings')

            sqlite_write_retry(_delete_user, label='delete_user')
            if old_data:
                write_audit(
                    'delete', 'users',
                    f"Xóa người dùng {old_data.get('username')}",
                    entity_type='user', entity_id=user_id,
                    entity_label=old_data.get('username'),
                    old_data=old_data,
                )
            return jsonify({"success": True, "message": "Đã xóa nhân viên"})
        except sqlite3.OperationalError as e:
            if _is_locked_error(e):
                return jsonify({"success": False, "error": locked_user_message()}), 503
            return jsonify({"success": False, "error": str(e)}), 500
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            conn.close()
    #============================================================================= Start of Settings Page ==========================================================================#
    # ====================== MASTER SETTINGS ======================
    # Chỉ cho phép user có role = 'master' truy cập
    @app.route('/master/settings')
    @login_required
    @master_required
    def master_settings():
        """Trang Master Settings - Quản trị toàn hệ thống"""
        with get_main_db_connection() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT id, tenant_id, db_path, business_name, phone,
                       address, email, is_active, created_at
                FROM tenants
                ORDER BY created_at DESC
            """)
            tenants = [dict(row) for row in c.fetchall()]

            tenants_dir = os.path.join(BASE_DIR, 'tenants')
            tenant_files = 0
            if os.path.isdir(tenants_dir):
                tenant_files = len([
                    f for f in os.listdir(tenants_dir)
                    if f.endswith('.db') and f != 'registry.db'
                ])

            return render_template(
                'master_settings.html',
                tenants=tenants,
                tenant_count=tenant_files,
            )


    # API lấy danh sách tenant (dùng cho AJAX)
    @app.route('/api/master/tenants', methods=['GET'])
    @login_required
    @master_required
    def api_master_tenants():
        """Lấy danh sách tất cả các tenants cho giao diện quản trị Master"""
        try:
            from Services.firm_tenant import ensure_firm_schema
            ensure_firm_schema()
            # Luôn đọc registry trên MAIN DB
            with get_main_db_connection() as conn:
                c = conn.cursor()
                c.execute("""
                    SELECT id, tenant_id, db_path, business_name, phone, email, address,
                           expiry_date, is_active, is_2fa_enabled, created_at, settings, business_type,
                           COALESCE(tenant_type, 'standalone') AS tenant_type,
                           COALESCE(max_clients, 50) AS max_clients
                    FROM tenants
                    ORDER BY created_at DESC
                """)
                rows = c.fetchall()
                from Services.tenant_profile import build_profile_from_registry
                from Services.firm_tenant import (
                    TENANT_TYPE_FIRM,
                    firm_usage_summary,
                    resolve_firm_package,
                )
                from Services.subscription_service import parse_tenant_settings

                firm_counts: dict[str, int] = {}
                firm_ids = [dict(r)['tenant_id'] for r in rows if dict(r).get('tenant_type') == TENANT_TYPE_FIRM]
                if firm_ids:
                    placeholders = ','.join('?' * len(firm_ids))
                    cnt_rows = c.execute(f"""
                        SELECT firm_tenant_id, COUNT(*) AS c
                        FROM firm_clients
                        WHERE status = 'active' AND firm_tenant_id IN ({placeholders})
                        GROUP BY firm_tenant_id
                    """, firm_ids).fetchall()
                    firm_counts = {r['firm_tenant_id']: int(r['c']) for r in cnt_rows}

                tenants = []
                for row in rows:
                    tenant_dict = dict(row)
                    tenant_dict['is_active'] = 1 if tenant_dict.get('is_active') else 0
                    tenant_dict['is_2fa_enabled'] = 1 if tenant_dict.get('is_2fa_enabled') else 0
                    profile = build_profile_from_registry(tenant_dict)
                    tenant_dict['revenue_tier'] = profile.get('revenue_tier')
                    tenant_dict['enabled_nn_sectors'] = profile.get('enabled_nn_sectors') or []
                    tenant_dict['business_line'] = profile.get('business_line')
                    st = parse_tenant_settings(tenant_dict.get('settings'))
                    tenant_dict['firm_package_id'] = st.get('firm_package_id')
                    if tenant_dict.get('tenant_type') == TENANT_TYPE_FIRM:
                        active = firm_counts.get(tenant_dict['tenant_id'], 0)
                        usage = firm_usage_summary(
                            tenant_dict['tenant_id'],
                            tenant_dict.get('max_clients'),
                        )
                        usage['active_clients'] = active
                        if not usage['unlimited']:
                            usage['usage_label'] = f"{active} / {usage['max_clients']}"
                            usage['at_capacity'] = active >= usage['max_clients']
                        else:
                            usage['usage_label'] = f'{active} / ∞'
                        pkg = resolve_firm_package(
                            tenant_dict.get('firm_package_id'),
                            tenant_dict.get('max_clients'),
                        )
                        tenant_dict['firm_package_label'] = pkg.get('label') or usage['package_label']
                        tenant_dict['firm_usage'] = usage
                    tenants.append(tenant_dict)

            return jsonify({
                "success": True,
                "data": tenants,
                "count": len(tenants)
            })

        except sqlite3.Error as e:
            return jsonify({
                "success": False,
                "error": f"Lỗi database: {str(e)}"
            }), 500
        except Exception as e:
            return jsonify({
                "success": False,
                "error": f"Lỗi hệ thống: {str(e)}"
            }), 500

    @app.route('/api/master/firm_packages', methods=['GET'])
    @login_required
    @master_required
    def api_master_firm_packages():
        from Services.firm_tenant import list_firm_client_packages
        return jsonify({'success': True, 'packages': list_firm_client_packages()})


    # ====================== MAIN DATABASE 2FA TOGGLE ======================

    @app.route('/api/master/main_2fa_status', methods=['GET'])
    def get_main_2fa_status():
        """Lấy trạng thái 2FA hiện tại của Main Database (Users)"""
        if 'user' not in session or session.get('role') != 'master':
            return jsonify({"success": False, "error": "Chỉ Master mới có quyền"}), 403

        try:
            with get_main_db_connection() as conn:
                result = conn.execute("""
                    SELECT
                        COUNT(*) as total_users,
                        SUM(CASE WHEN is_2fa_enabled = 1 THEN 1 ELSE 0 END) as enabled_users
                    FROM users
                """).fetchone()

            total = result['total_users'] or 0
            enabled = result['enabled_users'] or 0

            # Nếu tất cả users đều bật → coi như ON
            is_enabled = (total > 0 and enabled == total)

            return jsonify({
                "success": True,
                "is_2fa_enabled": is_enabled,
                "total_users": total,
                "enabled_users": enabled
            })

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route('/api/master/toggle_main_2fa', methods=['POST'])
    def toggle_main_2fa():
        """Bật/Tắt 2FA cho TẤT CẢ users trong Main Database (database.db)"""
    
        # 1. Kiểm tra quyền Master
        if 'user' not in session or session.get('role') != 'master':
            return jsonify({
                "success": False, 
                "error": "Chỉ Master mới có quyền thực hiện thao tác này."
            }), 403

        # 2. Xử lý dữ liệu đầu vào
        data = request.get_json(silent=True) or {}
        if 'is_2fa_enabled' not in data:
            return jsonify({
                "success": False,
                "error": "Thiếu tham số is_2fa_enabled",
            }), 400

        new_value = data.get('is_2fa_enabled')
        if isinstance(new_value, bool):
            is_2fa_enabled = 1 if new_value else 0
        elif isinstance(new_value, (int, float)):
            is_2fa_enabled = 1 if int(new_value) else 0
        else:
            s = str(new_value or '').strip().lower()
            if s in ('1', 'true', 'on', 'yes'):
                is_2fa_enabled = 1
            elif s in ('0', 'false', 'off', 'no'):
                is_2fa_enabled = 0
            else:
                return jsonify({
                    "success": False,
                    "error": "Giá trị is_2fa_enabled không hợp lệ.",
                }), 400

        action = "bật" if is_2fa_enabled == 1 else "tắt"

        # 3. Thực thi cập nhật Database
        try:
            from db_utils import sqlite_write_retry

            def _write():
                with get_main_db_connection() as conn:
                    conn.execute(
                        "UPDATE users SET is_2fa_enabled = ?",
                        (is_2fa_enabled,),
                    )
                    sqlite_commit(conn, label='settings')

            sqlite_write_retry(_write, label='toggle_main_2fa')

            return jsonify({
                "success": True,
                "message": f"Đã {action} xác thực 2 bước cho **tất cả users** trong hệ thống chính.",
                "is_2fa_enabled": is_2fa_enabled
            })

        except sqlite3.Error as e:
            return jsonify({
                "success": False, 
                "error": f"Lỗi Database: {str(e)}"
            }), 500
        except Exception as e:
            return jsonify({
                "success": False, 
                "error": f"Lỗi hệ thống: {str(e)}"
            }), 500

    @app.route('/api/master/totp/status', methods=['GET'])
    @login_required
    def api_master_totp_status():
        if session.get('role') != 'master':
            return jsonify({'success': False, 'error': 'Chỉ Master'}), 403
        from Services.totp_auth import MASTER_TRUST_DAYS, totp_status_for_user
        try:
            with get_main_db_connection() as conn:
                status = totp_status_for_user(conn, int(session.get('user_id') or 0))
            status['success'] = True
            status['trust_days'] = MASTER_TRUST_DAYS
            return jsonify(status)
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/master/totp/setup', methods=['POST'])
    @login_required
    def api_master_totp_setup():
        if session.get('role') != 'master':
            return jsonify({'success': False, 'error': 'Chỉ Master'}), 403
        from Services.totp_auth import generate_totp_secret, provisioning_uri, qr_png_data_url
        secret = generate_totp_secret()
        session['master_totp_setup_secret'] = secret
        session.modified = True
        username = (session.get('user') or {}).get('username') or session.get('username') or 'master'
        uri = provisioning_uri(secret, username)
        return jsonify({
            'success': True,
            'secret': secret,
            'otpauth_uri': uri,
            'qr_data_url': qr_png_data_url(uri),
        })

    @app.route('/api/master/totp/confirm', methods=['POST'])
    @login_required
    def api_master_totp_confirm():
        if session.get('role') != 'master':
            return jsonify({'success': False, 'error': 'Chỉ Master'}), 403
        data = request.get_json(silent=True) or {}
        code = data.get('code') or data.get('totp_code') or ''
        secret = session.get('master_totp_setup_secret')
        from Services.totp_auth import save_totp_secret, verify_totp_code
        if not secret or not verify_totp_code(secret, code):
            return jsonify({'success': False, 'error': 'Mã xác nhận không đúng'}), 400
        try:
            with get_main_db_connection() as conn:
                from db_utils import begin_immediate, sqlite_commit
                begin_immediate(conn, label='master_totp_confirm')
                save_totp_secret(conn, int(session.get('user_id') or 0), secret)
                sqlite_commit(conn, label='master_totp_confirm')
            session.pop('master_totp_setup_secret', None)
            return jsonify({'success': True, 'message': 'Đã bật Google Authenticator cho Master'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/master/totp/disable', methods=['POST'])
    @login_required
    def api_master_totp_disable():
        if session.get('role') != 'master':
            return jsonify({'success': False, 'error': 'Chỉ Master'}), 403
        data = request.get_json(silent=True) or {}
        code = data.get('code') or ''
        from Services.totp_auth import clear_totp_secret, get_user_totp_secret, verify_totp_code
        try:
            with get_main_db_connection() as conn:
                secret = get_user_totp_secret(conn, int(session.get('user_id') or 0))
                if secret and not verify_totp_code(secret, code):
                    return jsonify({'success': False, 'error': 'Mã Authenticator không đúng'}), 400
                from db_utils import begin_immediate, sqlite_commit
                begin_immediate(conn, label='master_totp_disable')
                clear_totp_secret(conn, int(session.get('user_id') or 0))
                conn.execute(
                    'UPDATE users SET is_2fa_enabled = 0 WHERE id = ?',
                    (int(session.get('user_id') or 0),),
                )
                sqlite_commit(conn, label='master_totp_disable')
            return jsonify({'success': True, 'message': 'Đã tắt Authenticator'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    def _master_auth_settings_payload(cfg):
        db_cfg = cfg if "google_client_secret" in cfg else get_auth_settings_db()
        cid = (db_cfg.get("google_client_id") or "").strip()
        return {
            **{k: v for k, v in db_cfg.items() if k not in ("google_client_secret", "sms_api_key", "sms_api_secret")},
            "has_google_secret": bool(db_cfg.get("google_client_secret")),
            "has_sms_key": bool(db_cfg.get("sms_api_key")),
            "has_sms_secret": bool(db_cfg.get("sms_api_secret")),
            "google_ready": google_login_enabled(),
            "google_config_error": google_client_id_error(cid),
            "sms_ready": sms_otp_enabled(),
        }

    @app.route('/api/master/auth_settings', methods=['GET'])
    @login_required
    @master_required
    def get_master_auth_settings():
        return jsonify({
            "success": True,
            "settings": _master_auth_settings_payload(get_auth_settings_db()),
        })

    @app.route('/api/master/auth_settings', methods=['POST'])
    @login_required
    @master_required
    def save_master_auth_settings():
        data = request.get_json() or {}
        if data.get("auth_google_enabled") in (True, 1, "1", "true"):
            cid = (data.get("google_client_id") or "").strip()
            if not cid:
                cid = (get_auth_settings_db().get("google_client_id") or "").strip()
            if not cid:
                return jsonify({"success": False, "error": "Vui lòng nhập Google Client ID."}), 400
            if cid.startswith("GOCSPX-"):
                return jsonify({
                    "success": False,
                    "error": "Nhầm Client Secret (GOCSPX-...) vào ô Client ID.",
                }), 400
            if ".apps.googleusercontent.com" not in cid:
                return jsonify({
                    "success": False,
                    "error": "Client ID phải có dạng 123456789-xxxx.apps.googleusercontent.com",
                }), 400
        try:
            saved = save_auth_settings(data)
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        configure_google_oauth(google)
        return jsonify({
            "success": True,
            "message": "Đã lưu cấu hình đăng nhập & OTP.",
            "settings": _master_auth_settings_payload(saved),
        })

    @app.route('/api/master/tax-rates', methods=['GET'])
    @login_required
    @master_required
    def api_master_tax_rates_list():
        from Services.tax_rate_helpers import ensure_tax_rate_schema, list_tax_rate_schedules
        ensure_tax_rate_schema()
        return jsonify({'success': True, 'data': list_tax_rate_schedules()})

    @app.route('/api/master/tax-rates', methods=['POST'])
    @login_required
    @master_required
    def api_master_tax_rates_add():
        from Services.tax_rate_helpers import add_tax_rate_schedule
        data = request.get_json(silent=True) or {}
        try:
            row = add_tax_rate_schedule(
                data,
                created_by=session.get('username') or 'master',
            )
            return jsonify({'success': True, 'data': row, 'message': 'Đã thêm mức thuế mới'})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/master/toggle_2fa/<tenant_id>', methods=['POST'])
    def toggle_tenant_2fa(tenant_id):
        # 1. Kiểm tra quyền Master
        if 'user' not in session or session.get('role') != 'master':
            return jsonify({
                "success": False,
                "error": "Bạn không có quyền thực hiện thao tác này. Chỉ Master mới được phép."
            }), 403

        # 2. Lấy và kiểm tra dữ liệu đầu vào
        data = request.get_json(silent=True)
        if data is None or 'is_2fa_enabled' not in data:
            return jsonify({
                "success": False,
                "error": "Thiếu tham số is_2fa_enabled"
            }), 400

        # 3. Ép kiểu linh hoạt cho boolean/string/int
        new_value = data['is_2fa_enabled']
        # Chấp nhận các giá trị: True, 1, "true", "on", "1"
        if isinstance(new_value, bool):
            is_2fa_enabled = 1 if new_value else 0
        else:
            is_2fa_enabled = 1 if str(new_value).lower() in ['1', 'true', 'on', 'yes'] else 0

        try:
            from db_utils import sqlite_write_retry
            missing = {'v': False}

            def _write():
                with get_main_db_connection() as conn:
                    tenant_check = conn.execute(
                        "SELECT tenant_id FROM tenants WHERE tenant_id = ?",
                        (tenant_id,),
                    ).fetchone()
                    if not tenant_check:
                        missing['v'] = True
                        return
                    conn.execute("""
                        UPDATE tenants
                        SET is_2fa_enabled = ?
                        WHERE tenant_id = ?
                    """, (is_2fa_enabled, tenant_id))
                    sqlite_commit(conn, label='settings')

            sqlite_write_retry(_write, label='toggle_tenant_2fa')
            if missing['v']:
                return jsonify({
                    "success": False,
                    "error": f"Tenant '{tenant_id}' không tồn tại."
                }), 404

            status_text = "bật" if is_2fa_enabled == 1 else "tắt"
            return jsonify({
                "success": True,
                "message": f"Đã {status_text} xác thực 2 bước cho tenant **{tenant_id}** thành công.",
                "tenant_id": tenant_id,
                "is_2fa_enabled": is_2fa_enabled
            })

        except sqlite3.Error as e:
            return jsonify({
                "success": False,
                "error": f"Lỗi database: {str(e)}"
            }), 500
        except Exception as e:
            return jsonify({
                "success": False,
                "error": f"Lỗi hệ thống: {str(e)}"
            }), 500

    @app.route('/api/master/create_tenant', methods=['POST'])
    @login_required
    @master_required
    def api_create_tenant():
        data = request.get_json() or {}
        tenant_id = data.get('tenant_id', '').strip().lower()
        business_name = data.get('business_name', 'Cửa Hàng Mới').strip()
        phone = data.get('phone', '').strip()

        if not tenant_id or not phone:
            return jsonify({"success": False, "error": "Vui lòng nhập Tenant ID và Số điện thoại"}), 400

        tenant_type = (data.get('tenant_type') or 'standalone').strip().lower()
        if tenant_type == 'firm':
            from Services.firm_tenant import provision_firm_tenant
            owner_email = (data.get('email') or '').strip()
            owner_password = data.get('customer_password') or data.get('owner_password') or 'admin'
            if not owner_email:
                return jsonify({'success': False, 'error': 'Đơn Vị Dịch Vụ Kế Toán cần email owner (đăng nhập)'}), 400
            from Services.firm_tenant import resolve_firm_package
            pkg = resolve_firm_package(
                data.get('firm_package_id'),
                data.get('max_clients'),
            )
            max_clients = pkg['max_clients']
            regime = (data.get('accounting_regime') or 'SME_TT99').strip()
            firm_client_regimes = data.get('firm_client_regimes') or ['SME_MICRO_TT58', 'SME_TT99']
            firm_client_regimes = [
                r for r in firm_client_regimes
                if r in ('SME_MICRO_TT58', 'SME_TT99')
            ] or ['SME_MICRO_TT58', 'SME_TT99']
            result = provision_firm_tenant(
                tenant_id,
                business_name,
                phone,
                owner_email=owner_email,
                owner_password=owner_password,
                owner_name=(data.get('representative_name') or business_name).strip(),
                address=(data.get('address') or '').strip(),
                tax_code=(data.get('tax_code') or '').strip(),
                expiry_date=data.get('expiry_date'),
                max_clients=max_clients,
                accounting_regime=regime,
                extra_settings={
                    'firm_client_regimes': firm_client_regimes,
                    'firm_package_id': pkg['id'],
                    'firm_package_label': pkg['label'],
                },
            )
            if not result.get('success'):
                return jsonify(result), 400
            from Services.audit_log import write_audit
            write_audit(
                'create', 'tenant',
                f"Tạo tenant DVKT {tenant_id} ({business_name})",
                entity_type='tenant', entity_id=tenant_id, entity_label=business_name,
                new_data={'tenant_id': tenant_id, 'tenant_type': 'firm', 'owner_email': owner_email},
                tenant_id=tenant_id,
                use_main=True,
            )
            return jsonify({
                'success': True,
                'message': (
                    f"Đơn Vị Dịch Vụ Kế Toán '{business_name}' đã sẵn sàng. "
                    f"Owner: {owner_email} — gói {pkg['label']}."
                    + (
                        f" Đã gửi email thông tin đăng nhập tới {owner_email}."
                        if result.get('email_sent')
                        else (
                            f" Chưa gửi được email: {result.get('email_error') or 'lỗi không xác định'}."
                            if result.get('email_error')
                            else ''
                        )
                    )
                ),
                'tenant_id': tenant_id,
                'tenant_type': 'firm',
                'email_sent': result.get('email_sent'),
                'email_error': result.get('email_error'),
            })

        from Services.subscription_service import provision_tenant
        from Services.tenant_profile import is_sme_regime

        try:
            regime = (data.get('accounting_regime') or 'HKD').strip()
            sme = is_sme_regime(regime)
            extra_settings = None
            if sme:
                from Services.tenant_profile import (
                    normalize_vat_filing_period,
                    default_vat_filing_period_for_regime,
                )
                from Services.sme.micro_enterprise import (
                    check_tt58_provision_eligibility,
                    normalize_enterprise_sector,
                )
                sector = normalize_enterprise_sector(data.get('enterprise_sector'))
                band = (data.get('sme_revenue_band') or '').strip()
                provision = check_tt58_provision_eligibility(
                    accounting_regime=regime,
                    enterprise_sector=sector,
                    sme_revenue_band=band,
                )
                if provision.get('warn'):
                    return jsonify({'success': False, 'error': provision.get('message')}), 400
                fp = normalize_vat_filing_period(
                    data.get('vat_filing_period') or data.get('filing_period'),
                    default=default_vat_filing_period_for_regime(regime),
                )
                extra_settings = {
                    'vat_filing_period': fp,
                    'filing_period': fp,
                    'enterprise_sector': sector,
                }
                if band:
                    extra_settings['sme_revenue_band'] = band
            result = provision_tenant(
                tenant_id,
                business_name,
                phone,
                email=data.get('email', '').strip(),
                address=data.get('address', '').strip(),
                tax_code=data.get('tax_code', '').strip(),
                business_line=(data.get('business_line') or 'pos').strip(),
                hkd_sector='' if sme else (data.get('hkd_sector') or data.get('primary_nn_sector') or 'G1').strip(),
                revenue_tier=None if sme else (data.get('revenue_tier') or 'DT1').strip(),
                accounting_regime=regime,
                expiry_date=data.get('expiry_date'),
                representative_name=data.get('representative_name', '').strip(),
                subscription_plan=(data.get('subscription_plan') or '').strip(),
                customer_password=data.get('customer_password') or 'admin',
                enabled_nn_sectors=[] if sme else data.get('enabled_nn_sectors'),
                extra_settings=extra_settings,
            )
            if not result.get('success'):
                return jsonify(result), 400

            from Services.audit_log import write_audit
            write_audit(
                'create', 'tenant',
                f"Tạo tenant {tenant_id} ({business_name})",
                entity_type='tenant', entity_id=tenant_id, entity_label=business_name,
                new_data={
                    'tenant_id': tenant_id,
                    'phone': phone,
                    'business_name': business_name,
                    'revenue_tier': result.get('revenue_tier'),
                    'accounting_regime': result.get('accounting_regime'),
                },
                tenant_id=tenant_id,
                use_main=True,
            )

            return jsonify({
                "success": True,
                "message": f"Hệ thống cho '{business_name}' đã sẵn sàng!",
                "tenant_id": tenant_id,
            })
        except Exception as e:
            return jsonify({"success": False, "error": f"Lỗi hệ thống: {str(e)}"}), 500

    @app.route('/api/master/edit_tenant/<tenant_id>', methods=['PUT'])
    @login_required
    @master_required
    def api_edit_tenant(tenant_id):
        data = request.get_json() or {}
        business_name = data.get('business_name', '').strip()
        phone = data.get('phone', '').strip()
        address = data.get('address', '').strip()
        email = data.get('email', '').strip()
        expiry_date = data.get('expiry_date')
        revenue_tier = (data.get('revenue_tier') or '').strip()
        enabled_nn_sectors = data.get('enabled_nn_sectors')
        hkd_sector = (data.get('hkd_sector') or data.get('primary_nn_sector') or '').strip()
        business_line = (data.get('business_line') or '').strip()
        accounting_regime = (data.get('accounting_regime') or '').strip()

        from Services.hkd_sector import normalize_enabled_nn_sectors, normalize_nn_code

        conn = get_main_db_connection()
        c = conn.cursor()

        try:
            old_row = c.execute(
                "SELECT tenant_id, business_name, phone, address, email, expiry_date, settings FROM tenants WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            old_data = dict(old_row) if old_row else None
            if not old_row:
                conn.close()
                return jsonify({"success": False, "error": "Không tìm thấy tenant"}), 404

            from Services.firm_tenant import (
                TENANT_TYPE_FIRM,
                count_firm_active_clients,
                is_firm_unlimited,
                resolve_firm_package,
            )
            tenant_type_row = c.execute(
                "SELECT COALESCE(tenant_type, 'standalone') AS tenant_type FROM tenants WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
            is_firm = (tenant_type_row['tenant_type'] if tenant_type_row else 'standalone') == TENANT_TYPE_FIRM
            if is_firm and (data.get('firm_package_id') is not None or data.get('max_clients') is not None):
                pkg = resolve_firm_package(data.get('firm_package_id'), data.get('max_clients'))
                active = count_firm_active_clients(tenant_id)
                if not is_firm_unlimited(pkg['max_clients']) and active > pkg['max_clients']:
                    conn.close()
                    return jsonify({
                        'success': False,
                        'error': (
                            f"Không thể hạ gói: đang phục vụ {active} DN, "
                            f"gói mới chỉ cho phép {pkg['max_clients']}."
                        ),
                    }), 400
                c.execute(
                    "UPDATE tenants SET max_clients = ? WHERE tenant_id = ?",
                    (pkg['max_clients'], tenant_id),
                )
                from Services.tenant_profile import update_registry_settings
                update_registry_settings(tenant_id, {
                    'firm_package_id': pkg['id'],
                    'firm_package_label': pkg['label'],
                    'max_clients': pkg['max_clients'],
                }, conn=conn)

            old_settings = {}
            try:
                from Services.subscription_service import parse_tenant_settings
                old_settings = parse_tenant_settings(
                    old_row['settings'] if hasattr(old_row, 'keys') else old_row[6]
                ) or {}
            except Exception:
                old_settings = {}

            c.execute("""
                UPDATE tenants 
                SET business_name = ?, phone = ?, address = ?, email = ?, expiry_date = ?
                WHERE tenant_id = ?
            """, (business_name, phone, address, email, expiry_date, tenant_id))

            settings_patch = {}
            from Services.tenant_profile import is_sme_regime, update_registry_settings

            if accounting_regime and is_sme_regime(accounting_regime):
                settings_patch['accounting_regime'] = accounting_regime
                settings_patch['revenue_tier'] = None
                settings_patch['revenue_tier_declared'] = None
                settings_patch['revenue_tier_effective'] = None
                settings_patch['enabled_nn_sectors'] = []
                settings_patch['primary_nn_sector'] = None
                settings_patch['default_hkd_sector'] = None
                from Services.tenant_profile import (
                    normalize_accounting_regime,
                    normalize_vat_filing_period,
                    default_vat_filing_period_for_regime,
                )
                from Services.sme.micro_enterprise import (
                    check_tt58_provision_eligibility,
                    normalize_enterprise_sector,
                )
                if data.get('enterprise_sector'):
                    settings_patch['enterprise_sector'] = normalize_enterprise_sector(
                        data.get('enterprise_sector')
                    )
                if data.get('sme_revenue_band'):
                    settings_patch['sme_revenue_band'] = str(data.get('sme_revenue_band')).strip()
                eff_regime = normalize_accounting_regime(
                    accounting_regime or old_settings.get('accounting_regime')
                )
                if eff_regime == 'SME_MICRO_TT58':
                    provision = check_tt58_provision_eligibility(
                        accounting_regime=eff_regime,
                        enterprise_sector=(
                            settings_patch.get('enterprise_sector')
                            or old_settings.get('enterprise_sector')
                            or data.get('enterprise_sector')
                        ),
                        sme_revenue_band=(
                            settings_patch.get('sme_revenue_band')
                            or old_settings.get('sme_revenue_band')
                            or data.get('sme_revenue_band')
                        ),
                    )
                    if provision.get('warn'):
                        conn.close()
                        return jsonify({'success': False, 'error': provision.get('message')}), 400
                if data.get('vat_filing_period') or data.get('filing_period'):
                    fp = normalize_vat_filing_period(
                        data.get('vat_filing_period') or data.get('filing_period'),
                        default=default_vat_filing_period_for_regime(accounting_regime),
                    )
                    settings_patch['vat_filing_period'] = fp
                    settings_patch['filing_period'] = fp
                if business_line:
                    settings_patch['business_line'] = business_line
                    c.execute(
                        "UPDATE tenants SET business_type = ? WHERE tenant_id = ?",
                        (business_line, tenant_id),
                    )
            else:
                if revenue_tier:
                    settings_patch['revenue_tier'] = revenue_tier
                    settings_patch['revenue_tier_declared'] = revenue_tier
                    settings_patch['revenue_tier_effective'] = revenue_tier
                if enabled_nn_sectors is not None:
                    nn_list = normalize_enabled_nn_sectors(enabled_nn_sectors)
                    settings_patch['enabled_nn_sectors'] = nn_list
                    primary = normalize_nn_code(hkd_sector or (nn_list[0] if nn_list else 'NN1'))
                    settings_patch['primary_nn_sector'] = primary
                    from Services.hkd_sector import nn_to_storage_code
                    settings_patch['default_hkd_sector'] = nn_to_storage_code(primary)
                elif business_line:
                    settings_patch['business_line'] = business_line
                    c.execute(
                        "UPDATE tenants SET business_type = ? WHERE tenant_id = ?",
                        (business_line, tenant_id),
                    )
                    from Services.subscription_service import resolve_provision_nn_profile
                    nn_list, primary = resolve_provision_nn_profile(business_line, None, hkd_sector)
                    settings_patch['enabled_nn_sectors'] = nn_list
                    settings_patch['primary_nn_sector'] = primary
                    from Services.hkd_sector import nn_to_storage_code
                    settings_patch['default_hkd_sector'] = nn_to_storage_code(primary)
                elif hkd_sector:
                    primary = normalize_nn_code(hkd_sector)
                    settings_patch['primary_nn_sector'] = primary
                    from Services.hkd_sector import nn_to_storage_code
                    settings_patch['default_hkd_sector'] = nn_to_storage_code(primary)
                if business_line and enabled_nn_sectors is not None:
                    settings_patch['business_line'] = business_line
                    c.execute(
                        "UPDATE tenants SET business_type = ? WHERE tenant_id = ?",
                        (business_line, tenant_id),
                    )
                elif business_line and 'business_line' not in settings_patch:
                    settings_patch['business_line'] = business_line
                    c.execute(
                        "UPDATE tenants SET business_type = ? WHERE tenant_id = ?",
                        (business_line, tenant_id),
                    )
                if accounting_regime:
                    settings_patch['accounting_regime'] = accounting_regime

            old_regime = old_settings.get('accounting_regime')

            if settings_patch:
                from Services.tenant_profile import update_registry_settings
                if not update_registry_settings(tenant_id, settings_patch, conn=conn):
                    conn.close()
                    return jsonify({"success": False, "error": "Không cập nhật được cấu hình tenant"}), 500

            from db_utils import sqlite_commit, locked_user_message, _is_locked_error
            sqlite_commit(conn, label='master_tenant_update')
            conn.close()
            conn = None

            # TT58 → TT99: đồng bộ COA/quy tắc/schema + kiểm tra toàn vẹn số liệu
            tt99_sync = None
            tt58_setup_warning = None
            new_regime = (settings_patch or {}).get('accounting_regime') or accounting_regime
            if new_regime and str(new_regime).upper() == 'SME_MICRO_TT58':
                try:
                    from datetime import datetime
                    from db_utils import get_tenant_db_connection
                    from Services.sme.micro_enterprise import evaluate_tt58_setup_warning

                    tconn = get_tenant_db_connection(tenant_id)
                    try:
                        merged = dict(old_settings or {})
                        merged.update(settings_patch or {})
                        tt58_setup_warning = evaluate_tt58_setup_warning(
                            tconn,
                            fiscal_year=datetime.now().year,
                            settings=merged,
                        )
                    finally:
                        tconn.close()
                except Exception:
                    tt58_setup_warning = None
            if new_regime:
                try:
                    from Services.sme.migrate_tt58_to_tt99 import migrate_tt58_to_tt99_if_needed
                    from flask import session
                    tt99_sync = migrate_tt58_to_tt99_if_needed(
                        tenant_id=tenant_id,
                        old_regime=old_regime,
                        new_regime=new_regime,
                        settings=old_settings,
                        migrated_by=session.get('username') or session.get('user'),
                    )
                except Exception as sync_exc:
                    tt99_sync = {'ok': False, 'error': str(sync_exc)}

            from Services.audit_log import write_audit
            write_audit(
                'update', 'tenant',
                f"Cập nhật tenant {tenant_id}",
                entity_type='tenant', entity_id=tenant_id, entity_label=business_name,
                old_data=old_data,
                new_data={'business_name': business_name, 'phone': phone, 'expiry_date': expiry_date},
                tenant_id=tenant_id,
                use_main=True,
            )
            payload = {
                "success": True,
                "message": f"Đã cập nhật thông tin cho {tenant_id}",
            }
            if tt99_sync is not None:
                payload['tt99_sync'] = tt99_sync
                if tt99_sync.get('error'):
                    payload['message'] += f" — đồng bộ TT99 lỗi: {tt99_sync.get('error')}"
                elif tt99_sync.get('integrity_ok'):
                    payload['message'] += ' — đã đồng bộ TT99; kiểm tra toàn vẹn đạt.'
                elif tt99_sync.get('synced') or tt99_sync.get('ok'):
                    payload['message'] += (
                        ' — đã đồng bộ TT99; kiểm tra toàn vẹn có cảnh báo '
                        '(xem tt99_sync.checks — cần rà soát số liệu trước khi khóa sổ).'
                    )
            if tt58_setup_warning and not tt58_setup_warning.get('eligible'):
                payload['tt58_setup_warning'] = tt58_setup_warning
                payload['message'] += f" — Cảnh báo TT58: {tt58_setup_warning.get('message')}"
            return jsonify(payload)
        
        except Exception as e:
            if conn: conn.close()
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route('/api/master/tenants/<tenant_id>/sync-tt99', methods=['POST'])
    @login_required
    @master_required
    def api_master_sync_tenant_tt99(tenant_id):
        """Master: đồng bộ lại tenant sang TT99 + kiểm tra toàn vẹn số liệu."""
        from Services.sme.migrate_tt58_to_tt99 import migrate_tenant_to_tt99
        from Services.tenant_profile import load_tenant_profile
        from db_utils import get_tenant_db_connection
        from flask import session

        profile = load_tenant_profile(tenant_id)
        if not profile:
            return jsonify({'success': False, 'error': 'Không tìm thấy tenant'}), 404
        settings = dict(profile.get('settings') or {})
        data = request.get_json(silent=True) or {}
        conn = get_tenant_db_connection(tenant_id)
        if not conn:
            return jsonify({'success': False, 'error': 'Không mở được DB tenant'}), 500
        try:
            result = migrate_tenant_to_tt99(
                conn,
                tenant_id=tenant_id,
                settings=settings,
                update_registry=True,
                migrated_by=session.get('username') or session.get('user'),
                force_coa_refresh=bool(data.get('force_coa_refresh')),
            )
            from db_utils import sqlite_commit
            sqlite_commit(conn, label='master_sync_tt99')
            return jsonify({'success': True, 'data': result})
        except Exception as e:
            conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/master/trial_settings', methods=['GET'])
    @login_required
    @master_required
    def api_get_trial_settings():
        from Services.subscription_service import (
            TRIAL_MONTHS_MAX,
            TRIAL_MONTHS_MIN,
            get_trial_months,
        )
        return jsonify({
            'success': True,
            'trial_months': get_trial_months(),
            'min': TRIAL_MONTHS_MIN,
            'max': TRIAL_MONTHS_MAX,
        })

    @app.route('/api/master/trial_settings', methods=['POST'])
    @login_required
    @master_required
    def api_save_trial_settings():
        from Services.subscription_service import set_trial_months
        from Services.audit_log import write_audit

        data = request.get_json() or {}
        result = set_trial_months(data.get('trial_months'))
        if not result.get('success'):
            return jsonify(result), 400
        write_audit(
            'update', 'trial_settings',
            f"Đặt thời hạn dùng thử mặc định: {result['trial_months']} tháng",
            entity_type='settings', entity_id='trial_months',
            new_data={'trial_months': result['trial_months']},
            use_main=True,
        )
        return jsonify({
            'success': True,
            'trial_months': result['trial_months'],
            'message': f"Đã lưu thời hạn dùng thử mặc định: {result['trial_months']} tháng",
        })

    @app.route('/api/master/tenant/<tenant_id>/adjust_trial', methods=['POST'])
    @login_required
    @master_required
    def api_adjust_tenant_trial(tenant_id):
        from Services.subscription_service import adjust_tenant_expiry_months
        from Services.audit_log import write_audit

        data = request.get_json() or {}
        delta = data.get('months', data.get('delta_months'))
        result = adjust_tenant_expiry_months(tenant_id, delta)
        if not result.get('success'):
            return jsonify(result), 400
        write_audit(
            'update', 'tenant',
            result.get('message') or f"Điều chỉnh dùng thử {tenant_id}",
            entity_type='tenant', entity_id=tenant_id,
            entity_label=result.get('business_name'),
            old_data={'expiry_date': result.get('old_expiry_date')},
            new_data={
                'expiry_date': result.get('expiry_date'),
                'delta_months': result.get('delta_months'),
            },
            tenant_id=tenant_id,
            use_main=True,
        )
        return jsonify(result)

    @app.route('/api/master/enter_tenant/<tenant_id>', methods=['POST'])
    @login_required
    @master_required
    def api_master_enter_tenant(tenant_id):
        """Master vào tenant để test — gán session db_path."""
        from Services.subscription_service import get_tenant_record
        from db_utils import _normalize_db_path
        from auth import User
        from flask_login import login_user

        rec = get_tenant_record(tenant_id.strip(), include_inactive=True)
        if not rec:
            return jsonify({'success': False, 'error': 'Không tìm thấy tenant'}), 404

        from Services.firm_tenant import is_firm_tenant
        tid = tenant_id.strip()
        is_firm = is_firm_tenant(tid)

        if is_firm:
            try:
                from Services.firm_tenant import ensure_firm_own_books_ready
                db_path = ensure_firm_own_books_ready(tid)
            except FileNotFoundError as exc:
                return jsonify({'success': False, 'error': str(exc)}), 400
            except Exception as exc:
                return jsonify({
                    'success': False,
                    'error': f'Không chuẩn bị được sổ DVKT: {exc}',
                }), 500
        else:
            db_path = _normalize_db_path(rec.get('db_path'))
            if not db_path:
                return jsonify({'success': False, 'error': 'Tenant chưa có database'}), 400

        session.setdefault('master_home_db_path', session.get('db_path'))
        session.setdefault('master_home_tenant_id', session.get('last_tenant_id'))
        session['last_tenant_id'] = tid
        session['db_path'] = db_path
        session['master_viewing_tenant'] = tid
        if session.get('user'):
            session['user'] = dict(session['user'])
            session['user']['role'] = 'master'
        session['role'] = 'master'

        u = session.get('user') or {}
        login_user(User(
            id=u.get('id'),
            username=u.get('username', 'master'),
            role='master',
            db_path=db_path,
            tenant_id=tid,
            full_name=u.get('full_name') or 'Master',
            permissions=u.get('permissions', ''),
        ), remember=True)
        session.modified = True

        from Services.tenant_profile import load_tenant_profile, is_sme_regime
        profile = load_tenant_profile(tid)
        regime = profile.get('accounting_regime') or 'HKD'
        if is_firm:
            redirect_to = url_for('settings_page')
            message = f'Đã mở Cài đặt DVKT {tid}'
        elif is_sme_regime(regime):
            redirect_to = url_for('SME_dashboard')
            message = f"Đã vào tenant {tid} ({profile.get('accounting_regime_label') or regime})"
        else:
            redirect_to = url_for('HKD_dashboard')
            message = f"Đã vào tenant {tid} ({profile.get('accounting_regime_label') or regime})"
        return jsonify({
            'success': True,
            'tenant_id': tid,
            'tenant_type': 'firm' if is_firm else 'standalone',
            'accounting_regime': regime,
            'revenue_tier': profile.get('revenue_tier'),
            'enabled_nn_sectors': profile.get('enabled_nn_sectors'),
            'redirect': redirect_to,
            'message': message,
        })

    @app.route('/api/master/firm/<firm_tenant_id>/clients', methods=['GET'])
    @login_required
    @master_required
    def api_master_firm_clients(firm_tenant_id):
        """Master: danh sách DN thuê DVKT để cấu hình HĐĐT."""
        from Services.firm_tenant import is_firm_tenant, list_clients_for_master
        tid = firm_tenant_id.strip()
        if not is_firm_tenant(tid):
            return jsonify({'success': False, 'error': 'Tenant không phải đơn vị DVKT'}), 400
        clients = list_clients_for_master(tid)
        return jsonify({'success': True, 'firm_tenant_id': tid, 'clients': clients, 'count': len(clients)})

    @app.route('/api/master/firm/<firm_tenant_id>/enter_client/<client_id>', methods=['POST'])
    @login_required
    @master_required
    def api_master_enter_firm_client(firm_tenant_id, client_id):
        """Master vào sổ DN thuê DVKT — chỉ cấu hình HĐĐT & MST."""
        from Services.firm_tenant import (
            client_db_abs,
            get_client_record,
            is_firm_tenant,
        )
        from auth import User
        from flask_login import login_user

        firm_tid = firm_tenant_id.strip()
        cid = client_id.strip()
        if not is_firm_tenant(firm_tid):
            return jsonify({'success': False, 'error': 'Tenant không phải đơn vị DVKT'}), 400
        client = get_client_record(firm_tid, cid)
        if not client:
            return jsonify({'success': False, 'error': 'Không tìm thấy doanh nghiệp thuê'}), 404

        abs_db = client_db_abs(client.get('db_path') or '')
        if not db_path_available(abs_db):
            return jsonify({'success': False, 'error': 'File sổ kế toán không tồn tại'}), 400

        try:
            from db.init import ensure_invoice_settings_schema
            from db_utils import open_sqlite, sqlite_write_retry

            def _prep_client_db():
                with open_sqlite(abs_db) as conn:
                    ensure_invoice_settings_schema(conn)
                    sqlite_commit(conn, label='settings')

            sqlite_write_retry(_prep_client_db, label='master_enter_client_schema')
        except Exception as exc:
            return jsonify({
                'success': False,
                'error': f'Không chuẩn bị được sổ DN thuê: {exc}',
            }), 500

        session.setdefault('master_home_db_path', session.get('db_path'))
        session.setdefault('master_home_tenant_id', session.get('last_tenant_id'))
        session['master_viewing_tenant'] = firm_tid
        session['master_viewing_firm_client_id'] = cid
        session['master_viewing_firm_client_name'] = client.get('client_name') or cid
        session['last_tenant_id'] = f'{firm_tid}:{cid}'
        session['db_path'] = abs_db
        for key in (
            'firm_tenant_id', 'firm_user_id', 'firm_viewing_client', 'firm_viewing_own_books',
            'firm_active_client_id', 'firm_client_name', 'firm_client_tax_code', 'firm_access_role',
        ):
            session.pop(key, None)
        if session.get('user'):
            session['user'] = dict(session['user'])
            session['user']['role'] = 'master'
        session['role'] = 'master'

        u = session.get('user') or {}
        login_user(User(
            id=u.get('id'),
            username=u.get('username', 'master'),
            role='master',
            db_path=abs_db,
            tenant_id=f'{firm_tid}:{cid}',
            full_name=u.get('full_name') or 'Master',
            permissions=u.get('permissions', ''),
        ), remember=True)
        session.modified = True

        redirect_to = url_for('settings_page') + '#tab-esign'
        return jsonify({
            'success': True,
            'firm_tenant_id': firm_tid,
            'client_id': cid,
            'client_name': client.get('client_name'),
            'redirect': redirect_to,
            'message': f'Đã mở cấu hình HĐĐT: {client.get("client_name") or cid}',
        })

    @app.route('/api/master/leave_tenant', methods=['POST'])
    @login_required
    @master_required
    def api_master_leave_tenant():
        from auth import User
        from flask_login import login_user
        from db_utils import MAIN_DB_PATH

        home_db = session.pop('master_home_db_path', None) or MAIN_DB_PATH
        home_tenant = session.pop('master_home_tenant_id', None)
        session.pop('master_viewing_tenant', None)
        session.pop('master_viewing_firm_client_id', None)
        session.pop('master_viewing_firm_client_name', None)
        session['last_tenant_id'] = home_tenant
        session['db_path'] = home_db
        if session.get('user'):
            session['user'] = dict(session['user'])
            session['user']['role'] = 'master'
        session['role'] = 'master'

        u = session.get('user') or {}
        login_user(User(
            id=u.get('id'),
            username=u.get('username', 'master'),
            role='master',
            db_path=home_db,
            tenant_id=home_tenant,
            full_name=u.get('full_name') or 'Master',
            permissions=u.get('permissions', ''),
        ), remember=True)
        session.modified = True
        return jsonify({'success': True, 'redirect': url_for('master_settings')})

    # API xóa tenant
    @app.route('/api/master/delete_tenant/<tenant_id>', methods=['DELETE'])
    @login_required
    @master_required
    def api_delete_tenant(tenant_id):
        # 1. Kiểm tra tính hợp lệ của tenant_id
        if not tenant_id or tenant_id.lower() in ['main', '']:
            return jsonify({"success": False, "error": "Không thể xóa tenant chính"}), 400

        from db_utils import (
            get_main_db_connection,
            remove_sqlite_files,
            force_close_request_db_if_path,
            _normalize_db_path,
        )

        # Luôn dùng MAIN registry — không dùng get_db_connection() (có thể đang
        # giữ tenant DB / trùng connection với write_audit → database is locked).
        conn = get_main_db_connection()
        db_path = None
        tenant_label = tenant_id
        is_firm = False
        try:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # 2. Lấy thông tin đường dẫn DB trước khi xóa
            c.execute(
                """
                SELECT db_path, business_name,
                       COALESCE(tenant_type, 'standalone') AS tenant_type
                FROM tenants WHERE tenant_id = ?
                """,
                (tenant_id,),
            )
            row = c.fetchone()
            if not row:
                return jsonify({"success": False, "error": "Không tìm thấy tenant"}), 404

            db_path = _normalize_db_path(row['db_path'])
            tenant_label = row['business_name'] or tenant_id
            tenant_type = (row['tenant_type'] or 'standalone').strip().lower()
            is_firm = tenant_type == 'firm'

            # Đóng connection request đang mở file tenant (nếu master đang xem tenant này)
            force_close_request_db_if_path(db_path)

            # 3. Xóa metadata DVKT trên registry (firm_users, firm_clients, …)
            if is_firm:
                from Services.firm_tenant import purge_firm_tenant_registry
                purge_result = purge_firm_tenant_registry(tenant_id, conn=conn)
                if not purge_result.get('success'):
                    conn.rollback()
                    return jsonify({
                        'success': False,
                        'error': purge_result.get('error') or 'Không xóa được dữ liệu DVKT',
                    }), 500

            # 4. Xóa metadata tenant trên MAIN trong một transaction
            c.execute("""
                DELETE FROM user_trusted_devices
                WHERE username IN (
                    SELECT username FROM user_tenant_mapping WHERE tenant_id = ?
                )
            """, (tenant_id,))
            c.execute("DELETE FROM user_tenant_mapping WHERE tenant_id = ?", (tenant_id,))
            c.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))
            try:
                c.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN "
                    "('tenants', 'user_tenant_mapping', 'user_trusted_devices')"
                )
            except Exception:
                pass

            # Ghi audit trên CÙNG connection MAIN (tránh mở connection thứ 2 → locked)
            try:
                from Services.audit_log import ensure_audit_table
                ensure_audit_table(conn)
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                user = session.get('user') or {}
                conn.execute(
                    """
                    INSERT INTO audit_log (
                        created_at, tenant_id, user_id, username, user_role,
                        action, module, entity_type, entity_id, entity_label,
                        summary, old_data, new_data, ip_address, request_path,
                        user_agent, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now, tenant_id,
                        user.get('id') or session.get('user_id'),
                        user.get('username') or session.get('username') or 'master',
                        user.get('role') or session.get('role') or 'master',
                        'delete', 'tenant', 'tenant', tenant_id, tenant_label,
                        f'Xóa tenant {tenant_id}',
                        json.dumps({'tenant_id': tenant_id, 'db_path': db_path}, ensure_ascii=False),
                        None,
                        request.remote_addr,
                        request.path,
                        (request.headers.get('User-Agent') or '')[:300],
                        'success',
                    ),
                )
            except Exception as audit_err:
                print(f'--- [WARN] audit delete_tenant skipped: {audit_err} ---')

            from db_utils import sqlite_commit
            sqlite_commit(conn, label='delete_tenant')

            # Nếu master đang xem tenant vừa xóa — thoát về MAIN
            if session.get('master_viewing_tenant') == tenant_id:
                home = session.get('master_home_db_path')
                session.pop('master_viewing_tenant', None)
                session.pop('master_home_db_path', None)
                if home:
                    session['db_path'] = home
                session.modified = True

        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({"success": False, "error": f"Lỗi hệ thống: {str(e)}"}), 500
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # 5. Xóa file vật lý sau khi đã đóng hết connection MAIN
        file_note = ''
        if is_firm:
            from Services.firm_tenant import remove_firm_tenant_files
            firm_removed = remove_firm_tenant_files(tenant_id)
            if firm_removed.get('errors'):
                file_note += ' ' + '; '.join(firm_removed['errors'])
        if db_path:
            removed = remove_sqlite_files(db_path)
            if removed.get('errors'):
                file_note = (
                    f" Metadata đã xóa; file DB còn giữ do đang bị khóa: "
                    f"{'; '.join(removed['errors'])}"
                )
                print(f'Lỗi xóa file vật lý ({db_path}): {removed["errors"]}')

        return jsonify({
            "success": True,
            "message": (
                f"Đã xóa thành công tenant {tenant_id} và các dữ liệu liên quan."
                + file_note
            ),
        })
    @app.route('/thiet-lap')
    @admin_or_store_setup_required
    def store_setup_page():
        from flask import g
        from Services.tenant_profile import is_sme_regime

        db = get_db_connection()
        info_row = db.execute("SELECT * FROM business_info LIMIT 1").fetchone()
        info = dict(info_row) if info_row else {}
        profile = getattr(g, 'tenant_profile', None) or {}
        setup_is_sme = is_sme_regime(profile.get('accounting_regime'))
        return render_template(
            'store_setup.html',
            info=info,
            setup_is_sme=setup_is_sme,
            base_layout=(
                'KeToanSME/_layout.html' if setup_is_sme else 'KeToanHKD/_layout.html'
            ),
        )

    @app.route('/settings')
    @tenant_settings_required
    def settings_page():
        from helpers import get_setting as _get_setting
        from Services.invoice_config import get_active_invoice_config
        db = get_db_connection()
    
        # 1. Lấy thông tin doanh nghiệp
        info_row = db.execute("SELECT * FROM business_info LIMIT 1").fetchone()
        info = dict(info_row) if info_row else {}
    
        # 2. Lấy cấu hình HĐĐT đang active (invoice_settings) — không dùng legacy esign_%
        esign = {}
        active = get_active_invoice_config() or {}
        if active:
            esign = dict(active)
        else:
            # Fallback legacy settings.esign_* nếu chưa migrate
            esign_rows = db.execute("SELECT key, value FROM settings WHERE key LIKE 'esign_%'").fetchall()
            for row in esign_rows:
                clean_key = row['key'].replace('esign_', '')
                esign[clean_key] = row['value']

        for field in ('password', 'app_secret', 'esign_pin', 'etax_password'):
            if esign.get(field):
                esign[f'has_{field}'] = True
            esign[field] = ''

        tt58_ctx = None
        try:
            from Services.tenant_profile import get_current_tenant_profile
            regime = str(
                (get_current_tenant_profile() or {}).get('accounting_regime') or ''
            ).upper()
            if 'TT58' in regime or 'MICRO' in regime:
                from Services.sme.regime_profile import get_ledger_profile
                from Services.sme.tt58_tax_rates import (
                    get_tt58_tax_rates,
                    rates_ui_context_for_method,
                )
                lp = get_ledger_profile(db)
                tt58_ctx = {
                    'methods': lp.get('tt58_tax_methods') or [],
                    'method': lp.get('tt58_tax_method'),
                    'profile': lp,
                    'rates': get_tt58_tax_rates(db),
                    'ui': rates_ui_context_for_method(lp.get('tt58_tax_method')),
                }
        except Exception:
            tt58_ctx = None

        from auth import is_master_configuring_firm_tenant, is_master_configuring_firm_client

        return render_template('settings.html', 
                               info=info, 
                               esign=esign,
                               invoice_providers=list_providers_for_ui(),
                               payment_provider=_get_setting('payment_provider', 'none'),
                               payment_tolerance=_get_setting('payment_amount_tolerance', '1000'),
                               has_sepay_key=bool(_get_setting('sepay_api_key', '')),
                               has_casso_key=bool(_get_setting('casso_api_key', '')),
                               tt58=tt58_ctx,
                               is_firm_tenant_settings=is_master_configuring_firm_tenant(),
                               is_master_firm_client_einvoice=is_master_configuring_firm_client())

    @app.route('/api/settings/business', methods=['POST'])
    @admin_or_store_setup_required
    def api_save_business():
        data = request.get_json(silent=True)  # An toàn hơn, tránh exception nếu không phải JSON

        if not data or not isinstance(data, dict):
            return jsonify({
                "success": False,
                "error": "Dữ liệu gửi lên không hợp lệ (JSON)"
            }), 400

        # Danh sách trường được phép cập nhật (Whitelist)
        allowed_fields = [
            'business_name',
            'representative_name',
            'address',
            'phone',
            'email',
            'tax_code',
            'bank_name',                    # ← ĐÃ THÊM
            'bank_account',
            'bank_code',
            'account_holder',
            'warehouse_location',
            'warehouse_location1',
            'warehouse_location2'
        ]

        # Chuẩn bị dữ liệu (loại bỏ trường lạ + xử lý None)
        values = {}
        for field in allowed_fields:
            value = data.get(field)
            values[field] = str(value).strip() if value is not None else ''

        # Validation cơ bản
        if not values.get('business_name'):
            return jsonify({
                "success": False,
                "error": "Tên doanh nghiệp/Hộ kinh doanh là bắt buộc"
            }), 400

        conn = None
        try:
            from db_utils import sqlite_run_write, locked_user_message, _is_locked_error
            conn = get_db_connection()
            cursor = conn.cursor()
            old_row = cursor.execute("SELECT * FROM business_info LIMIT 1").fetchone()
            old_data = dict(old_row) if old_row else None

            def _save_business(cn):
                cur = cn.cursor()
                cur.execute("SELECT id FROM business_info LIMIT 1")
                existing = cur.fetchone()
                if existing:
                    set_clause = ", ".join([f"{field} = ?" for field in allowed_fields])
                    sql = f"UPDATE business_info SET {set_clause} WHERE id = ?"
                    params = list(values.values()) + [existing['id']]
                    cur.execute(sql, params)
                    msg = "Cập nhật thông tin thành công"
                else:
                    columns = ", ".join(allowed_fields)
                    placeholders = ", ".join(["?"] * len(allowed_fields))
                    sql = f"INSERT INTO business_info ({columns}) VALUES ({placeholders})"
                    cur.execute(sql, list(values.values()))
                    msg = "Tạo thông tin kinh doanh thành công"
                try:
                    from Services.tenant_profile import is_sme_regime
                    from Services.sme.bank_accounts import sync_default_bank_from_qr
                    from flask import g
                    profile = getattr(g, 'tenant_profile', None) or {}
                    if is_sme_regime(profile.get('accounting_regime')):
                        sync_default_bank_from_qr(cn, commit=False)
                except Exception:
                    pass
                from Services.chu_ho_helpers import sync_chu_ho_from_business_info
                matched, rep_name = sync_chu_ho_from_business_info(cn, commit=False)
                return msg, matched, rep_name

            message, matched, rep_name = sqlite_run_write(conn, _save_business, label='save_business_info')
            sync_msg = ''
            if rep_name:
                if matched:
                    sync_msg = f' Đã gán Chủ hộ cho {len(matched)} nhân viên trùng tên.'
                else:
                    sync_msg = (
                        f' Chưa tìm thấy nhân viên trùng tên Chủ hộ «{rep_name}»'
                        f' — thêm NV trong bảng lương nếu cần.'
                    )

            from Services.audit_log import write_audit
            write_audit(
                'settings', 'settings',
                message,
                old_data=old_data,
                new_data=values,
            )

            return jsonify({
                "success": True,
                "message": message + sync_msg,
                "chu_ho_matched": len(matched) if rep_name else 0,
            })

        except sqlite3.OperationalError as e:
            if conn:
                conn.rollback()
            from db_utils import locked_user_message, _is_locked_error
            if _is_locked_error(e):
                return jsonify({"success": False, "error": locked_user_message()}), 503
            return jsonify({
                "success": False,
                "error": f"Lỗi database: {str(e)}"
            }), 500
        except Exception as e:
            if conn:
                conn.rollback()
            return jsonify({
                "success": False,
                "error": f"Lỗi database: {str(e)}"
            }), 500

        finally:
            if conn:
                conn.close()

    @app.route('/api/settings/business-logo', methods=['POST'])
    @admin_or_store_setup_required
    def api_upload_business_logo():
        from Services.business_branding import ensure_business_logo_column, save_business_logo_file

        file = request.files.get('logo') or request.files.get('file')
        result = save_business_logo_file(file, tenant_id=getattr(g, 'tenant_id', None))
        if not result.get('success'):
            return jsonify(result), 400

        conn = None
        try:
            from db_utils import sqlite_run_write
            conn = get_db_connection()
            cur = conn.cursor()
            ensure_business_logo_column(cur)
            row = cur.execute('SELECT id FROM business_info LIMIT 1').fetchone()
            logo_path = result['logo_path']

            def _save_logo(cn):
                c = cn.cursor()
                ensure_business_logo_column(c)
                r = c.execute('SELECT id FROM business_info LIMIT 1').fetchone()
                if r:
                    c.execute(
                        'UPDATE business_info SET logo_path = ? WHERE id = ?',
                        (logo_path, r['id'] if hasattr(r, 'keys') else r[0]),
                    )
                else:
                    c.execute(
                        'INSERT INTO business_info (business_name, logo_path) VALUES (?, ?)',
                        ('', logo_path),
                    )

            sqlite_run_write(conn, _save_logo, label='save_business_logo')
            return jsonify({
                'success': True,
                'message': 'Đã lưu logo',
                'logo_path': logo_path,
                'logo_url': result.get('logo_url'),
            })
        except Exception as exc:
            if conn:
                conn.rollback()
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            if conn:
                conn.close()

    @app.route('/api/settings/business-logo', methods=['DELETE'])
    @admin_or_store_setup_required
    def api_delete_business_logo():
        from Services.business_branding import ensure_business_logo_column, remove_business_logo_file

        conn = None
        try:
            from db_utils import sqlite_run_write
            conn = get_db_connection()
            cur = conn.cursor()
            ensure_business_logo_column(cur)
            row = cur.execute('SELECT id, logo_path FROM business_info LIMIT 1').fetchone()
            if row:
                old_path = row['logo_path'] if hasattr(row, 'keys') else row[1]
                remove_business_logo_file(old_path)
                bid = row['id'] if hasattr(row, 'keys') else row[0]

                def _clear_logo(cn):
                    cn.execute('UPDATE business_info SET logo_path = NULL WHERE id = ?', (bid,))

                sqlite_run_write(conn, _clear_logo, label='delete_business_logo')
            from Services.business_branding import default_business_logo_url
            return jsonify({
                'success': True,
                'message': 'Đã đặt lại logo mặc định (cờ Việt Nam)',
                'logo_url': default_business_logo_url(),
            })
        except Exception as exc:
            if conn:
                conn.rollback()
            return jsonify({'success': False, 'error': str(exc)}), 500
        finally:
            if conn:
                conn.close()

    @app.route('/api/settings/payment-bank', methods=['GET'])
    @admin_or_store_setup_required
    def api_get_payment_bank():
        from Services.payment_bank import get_full_payment_setup
        from flask import url_for
        data = get_full_payment_setup()
        try:
            data['webhook_sepay'] = url_for('webhook_sepay', _external=True)
            data['webhook_casso'] = url_for('webhook_casso', _external=True)
        except Exception:
            data['webhook_sepay'] = '/api/payment/webhook/sepay'
            data['webhook_casso'] = '/api/payment/webhook/casso'
        return jsonify(data)

    @app.route('/api/settings/payment-bank', methods=['POST'])
    @admin_or_store_setup_required
    def api_save_payment_bank():
        """Lưu đồng bộ: thông tin cửa hàng + VietQR + SePay/Casso."""
        from Services.payment_bank import save_payment_settings, validate_payment_provider_setup, get_payment_config, get_full_payment_setup

        data = request.get_json(silent=True)
        if not data or not isinstance(data, dict):
            return jsonify({'success': False, 'error': 'Dữ liệu không hợp lệ'}), 400

        allowed_business = [
            'business_name', 'representative_name', 'address', 'phone', 'email', 'tax_code',
            'bank_name', 'bank_account', 'bank_code', 'account_holder',
            'warehouse_location', 'warehouse_location1', 'warehouse_location2',
        ]
        values = {}
        for field in allowed_business:
            val = data.get(field)
            values[field] = str(val).strip() if val is not None else ''

        if not values.get('business_name'):
            return jsonify({'success': False, 'error': 'Tên doanh nghiệp/Hộ kinh doanh là bắt buộc'}), 400

        provider = str(data.get('payment_provider', 'none')).strip().lower()
        if provider in ('sepay', 'casso'):
            if not values.get('bank_account') or not values.get('bank_code'):
                return jsonify({
                    'success': False,
                    'error': 'Khi bật SePay/Casso cần nhập đủ Số TK và Mã BIN (VietQR) — STK phải trùng tài khoản liên kết trên SePay/Casso'
                }), 400

        conn = None
        try:
            from db_utils import sqlite_run_write
            conn = get_db_connection()
            cursor = conn.cursor()
            old_row = cursor.execute("SELECT * FROM business_info LIMIT 1").fetchone()
            old_data = dict(old_row) if old_row else None

            def _save_payment_business(cn):
                cur = cn.cursor()
                cur.execute("SELECT id FROM business_info LIMIT 1")
                existing = cur.fetchone()
                if existing:
                    set_clause = ", ".join([f"{f} = ?" for f in allowed_business])
                    cur.execute(
                        f"UPDATE business_info SET {set_clause} WHERE id = ?",
                        list(values.values()) + [existing['id']]
                    )
                else:
                    cols = ", ".join(allowed_business)
                    ph = ", ".join(["?"] * len(allowed_business))
                    cur.execute(f"INSERT INTO business_info ({cols}) VALUES ({ph})", list(values.values()))
                try:
                    from Services.tenant_profile import is_sme_regime
                    from Services.sme.bank_accounts import sync_default_bank_from_qr
                    from flask import g
                    profile = getattr(g, 'tenant_profile', None) or {}
                    if is_sme_regime(profile.get('accounting_regime')):
                        sync_default_bank_from_qr(cn, commit=False)
                except Exception:
                    pass
                from Services.chu_ho_helpers import sync_chu_ho_from_business_info
                sync_chu_ho_from_business_info(cn, commit=False)

            sqlite_run_write(conn, _save_payment_business, label='save_payment_bank')
            save_payment_settings(data)

            from Services.audit_log import write_audit
            write_audit(
                'settings', 'payment',
                'Cập nhật cấu hình thanh toán / VietQR',
                old_data=old_data,
                new_data={
                    'payment_provider': provider,
                    'business_name': values.get('business_name'),
                    'bank_code': values.get('bank_code'),
                },
            )

            status = validate_payment_provider_setup(get_payment_config())
            setup = get_full_payment_setup()
            return jsonify({
                'success': True,
                'message': 'Đã lưu thông tin cửa hàng, VietQR và cấu hình ngân hàng điện tử',
                'vietqr': setup.get('vietqr'),
                'provider_status': setup.get('provider_status'),
            })
        except Exception as e:
            if conn:
                conn.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            if conn:
                conn.close()

    @app.route('/api/settings/test_payment_connection', methods=['POST'])
    @tenant_settings_required
    def api_test_payment_connection():
        from Services.payment_bank import test_provider_connection
        from db_utils import close_request_db
        data = request.get_json(silent=True) or {}
        provider = data.get('payment_provider') or data.get('provider')
        close_request_db()
        result = test_provider_connection(provider)
        code = 200 if result.get('success') else 400
        return jsonify(result), code

    @app.route('/api/settings/system', methods=['POST'])
    @tenant_settings_required
    def api_save_system():
        data = request.json
        conn = get_db_connection()
        try:
            from db_utils import sqlite_commit
            for key in ['auto_print', 'auto_backup', 'printer_vendor_id', 'printer_product_id', 'low_stock_alert',
                        'scale_enabled', 'scale_protocol', 'scale_auto_add', 'scale_stable_reads',
                        'scale_decimal_places', 'scale_barcode_prefix']:
                if key in data:
                    val = data[key]
                    if key in ('scale_enabled', 'scale_auto_add'):
                        val = '1' if str(val) in ('1', 'true', 'True', True) else '0'
                    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(val)))
            sqlite_commit(conn, label='save_system_settings')
            return jsonify({"success": True, "message": "Đã lưu cài đặt!"})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
        finally:
            conn.close()

    @app.route('/api/settings/inventory-cost-method', methods=['GET'])
    @login_required
    @tenant_settings_required
    def api_get_inventory_cost_method():
        from Services.inventory_cost_method import cost_method_status, ensure_inventory_lot_schema
        conn = get_db_connection()
        try:
            ensure_inventory_lot_schema(conn)
            return jsonify({'success': True, **cost_method_status(conn)})
        finally:
            conn.close()

    @app.route('/api/settings/inventory-cost-method', methods=['POST'])
    @login_required
    @tenant_settings_required
    def api_save_inventory_cost_method():
        from datetime import datetime
        from Services.inventory_cost_method import apply_cost_method_change, ensure_inventory_lot_schema
        data = request.get_json() or {}
        method = (data.get('method') or '').strip().lower()
        target_year = data.get('effective_year') or datetime.now().year
        confirm = bool(data.get('confirm'))
        try:
            target_year = int(target_year)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Năm tài chính không hợp lệ'}), 400
        conn = get_db_connection()
        try:
            ensure_inventory_lot_schema(conn)
            status = apply_cost_method_change(
                conn,
                method,
                target_year,
                changed_by=session.get('user_name') or session.get('username') or '',
                confirm=confirm,
            )
            return jsonify({'success': True, 'message': 'Đã lưu phương pháp giá vốn.', **status})
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/settings/inventory-lot-ops', methods=['POST'])
    @login_required
    @tenant_settings_required
    def api_save_inventory_lot_ops():
        """Bật/tắt theo dõi lô vận hành khi đang WAC (không ảnh hưởng giá vốn)."""
        conn = None
        try:
            from Services.inventory_cost_method import (
                ensure_inventory_lot_schema,
                set_lot_ops_tracking,
            )
            data = request.get_json(silent=True) or {}
            enabled = data.get('enabled') in (True, 1, '1', 'true', 'True', 'yes')
            conn = get_db_connection()
            ensure_inventory_lot_schema(conn)
            status = set_lot_ops_tracking(
                conn,
                enabled,
                changed_by=session.get('user_name') or session.get('username') or '',
            )
            msg = (
                'Đã bật theo dõi lô vận hành (giá vốn vẫn WAC).'
                if status.get('lot_ops_tracking')
                else 'Đã tắt theo dõi lô vận hành.'
            )
            if status.get('seeded_opening_lots'):
                msg += f" Đã tạo {status['seeded_opening_lots']} lô tồn đầu kỳ."
            # Chỉ trả field JSON-safe
            payload = {
                'success': True,
                'message': msg,
                'method': status.get('method'),
                'method_label': status.get('method_label'),
                'lot_ops_tracking': bool(status.get('lot_ops_tracking')),
                'lot_tracking_enabled': bool(status.get('lot_tracking_enabled')),
                'lot_ops_only': bool(status.get('lot_ops_only')),
                'seeded_opening_lots': int(status.get('seeded_opening_lots') or 0),
                'can_change': bool(status.get('can_change')),
                'effective_year': status.get('effective_year'),
                'current_fiscal_year': status.get('current_fiscal_year'),
            }
            return jsonify(payload)
        except Exception as e:
            try:
                current_app.logger.exception('inventory-lot-ops: %s', e)
            except Exception:
                pass
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    @app.route('/api/settings/list_backups', methods=['GET'])
    @login_required
    @tenant_settings_required
    def list_backups():
        # 1. Đảm bảo tenant_id không bao giờ là None
        # Nếu không có tenant_id (đang ở main), mặc định dùng thư mục 'main'
        tenant_id = getattr(g, 'tenant_id', None)
        if tenant_id is None:
            tenant_id = 'main'

        # 2. Xây dựng đường dẫn tuyệt đối an toàn
        # BACKUP_ROOT nên được định nghĩa ở đầu file app.py bằng đường dẫn tuyệt đối
        tenant_backup_dir = os.path.join(BACKUP_ROOT, tenant_id)

        # 3. Kiểm tra thư mục tồn tại
        if not os.path.isdir(tenant_backup_dir):
            # Nếu chưa có file nào, tạo thư mục rỗng và trả về mảng rỗng
            try:
                os.makedirs(tenant_backup_dir, exist_ok=True)
            except:
                pass
            return jsonify([]), 200

        files = []
        try:
            for filename in os.listdir(tenant_backup_dir):
                # Chỉ lấy các file database
                if not filename.lower().endswith(('.db', '.sqlite', '.sqlite3')):
                    continue

                full_path = os.path.join(tenant_backup_dir, filename)
            
                # Kiểm tra xem có phải là file thực sự không (tránh thư mục con)
                if not os.path.isfile(full_path):
                    continue

                stat = os.stat(full_path)
                created_time = datetime.fromtimestamp(stat.st_ctime)
                size_mb = round(stat.st_size / (1024 * 1024), 2)

                files.append({
                    "name": filename,
                    "size": f"{size_mb} MB",
                    "time": created_time.strftime('%Y-%m-%d %H:%M:%S'),
                    "timestamp": int(created_time.timestamp()),
                    "url": f"/api/settings/download_backup/{tenant_id}/{filename}"
                })

            # Sắp xếp mới nhất lên đầu
            files.sort(key=lambda x: x['timestamp'], reverse=True)
            return jsonify(files[:10])

        except Exception as e:
            current_app.logger.error(f"Lỗi list_backups: {e}")
            return jsonify({"error": "Không thể đọc danh sách"}), 500

    @app.route('/api/settings/backup_now', methods=['POST'])
    @tenant_settings_required
    def backup_now():
        try:
            # 1. Xác định Tenant ID (Mặc định là 'main' nếu không có g.tenant_id)
            tenant_id = getattr(g, 'tenant_id', None)
            if tenant_id is None:
                tenant_id = 'main'

            # 2. Database đang active (tenant shop hoặc main)
            db_path = resolve_db_path()

            # Kiểm tra DB nguồn có sẵn (SQLite file hoặc Postgres)
            if not db_path_available(db_path):
                return jsonify({"success": False, "error": f"Không tìm thấy file database tại: {db_path}"})

            # 4. Xác định thư mục lưu trữ backup
            # Đảm bảo BACKUP_ROOT không phải None (Ví dụ: BACKUP_ROOT = os.path.join(BASE_DIR, 'backups'))
            tenant_backup_dir = os.path.join(BACKUP_ROOT, tenant_id)
        
            # Tạo thư mục nếu chưa có (exist_ok=True để tránh lỗi nếu thư mục vừa được tạo bởi luồng khác)
            os.makedirs(tenant_backup_dir, exist_ok=True)

            # 5. Đặt tên file và thực hiện sao lưu
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{tenant_id}_manual_{timestamp}.db"
            dest_path = os.path.join(tenant_backup_dir, filename)
        
            shutil.copy2(db_path, dest_path)
        
            # Gửi email thông báo (nếu bạn có hàm này)
            # send_backup_email(filename, status="Thủ công")
        
            return jsonify({
                "success": True, 
                "filename": filename,
                "tenant": tenant_id
            })

        except Exception as e:
            # Log lỗi chi tiết ra console của VPS để bạn dễ theo dõi
            print(f"CRITICAL ERROR trong backup_now: {str(e)}")
            return jsonify({"success": False, "error": str(e)})

    @app.route('/api/settings/download_backup/<filename>')
    def download_backup(filename):
        return send_from_directory(BACKUP_DIR, filename, as_attachment=True)

    # --- CẤU HÌNH GỬI EMAIL ---

    def send_backup_email(filename, status="Tự động"):
        """Hàm gửi file database qua email"""
        file_path = os.path.join(BACKUP_DIR, filename)
        cfg = get_smtp_config()
        msg = EmailMessage()
        msg['Subject'] = f"[{status}] Backup Database - {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        msg['From'] = cfg['sender']
        msg['To'] = RECEIVER_EMAIL
        msg.set_content(f"Gửi bạn file sao lưu dữ liệu hệ thống.\nTên file: {filename}\nTrạng thái: {status}")

        try:
            with open(file_path, 'rb') as f:
                msg.add_attachment(f.read(), maintype='application', subtype='octet-stream', filename=filename)
            with smtplib.SMTP(cfg['server'], cfg['port'], timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(cfg['sender'], cfg['password'])
                server.send_message(msg)
            return True
        except Exception as e:
            current_app.logger.error("Lỗi gửi backup email: %s", e)
            return False

    # --- LOGIC NGHIỆP VỤ ---

    def send_backup_notification(filename, status="Thành công", error=""):
        """Gửi email báo cáo kết quả backup"""
        try:
            with app.app_context():
                subject = f"[POS SYSTEM] Báo cáo sao lưu - {status}"
                body = (f"Hệ thống vừa thực hiện sao lưu vào: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
                        f"Tên file: {filename}\nTrạng thái: {status}")
                if error: body += f"\nLỗi chi tiết: {error}"
            
                msg = Message(subject, recipients=['admin_email@gmail.com']) # Email nhận
                msg.body = body
                mail.send(msg)
        except Exception as e:
            print(f"Lỗi gửi email: {e}")

    # Helper function dùng trong Template để lấy giá trị cài đặt nhanh
    @app.context_processor
    def utility_processor():
        def get_setting(key, default=''):
            conn = get_db_connection()
            res = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            conn.close()
            return res['value'] if res else default
        return dict(get_setting=get_setting)

    #=== Kết Thúc Phần Backup===#

    #=== API Esign ký file XML để nộp cơ quan thuế===#
    def sign_xml_online(xml_content):
        response = requests.post(
            "https://api-esign-provider/sign",
            headers={
                "Authorization": "Bearer YOUR_API_KEY"
            },
            json={
                "xml": xml_content,
                "signer": "Trần Thị Mỹ Dung"
            }
        )
        return response.json()["signed_xml"]


    # === KÝ SỐ eSign (giữ nguyên nếu đã có, hoặc dùng API) ===
    # Route: Lưu cấu hình eSign từ frontend
    @app.route('/api/settings/esign', methods=['POST'])
    @login_required
    @tenant_settings_required
    def save_esign_settings():
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "Không nhận được dữ liệu JSON"}), 400

        # Lấy các giá trị từ request
        provider_name = data.get('provider_name', '').strip()
        if not provider_name:
            return jsonify({"success": False, "error": "provider_name là bắt buộc"}), 400
        if not get_provider_meta(provider_name):
            return jsonify({"success": False, "error": f"Nhà cung cấp không hỗ trợ: {provider_name}"}), 400

        api_url = data.get('api_url', '').strip().rstrip('/')
        auto_issue_invoice = 1 if data.get('auto_issue_invoice') in (True, 'true', '1', 1) else 0

        # Các trường có thể để trống hoặc giữ nguyên giá trị cũ nếu không gửi mới
        sensitive_fields = ['password', 'app_secret', 'esign_pin', 'etax_password', 'etax_cvalue', 'etax_ckey']
        normal_fields = [
            'username', 'api_key', 'serial_number', 'tax_code',
            'invoice_series', 'invoice_type', 'sign_service_url',
            'minvoice_cctbao_id',
        ]

        conn = None
        cursor = None
        try:
            from db_utils import (
                begin_immediate,
                locked_user_message,
                sqlite_write_retry,
                _is_locked_error,
            )
            from db.init import ensure_invoice_settings_schema

            def _persist():
                nonlocal conn, cursor
                conn = get_db_connection()
                cursor = conn.cursor()
                ensure_invoice_settings_schema(conn)

                cursor.execute("""
                    SELECT username, password, api_key, app_secret, serial_number, 
                           tax_code, invoice_series, invoice_type, 
                           etax_password, etax_cvalue, etax_ckey,
                           api_url, sign_service_url, misa_has_code,
                           minvoice_cctbao_id, minvoice_has_code,
                           auto_issue_schedule, auto_sync_purchase, purchase_api_url
                    FROM invoice_settings 
                    WHERE provider_name = ?
                """, (provider_name,))
            
                row = cursor.fetchone()
                old = dict(zip([
                    'username', 'password', 'api_key', 'app_secret', 'serial_number',
                    'tax_code', 'invoice_series', 'invoice_type',
                    'etax_password', 'etax_cvalue', 'etax_ckey', 'api_url',
                    'sign_service_url', 'misa_has_code',
                    'minvoice_cctbao_id', 'minvoice_has_code',
                    'auto_issue_schedule', 'auto_sync_purchase', 'purchase_api_url',
                ], row)) if row else {}

                if 'auto_issue_schedule' in data:
                    auto_issue_schedule = 1 if data.get('auto_issue_schedule') in (True, 'true', '1', 1) else 0
                else:
                    auto_issue_schedule = 1 if str(old.get('auto_issue_schedule') or '0') in ('1', 'True', 'true') else 0
                if not auto_issue_invoice:
                    auto_issue_schedule = 0

                if 'auto_sync_purchase' in data:
                    auto_sync_purchase = 1 if data.get('auto_sync_purchase') in (True, 'true', '1', 1) else 0
                else:
                    default_sync = '1' if provider_name == 'matbao' else '0'
                    auto_sync_purchase = 1 if str(old.get('auto_sync_purchase', default_sync) or default_sync) in ('1', 'True', 'true') else 0

                purchase_api_url = (data.get('purchase_api_url') or '').strip().rstrip('/')
                if not purchase_api_url:
                    purchase_api_url = (old.get('purchase_api_url') or '').strip().rstrip('/')

                values = {
                    'provider_name': provider_name,
                    'api_url': api_url if api_url else old.get('api_url', ''),
                    'purchase_api_url': purchase_api_url,
                    'username': data.get('username', old.get('username', '')),
                    'api_key': data.get('api_key', old.get('api_key', '')),
                    'serial_number': data.get('serial_number', old.get('serial_number', '')),
                    'tax_code': str(data.get('tax_code', old.get('tax_code', '')) or '').strip(),
                    'invoice_series': str(data.get('invoice_series', old.get('invoice_series', 'C26MES')) or '').strip() or 'C26MES',
                    'invoice_type': str(data.get('invoice_type', old.get('invoice_type', '2')) or '').strip() or '2',
                    'sign_service_url': data.get('sign_service_url', old.get('sign_service_url', '')),
                    'misa_has_code': 1 if data.get('misa_has_code') in (True, 'true', '1', 1) else 0,
                    'minvoice_cctbao_id': data.get('minvoice_cctbao_id', old.get('minvoice_cctbao_id', '')),
                    'minvoice_has_code': 1 if data.get('minvoice_has_code') in (True, 'true', '1', 1) else 0,
                    'etax_cvalue': data.get('etax_cvalue', old.get('etax_cvalue', '')),
                    'etax_ckey': data.get('etax_ckey', old.get('etax_ckey', '')),
                    'auto_issue_invoice': auto_issue_invoice,
                    'auto_issue_schedule': auto_issue_schedule,
                    'auto_sync_purchase': auto_sync_purchase,
                    'is_active': 1,
                    'updated_at': 'datetime("now")'
                }

                for field in sensitive_fields:
                    new_val = data.get(field)
                    if new_val is not None and str(new_val).strip() != "":
                        values[field] = str(new_val).strip()
                    else:
                        values[field] = old.get(field, '')

                sql = """
                    INSERT OR REPLACE INTO invoice_settings (
                        provider_name, api_url, purchase_api_url, username, password, api_key, app_secret,
                        serial_number, tax_code, invoice_series, invoice_type,
                        sign_service_url, misa_has_code,
                        minvoice_cctbao_id, minvoice_has_code,
                        etax_password, etax_cvalue, etax_ckey,
                        auto_issue_invoice, auto_issue_schedule, auto_sync_purchase,
                        is_active, updated_at
                    ) VALUES (
                        :provider_name, :api_url, :purchase_api_url, :username, :password, :api_key, :app_secret,
                        :serial_number, :tax_code, :invoice_series, :invoice_type,
                        :sign_service_url, :misa_has_code,
                        :minvoice_cctbao_id, :minvoice_has_code,
                        :etax_password, :etax_cvalue, :etax_ckey,
                        :auto_issue_invoice, :auto_issue_schedule, :auto_sync_purchase,
                        :is_active, datetime('now')
                    )
                """

                begin_immediate(conn, label='save_esign_settings')
                cursor.execute(
                    "UPDATE invoice_settings SET is_active = 0, auto_issue_schedule = 0 WHERE provider_name != ?",
                    (provider_name,),
                )
                cursor.execute(sql, values)
                sqlite_commit(conn, label='settings')

            sqlite_write_retry(_persist, label='save_esign_settings')

            return jsonify({
                "success": True,
                "message": f"Đã lưu cấu hình cho nhà cung cấp {provider_name} thành công"
            })

        except sqlite3.OperationalError as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            from db_utils import locked_user_message, _is_locked_error
            if _is_locked_error(e):
                logging.warning("save_esign_settings locked: %s", e)
                return jsonify({
                    "success": False,
                    "error": locked_user_message(),
                }), 503
            logging.error(f"Lỗi khi lưu cấu hình eSign: {str(e)}", exc_info=True)
            return jsonify({
                "success": False,
                "error": f"Lỗi hệ thống: {str(e)}"
            }), 500

        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logging.error(f"Lỗi khi lưu cấu hình eSign: {str(e)}", exc_info=True)
            return jsonify({
                "success": False,
                "error": f"Lỗi hệ thống: {str(e)}"
            }), 500

        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @app.route('/api/settings/esign', methods=['GET'])
    @login_required
    @tenant_settings_required
    def get_esign_settings():

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            from db.init import ensure_invoice_settings_schema
            ensure_invoice_settings_schema(conn)

            cursor.execute("""
                SELECT provider_name, api_url, purchase_api_url, username, app_id, 
                       serial_number, tax_code, invoice_series, invoice_type, 
                       sign_service_url, misa_has_code,
                       minvoice_cctbao_id, minvoice_has_code,
                       password, app_secret, esign_pin, auto_issue_invoice,
                       auto_issue_schedule, auto_sync_purchase,
                       etax_password, etax_cvalue, etax_ckey, api_key
                FROM invoice_settings
                ORDER BY is_active DESC, updated_at DESC
                LIMIT 1
            """)

            row = cursor.fetchone()

            if not row:
                return jsonify({
                    "success": True,
                    "data": None,
                    "providers": list_providers_for_ui(),
                    "message": "Chưa có cấu hình hóa đơn điện tử."
                })

            res_data = dict(row)

            # FIX: chuẩn hóa auto_issue_invoice
            res_data['auto_issue_invoice'] = int(res_data.get('auto_issue_invoice', 0))
            res_data['auto_issue_schedule'] = int(res_data.get('auto_issue_schedule') or 0)
            res_data['auto_sync_purchase'] = int(res_data.get('auto_sync_purchase') if res_data.get('auto_sync_purchase') is not None else 1)
            res_data['misa_has_code'] = int(res_data.get('misa_has_code') or 0)
            res_data['minvoice_has_code'] = int(res_data.get('minvoice_has_code') if res_data.get('minvoice_has_code') is not None else 1)

            # Bảo mật
            sensitive_fields = ['password', 'app_secret', 'esign_pin', 'etax_password', 'etax_cvalue', 'etax_ckey']

            for field in sensitive_fields:
                if field in res_data:
                    res_data[field] = "********" if res_data[field] else ""

            return jsonify({
                "success": True,
                "data": res_data,
                "providers": list_providers_for_ui(),
            })

        except Exception as e:

            logging.error(f"Lỗi lấy cấu hình eSign: {str(e)}")

            return jsonify({
                "success": False,
                "error": str(e)
            }), 500

        finally:
            if conn:
                conn.close()
