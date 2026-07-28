"""Cấu hình kênh hỗ trợ khách hàng — Zalo, trợ lý AI."""
from __future__ import annotations

import os
import re

SUPPORT_ZALO_PHONE = (os.getenv('ZALO_SUPPORT_PHONE') or '0908870287').strip()
SUPPORT_ZALO_DISPLAY = re.sub(r'(\d{4})(\d{3})(\d{3})', r'\1 \2 \3', SUPPORT_ZALO_PHONE.replace(' ', ''))
SUPPORT_ZALO_URL = f'https://zalo.me/{SUPPORT_ZALO_PHONE.replace(" ", "")}'

OPENAI_API_KEY = (os.getenv('OPENAI_API_KEY') or '').strip()
OPENAI_MODEL = (os.getenv('OPENAI_ASSISTANT_MODEL') or 'gpt-4o-mini').strip()
ASSISTANT_ENABLED = os.getenv('KETO_ASSISTANT_ENABLED', '1').strip().lower() not in ('0', 'false', 'no')

# Zalo OA — ưu tiên .env, fallback Master Settings DB
ZALO_OA_APP_ID = (os.getenv('ZALO_OA_APP_ID') or '').strip()
ZALO_OA_SECRET = (os.getenv('ZALO_OA_SECRET') or '').strip()


def support_context() -> dict:
    return {
        'support_zalo_phone': SUPPORT_ZALO_PHONE,
        'support_zalo_display': SUPPORT_ZALO_DISPLAY,
        'support_zalo_url': SUPPORT_ZALO_URL,
        'assistant_enabled': ASSISTANT_ENABLED,
        'openai_configured': bool(OPENAI_API_KEY),
    }
