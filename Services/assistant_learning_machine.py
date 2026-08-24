# -*- coding: utf-8 -*-
"""Learning Machine — chẩn đoán lỗi hệ thống, tự sửa an toàn, học câu trả lời cho trợ lý AI."""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from typing import Any

from db_utils import BASE_DIR, MAIN_DB_PATH, open_sqlite, sqlite_commit

logger = logging.getLogger(__name__)

_ISSUE_PATTERNS: list[dict[str, Any]] = [
    {
        'code': 'google_login',
        'keywords': [
            'google', 'gmail', 'oauth', 'client id', 'redirect', 'đăng nhập google',
            'dang nhap google', 'one tap', 'google identity', 'phiên google',
            'redirect_uri', 'access_denied', 'gocspx',
        ],
        'pages': ['login', 'login_2fa'],
    },
    {
        'code': 'otp',
        'keywords': [
            'otp', 'mã xác thực', 'ma xac thuc', 'mã sms', 'không nhận được mã',
            'khong nhan duoc ma', 'gửi mã', 'gui ma', 'mã email', '2fa', 'xác minh',
            'send otp', 'mã không đúng',
        ],
        'pages': ['login_2fa', 'login'],
    },
    {
        'code': 'db_locked',
        'keywords': [
            'database is locked', 'database locked', 'khóa database', 'khoa database',
            'locked', 'sqlite locked', 'bị khóa', 'bi khoa', 'không lưu được',
        ],
    },
    {
        'code': 'session',
        'keywords': [
            'phiên hết hạn', 'phien het han', 'session', 'cookie', 'www', 'ketoshop',
            'đăng xuất', 'bị đá', 'unauthorized', 'chưa đăng nhập',
        ],
        'pages': ['login', 'login_2fa'],
    },
    {
        'code': 'login_general',
        'keywords': [
            'không đăng nhập', 'khong dang nhap', 'đăng nhập lỗi', 'sai mật khẩu',
            'quên mật khẩu', 'tài khoản', 'không vào được',
        ],
        'pages': ['login', 'login_2fa', 'forgot_password'],
    },
]

