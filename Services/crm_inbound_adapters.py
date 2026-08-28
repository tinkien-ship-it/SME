# -*- coding: utf-8 -*-
"""Adapters payload theo từng kênh inbound (Make / Ads / OA / native webhook)."""
from __future__ import annotations

from typing import Any


CHANNEL_SLUGS = (
    'website',
    'facebook',
    'zalo',
    'google',
    'tiktok',
    'whatsapp',
    'viber',
    'hotline',
)

CHANNEL_TO_SOURCE = {
    'website': 'Website',
    'facebook': 'Facebook',
    'zalo': 'Zalo',
    'google': 'Google',
    'tiktok': 'TikTok',
    'whatsapp': 'WhatsApp',
    'viber': 'Viber',
    'hotline': 'Hotline',
}

CHANNEL_TO_PHASE = {
    'website': 'website',
    'facebook': 'facebook',
    'zalo': 'zalo',
    'google': 'google',
    'tiktok': 'tiktok',
    'whatsapp': 'whatsapp',
    'viber': 'viber',
    'hotline': 'other',
}


def _s(v: Any) -> str:
    return str(v or '').strip()


def _field_data_to_map(field_data: list | None) -> dict[str, str]:
    """Meta Lead Ads field_data: [{name, values: [...]}]."""
    out: dict[str, str] = {}
    for item in field_data or []:
        if not isinstance(item, dict):
            continue
        name = _s(item.get('name') or item.get('key')).lower()
        vals = item.get('values') or item.get('value')
        if isinstance(vals, list):
            val = _s(vals[0] if vals else '')
        else:
            val = _s(vals)
        if name:
            out[name] = val
    return out


def _pick_phone(m: dict) -> str:
    for k in (
        'phone', 'phone_number', 'mobile', 'tel', 'so_dien_thoai',
        'sđt', 'sdt', 'phonenumber', 'wa_id',
    ):
        if _s(m.get(k)):
            return _s(m.get(k))
    return ''


def _pick_name(m: dict) -> str:
    for k in (
        'contact_name', 'full_name', 'fullname', 'name', 'ho_ten', 'full name',
        'first_name', 'ten',
    ):
        if _s(m.get(k)):
            return _s(m.get(k))
    first = _s(m.get('first_name') or m.get('firstname'))
    last = _s(m.get('last_name') or m.get('lastname'))
    if first or last:
        return f'{first} {last}'.strip()
    return ''


def _pick_email(m: dict) -> str:
    for k in ('email', 'email_address', 'mail'):
        if _s(m.get(k)):
            return _s(m.get(k))
    return ''


def adapt_facebook(data: dict) -> dict:
    """Make flat, Meta field_data, hoặc webhook leadgen (stub nếu thiếu SĐT)."""
    data = dict(data or {})
    # Nested: entry[].changes[].value.field_data / leadgen_id
    if isinstance(data.get('entry'), list) and data['entry']:
        for entry in data['entry']:
            for ch in (entry.get('changes') or []):
                val = ch.get('value') or {}
                if not isinstance(val, dict):
                    continue
                fd = _field_data_to_map(val.get('field_data'))
                if fd or val.get('leadgen_id'):
                    merged = {**fd, **val}
                    return {
                        'contact_name': _pick_name(merged) or _pick_name(fd) or 'Lead Facebook',
                        'phone': _pick_phone(merged) or _pick_phone(fd),
                        'email': _pick_email(merged) or _pick_email(fd),
                        'company_name': _s(fd.get('company_name') or fd.get('company')),
                        'source': 'Facebook',
                        'channel': 'Facebook',
                        'external_id': _s(val.get('leadgen_id') or fd.get('leadgen_id')),
                        'notes': _s(fd.get('notes') or fd.get('message') or data.get('notes'))
                        or f"FB form={_s(val.get('form_id'))} page={_s(val.get('page_id'))}",
                        'utm_source': 'facebook',
                        'utm_medium': 'paid',
                    }
    fd = _field_data_to_map(data.get('field_data'))
    if fd:
        data = {**fd, **data}
    return {
        'contact_name': _pick_name(data) or 'Lead Facebook',
        'phone': _pick_phone(data),
        'email': _pick_email(data),
        'company_name': _s(data.get('company_name') or data.get('company')),
        'source': 'Facebook',
        'channel': 'Facebook',
        'external_id': _s(
            data.get('external_id') or data.get('leadgen_id') or data.get('lead_id') or data.get('id')
        ),
        'notes': _s(data.get('notes') or data.get('message') or data.get('ad_name')),
        'utm_source': _s(data.get('utm_source')) or 'facebook',
        'utm_medium': _s(data.get('utm_medium')) or 'paid',
        'utm_campaign': _s(data.get('utm_campaign') or data.get('campaign_name')),
        'owner': _s(data.get('owner')) or None,
    }


