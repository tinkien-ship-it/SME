"""Gửi email SMTP — dùng chung cho OTP, reset password, hóa đơn."""
import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")

# Mật khẩu ứng dụng Zoho mặc định (trước refactor). Ưu tiên APP_PASSWORD trong .env nếu có.
_DEFAULT_SMTP = {
    "server": "smtp.zoho.com",
    "port": 587,
    "sender": "keto@ketoshop.pro.vn",
    "password": "YNtzs7AkMgYg",
}


def _env_or_default(key, default):
    """Giá trị .env rỗng coi như chưa cấu hình — dùng default (tránh cp .env.txt ghi đè)."""
    val = os.getenv(key)
    if val is None or not str(val).strip():
        return default
    return str(val).strip()


def get_smtp_config():
    """Đọc cấu hình SMTP — .env ghi đè giá trị mặc định (chỉ khi không rỗng)."""
    return {
        "server": _env_or_default("SMTP_SERVER", _DEFAULT_SMTP["server"]),
        "port": int(_env_or_default("SMTP_PORT", str(_DEFAULT_SMTP["port"]))),
        "sender": _env_or_default("SENDER_EMAIL", _DEFAULT_SMTP["sender"]),
        "password": _env_or_default("APP_PASSWORD", _DEFAULT_SMTP["password"]),
    }


def smtp_configured():
    cfg = get_smtp_config()
    return bool(cfg["sender"] and cfg["password"])


def send_email(to_email, subject, body, *, html_body=None):
    """
    Gửi email qua SMTP.
    Trả về (True, None) hoặc (False, thông báo lỗi).
    """
    to_email = (to_email or "").strip()
    if not to_email:
        return False, "Email người nhận trống"

    cfg = get_smtp_config()
    if not cfg["password"]:
        return False, "Chưa cấu hình mật khẩu SMTP (APP_PASSWORD trong .env)."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["sender"]
    msg["To"] = to_email
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(cfg["server"], cfg["port"], timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg["sender"], cfg["password"])
            server.send_message(msg)
        return True, None
    except smtplib.SMTPAuthenticationError as exc:
        code = exc.smtp_code if hasattr(exc, "smtp_code") else ""
        logger.error("SMTP auth lỗi (%s): %s", code, exc)
        return False, f"Xác thực SMTP thất bại ({code}). Kiểm tra SENDER_EMAIL và APP_PASSWORD."
    except smtplib.SMTPException as exc:
        code = getattr(exc, "smtp_code", "")
        logger.error("SMTP lỗi (%s): %s", code, exc)
        return False, f"Gửi email thất bại ({code}): {exc}"
    except Exception as exc:
        logger.error("Email lỗi: %s", exc)
        return False, str(exc)


def send_otp_email(to_email, otp_code):
    """Gửi mã OTP xác minh thiết bị."""
    subject = f"[{otp_code}] Mã xác minh thiết bị - KETO POS"
    body = f"""Kính gửi Quý khách,

Chúng tôi phát hiện bạn đang đăng nhập từ một thiết bị mới.
Mã xác minh (OTP) của bạn là: {otp_code}

Lưu ý: Mã này có hiệu lực trong 5 phút. Nếu không phải bạn thực hiện đăng nhập, vui lòng đổi mật khẩu ngay lập tức để bảo mật tài khoản.

Trân trọng,
Hệ thống quản lý KETO POS"""
    return send_email(to_email, subject, body)