_LEARNED_ANSWERS: dict[str, str] = {
    'google_login': (
        '**Đăng nhập Google không được** — kiểm tra theo thứ tự:\n\n'
        '1. **Master → Thiết lập tổng quản trị → Đăng nhập & OTP**: Client ID dạng '
        '`123…-xxx.apps.googleusercontent.com` (không phải `GOCSPX-…` — đó là Secret).\n'
        '2. Trong Google Cloud Console, thêm **Authorized redirect URI** đúng domain '
        '(cả `https://domain/login/google/callback` và bản `www` nếu dùng).\n'
        '3. Truy cập **một domain cố định** (www hoặc không www), không đổi qua lại.\n'
        '4. Nếu vừa đăng nhập Google rồi bị văng: xóa cookie trình duyệt hoặc thử cửa sổ ẩn danh.\n\n'
        'Quản trị viên có thể chạy **Learning Machine → Kiểm tra hệ thống** để tự sửa cấu hình Google nhầm.'
    ),
    'otp': (
        '**Không nhận được mã OTP / mã sai**:\n\n'
        '1. **Email OTP**: cần cấu hình SMTP (`SMTP_SERVER`, `SENDER_EMAIL`, `APP_PASSWORD` trong `.env`).\n'
        '2. **SMS OTP**: Master bật SMS và nhập API URL/Key tại **Đăng nhập & OTP**.\n'
        '3. Tài khoản phải có **email** (OTP email) hoặc **SĐT** (OTP SMS) trong hồ sơ.\n'
        '4. Mã có hiệu lực vài phút — bấm **Gửi lại** nếu hết hạn.\n'
        '5. Đừng mở nhiều tab đăng nhập cùng lúc (dễ mất phiên `pending_auth`).\n\n'
        'Nếu báo **database is locked** khi gửi OTP: chờ vài giây rồi thử lại; quản trị bật WAL qua Learning Machine.'
    ),
    'db_locked': (
        '**Lỗi database is locked** (SQLite bị khóa tạm thời):\n\n'
        '1. **Thử lại** sau 3–5 giây — thường do nhiều thao tác ghi cùng lúc.\n'
        '2. Quản trị **restart** app/Gunicorn nếu lỗi kéo dài.\n'
        '3. Master chạy **Learning Machine → Kiểm tra & tự sửa** để bật **WAL** cho các file DB.\n'
        '4. Tránh mở cùng file `.db` bằng DB Browser khi app đang chạy.\n\n'
        'Đây là lỗi hạ tầng, không phải sai thao tác kế toán.'
    ),
    'session': (
        '**Phiên đăng nhập / cookie**:\n\n'
        '1. Dùng **một địa chỉ** cố định (ví dụ chỉ `https://ketoshop.pro.vn` hoặc chỉ `www`).\n'
        '2. Trên **localhost HTTP**: app tự tắt `SESSION_COOKIE_SECURE` — nếu VPS HTTPS thì phải bật Secure.\n'
        '3. Xóa cookie site cũ, đăng nhập lại.\n'
        '4. Sau Google OAuth mà về trang login: thường do **redirect URI** hoặc **cookie domain** chưa khớp.\n\n'
        'Liên hệ Master để kiểm tra `SME_SESSION_COOKIE_DOMAIN` trên VPS.'
    ),
    'login_general': (
        '**Không đăng nhập được**:\n\n'
        '1. Kiểm tra **SĐT / tên đăng nhập** và mật khẩu (phân biệt hoa thường).\n'
        '2. Nếu bật **2FA**: hoàn tất bước OTP hoặc Google trên cùng trình duyệt.\n'
        '3. **Quên mật khẩu** → dùng link trên trang login.\n'
        '4. Tài khoản hết hạn thuê bao: liên hệ Zalo **0908870287**.\n'
        '5. Thiết bị mới có thể cần xác minh OTP dù đã đăng nhập Google.'
    ),
}


def _norm(text: str) -> str:
    return (text or '').lower()


def detect_issue(message: str, *, page: str | None = None) -> dict[str, Any] | None:
    msg = _norm(message)
    if not msg:
        return None
    best_code = None
    best_score = 0.0
    for pat in _ISSUE_PATTERNS:
        score = 0.0
        for kw in pat['keywords']:
            if kw in msg or kw.replace(' ', '') in msg.replace(' ', ''):
                score += 2.0
        if page and page in (pat.get('pages') or []):
            score += 1.5
        if score > best_score:
            best_score = score
            best_code = pat['code']
    if not best_code or best_score < 2.0:
        return None
    return {'code': best_code, 'score': best_score}


def _check_google_oauth() -> dict[str, Any]:
    from Services.login_service import (
        get_auth_settings,
        get_google_client_id,
        google_client_id_error,
        google_login_enabled,
        google_oauth_redirect_ready,
    )

    auth = get_auth_settings()
    cid = get_google_client_id()
    err = google_client_id_error(auth.get('google_client_id'))
    enabled = google_login_enabled()
    redirect_ok = google_oauth_redirect_ready()
    swapped = (auth.get('google_client_id') or '').strip().startswith('GOCSPX-')

    if swapped:
        status, message = 'error', 'Client Secret bị nhập nhầm vào ô Client ID (GOCSPX-…).'
        fixable = True
    elif err:
        status, message = 'error', err
        fixable = False
    elif not enabled:
        status, message = 'warn', 'Đăng nhập Google đang tắt hoặc thiếu Client ID hợp lệ.'
        fixable = True
    elif not redirect_ok:
        status, message = 'warn', 'Có Client ID nhưng thiếu Client Secret (cần cho OAuth redirect / dùng thử).'
        fixable = False
    else:
        status, message = 'ok', f'Google OAuth sẵn sàng (Client ID …{cid[-20:] if len(cid) > 20 else cid}).'
        fixable = False

    return {
        'id': 'google_oauth',
        'label': 'Đăng nhập Google',
        'status': status,
        'message': message,
        'fixable': fixable,
        'fix_action': 'repair_google_oauth',
    }


