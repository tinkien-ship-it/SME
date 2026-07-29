"""Helper đăng nhập: Google OAuth, OTP SMS, tra cứu tài khoản."""
import json
import os
import re
import sqlite3
from pathlib import Path

import requests
from dotenv import load_dotenv

from db_utils import BASE_DIR, MAIN_DB_PATH

REGISTRY_PATH = MAIN_DB_PATH
_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")
_AUTH_FILE = _BASE_DIR / "config" / "auth.local.json"

AUTH_SETTING_KEYS = (
    "auth_google_enabled",
    "auth_sms_enabled",
    "google_client_id",
    "google_client_secret",
    "sms_provider",
    "sms_api_url",
    "sms_api_key",
    "sms_api_secret",
    "sms_brandname",
)


def _get_main_setting(key, default=""):
    try:
        with sqlite3.connect(MAIN_DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
            return row[0] if row else default
    except Exception:
        return default


def _set_main_setting(key, value):
    with sqlite3.connect(MAIN_DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        conn.commit()


def _load_auth_file():
    """Đọc config/auth.local.json (copy từ auth.local.json.example)."""
    if not _AUTH_FILE.exists():
        return {}
    try:
        with open(_AUTH_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _pick(*values):
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def repair_swapped_google_credentials():
    """Chuyển Client Secret nhầm vào ô Client ID (GOCSPX-...) về đúng chỗ trong DB."""
    cid = _get_main_setting("google_client_id", "")
    secret = _get_main_setting("google_client_secret", "")
    if not cid.startswith("GOCSPX-"):
        return False
    if not secret:
        _set_main_setting("google_client_secret", cid)
    _set_main_setting("google_client_id", "")
    return True


def get_auth_settings_db():
    """Chỉ đọc từ DB — dùng cho form Master Settings (không gộp .env)."""
    db = {k: _get_main_setting(k, "") for k in AUTH_SETTING_KEYS}
    return {
        "auth_google_enabled": db.get("auth_google_enabled") or "1",
        "auth_sms_enabled": db.get("auth_sms_enabled") or "1",
        "google_client_id": db.get("google_client_id") or "",
        "google_client_secret": db.get("google_client_secret") or "",
        "sms_provider": db.get("sms_provider") or "generic",
        "sms_api_url": db.get("sms_api_url") or "",
        "sms_api_key": db.get("sms_api_key") or "",
        "sms_api_secret": db.get("sms_api_secret") or "",
        "sms_brandname": db.get("sms_brandname") or "KETO POS",
    }


def get_auth_settings():
    """Đọc cấu hình: .env → config/auth.local.json → DB Master Settings."""
    file_cfg = _load_auth_file()
    db = {k: _get_main_setting(k, "") for k in AUTH_SETTING_KEYS}
    return {
        "auth_google_enabled": _pick(db.get("auth_google_enabled"), "1"),
        "auth_sms_enabled": _pick(db.get("auth_sms_enabled"), "1"),
        "google_client_id": _pick(
            os.getenv("GOOGLE_CLIENT_ID"),
            file_cfg.get("google_client_id"),
            db.get("google_client_id"),
        ),
        "google_client_secret": _pick(
            os.getenv("GOOGLE_CLIENT_SECRET"),
            file_cfg.get("google_client_secret"),
            db.get("google_client_secret"),
        ),
        "sms_provider": _pick(os.getenv("SMS_PROVIDER"), file_cfg.get("sms_provider"), db.get("sms_provider"), "generic"),
        "sms_api_url": _pick(os.getenv("SMS_API_URL"), file_cfg.get("sms_api_url"), db.get("sms_api_url")),
        "sms_api_key": _pick(os.getenv("SMS_API_KEY"), file_cfg.get("sms_api_key"), db.get("sms_api_key")),
        "sms_api_secret": _pick(os.getenv("SMS_API_SECRET"), file_cfg.get("sms_api_secret"), db.get("sms_api_secret")),
        "sms_brandname": _pick(os.getenv("SMS_BRANDNAME"), file_cfg.get("sms_brandname"), db.get("sms_brandname"), "KETO POS"),
    }


def save_auth_settings(data):
    """Lưu cấu hình vào DB chính (Master Settings)."""
    cid = (data.get("google_client_id") or "").strip()
    secret = (data.get("google_client_secret") or "").strip()
    if not cid:
        cid = (_get_main_setting("google_client_id", "") or "").strip()
    if cid.startswith("GOCSPX-"):
        raise ValueError(
            "Nhầm Client Secret vào ô Client ID. "
            "Client ID có dạng 123456789-xxxx.apps.googleusercontent.com"
        )
    if cid and ".apps.googleusercontent.com" not in cid:
        raise ValueError(
            "Google Client ID không hợp lệ. "
            "Phải copy từ Google Cloud → Credentials → OAuth 2.0 Client IDs (Web)."
        )

    mapping = {
        "auth_google_enabled": "1" if data.get("auth_google_enabled") in (True, 1, "1", "true") else "0",
        "auth_sms_enabled": "1" if data.get("auth_sms_enabled") in (True, 1, "1", "true") else "0",
        "google_client_id": cid,
        "google_client_secret": secret,
        "sms_provider": (data.get("sms_provider") or "generic").strip(),
        "sms_api_url": (data.get("sms_api_url") or "").strip(),
        "sms_api_key": (data.get("sms_api_key") or "").strip(),
        "sms_api_secret": (data.get("sms_api_secret") or "").strip(),
        "sms_brandname": (data.get("sms_brandname") or "KETO POS").strip(),
    }
    for key, value in mapping.items():
        if key in ("google_client_secret", "sms_api_key", "sms_api_secret"):
            if value:
                _set_main_setting(key, value)
            continue
        if key == "google_client_id" and not (data.get("google_client_id") or "").strip() and not value:
            continue
        _set_main_setting(key, value)
    return get_auth_settings_db()


def google_login_visible():
    return get_auth_settings()["auth_google_enabled"] == "1"


def sms_otp_visible():
    return get_auth_settings()["auth_sms_enabled"] == "1"


def google_login_enabled():
    """Chỉ cần Client ID — đăng nhập qua Gmail trên trình duyệt (Google Identity Services)."""
    if get_auth_settings()["auth_google_enabled"] != "1":
        return False
    return bool(get_google_client_id())


def get_google_client_id():
    cid = (get_auth_settings().get("google_client_id") or "").strip()
    if not cid:
        return ""
    if cid.startswith("GOCSPX-"):
        return ""
    if ".apps.googleusercontent.com" not in cid:
        return ""
    return cid


def google_oauth_redirect_ready():
    """OAuth redirect (đăng ký dùng thử) cần cả Client ID và Secret."""
    if not google_login_enabled():
        return False
    secret = (get_auth_settings().get("google_client_secret") or "").strip()
    return bool(secret)


def get_public_base_url(fallback_root: str | None = None) -> str:
    """URL gốc công khai — ưu tiên PUBLIC_BASE_URL (.env) khi đứng sau proxy."""
    base = (os.getenv("PUBLIC_BASE_URL") or os.getenv("APP_BASE_URL") or "").strip().rstrip("/")
    if base:
        return base
    return (fallback_root or "").rstrip("/")


_OAUTH_CALLBACK_ENDPOINTS = (
    "login_google_callback",
    "trial_google_callback",
    "authorize_google_2fa",
)


def oauth_redirect_uri(endpoint: str) -> str:
    """
    Redirect URI OAuth — luôn khớp host/port trình duyệt đang mở.
    Tránh redirect_uri_mismatch khi PUBLIC_BASE_URL trỏ production nhưng test localhost.
    """
    from flask import has_request_context, request, url_for

    try:
        if has_request_context() and getattr(request, "url_root", None):
            root = request.url_root.rstrip("/")
            path = url_for(endpoint, _external=False)
            if path.startswith("http://") or path.startswith("https://"):
                return path
            return f"{root}{path}"
        return url_for(endpoint, _external=True)
    except Exception:
        return ""


def _localhost_mirror_root(url_root: str) -> str | None:
    from urllib.parse import urlparse

    parsed = urlparse((url_root or "").rstrip("/"))
    host = parsed.hostname or ""
    if host not in ("127.0.0.1", "localhost"):
        return None
    port = parsed.port or 5000
    alt = "localhost" if host == "127.0.0.1" else "127.0.0.1"
    return f"http://{alt}:{port}"


def google_oauth_setup_hints(base_url: str | None = None) -> dict:
    """
    Gợi ý cấu hình Google Cloud Console — sửa lỗi origin_mismatch / redirect_uri_mismatch.
    """
    from flask import has_request_context, request, url_for
    from urllib.parse import urlparse

    root = get_public_base_url(base_url)
    origins: list[str] = []
    redirects: list[str] = []
    current_redirects: list[str] = []

    def _add_origin(url: str) -> None:
        u = (url or "").strip().rstrip("/")
        if u and u not in origins:
            origins.append(u)

    def _add_redirect(url: str) -> None:
        u = (url or "").strip()
        if u and u not in redirects:
            redirects.append(u)

    def _add_redirects_for_base(base: str) -> None:
        b = (base or "").strip().rstrip("/")
        if not b:
            return
        for ep in _OAUTH_CALLBACK_ENDPOINTS:
            try:
                path = url_for(ep, _external=False)
            except Exception:
                continue
            if path.startswith("http://") or path.startswith("https://"):
                _add_redirect(path)
            else:
                _add_redirect(f"{b}{path}")

    # URI đang dùng thực tế (host/port trình duyệt) — quan trọng khi test localhost
    if has_request_context() and getattr(request, "url_root", None):
        req_root = request.url_root.rstrip("/")
        _add_origin(req_root)
        mirror = _localhost_mirror_root(req_root)
        if mirror:
            _add_origin(mirror)
        for ep in _OAUTH_CALLBACK_ENDPOINTS:
            try:
                uri = oauth_redirect_uri(ep)
            except Exception:
                continue
            if not uri:
                continue
            current_redirects.append(uri)
            _add_redirect(uri)
            if mirror:
                parsed = urlparse(uri)
                _add_redirect(f"{mirror}{parsed.path}")

    if root:
        parsed = urlparse(root)
        if parsed.scheme and parsed.netloc:
            _add_origin(f"{parsed.scheme}://{parsed.netloc}")
            host = parsed.hostname or ""
            port = parsed.port
            if host in ("127.0.0.1", "localhost"):
                _add_origin(f"http://127.0.0.1:{port or 5000}")
                _add_origin(f"http://localhost:{port or 5000}")
            if parsed.scheme == "http" and host not in ("127.0.0.1", "localhost"):
                _add_origin(f"https://{parsed.netloc}")
        _add_redirects_for_base(root)

    for extra in (os.getenv("GOOGLE_EXTRA_ORIGINS") or "").split(","):
        _add_origin(extra.strip())

    _add_origin("http://127.0.0.1:5000")
    _add_origin("http://localhost:5000")
    for local_base in ("http://127.0.0.1:5000", "http://localhost:5000"):
        _add_redirects_for_base(local_base)

    return {
        "javascript_origins": origins,
        "redirect_uris": redirects,
        "current_redirect_uris": current_redirects,
        "public_base_url": root,
        "request_base_url": request.url_root.rstrip("/") if has_request_context() else "",
        "redirect_ready": google_oauth_redirect_ready(),
        "is_localhost": has_request_context()
        and (request.host or "").split(":")[0] in ("127.0.0.1", "localhost"),
    }


def google_client_id_error(client_id=None):
    """Thông báo nếu cấu hình Google Client ID sai định dạng."""
    if client_id is None:
        raw = (get_auth_settings().get("google_client_id") or "").strip()
    else:
        raw = (client_id or "").strip()
    if not raw:
        return ""
    if raw.startswith("GOCSPX-"):
        return (
            "Bạn đã nhập nhầm Client Secret (GOCSPX-...) vào ô Client ID. "
            "Client ID phải có dạng 123456789-xxxx.apps.googleusercontent.com"
        )
    if ".apps.googleusercontent.com" not in raw:
        return "Google Client ID không đúng định dạng (phải kết thúc bằng .apps.googleusercontent.com)"
    return ""


def verify_google_credential(credential):
    """
    Xác minh JWT từ nút Google / One Tap (tài khoản Gmail đang lưu trên trình duyệt).
    Trả về (user_info dict, error message).
    """
    credential = (credential or "").strip()
    if not credential:
        return None, "Thiếu mã xác thực Google"

    client_id = get_google_client_id()
    if not client_id:
        return None, "Chưa cấu hình GOOGLE_CLIENT_ID (Master Settings hoặc .env)"

    try:
        resp = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": credential},
            timeout=15,
        )
        if not resp.ok:
            return None, "Phiên Google không hợp lệ hoặc đã hết hạn"

        data = resp.json()
        if data.get("aud") != client_id:
            return None, "Client ID Google không khớp"

        if str(data.get("email_verified", "")).lower() not in ("true", "1"):
            return None, "Email Google chưa được xác minh"

        email = (data.get("email") or "").strip().lower()
        if not email:
            return None, "Không lấy được email từ Google"

        return {
            "email": email,
            "name": data.get("name"),
            "picture": data.get("picture"),
        }, None
    except Exception as exc:
        return None, str(exc)


def sms_otp_enabled():
    cfg = get_auth_settings()
    if cfg["auth_sms_enabled"] != "1":
        return False
    return bool(cfg["sms_api_url"] and cfg["sms_api_key"])


def configure_google_oauth(google):
    """Áp Client ID/Secret trước khi redirect OAuth."""
    cfg = get_auth_settings()
    client_id = get_google_client_id()
    client_secret = cfg.get("google_client_secret") or ""
    if not client_id or not client_secret:
        return False
    google.client_id = client_id
    google.client_secret = client_secret
    return True


def fetch_google_user_info(google):
    """Lấy email/profile sau OAuth callback (Authlib OpenID Connect)."""
    token = google.authorize_access_token()
    userinfo = token.get("userinfo")
    if userinfo is not None:
        return dict(userinfo) if not isinstance(userinfo, dict) else userinfo

    resp = google.get("https://openidconnect.googleapis.com/v1/userinfo")
    if resp.ok:
        return resp.json()
    raise ValueError(f"Google userinfo lỗi: {resp.status_code} {resp.text[:200]}")


def normalize_vn_phone(phone):
    """Chuẩn hóa SĐT VN → 84xxxxxxxxx."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if digits.startswith("84"):
        normalized = digits
    elif digits.startswith("0"):
        normalized = "84" + digits[1:]
    elif len(digits) == 9:
        normalized = "84" + digits
    else:
        normalized = digits
    if len(normalized) < 11 or len(normalized) > 12:
        return None
    return normalized


def _abs_db_path(db_path_raw):
    if not db_path_raw:
        return os.path.join(BASE_DIR, "database.db")
    if os.path.isabs(db_path_raw):
        return db_path_raw
    return os.path.join(BASE_DIR, db_path_raw)


def find_user_by_email(email):
    """Tìm user theo email (mapping tenant hoặc DB main)."""
    email = (email or "").strip().lower()
    if not email:
        return None

    conn = sqlite3.connect(REGISTRY_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT m.username, m.tenant_id, t.db_path, t.is_2fa_enabled, m.email
            FROM user_tenant_mapping m
            JOIN tenants t ON t.tenant_id = m.tenant_id
            WHERE LOWER(COALESCE(m.email, '')) = ?
              AND COALESCE(m.is_active, 1) = 1
              AND COALESCE(t.is_active, 1) = 1
            """,
            (email,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        db_path = _abs_db_path(row["db_path"])
        if not os.path.exists(db_path):
            continue
        with sqlite3.connect(db_path) as conn_u:
            conn_u.row_factory = sqlite3.Row
            user = conn_u.execute(
                """
                SELECT * FROM users
                WHERE username = ?
                   OR LOWER(COALESCE(email, '')) = ?
                LIMIT 1
                """,
                (row["username"], email),
            ).fetchone()
        if user:
            return {
                "user": dict(user),
                "db_path": db_path,
                "tenant_id": row["tenant_id"],
                "tenant_2fa_enabled": bool(row["is_2fa_enabled"]),
                "email": row["email"] or user["email"] or email,
            }

    main_db = os.path.join(BASE_DIR, "database.db")
    if os.path.exists(main_db):
        with sqlite3.connect(main_db) as conn:
            conn.row_factory = sqlite3.Row
            user = conn.execute(
                "SELECT * FROM users WHERE LOWER(COALESCE(email, '')) = ?",
                (email,),
            ).fetchone()
        if user:
            return {
                "user": dict(user),
                "db_path": main_db,
                "tenant_id": None,
                "tenant_2fa_enabled": False,
                "email": email,
            }
    return None


def resolve_user_phone(user, db_path, username):
    """Lấy SĐT user từ DB tenant hoặc username (thường là SĐT)."""
    phone = (user or {}).get("phone")
    if phone:
        return normalize_vn_phone(phone) or phone.strip()

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT phone FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row and row["phone"]:
            return normalize_vn_phone(row["phone"]) or row["phone"]
    except Exception:
        pass

    return normalize_vn_phone(username) or (username or "").strip()


def send_otp_sms(phone, otp_code):
    """Gửi OTP qua SMS (ESMS hoặc API generic)."""
    phone = normalize_vn_phone(phone)
    if not phone:
        return False, "Số điện thoại không hợp lệ"

    cfg = get_auth_settings()
    api_url = cfg["sms_api_url"].strip()
    api_key = cfg["sms_api_key"].strip()
    api_secret = cfg["sms_api_secret"].strip()
    brand = cfg["sms_brandname"]
    provider = (cfg["sms_provider"] or "generic").lower()

    if not api_url or not api_key:
        return False, "Chưa cấu hình SMS API trong Master Settings hoặc .env"

    message = f"Ma xac minh {brand}: {otp_code}. Hieu luc 5 phut."

    try:
        if provider == "esms":
            payload = {
                "ApiKey": api_key,
                "SecretKey": api_secret,
                "Phone": phone,
                "Content": message,
                "Brandname": brand,
                "SmsType": "2",
            }
            resp = requests.post(api_url, json=payload, timeout=20)
        else:
            headers = {"Authorization": f"Bearer {api_key}"}
            payload = {"phone": phone, "message": message, "otp": otp_code}
            resp = requests.post(api_url, json=payload, headers=headers, timeout=20)

        if resp.ok:
            return True, phone
        return False, f"SMS API lỗi: {resp.status_code} {resp.text[:200]}"
    except Exception as exc:
        return False, str(exc)


def login_redirect_target(user_role, tenant_id):
    """Xác định endpoint redirect sau đăng nhập."""
    if user_role in ('admin*', 'manager*') and tenant_id is not None:
        return 'rental_service'
    if user_role in ('adminFB', 'managerFB') and tenant_id is not None:
        return 'F_and_B_service'
    if user_role == 'master' and tenant_id is None:
        return 'master_settings'
    return 'sale'
