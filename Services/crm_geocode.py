# -*- coding: utf-8 -*-
"""Reverse geocoding — Google Maps Geocoding API (lat/lng → Phường, Tỉnh/TP)."""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
from typing import Any


def get_maps_api_key(conn: sqlite3.Connection) -> str:
    from Services.crm_ops import get_setting

    return (get_setting(conn, 'google_maps_api_key') or '').strip()


def reverse_geocode(
    lat: float | None,
    lng: float | None,
    *,
    api_key: str = '',
    language: str = 'vi',
) -> dict[str, Any]:
    """Đổi tọa độ → địa chỉ hành chính. Trả dict rỗng nếu thiếu key hoặc lỗi."""
    out: dict[str, Any] = {
        'ward': '',
        'district': '',
        'province': '',
        'formatted_address': '',
    }
    if lat is None or lng is None or not api_key:
        return out
    try:
        q = urllib.parse.urlencode({
            'latlng': f'{float(lat):.7f},{float(lng):.7f}',
            'key': api_key,
            'language': language,
        })
        url = f'https://maps.googleapis.com/maps/api/geocode/json?{q}'
        req = urllib.request.Request(url, headers={'User-Agent': 'SME-CRM-Visit/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
    except Exception:
        return out

    if payload.get('status') != 'OK':
        return out

    results = payload.get('results') or []
    if not results:
        return out

    best = results[0]
    out['formatted_address'] = str(best.get('formatted_address') or '').strip()

    ward = district = province = ''
    for comp in best.get('address_components') or []:
        types = comp.get('types') or []
        name = str(comp.get('long_name') or '').strip()
        if not name:
            continue
        if 'administrative_area_level_1' in types:
            province = name
        elif 'administrative_area_level_2' in types:
            district = name
        elif 'administrative_area_level_3' in types and not ward:
            ward = name
        elif ('sublocality_level_1' in types or 'sublocality' in types) and not ward:
            ward = name
        elif 'locality' in types and not ward:
            ward = name

    out['ward'] = ward
    out['district'] = district
    out['province'] = province
    return out


def location_label(geo: dict[str, Any]) -> str:
    parts = [
        str(geo.get('ward') or '').strip(),
        str(geo.get('district') or '').strip(),
        str(geo.get('province') or '').strip(),
    ]
    parts = [p for p in parts if p]
    if parts:
        return ', '.join(parts)
    return str(geo.get('formatted_address') or '').strip()