def _check_otp_email() -> dict[str, Any]:
    from Services.email_service import smtp_configured, get_smtp_config

    ok = smtp_configured()
    cfg = get_smtp_config()
    if ok:
        msg = f'SMTP đã cấu hình ({cfg.get("server")}, {cfg.get("sender")}).'
        status = 'ok'
    else:
        msg = 'Chưa cấu hình SMTP (APP_PASSWORD / SENDER_EMAIL trong .env) — OTP email không gửi được.'
        status = 'warn'
    return {
        'id': 'otp_email',
        'label': 'OTP Email (SMTP)',
        'status': status,
        'message': msg,
        'fixable': False,
    }


def _check_otp_sms() -> dict[str, Any]:
    from Services.login_service import get_auth_settings, sms_otp_visible

    auth = get_auth_settings()
    if auth.get('auth_sms_enabled') != '1':
        return {
            'id': 'otp_sms',
            'label': 'OTP SMS',
            'status': 'warn',
            'message': 'OTP SMS đang tắt trong Master Settings.',
            'fixable': False,
        }
    if not (auth.get('sms_api_url') or '').strip():
        return {
            'id': 'otp_sms',
            'label': 'OTP SMS',
            'status': 'warn',
            'message': 'SMS bật nhưng chưa có SMS API URL.',
            'fixable': False,
        }
    return {
        'id': 'otp_sms',
        'label': 'OTP SMS',
        'status': 'ok',
        'message': f'SMS OTP cấu hình ({auth.get("sms_provider") or "generic"}).',
        'fixable': False,
    }


def _db_journal_mode(db_path: str) -> str:
    if not os.path.isfile(db_path):
        return 'missing'
    try:
        with open_sqlite(db_path) as conn:
            row = conn.execute('PRAGMA journal_mode').fetchone()
            return str(row[0]).lower() if row else 'unknown'
    except Exception as exc:
        return f'error:{exc}'


def _check_databases() -> dict[str, Any]:
    paths = [MAIN_DB_PATH]
    tenants_dir = os.path.join(BASE_DIR, 'tenants')
    if os.path.isdir(tenants_dir):
        paths.extend(sorted(glob.glob(os.path.join(tenants_dir, '*.db'))))

    non_wal = []
    missing = []
    for p in paths:
        mode = _db_journal_mode(p)
        if mode == 'missing':
            missing.append(os.path.basename(p))
        elif mode != 'wal':
            non_wal.append({'file': os.path.basename(p), 'mode': mode})

    if non_wal:
        status = 'warn' if len(non_wal) <= 2 else 'error'
        msg = f'{len(non_wal)} DB chưa WAL (dễ locked): ' + ', '.join(x['file'] for x in non_wal[:5])
        if len(non_wal) > 5:
            msg += f' … (+{len(non_wal) - 5})'
        fixable = True
    else:
        status = 'ok'
        msg = f'{len(paths)} database đang dùng WAL.'
        fixable = False

    return {
        'id': 'sqlite_wal',
        'label': 'SQLite WAL',
        'status': status,
        'message': msg,
        'fixable': fixable,
        'fix_action': 'enable_wal_all',
        'details': {'non_wal': non_wal, 'missing': missing, 'total': len(paths)},
    }