def adapt_zalo(data: dict) -> dict:
    data = dict(data or {})
    # Zalo OA often nests sender / message
    sender = data.get('sender') if isinstance(data.get('sender'), dict) else {}
    msg = data.get('message') if isinstance(data.get('message'), dict) else {}
    text = _s(msg.get('text') or data.get('text') or data.get('notes') or data.get('message'))
    phone = _pick_phone(data) or _s(data.get('user_id_by_app')) # fallback rarely phone
    # Some Zalo tools send phone in info
    info = data.get('info') if isinstance(data.get('info'), dict) else {}
    phone = phone or _pick_phone(info)
    name = (
        _pick_name(data)
        or _pick_name(info)
        or _s(sender.get('name') or data.get('display_name'))
        or 'Lead Zalo'
    )
    return {
        'contact_name': name,
        'phone': phone,
        'email': _pick_email(data) or _pick_email(info),
        'company_name': _s(data.get('company_name') or data.get('company')),
        'source': 'Zalo',
        'channel': 'Zalo',
        'external_id': _s(
            data.get('external_id')
            or data.get('msg_id')
            or sender.get('id')
            or data.get('user_id')
        ),
        'notes': text or _s(data.get('event_name')),
        'utm_source': _s(data.get('utm_source')) or 'zalo',
        'utm_medium': _s(data.get('utm_medium')) or 'oa',
        'utm_campaign': _s(data.get('utm_campaign')),
        'owner': _s(data.get('owner')) or None,
    }


def adapt_google(data: dict) -> dict:
    data = dict(data or {})
    # Google Lead Form Extension / Ads
    ud = data.get('user_column_data') or data.get('userColumnData')
    if isinstance(ud, list):
        m = {}
        for row in ud:
            if not isinstance(row, dict):
                continue
            key = _s(row.get('column_id') or row.get('columnId') or row.get('name')).lower()
            m[key] = _s(row.get('string_value') or row.get('stringValue') or row.get('value'))
        data = {**m, **data}
    return {
        'contact_name': _pick_name(data) or 'Lead Google',
        'phone': _pick_phone(data),
        'email': _pick_email(data),
        'company_name': _s(data.get('company_name') or data.get('company')),
        'source': 'Google',
        'channel': 'Google',
        'external_id': _s(data.get('external_id') or data.get('lead_id') or data.get('gcl_id') or data.get('gclid')),
        'notes': _s(data.get('notes') or data.get('message')),
        'utm_source': _s(data.get('utm_source')) or 'google',
        'utm_medium': _s(data.get('utm_medium')) or 'cpc',
        'utm_campaign': _s(data.get('utm_campaign') or data.get('campaign_id')),
        'owner': _s(data.get('owner')) or None,
    }


def adapt_tiktok(data: dict) -> dict:
    data = dict(data or {})
    # TikTok batch: data[].leads[]
    if isinstance(data.get('data'), list):
        for block in data['data']:
            leads = (block or {}).get('leads') if isinstance(block, dict) else None
            if isinstance(leads, list) and leads:
                lead = leads[0] if isinstance(leads[0], dict) else {}
                data = {**lead, **data}
                break
    if isinstance(data.get('leads'), list) and data['leads']:
        lead = data['leads'][0] if isinstance(data['leads'][0], dict) else {}
        data = {**lead, **data}
    return {
        'contact_name': _pick_name(data) or 'Lead TikTok',
        'phone': _pick_phone(data),
        'email': _pick_email(data),
        'company_name': _s(data.get('company_name') or data.get('company')),
        'source': 'TikTok',
        'channel': 'TikTok',
        'external_id': _s(data.get('external_id') or data.get('lead_id') or data.get('id')),
        'notes': _s(data.get('notes') or data.get('message') or data.get('ad_name')),
        'utm_source': _s(data.get('utm_source')) or 'tiktok',
        'utm_medium': _s(data.get('utm_medium')) or 'paid',
        'utm_campaign': _s(data.get('utm_campaign') or data.get('campaign_name')),
        'owner': _s(data.get('owner')) or None,
    }


