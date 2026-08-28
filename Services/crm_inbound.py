# -*- coding: utf-8 -*-
"""CRM inbound lead hub — chuẩn hóa nguồn + catalog kênh + form công khai."""
from __future__ import annotations

import re
import sqlite3
from typing import Any

# Nguồn chuẩn hiển thị trên CRM (Analytics / Leads filter)
CANONICAL_SOURCES = (
    'Website',
    'Facebook',
    'Zalo',
    'Google',
    'TikTok',
    'WhatsApp',
    'Viber',
    'Hotline',
    'Giới thiệu',
    'Triển lãm',
    'Khác',
)

# Alias từ Make / Ads / form → nguồn chuẩn
SOURCE_ALIASES = {
    'website': 'Website',
    'web': 'Website',
    'landing': 'Website',
    'landing_page': 'Website',
    'facebook': 'Facebook',
    'fb': 'Facebook',
    'meta': 'Facebook',
    'facebook_ads': 'Facebook',
    'fb_lead_ads': 'Facebook',
    'messenger': 'Facebook',
    'instagram': 'Facebook',
    'zalo': 'Zalo',
    'zalo_oa': 'Zalo',
    'google': 'Google',
    'google_ads': 'Google',
    'gads': 'Google',
    'adwords': 'Google',
    'tiktok': 'TikTok',
    'tt': 'TikTok',
    'tik_tok': 'TikTok',
    'whatsapp': 'WhatsApp',
    'wa': 'WhatsApp',
    'whats_app': 'WhatsApp',
    'viber': 'Viber',
    'hotline': 'Hotline',
    'phone': 'Hotline',
    'call': 'Hotline',
    'referral': 'Giới thiệu',
    'gioi_thieu': 'Giới thiệu',
    'exhibition': 'Triển lãm',
    'trien_lam': 'Triển lãm',
    'other': 'Khác',
    'khac': 'Khác',
}

# Catalog kênh inbound end-to-end (hub CRM)
INBOUND_PHASES = (
    {
        'id': 'website',
        'phase': 1,
        'title': 'Website — Form liên hệ',
        'priority': 'Tầng 1',
        'source': 'Website',
        'summary': 'Form công khai trên SME (không lộ Token) hoặc form site tự host → webhook.',
        'steps': [
            'Bật Form công khai tại Hub inbound (hoặc nhúng link /lead).',
            'Gắn UTM trên quảng cáo/landing: ?utm_source=google&utm_campaign=...',
            'Kiểm thử: gửi 1 lead → CRM → Leads → source = Website.',
            'Đảm bảo đã có user role Nhân Viên Bán Hàng (round-robin).',
        ],
    },
    {
        'id': 'facebook',
        'phase': 2,
        'title': 'Facebook Lead Ads',
        'priority': 'Tầng 1',
        'source': 'Facebook',
        'summary': 'Make/n8n: New Facebook Lead → HTTP POST SME với X-CRM-Token.',
        'steps': [
            'Tạo Lead Ads trên Meta (form tên + SĐT).',
            'Make: Trigger Facebook Lead Ads → Action HTTP.',
            'Map: full_name→contact_name, phone→phone, source=Facebook.',
            'Gửi external_id = lead id Facebook để đối soát.',
        ],
    },
    {
        'id': 'zalo',
        'phase': 3,
        'title': 'Zalo OA',
        'priority': 'Tầng 1',
        'source': 'Zalo',
        'summary': 'Bot/OA hoặc Make khi khách để SĐT → POST SME (source=Zalo).',
        'steps': [
            'Cấu hình OA thu thập SĐT (form / chatbot).',
            'Webhook Zalo tool hoặc Make → POST SME.',
            'source=Zalo, notes = nội dung hội thoại ngắn.',
        ],
    },
    {
        'id': 'google',
        'phase': 4,
        'title': 'Google Ads + Landing',
        'priority': 'Tầng 1',
        'source': 'Google',
        'summary': 'Ads trỏ landing/form Website; UTM google; Lead Form Extension qua Make nếu dùng.',
        'steps': [
            'Landing dùng form /lead hoặc form website.',
            'URL Ads kèm utm_source=google&utm_medium=cpc&utm_campaign=...',
            'Nếu Lead Form Extension: Make → POST với source=Google.',
        ],
    },
    {
        'id': 'tiktok',
        'phase': 5,
        'title': 'TikTok Lead Ads',
        'priority': 'Tầng 2',
        'source': 'TikTok',
        'summary': 'Giống Facebook: Make/n8n Lead Ads → POST SME (source=TikTok).',
        'steps': [
            'Bật TikTok Lead Generation.',
            'Make trigger TikTok Lead → HTTP POST SME.',
            'source=TikTok, external_id = lead id.',
        ],
    },
    {
        'id': 'whatsapp',
        'phase': 6,
        'title': 'WhatsApp Business',
        'priority': 'Tầng 2',
        'source': 'WhatsApp',
        'summary': 'API/ManyChat/Make khi khách gửi SĐT hoặc điền form → POST SME.',
        'steps': [
            'Dùng flow bắt buộc để lại tên + SĐT.',
            'Make/WhatsApp tool → POST source=WhatsApp.',
        ],
    },
    {
        'id': 'viber',
        'phase': 7,
        'title': 'Viber',
        'priority': 'Tầng 3',
        'source': 'Viber',
        'summary': 'Chỉ khi đội sale đã chat Viber — bot/Make POST source=Viber.',
        'steps': [
            'Thu thập SĐT qua bot/form.',
            'POST SME với source=Viber.',
        ],
    },
    {
        'id': 'other',
        'phase': 8,
        'title': 'Hotline & nguồn khác',
        'priority': 'Bổ sung',
        'source': 'Hotline',
        'summary': 'Tổng đài / giới thiệu / triển lãm → form nội bộ hoặc POST thủ công/Make.',
        'steps': [
            'Hotline: NV hoặc CTI POST source=Hotline.',
            'Giới thiệu / Triển lãm: dùng source tương ứng.',
        ],
    },
)


