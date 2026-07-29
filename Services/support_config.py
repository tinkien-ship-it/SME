"""Cấu hình kênh hỗ trợ khách hàng — Zalo, trợ lý AI (miễn phí + premium)."""
from __future__ import annotations

import os
import re

SUPPORT_ZALO_PHONE = (os.getenv('ZALO_SUPPORT_PHONE') or '0908870287').strip()
SUPPORT_ZALO_DISPLAY = re.sub(r'(\d{4})(\d{3})(\d{3})', r'\1 \2 \3', SUPPORT_ZALO_PHONE.replace(' ', ''))
SUPPORT_ZALO_URL = f'https://zalo.me/{SUPPORT_ZALO_PHONE.replace(" ", "")}'

OPENAI_API_KEY = (os.getenv('OPENAI_API_KEY') or '').strip()
OPENAI_MODEL_DEFAULT = (os.getenv('OPENAI_ASSISTANT_MODEL') or 'gpt-4o-mini').strip()
OPENAI_MODEL_PREMIUM = (os.getenv('OPENAI_ASSISTANT_MODEL_PREMIUM') or 'gpt-4o').strip()

# Widget hiển thị mặc định; chỉ tắt khi bảo trì (KETO_ASSISTANT_ENABLED=0)
ASSISTANT_WIDGET_ENABLED = os.getenv('KETO_ASSISTANT_ENABLED', '1').strip().lower() not in ('0', 'false', 'no')

# Mô hình OpenAI khả dụng (Master chọn khi bật premium)
ASSISTANT_MODEL_OPTIONS = (
    {'id': 'gpt-4o-mini', 'label': 'GPT-4o mini — rẻ, nhanh (khuyến nghị)'},
    {'id': 'gpt-4o', 'label': 'GPT-4o — thông minh hơn, tốn phí hơn'},
    {'id': 'gpt-4.1-mini', 'label': 'GPT-4.1 mini — cân bằng'},
    {'id': 'gpt-4.1', 'label': 'GPT-4.1 — cao cấp'},
)


def _assistant_db_settings() -> dict[str, str]:
    try:
        from Services.assistant_store import get_assistant_settings
        return get_assistant_settings()
    except Exception:
        return {}


def get_assistant_runtime_config() -> dict:
    """
    Cấu hình runtime trợ lý AI:
    - free: FAQ + RAG + ngữ cảnh màn hình (không gọi OpenAI)
    - premium: thêm OpenAI khi có OPENAI_API_KEY
    """
    db = _assistant_db_settings()
    ai_mode = (db.get('assistant_ai_mode') or os.getenv('KETO_ASSISTANT_AI_MODE') or 'free').strip().lower()
    if ai_mode not in ('free', 'premium'):
        ai_mode = 'free'

    model = (db.get('assistant_openai_model') or OPENAI_MODEL_DEFAULT).strip()
    openai_available = bool(OPENAI_API_KEY)
    premium_active = ai_mode == 'premium' and openai_available

    return {
        'widget_enabled': ASSISTANT_WIDGET_ENABLED,
        'ai_mode': ai_mode,
        'ai_mode_label': 'AI Pro (OpenAI)' if premium_active else 'Miễn phí (FAQ + hướng dẫn)',
        'openai_available': openai_available,
        'openai_configured': openai_available,
        'premium_active': premium_active,
        'openai_model': model if premium_active else '',
        'openai_model_default': OPENAI_MODEL_DEFAULT,
        'openai_model_premium': OPENAI_MODEL_PREMIUM,
        'model_options': ASSISTANT_MODEL_OPTIONS,
    }


def resolve_openai_model() -> str:
    cfg = get_assistant_runtime_config()
    if not cfg['premium_active']:
        return ''
    db = _assistant_db_settings()
    model = (db.get('assistant_openai_model') or OPENAI_MODEL_DEFAULT).strip()
    allowed = {m['id'] for m in ASSISTANT_MODEL_OPTIONS}
    if model in allowed:
        return model
    return OPENAI_MODEL_DEFAULT


def support_context() -> dict:
    rt = get_assistant_runtime_config()
    return {
        'support_zalo_phone': SUPPORT_ZALO_PHONE,
        'support_zalo_display': SUPPORT_ZALO_DISPLAY,
        'support_zalo_url': SUPPORT_ZALO_URL,
        'assistant_enabled': rt['widget_enabled'],
        'assistant_widget_enabled': rt['widget_enabled'],
        'assistant_ai_mode': rt['ai_mode'],
        'assistant_premium_active': rt['premium_active'],
        'assistant_mode_label': rt['ai_mode_label'],
        'openai_configured': rt['openai_configured'],
        'openai_available': rt['openai_available'],
    }