def adapt_whatsapp(data: dict) -> dict:
    data = dict(data or {})
    # WhatsApp Cloud API entry.changes.value.messages
    if isinstance(data.get('entry'), list):
        for entry in data['entry']:
            for ch in (entry.get('changes') or []):
                val = ch.get('value') or {}
                contacts = val.get('contacts') or []
                messages = val.get('messages') or []
                cname = ''
                if contacts and isinstance(contacts[0], dict):
                    profile = contacts[0].get('profile') or {}
                    cname = _s(profile.get('name'))
                    wa_id = _s(contacts[0].get('wa_id'))
                else:
                    wa_id = ''
                text = ''
                phone = wa_id
                if messages and isinstance(messages[0], dict):
                    m0 = messages[0]
                    phone = _s(m0.get('from')) or phone
                    text = _s((m0.get('text') or {}).get('body') if isinstance(m0.get('text'), dict) else m0.get('text'))
                    if m0.get('type') == 'button' and isinstance(m0.get('button'), dict):
                        text = _s(m0['button'].get('text') or text)
                return {
                    'contact_name': cname or 'Lead WhatsApp',
                    'phone': phone,
                    'email': '',
                    'source': 'WhatsApp',
                    'channel': 'WhatsApp',
                    'external_id': _s((messages[0] or {}).get('id') if messages else '') or phone,
                    'notes': text or 'Tin nhắn WhatsApp',
                    'utm_source': 'whatsapp',
                    'utm_medium': 'chat',
                }
    return {
        'contact_name': _pick_name(data) or 'Lead WhatsApp',
        'phone': _pick_phone(data),
        'email': _pick_email(data),
        'company_name': _s(data.get('company_name') or data.get('company')),
        'source': 'WhatsApp',
        'channel': 'WhatsApp',
        'external_id': _s(data.get('external_id') or data.get('message_id') or data.get('wa_id')),
        'notes': _s(data.get('notes') or data.get('message') or data.get('text')),
        'utm_source': _s(data.get('utm_source')) or 'whatsapp',
        'utm_medium': _s(data.get('utm_medium')) or 'chat',
        'utm_campaign': _s(data.get('utm_campaign')),
        'owner': _s(data.get('owner')) or None,
    }


def adapt_viber(data: dict) -> dict:
    data = dict(data or {})
    sender = data.get('sender') if isinstance(data.get('sender'), dict) else {}
    msg = data.get('message') if isinstance(data.get('message'), dict) else {}
    return {
        'contact_name': _pick_name(data) or _s(sender.get('name')) or 'Lead Viber',
        'phone': _pick_phone(data) or _s(data.get('phone_number')),
        'email': _pick_email(data),
        'company_name': _s(data.get('company_name') or data.get('company')),
        'source': 'Viber',
        'channel': 'Viber',
        'external_id': _s(
            data.get('external_id') or data.get('message_token') or sender.get('id')
        ),
        'notes': _s(msg.get('text') or data.get('notes') or data.get('message') or data.get('text')),
        'utm_source': _s(data.get('utm_source')) or 'viber',
        'utm_medium': _s(data.get('utm_medium')) or 'chat',
        'utm_campaign': _s(data.get('utm_campaign')),
        'owner': _s(data.get('owner')) or None,
    }


def adapt_hotline(data: dict) -> dict:
    data = dict(data or {})
    raw_src = _s(data.get('source'))
    if raw_src in ('Giới thiệu', 'Triển lãm', 'Khác', 'Hotline'):
        source = raw_src
    else:
        source = 'Hotline'
    return {
        'contact_name': _pick_name(data) or f'Lead {source}',
        'phone': _pick_phone(data),
        'email': _pick_email(data),
        'company_name': _s(data.get('company_name') or data.get('company')),
        'source': source,
        'channel': source,
        'external_id': _s(data.get('external_id') or data.get('call_id') or data.get('cdr_id')),
        'notes': _s(data.get('notes') or data.get('message') or data.get('disposition')),
        'utm_source': _s(data.get('utm_source')) or 'hotline',
        'utm_medium': _s(data.get('utm_medium')) or 'phone',
        'utm_campaign': _s(data.get('utm_campaign')),
        'owner': _s(data.get('owner')) or None,
    }


def adapt_website(data: dict) -> dict:
    data = dict(data or {})
    data.setdefault('source', 'Website')
    return {
        'contact_name': _pick_name(data) or 'Lead Website',
        'phone': _pick_phone(data),
        'email': _pick_email(data),
        'company_name': _s(data.get('company_name') or data.get('company')),
        'source': 'Website',
        'channel': 'Website',
        'external_id': _s(data.get('external_id')),
        'notes': _s(data.get('notes') or data.get('message')),
        'utm_source': _s(data.get('utm_source')),
        'utm_medium': _s(data.get('utm_medium')),
        'utm_campaign': _s(data.get('utm_campaign')),
        'owner': _s(data.get('owner')) or None,
    }


ADAPTERS = {
    'facebook': adapt_facebook,
    'zalo': adapt_zalo,
    'google': adapt_google,
    'tiktok': adapt_tiktok,
    'whatsapp': adapt_whatsapp,
    'viber': adapt_viber,
    'hotline': adapt_hotline,
    'website': adapt_website,
}


def adapt_channel(channel: str, data: dict | None) -> dict:
    slug = _s(channel).lower()
    fn = ADAPTERS.get(slug)
    if not fn:
        raise ValueError(f'Kênh không hỗ trợ: {channel}')
    out = fn(dict(data or {}))
    # Hotline adapter có thể mang source Giới thiệu / Triển lãm
    if slug == 'hotline' and out.get('source') in (
        'Hotline', 'Giới thiệu', 'Triển lãm', 'Khác',
    ):
        pass
    else:
        out['source'] = CHANNEL_TO_SOURCE.get(slug, out.get('source') or 'Khác')
    out['channel'] = out.get('channel') or CHANNEL_TO_SOURCE.get(slug) or out['source']
    return out