def normalize_source(raw: str | None) -> str:
    s = str(raw or '').strip()
    if not s:
        return 'Website'
    key = re.sub(r'[\s\-]+', '_', s.lower())
    if s in CANONICAL_SOURCES:
        return s
    return SOURCE_ALIASES.get(key) or SOURCE_ALIASES.get(s.lower()) or (
        s if len(s) <= 40 else 'Khác'
    )


def normalize_inbound_payload(data: dict | None) -> dict:
    """Chuẩn hóa body từ website / Make / Ads về schema create_inbound_lead."""
    data = dict(data or {})
    # bỏ token khỏi payload nghiệp vụ
    data.pop('token', None)

    contact = (
        data.get('contact_name')
        or data.get('name')
        or data.get('full_name')
        or data.get('fullname')
        or ''
    )
    phone = data.get('phone') or data.get('phone_number') or data.get('mobile') or ''
    email = data.get('email') or data.get('email_address') or ''
    company = data.get('company_name') or data.get('company') or data.get('org') or ''
    notes = data.get('notes') or data.get('message') or data.get('content') or data.get('comment') or ''
    source_raw = (
        data.get('source')
        or data.get('channel')
        or data.get('utm_source')
        or data.get('platform')
        or ''
    )
    source = normalize_source(str(source_raw) if source_raw else 'Website')

    out = {
        'title': (data.get('title') or str(contact).strip() or 'Lead inbound'),
        'contact_name': str(contact).strip() or 'Khách mới',
        'company_name': str(company).strip() or None,
        'phone': str(phone).strip() or None,
        'email': str(email).strip() or None,
        'source': source,
        'channel': normalize_source(str(data.get('channel') or source)),
        'notes': str(notes).strip() or None,
        'expected_value': data.get('expected_value') or 0,
        'owner': (str(data.get('owner') or '').strip() or None),
        'campaign_id': data.get('campaign_id'),
        'utm_source': (str(data.get('utm_source') or '').strip() or None),
        'utm_medium': (str(data.get('utm_medium') or '').strip() or None),
        'utm_campaign': (str(data.get('utm_campaign') or '').strip() or None),
        'external_id': (
            str(
                data.get('external_id')
                or data.get('lead_id')
                or data.get('id')
                or ''
            ).strip()
            or None
        ),
        'score': data.get('score'),
    }
    return out


