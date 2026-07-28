"""Zalo Official Account — webhook, gửi tin, refresh token."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import requests

from Services.assistant_store import get_assistant_settings, save_zalo_tokens, set_zalo_escalated, touch_zalo_session
from Services.support_config import SUPPORT_ZALO_PHONE, SUPPORT_ZALO_URL

logger = logging.getLogger(__name__)

ZALO_OAUTH_URL = 'https://oauth.zaloapp.com/v4/oa/access_token'
ZALO_MESSAGE_URL = 'https://openapi.zalo.me/v2.0/oa/message'


def is_zalo_oa_configured() -> bool:
    cfg = get_assistant_settings()
    return bool(
        (cfg.get('zalo_oa_app_id') or '').strip()
        and (cfg.get('zalo_oa_secret') or '').strip()
        and (
            (cfg.get('zalo_oa_refresh_token') or '').strip()
            or (cfg.get('zalo_oa_access_token') or '').strip()
        )
    )


def verify_webhook_get(code: str, oa_id: str | None = None) -> str | None:
    """Xác minh webhook khi đăng ký URL trên Zalo Developer."""
    cfg = get_assistant_settings()
    expected = (cfg.get('zalo_webhook_verify_token') or '').strip()
    if not expected:
        return code if code else None
    if code == expected:
        return code
    return None


def _refresh_access_token() -> str | None:
    cfg = get_assistant_settings()
    app_id = (cfg.get('zalo_oa_app_id') or '').strip()
    secret = (cfg.get('zalo_oa_secret') or '').strip()
    refresh = (cfg.get('zalo_oa_refresh_token') or '').strip()
    if not app_id or not secret or not refresh:
        return (cfg.get('zalo_oa_access_token') or '').strip() or None

    try:
        res = requests.post(
            ZALO_OAUTH_URL,
            headers={'secret_key': secret},
            data={
                'app_id': app_id,
                'grant_type': 'refresh_token',
                'refresh_token': refresh,
            },
            timeout=20,
        )
        data = res.json()
        token = (data.get('access_token') or '').strip()
        if not token:
            logger.warning('Zalo refresh failed: %s', data)
            return (cfg.get('zalo_oa_access_token') or '').strip() or None
        expires_in = int(data.get('expires_in') or 3600)
        expires_at = (datetime.now() + timedelta(seconds=expires_in - 60)).strftime('%Y-%m-%d %H:%M:%S')
        save_zalo_tokens(token, expires_at)
        return token
    except Exception as exc:
        logger.exception('Zalo token refresh error: %s', exc)
        return (cfg.get('zalo_oa_access_token') or '').strip() or None


def get_access_token() -> str | None:
    cfg = get_assistant_settings()
    token = (cfg.get('zalo_oa_access_token') or '').strip()
    expires = (cfg.get('zalo_oa_token_expires') or '').strip()
    if token and expires:
        try:
            exp_dt = datetime.strptime(expires[:19], '%Y-%m-%d %H:%M:%S')
            if exp_dt > datetime.now():
                return token
        except ValueError:
            pass
    return _refresh_access_token()


def send_text_message(user_id: str, text: str) -> bool:
    token = get_access_token()
    if not token or not user_id:
        return False
    try:
        res = requests.post(
            ZALO_MESSAGE_URL,
            params={'access_token': token},
            json={
                'recipient': {'user_id': str(user_id)},
                'message': {'text': (text or '')[:2000]},
            },
            timeout=20,
        )
        data = res.json()
        ok = data.get('error') == 0 or data.get('message') == 'Success'
        if not ok:
            logger.warning('Zalo send failed: %s', data)
        return ok
    except Exception as exc:
        logger.exception('Zalo send error: %s', exc)
        return False


def parse_webhook_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Trích tin nhắn text từ payload webhook Zalo OA."""
    if not payload:
        return None
    event = payload.get('event_name') or payload.get('event')
    if event != 'user_send_text':
        return None
    sender = payload.get('sender') or {}
    message = payload.get('message') or {}
    text = (message.get('text') or '').strip()
    user_id = str(sender.get('id') or '')
    if not user_id or not text:
        return None
    return {
        'user_id': user_id,
        'text': text,
        'display_name': sender.get('name') or sender.get('display_name') or '',
    }


def escalation_footer(escalate: bool) -> str:
    if not escalate:
        return ''
    return (
        f'\n\n---\nCâu hỏi phức tạp — vui lòng nhắn trực tiếp Zalo {SUPPORT_ZALO_PHONE} '
        f'hoặc mở: {SUPPORT_ZALO_URL}\nNhân viên sẽ hỗ trợ bạn sớm nhất.'
    )


def handle_zalo_message(user_id: str, text: str, *, display_name: str | None, reply_fn) -> None:
    """
    reply_fn(message) -> dict với text, confidence, needs_escalation
    """
    touch_zalo_session(user_id, display_name=display_name)
    result = reply_fn(text)
    reply_text = result.get('text') or 'Xin lỗi, tôi chưa trả lời được. Vui lòng thử lại.'
    if result.get('needs_escalation'):
        set_zalo_escalated(user_id)
        reply_text += escalation_footer(True)
    send_text_message(user_id, reply_text)