def _check_session_cookie() -> dict[str, Any]:
    secure = (os.getenv('SME_SESSION_COOKIE_SECURE') or '').strip().lower()
    domain = (os.getenv('SME_SESSION_COOKIE_DOMAIN') or '').strip()
    parts = []
    if secure in ('0', 'false', 'no'):
        parts.append('SESSION_COOKIE_SECURE=0 (phù hợp localhost HTTP).')
    elif secure:
        parts.append('SESSION_COOKIE_SECURE bật (cần HTTPS).')
    else:
        parts.append('SESSION_COOKIE_SECURE mặc định theo môi trường.')
    if domain:
        parts.append(f'Cookie domain: {domain}.')
    return {
        'id': 'session_cookie',
        'label': 'Cookie phiên',
        'status': 'ok',
        'message': ' '.join(parts),
        'fixable': False,
    }


def _check_assistant() -> dict[str, Any]:
    from Services.support_config import get_assistant_runtime_config

    rt = get_assistant_runtime_config()
    if rt.get('premium_active') and not rt.get('openai_configured'):
        return {
            'id': 'assistant_ai',
            'label': 'Trợ lý AI',
            'status': 'warn',
            'message': 'Chế độ AI Pro nhưng thiếu OPENAI_API_KEY.',
            'fixable': False,
        }
    return {
        'id': 'assistant_ai',
        'label': 'Trợ lý AI',
        'status': 'ok',
        'message': rt.get('ai_mode_label') or 'Miễn phí',
        'fixable': False,
    }