def make_http_template(endpoint: str, token: str, source: str) -> dict[str, Any]:
    """JSON cấu hình mẫu cho Make / n8n HTTP module."""
    return {
        'method': 'POST',
        'url': endpoint,
        'headers': {
            'Content-Type': 'application/json',
            'X-CRM-Token': token or '<CRM_INBOUND_TOKEN>',
        },
        'body': {
            'contact_name': '{{name}}',
            'phone': '{{phone}}',
            'email': '{{email}}',
            'company_name': '{{company}}',
            'source': source,
            'utm_source': '{{utm_source}}',
            'utm_medium': '{{utm_medium}}',
            'utm_campaign': '{{utm_campaign}}',
            'external_id': '{{lead_id}}',
            'notes': '{{notes}}',
        },
        'curl': (
            f"curl -X POST '{endpoint}' \\\n"
            f"  -H 'Content-Type: application/json' \\\n"
            f"  -H 'X-CRM-Token: {token or '<TOKEN>'}' \\\n"
            f"  -d '{{\"contact_name\":\"Nguyen A\",\"phone\":\"0901234567\","
            f"\"source\":\"{source}\",\"notes\":\"Test {source}\"}}'"
        ),
    }


def get_public_form_settings(conn: sqlite3.Connection) -> dict:
    from Services.crm_ops import get_setting

    enabled = get_setting(conn, 'public_lead_form_enabled', '1')
    return {
        'enabled': str(enabled).strip() not in ('0', 'false', 'off', ''),
        'title': get_setting(conn, 'public_lead_form_title', 'Để lại thông tin liên hệ')
        or 'Để lại thông tin liên hệ',
        'subtitle': get_setting(
            conn,
            'public_lead_form_subtitle',
            'Chúng tôi sẽ liên hệ lại sớm nhất.',
        )
        or 'Chúng tôi sẽ liên hệ lại sớm nhất.',
        'success_message': get_setting(
            conn,
            'public_lead_form_success',
            'Cảm ơn bạn! Thông tin đã được ghi nhận.',
        )
        or 'Cảm ơn bạn! Thông tin đã được ghi nhận.',
        'require_phone': str(get_setting(conn, 'public_lead_require_phone', '1')).strip()
        not in ('0', 'false', 'off'),
    }


def set_public_form_settings(conn: sqlite3.Connection, data: dict) -> dict:
    from Services.crm_ops import set_setting

    if 'enabled' in data:
        on = str(data.get('enabled')).strip().lower() in ('1', 'true', 'yes', 'on')
        set_setting(conn, 'public_lead_form_enabled', '1' if on else '0')
    if 'title' in data:
        set_setting(conn, 'public_lead_form_title', str(data.get('title') or '').strip()[:120])
    if 'subtitle' in data:
        set_setting(conn, 'public_lead_form_subtitle', str(data.get('subtitle') or '').strip()[:200])
    if 'success_message' in data:
        set_setting(conn, 'public_lead_form_success', str(data.get('success_message') or '').strip()[:200])
    if 'require_phone' in data:
        on = str(data.get('require_phone')).strip().lower() in ('1', 'true', 'yes', 'on')
        set_setting(conn, 'public_lead_require_phone', '1' if on else '0')
    return get_public_form_settings(conn)


def get_phase_status(conn: sqlite3.Connection) -> dict[str, bool]:
    import json
    from Services.crm_ops import get_setting

    raw = get_setting(conn, 'inbound_phase_done', '{}')
    try:
        data = json.loads(raw or '{}')
    except Exception:
        data = {}
    out = {p['id']: bool(data.get(p['id'])) for p in INBOUND_PHASES}
    return out


def set_phase_done(conn: sqlite3.Connection, phase_id: str, done: bool) -> dict[str, bool]:
    import json
    from Services.crm_ops import get_setting, set_setting

    allowed = {p['id'] for p in INBOUND_PHASES}
    if phase_id not in allowed:
        raise ValueError('phase_id không hợp lệ')
    raw = get_setting(conn, 'inbound_phase_done', '{}')
    try:
        data = json.loads(raw or '{}')
    except Exception:
        data = {}
    data[phase_id] = bool(done)
    set_setting(conn, 'inbound_phase_done', json.dumps(data, ensure_ascii=False))
    return get_phase_status(conn)


