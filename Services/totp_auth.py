"""TOTP (Google Authenticator) + nhận diện thiết bị ổn định giữa các trình duyệt.

Fingerprint máy không dùng User-Agent đầy đủ / IP — Chrome và Edge trên cùng PC
thường trùng platform/màn hình/timezone/CPU → không hỏi TOTP lại.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

import pyotp

MASTER_TRUST_DAYS = int(os.environ.get('SME_MASTER_DEVICE_TRUST_DAYS', '30') or 30)
TOTP_ISSUER = os.environ.get('SME_TOTP_ISSUER', 'KETO Master')


def parse_os_family(user_agent: str) -> str:
    ua = (user_agent or '').lower()
    if 'windows' in ua:
        return 'windows'
    if 'android' in ua:
        return 'android'
    if 'iphone' in ua or 'ipad' in ua or 'ios' in ua:
        return 'ios'
    if 'mac os' in ua or 'macintosh' in ua:
        return 'macos'
    if 'linux' in ua or 'cros' in ua:
        return 'linux'
    return 'other'


def parse_device_signal(raw: str | dict | None) -> dict[str, str]:
    """Chuẩn hóa tín hiệu thiết bị từ form/JSON (JS gửi lên)."""
    if raw is None:
        return {}
    data = raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            import json
            data = json.loads(raw)
        except Exception:
            return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    # device_id (localStorage) ổn định nhất trên cùng trình duyệt.
    # Không dùng deviceMemory — Chrome-only và hay đổi/thiếu → fingerprint lệch → hỏi TOTP lại.
    for key in ('device_id', 'platform', 'screen', 'timezone', 'cores', 'touch', 'lang'):
        val = data.get(key)
        if val is None:
            continue
        text = re.sub(r'\s+', ' ', str(val).strip())[:80]
        if text:
            out[key] = text
    return out


def machine_fingerprint(user_agent: str, accept_language: str, signal: dict | None = None) -> str:
    """Vân tay máy — ổn định giữa trình duyệt khác trên cùng thiết bị."""
    signal = signal or {}
    os_family = parse_os_family(user_agent)
    lang = (signal.get('lang') or (accept_language or '').split(',')[0] or '').strip()[:24]
    parts = [
        os_family,
        signal.get('device_id') or '',
        signal.get('platform') or '',
        signal.get('screen') or '',
        signal.get('timezone') or '',
        signal.get('cores') or '',
        signal.get('touch') or '',
        lang.lower(),
    ]
    raw = '|'.join(parts)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def fingerprint_from_request(request, signal: dict | None = None) -> str:
    return machine_fingerprint(
        request.headers.get('User-Agent', ''),
        request.headers.get('Accept-Language', ''),
        signal,
    )


def ensure_totp_columns(conn: sqlite3.Connection) -> None:
    """Thêm cột TOTP nếu thiếu — dùng chung SQLite / PostgreSQL."""
    try:
        from db.schema_helpers import add_column_if_missing
        add_column_if_missing(conn, 'users', 'totp_secret', 'TEXT')
        add_column_if_missing(conn, 'users', 'totp_confirmed_at', 'TEXT')
        return
    except Exception:
        pass
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(users)')}
        if 'totp_secret' not in cols:
            conn.execute('ALTER TABLE users ADD COLUMN totp_secret TEXT')
        if 'totp_confirmed_at' not in cols:
            conn.execute('ALTER TABLE users ADD COLUMN totp_confirmed_at TEXT')
    except Exception:
        pass


def get_user_totp_secret(conn: sqlite3.Connection, user_id: int) -> str | None:
    """Secret đã xác nhận (đủ điều kiện xác thực đăng nhập)."""
    ensure_totp_columns(conn)
    row = conn.execute(
        'SELECT totp_secret, totp_confirmed_at FROM users WHERE id = ?',
        (user_id,),
    ).fetchone()
    if not row:
        return None
    secret = row['totp_secret'] if hasattr(row, 'keys') else row[0]
    confirmed = row['totp_confirmed_at'] if hasattr(row, 'keys') else row[1]
    if not secret or not confirmed:
        return None
    return str(secret).strip() or None


def get_user_totp_secret_any(conn: sqlite3.Connection, user_id: int) -> str | None:
    """Secret đã lưu (kể cả chưa confirm) — tái dùng khi setup để tránh tạo nhiều QR."""
    ensure_totp_columns(conn)
    row = conn.execute(
        'SELECT totp_secret FROM users WHERE id = ?',
        (user_id,),
    ).fetchone()
    if not row:
        return None
    secret = row['totp_secret'] if hasattr(row, 'keys') else row[0]
    return str(secret).strip() if secret else None


def save_totp_draft(conn: sqlite3.Connection, user_id: int, secret: str) -> None:
    """Lưu secret nháp khi hiện QR — chưa confirm nên chưa dùng để đăng nhập."""
    ensure_totp_columns(conn)
    conn.execute(
        """
        UPDATE users
        SET totp_secret = ?, totp_confirmed_at = NULL
        WHERE id = ? AND (totp_secret IS NULL OR totp_confirmed_at IS NULL)
        """,
        (secret, user_id),
    )


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(
        name=username or 'master',
        issuer_name=TOTP_ISSUER,
    )


def qr_png_data_url(otpauth_uri: str) -> str:
    """Tạo data-URL QR (PNG ưu tiên; SVG nếu thiếu Pillow — hay gặp trên VPS)."""
    try:
        import qrcode
    except ImportError as exc:
        raise RuntimeError(
            "Thiếu thư viện qrcode. Chạy: pip install 'qrcode[pil]==8.2'"
        ) from exc

    uri = (otpauth_uri or '').strip()
    if not uri:
        raise ValueError('otpauth_uri trống')

    # 1) PNG qua Pillow (nếu có)
    try:
        from qrcode.image.pil import PilImage
        img = qrcode.make(uri, image_factory=PilImage)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        return f'data:image/png;base64,{b64}'
    except Exception:
        pass

    # 2) Fallback SVG — không cần Pillow (phù hợp VPS tối giản)
    try:
        from qrcode.image.svg import SvgPathImage
        img = qrcode.make(uri, image_factory=SvgPathImage)
        buf = io.BytesIO()
        img.save(buf)
        raw = buf.getvalue()
        if isinstance(raw, str):
            raw = raw.encode('utf-8')
        b64 = base64.b64encode(raw).decode('ascii')
        return f'data:image/svg+xml;base64,{b64}'
    except Exception as exc:
        raise RuntimeError(
            "Không tạo được QR. Cài: pip install 'qrcode[pil]==8.2' pillow"
        ) from exc


def safe_qr_data_url(otpauth_uri: str) -> str:
    """Không ném lỗi — trả '' nếu tạo QR thất bại."""
    try:
        return qr_png_data_url(otpauth_uri) or ''
    except Exception:
        return ''


def verify_totp_code(secret: str, code: str, *, window: int = 1) -> bool:
    code = re.sub(r'\D', '', str(code or ''))
    if len(code) != 6 or not secret:
        return False
    totp = pyotp.TOTP(secret)
    return bool(totp.verify(code, valid_window=window))


def save_totp_secret(conn: sqlite3.Connection, user_id: int, secret: str) -> None:
    ensure_totp_columns(conn)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        """
        UPDATE users
        SET totp_secret = ?, totp_confirmed_at = ?, is_2fa_enabled = 1
        WHERE id = ?
        """,
        (secret, now, user_id),
    )


def clear_totp_secret(conn: sqlite3.Connection, user_id: int) -> None:
    ensure_totp_columns(conn)
    conn.execute(
        """
        UPDATE users
        SET totp_secret = NULL, totp_confirmed_at = NULL
        WHERE id = ?
        """,
        (user_id,),
    )


def is_device_trusted(
    conn: sqlite3.Connection,
    username: str,
    fingerprint: str,
    *,
    trust_days: int = MASTER_TRUST_DAYS,
) -> bool:
    if not username or not fingerprint:
        return False
    row = conn.execute(
        """
        SELECT last_login FROM user_trusted_devices
        WHERE username = ? AND device_fingerprint = ?
        """,
        (username, fingerprint),
    ).fetchone()
    if not row:
        return False
    last_login_val = row['last_login'] if hasattr(row, 'keys') else row[0]
    if not last_login_val:
        return False
    try:
        last_login_dt = datetime.strptime(str(last_login_val)[:19], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return False
    return datetime.now() - last_login_dt <= timedelta(days=max(1, trust_days))


def remember_trusted_device(
    conn: sqlite3.Connection,
    username: str,
    fingerprint: str,
) -> None:
    if not username or not fingerprint:
        return
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        """
        INSERT INTO user_trusted_devices (username, device_fingerprint, last_login)
        VALUES (?, ?, ?)
        ON CONFLICT(username, device_fingerprint) DO UPDATE SET last_login = excluded.last_login
        """,
        (username, fingerprint, now),
    )


def totp_status_for_user(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    ensure_totp_columns(conn)
    row = conn.execute(
        """
        SELECT COALESCE(is_2fa_enabled, 0) AS is_2fa_enabled,
               totp_secret, totp_confirmed_at
        FROM users WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    if not row:
        return {'enabled': False, 'configured': False}
    secret = row['totp_secret'] if hasattr(row, 'keys') else row[1]
    confirmed = row['totp_confirmed_at'] if hasattr(row, 'keys') else row[2]
    enabled = bool(row['is_2fa_enabled'] if hasattr(row, 'keys') else row[0])
    configured = bool(secret and confirmed)
    return {
        'enabled': enabled,
        'configured': configured,
        'confirmed_at': confirmed,
    }
