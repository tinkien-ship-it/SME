"""Orchestrator trợ lý AI — FAQ + RAG + ngữ cảnh + OpenAI + log học từ ticket."""
from __future__ import annotations

import logging
from typing import Any

import requests

from Services.assistant_context import PAGE_CONTEXT, build_context_prompt, rag_section_for_regime
from Services.assistant_faq import get_suggestions, search_faq, should_escalate
from Services.assistant_rag import rag_context_for_prompt, search_rag
from Services.assistant_store import get_assistant_settings, log_chat
from Services.support_config import OPENAI_API_KEY, OPENAI_MODEL, SUPPORT_ZALO_PHONE

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Bạn là trợ lý AI của phần mềm KETO POS (POS + Kế toán HKD/SME, Việt Nam).
Nhiệm vụ: hướng dẫn người dùng thao tác trên phần mềm — ngắn gọn, từng bước, tiếng Việt.
Quy tắc:
- Chỉ trả lời trong phạm vi KETO POS; ưu tiên tài liệu tham khảo và FAQ được cung cấp.
- Không bịa tính năng; nếu không chắc, gợi ý Hướng Dẫn Sử Dụng hoặc Zalo 0908870287.
- Dùng **in đậm** cho tên menu/nút. Tối đa 6 bước hoặc 180 từ.
- Không tư vấn trốn thuế; nhắc tuân thủ pháp luật khi hỏi về thuế."""


def _confidence_from_sources(
    faq_score: float,
    rag_score: float,
    source: str,
) -> float:
    if source == 'faq':
        return min(0.95, 0.55 + faq_score * 0.08)
    if source == 'rag':
        return min(0.85, 0.45 + rag_score * 0.06)
    if source == 'openai':
        return 0.65
    if source == 'fallback':
        return 0.25
    return 0.5


def _call_openai(
    message: str,
    *,
    context_prompt: str,
    rag_text: str,
    faq_hint: str | None,
) -> str | None:
    if not OPENAI_API_KEY:
        return None
    parts = [SYSTEM_PROMPT]
    if context_prompt:
        parts.append('\nNgữ cảnh người dùng:\n' + context_prompt)
    if rag_text:
        parts.append('\n' + rag_text)
    if faq_hint:
        parts.append('\nGợi ý FAQ nội bộ: ' + faq_hint)
    parts.append('\nCâu hỏi: ' + message)
    try:
        res = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {OPENAI_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'model': OPENAI_MODEL,
                'messages': [
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user', 'content': '\n'.join(parts[1:])},
                ],
                'temperature': 0.25,
                'max_tokens': 500,
            },
            timeout=28,
        )
        res.raise_for_status()
        data = res.json()
        content = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
        return (content or '').strip() or None
    except Exception as exc:
        logger.warning('OpenAI assistant error: %s', exc)
        return None


def _compose_rag_answer(hits) -> str:
    if not hits:
        return ''
    h = hits[0]
    extra = f'\n\n(Nguồn: {h.section} — {h.title})' if h.title else ''
    text = h.text
    if len(text) > 600:
        text = text[:597] + '...'
    return text + extra


def ask_assistant(
    message: str,
    *,
    page: str | None = None,
    help_url: str | None = None,
    context: dict[str, Any] | None = None,
    channel: str = 'web',
    tenant_id: str | None = None,
    username: str | None = None,
    zalo_user_id: str | None = None,
    log_interaction: bool = True,
) -> dict[str, Any]:
    msg = (message or '').strip()
    ctx = dict(context or {})
    if page and 'page' not in ctx:
        ctx['page'] = page

    if not msg:
        return {
            'text': 'Bạn cần hỗ trợ gì về KETO POS? Hãy mô tả thao tác hoặc chọn câu hỏi gợi ý bên dưới.',
            'source': 'system',
            'confidence': 1.0,
            'needs_escalation': False,
        }

    regime = (ctx.get('regime') or '').strip()
    context_prompt = build_context_prompt(ctx)
    page_key = (ctx.get('page') or page or '').strip()

    faq_match = search_faq(msg, page=page_key)
    faq_hint = faq_match.entry['answer'] if faq_match else None
    faq_score = faq_match.score if faq_match else 0.0

    rag_section = rag_section_for_regime(regime)
    rag_hits = search_rag(msg, top_k=3, section=rag_section)
    rag_text = rag_context_for_prompt(msg, section=rag_section)
    rag_score = rag_hits[0].score if rag_hits else 0.0

    escalate = should_escalate(msg)
    cfg = get_assistant_settings()
    if cfg.get('assistant_escalation_enabled') == '1' and escalate:
        pass  # force lower confidence path

    # Ưu tiên FAQ khớp cao
    if faq_match and faq_score >= 3.5 and not OPENAI_API_KEY:
        text = faq_match.entry['answer']
        if faq_match.entry.get('id') != 'support_zalo':
            text += '\n\nCần hỗ trợ thêm, nhắn Zalo **0908870287** hoặc xem **Hướng Dẫn Sử Dụng**.'
        source = 'faq'
        confidence = _confidence_from_sources(faq_score, rag_score, source)
        result = {
            'text': text,
            'source': source,
            'confidence': confidence,
            'faq_id': faq_match.entry.get('id'),
            'needs_escalation': escalate and confidence < 0.5,
            'help_url': help_url,
        }
        if log_interaction:
            _log_result(msg, result, channel, tenant_id, username, zalo_user_id, page_key, ctx)
        return result

    # OpenAI + RAG + context
    ai_text = _call_openai(
        msg,
        context_prompt=context_prompt,
        rag_text=rag_text,
        faq_hint=faq_hint,
    )
    if ai_text:
        source = 'openai'
        confidence = _confidence_from_sources(faq_score, rag_score, source)
        result = {
            'text': ai_text,
            'source': source,
            'confidence': confidence,
            'faq_id': faq_match.entry.get('id') if faq_match else None,
            'needs_escalation': escalate or confidence < 0.45,
            'help_url': help_url,
        }
        if log_interaction:
            _log_result(msg, result, channel, tenant_id, username, zalo_user_id, page_key, ctx)
        return result

    # FAQ vừa khớp
    if faq_match:
        text = faq_match.entry['answer']
        if faq_match.entry.get('id') != 'support_zalo':
            text += '\n\nCần hỗ trợ thêm, nhắn Zalo **0908870287** hoặc xem **Hướng Dẫn Sử Dụng**.'
        source = 'faq'
        confidence = _confidence_from_sources(faq_score, rag_score, source)
        result = {
            'text': text,
            'source': source,
            'confidence': confidence,
            'faq_id': faq_match.entry.get('id'),
            'needs_escalation': escalate,
            'help_url': help_url,
        }
        if log_interaction:
            _log_result(msg, result, channel, tenant_id, username, zalo_user_id, page_key, ctx)
        return result

    # RAG fallback
    if rag_hits and rag_score >= 2.5:
        text = _compose_rag_answer(rag_hits)
        text += '\n\nChi tiết: **Hướng Dẫn Sử Dụng** hoặc Zalo **0908870287**.'
        source = 'rag'
        confidence = _confidence_from_sources(faq_score, rag_score, source)
        result = {
            'text': text,
            'source': source,
            'confidence': confidence,
            'needs_escalation': escalate,
            'help_url': help_url,
        }
        if log_interaction:
            _log_result(msg, result, channel, tenant_id, username, zalo_user_id, page_key, ctx)
        return result

    # Page hint khi đang ở màn hình cụ thể
    if page_key and page_key in PAGE_CONTEXT and not faq_match:
        pc = PAGE_CONTEXT[page_key]
        hint = pc.get('hint') or ''
        if hint:
            source = 'context'
            confidence = 0.4
            result = {
                'text': (
                    f'Bạn đang ở **{pc.get("label", page_key)}**.\n\n{hint}\n\n'
                    'Hỏi cụ thể hơn hoặc xem **Hướng Dẫn Sử Dụng** / Zalo **0908870287**.'
                ),
                'source': source,
                'confidence': confidence,
                'needs_escalation': True,
                'help_url': help_url,
            }
            if log_interaction:
                _log_result(msg, result, channel, tenant_id, username, zalo_user_id, page_key, ctx)
            return result

    result = {
        'text': (
            'Tôi chưa tìm thấy hướng dẫn khớp trong cơ sở kiến thức KETO POS.\n\n'
            'Bạn có thể:\n'
            '1. Xem **Hướng Dẫn Sử Dụng** (menu Kế Toán HKD)\n'
            f'2. Nhắn Zalo **{SUPPORT_ZALO_PHONE}** để được hỗ trợ trực tiếp\n'
            '3. Thử hỏi cụ thể hơn, ví dụ: "Cách lập phiếu nhập kho từ hóa đơn mua?"'
        ),
        'source': 'fallback',
        'confidence': 0.2,
        'needs_escalation': True,
        'help_url': help_url,
    }
    if log_interaction:
        _log_result(msg, result, channel, tenant_id, username, zalo_user_id, page_key, ctx)
    return result


def _log_result(
    message: str,
    result: dict,
    channel: str,
    tenant_id: str | None,
    username: str | None,
    zalo_user_id: str | None,
    page: str | None,
    ctx: dict,
) -> None:
    try:
        conf = float(result.get('confidence') or 0)
        needs = bool(result.get('needs_escalation')) or conf < 0.45 or result.get('source') == 'fallback'
        log_chat(
            channel=channel,
            user_message=message,
            bot_reply=result.get('text') or '',
            source=result.get('source') or '',
            confidence=conf,
            needs_review=needs,
            tenant_id=tenant_id,
            username=username,
            zalo_user_id=zalo_user_id,
            page=page,
            context=ctx,
        )
    except Exception as exc:
        logger.warning('log_chat failed: %s', exc)


# Re-export for routes
__all__ = ['ask_assistant', 'get_suggestions', 'search_faq']