def log_inbound(
    conn: sqlite3.Connection,
    *,
    channel: str,
    status: str,
    lead_id: int | None = None,
    owner: str | None = None,
    source: str | None = None,
    external_id: str | None = None,
    contact_name: str | None = None,
    phone: str | None = None,
    error: str | None = None,
    payload: dict | None = None,
) -> None:
    import json
    from Services.crm_schema import ensure_crm_schema

    ensure_crm_schema(conn, commit=False)
    preview = ''
    try:
        preview = json.dumps(payload or {}, ensure_ascii=False)[:1500]
    except Exception:
        preview = str(payload)[:1500]
    conn.execute(
        """
        INSERT INTO crm_inbound_logs (
            channel, source, status, lead_id, owner, external_id,
            contact_name, phone, error, payload_preview, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))
        """,
        (
            channel,
            source,
            status,
            lead_id,
            owner,
            external_id,
            contact_name,
            phone,
            (error or '')[:500] or None,
            preview or None,
        ),
    )


def list_inbound_logs(conn: sqlite3.Connection, *, limit: int = 50, channel: str | None = None) -> list[dict]:
    from Services.crm_schema import ensure_crm_schema

    ensure_crm_schema(conn, commit=False)
    if channel:
        rows = conn.execute(
            """
            SELECT * FROM crm_inbound_logs
            WHERE channel = ?
            ORDER BY id DESC LIMIT ?
            """,
            (channel, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM crm_inbound_logs ORDER BY id DESC LIMIT ?',
            (int(limit),),
        ).fetchall()
    out = []
    for r in rows:
        out.append(dict(r) if hasattr(r, 'keys') else {})
    return out


def get_channel_verify_token(conn: sqlite3.Connection, channel: str) -> str:
    from Services.crm_ops import ensure_inbound_token, get_setting

    ch = (channel or '').strip().lower()
    dedicated = get_setting(conn, f'inbound_verify_{ch}', '')
    if dedicated.strip():
        return dedicated.strip()
    return ensure_inbound_token(conn)


def set_channel_verify_token(conn: sqlite3.Connection, channel: str, token: str) -> str:
    from Services.crm_ops import set_setting

    ch = (channel or '').strip().lower()
    set_setting(conn, f'inbound_verify_{ch}', (token or '').strip())
    return get_channel_verify_token(conn, ch)


def mark_phase_for_source(conn: sqlite3.Connection, source: str) -> None:
    from Services.crm_inbound_adapters import CHANNEL_TO_PHASE, CHANNEL_TO_SOURCE

    src = (source or '').strip()
    phase_id = None
    for slug, sname in CHANNEL_TO_SOURCE.items():
        if sname == src:
            phase_id = CHANNEL_TO_PHASE.get(slug)
            break
    if not phase_id and src in ('Hotline', 'Giới thiệu', 'Triển lãm', 'Khác'):
        phase_id = 'other'
    if not phase_id:
        return
    status = get_phase_status(conn)
    if not status.get(phase_id):
        try:
            set_phase_done(conn, phase_id, True)
        except ValueError:
            pass


def process_channel_inbound(
    conn: sqlite3.Connection,
    channel: str,
    raw: dict,
    *,
    require_phone: bool = False,
) -> dict:
    """Adapt → create lead → log → auto tick phase."""
    from Services import crm_ops
    from Services.crm_inbound_adapters import CHANNEL_SLUGS, adapt_channel

    slug = (channel or '').strip().lower()
    if slug not in CHANNEL_SLUGS:
        raise ValueError(f'Kênh không hỗ trợ: {channel}')

    adapted = adapt_channel(slug, raw)
    if require_phone and not (adapted.get('phone') or '').strip():
        log_inbound(
            conn,
            channel=slug,
            status='error',
            source=adapted.get('source'),
            external_id=adapted.get('external_id'),
            contact_name=adapted.get('contact_name'),
            error='Thiếu số điện thoại',
            payload=raw,
        )
        raise ValueError('Thiếu số điện thoại')

    # Cho phép tạo lead dù thiếu phone (Facebook stub) — ghi notes
    if not (adapted.get('phone') or '').strip() and not (adapted.get('contact_name') or '').strip():
        raise ValueError('Thiếu thông tin liên hệ')

    try:
        result = crm_ops.create_inbound_lead(conn, adapted, auto_assign=True)
        log_inbound(
            conn,
            channel=slug,
            status='ok' if not result.get('deduped') else 'dedup',
            lead_id=result.get('id'),
            owner=result.get('owner'),
            source=result.get('source') or adapted.get('source'),
            external_id=adapted.get('external_id'),
            contact_name=result.get('contact_name') or adapted.get('contact_name'),
            phone=adapted.get('phone'),
            payload=raw,
        )
        mark_phase_for_source(conn, result.get('source') or adapted.get('source') or '')
        return {**result, 'channel': slug, 'adapted': adapted}
    except Exception as e:
        log_inbound(
            conn,
            channel=slug,
            status='error',
            source=adapted.get('source'),
            external_id=adapted.get('external_id'),
            contact_name=adapted.get('contact_name'),
            phone=adapted.get('phone'),
            error=str(e),
            payload=raw,
        )
        raise


def channel_endpoint_path(channel: str, *, tenant_id: str | None = None) -> str:
    slug = (channel or '').strip().lower()
    if tenant_id:
        return f'/{tenant_id}/api/crm/inbound/{slug}'
    return f'/api/crm/inbound/{slug}'


def inbound_hub_payload(conn: sqlite3.Connection, *, endpoint: str, token: str, base_url: str = '', tenant_id: str | None = None) -> dict:
    from Services.crm_ops import list_crm_sales_staff, sync_assign_owners_from_staff
    from Services.crm_inbound_adapters import CHANNEL_SLUGS, CHANNEL_TO_SOURCE

    owners = sync_assign_owners_from_staff(conn)
    staff = list_crm_sales_staff(conn)
    base = (base_url or '').rstrip('/')
    phases = []
    status = get_phase_status(conn)
    channel_endpoints = {}
    for slug in CHANNEL_SLUGS:
        path = channel_endpoint_path(slug, tenant_id=tenant_id)
        full = f'{base}{path}' if base else path
        channel_endpoints[slug] = {
            'path': path,
            'url': full,
            'source': CHANNEL_TO_SOURCE[slug],
            'verify_token_set': bool(get_channel_verify_token(conn, slug)),
        }
    for p in INBOUND_PHASES:
        slug = p['id'] if p['id'] != 'other' else 'hotline'
        ch_url = channel_endpoints.get(slug, {}).get('url') or endpoint
        tpl = make_http_template(ch_url, token, p['source'])
        # curl kênh riêng
        tpl['channel_url'] = ch_url
        tpl['curl_channel'] = (
            f"curl -X POST '{ch_url}' \\\n"
            f"  -H 'Content-Type: application/json' \\\n"
            f"  -H 'X-CRM-Token: {token or '<TOKEN>'}' \\\n"
            f"  -d '{{\"contact_name\":\"Nguyen A\",\"phone\":\"0901234567\","
            f"\"notes\":\"Test {p['source']}\"}}'"
        )
        phases.append({
            **p,
            'done': bool(status.get(p['id'])),
            'template': tpl,
            'channel_slug': slug,
            'channel_url': ch_url,
        })
    return {
        'endpoint': endpoint,
        'token_set': bool((token or '').strip()),
        'sources': list(CANONICAL_SOURCES),
        'phases': phases,
        'channels': channel_endpoints,
        'sales_staff': staff,
        'assign_owners': owners,
        'public_form': get_public_form_settings(conn),
        'recent_logs': list_inbound_logs(conn, limit=25),
        'embed_snippet': _embed_snippet(base, tenant_id),
    }


def _embed_snippet(base_url: str, tenant_id: str | None) -> str:
    base = (base_url or '').rstrip('/') or 'https://YOUR_DOMAIN'
    if tenant_id:
        form = f'{base}/{tenant_id}/lead'
        js = f'{base}/{tenant_id}/static/js/crm-lead-embed.js'
    else:
        form = f'{base}/lead'
        js = f'{base}/static/js/crm-lead-embed.js'
    return (
        f'<!-- SME CRM Lead Form -->\n'
        f'<div id="sme-crm-lead" data-form-url="{form}"></div>\n'
        f'<script src="{js}" defer></script>\n'
        f'<!-- hoặc iframe: <iframe src="{form}" style="width:100%;min-height:520px;border:0"></iframe> -->'
    )