def _read_recent_log_errors(*, limit: int = 8) -> list[str]:
    log_path = os.path.join(BASE_DIR, 'logs', 'app_error.log')
    if not os.path.isfile(log_path):
        return []
    try:
        with open(log_path, encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
        interesting = []
        for line in reversed(lines[-200:]):
            low = line.lower()
            if any(k in low for k in ('error', 'locked', 'exception', 'traceback', 'failed')):
                interesting.append(line.strip()[:240])
            if len(interesting) >= limit:
                break
        return list(reversed(interesting))
    except Exception:
        return []


def _overall_status(checks: list[dict]) -> tuple[str, int]:
    errors = sum(1 for c in checks if c.get('status') == 'error')
    warns = sum(1 for c in checks if c.get('status') == 'warn')
    if errors:
        return 'error', max(0, 100 - errors * 25 - warns * 8)
    if warns:
        return 'warn', max(40, 100 - warns * 12)
    return 'ok', 100


def run_system_diagnostics() -> dict[str, Any]:
    checks = [
        _check_google_oauth(),
        _check_otp_email(),
        _check_otp_sms(),
        _check_databases(),
        _check_session_cookie(),
        _check_assistant(),
    ]
    overall, score = _overall_status(checks)
    recent = _read_recent_log_errors()
    if recent and overall == 'ok':
        overall = 'warn'
        score = min(score, 85)

    return {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'overall': overall,
        'score': score,
        'checks': checks,
        'recent_errors': recent,
        'learning_patterns': len(_ISSUE_PATTERNS),
    }


def _enable_wal_on_path(db_path: str) -> bool:
    if not os.path.isfile(db_path):
        return False
    try:
        with open_sqlite(db_path) as conn:
            mode = conn.execute('PRAGMA journal_mode=WAL').fetchone()
            sqlite_commit(conn, label='assistant_learning_machine')
        return bool(mode and str(mode[0]).lower() == 'wal')
    except Exception as exc:
        logger.warning('enable_wal %s: %s', db_path, exc)
        return False


def apply_auto_fix(action: str) -> dict[str, Any]:
    """Chỉ gọi từ Master API — sửa an toàn, không phá dữ liệu."""
    result: dict[str, Any] = {'action': action, 'success': False, 'message': ''}

    if action == 'repair_google_oauth':
        from Services.login_service import repair_swapped_google_credentials, restore_google_oauth_persist

        fixed_swap = repair_swapped_google_credentials()
        restored = restore_google_oauth_persist()
        parts = []
        if fixed_swap:
            parts.append('Đã hoán đổi Client Secret khỏi ô Client ID.')
        if restored.get('restored'):
            parts.append('Khôi phục Google OAuth từ file persist: ' + ', '.join(restored.get('changed') or []))
        if parts:
            result.update({'success': True, 'message': ' '.join(parts)})
        else:
            result['message'] = 'Không có cấu hình Google cần sửa tự động.'
        return result

    if action == 'enable_wal_all':
        paths = [MAIN_DB_PATH]
        tenants_dir = os.path.join(BASE_DIR, 'tenants')
        if os.path.isdir(tenants_dir):
            paths.extend(glob.glob(os.path.join(tenants_dir, '*.db')))
        ok_count = 0
        for p in paths:
            if _enable_wal_on_path(p):
                ok_count += 1
        result.update({
            'success': ok_count > 0,
            'message': f'Đã bật WAL cho {ok_count}/{len(paths)} database.',
            'fixed_count': ok_count,
        })
        return result

    if action == 'run_all_fixes':
        fixes = []
        for act in ('repair_google_oauth', 'enable_wal_all'):
            r = apply_auto_fix(act)
            if r.get('success'):
                fixes.append(r.get('message') or act)
        result.update({
            'success': bool(fixes),
            'message': '; '.join(fixes) if fixes else 'Không có lỗi tự sửa được.',
            'fixes': fixes,
        })
        return result

    result['message'] = f'Hành động không hỗ trợ: {action}'
    return result


def run_health_with_fixes(*, auto_fix: bool = False) -> dict[str, Any]:
    before = run_system_diagnostics()
    fixes_applied: list[dict[str, Any]] = []

    if auto_fix:
        seen: set[str] = set()
        for chk in before.get('checks') or []:
            act = chk.get('fix_action')
            if not chk.get('fixable') or not act or act in seen:
                continue
            seen.add(str(act))
            fx = apply_auto_fix(str(act))
            if fx.get('success'):
                fixes_applied.append(fx)

    after = run_system_diagnostics()
    report = {
        **after,
        'fixes_applied': fixes_applied,
        'auto_fix_ran': auto_fix,
    }

    try:
        from Services.assistant_store import log_health_run
        log_health_run(report)
    except Exception as exc:
        logger.warning('log_health_run: %s', exc)

    return report


def resolve_learning_machine(
    message: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Trả lời nhanh khi người dùng hỏi về lỗi hệ thống — không chạy full diagnostic."""
    ctx = context or {}
    page = (ctx.get('page') or '').strip()
    hit = detect_issue(message, page=page)
    if not hit:
        return None

    code = hit['code']
    answer = _LEARNED_ANSWERS.get(code, '')
    if not answer:
        return None

    hints: list[str] = []
    if code == 'google_login':
        gchk = _check_google_oauth()
        if gchk['status'] != 'ok':
            hints.append(f'⚠️ {gchk["message"]}')
    elif code == 'otp':
        ech = _check_otp_email()
        sch = _check_otp_sms()
        if ech['status'] != 'ok':
            hints.append(f'⚠️ {ech["message"]}')
        if sch['status'] != 'ok':
            hints.append(f'⚠️ {sch["message"]}')
    elif code == 'db_locked':
        dchk = _check_databases()
        if dchk['status'] != 'ok':
            hints.append(f'⚠️ {dchk["message"]}')

    text = answer
    if hints:
        text += '\n\n**Chẩn đoán nhanh:**\n' + '\n'.join(hints)

    confidence = min(0.92, 0.55 + hit['score'] * 0.08)
    return {
        'text': text,
        'source': 'learning_machine',
        'confidence': confidence,
        'faq_id': f'lm_{code}',
        'needs_escalation': confidence < 0.65,
        'issue_code': code,
    }


__all__ = [
    'apply_auto_fix',
    'detect_issue',
    'resolve_learning_machine',
    'run_health_with_fixes',
    'run_system_diagnostics',
]
