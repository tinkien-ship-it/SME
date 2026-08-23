# -*- coding: utf-8 -*-
"""Logo & hiển thị thương hiệu HKD/DN trên navbar."""
from __future__ import annotations

import os
import re
from typing import Any

from flask import g, url_for

from db_utils import BASE_DIR

ALLOWED_LOGO_EXTENSIONS = frozenset({'png', 'jpg', 'jpeg', 'webp', 'gif'})
MAX_LOGO_BYTES = 2 * 1024 * 1024
DEFAULT_LOGO_REL = 'img/vietnam-flag.svg'


def default_business_logo_url() -> str:
    try:
        return url_for('static', filename=DEFAULT_LOGO_REL)
    except Exception:
        return f'/static/{DEFAULT_LOGO_REL}'


def is_custom_business_logo(logo_path: str | None) -> bool:
    rel = (logo_path or '').strip().lstrip('/')
    if not rel:
        return False
    if rel == DEFAULT_LOGO_REL:
        return False
    return rel.startswith('branding/')


def resolve_business_logo_url(logo_path: str | None) -> str:
    """URL logo tùy chỉnh hoặc cờ Việt Nam mặc định."""
    rel = (logo_path or '').strip().lstrip('/')
    if not rel or rel == DEFAULT_LOGO_REL:
        return default_business_logo_url()
    if rel.startswith('http://') or rel.startswith('https://'):
        return rel
    abs_path = os.path.join(BASE_DIR, 'static', rel.replace('/', os.sep))
    if not os.path.isfile(abs_path):
        return default_business_logo_url()
    try:
        return url_for('static', filename=rel)
    except Exception:
        return f'/static/{rel}'


def get_business_logo_context(logo_path: str | None = None) -> dict[str, Any]:
    """Biến template: business_logo_url, has_custom_business_logo."""
    url = resolve_business_logo_url(logo_path)
    if not url:
        url = default_business_logo_url()
    return {
        'business_logo_url': url,
        'has_custom_business_logo': is_custom_business_logo(logo_path),
    }


def _branding_root() -> str:
    return os.path.join(BASE_DIR, 'static', 'branding')


def branding_tenant_key(tenant_id: str | None = None) -> str:
    tid = (tenant_id or getattr(g, 'tenant_id', None) or 'main').strip() or 'main'
    safe = re.sub(r'[^\w\-.:]', '_', tid)
    return safe[:96] or 'main'


def ensure_business_logo_column(cursor) -> None:
    cursor.execute('PRAGMA table_info(business_info)')
    existing = {row[1] for row in cursor.fetchall()}
    if 'logo_path' not in existing:
        cursor.execute('ALTER TABLE business_info ADD COLUMN logo_path TEXT')


def save_business_logo_file(
    file_storage,
    *,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Lưu file logo vào static/branding/{tenant}/ — trả logo_path tương đối."""
    if not file_storage or not getattr(file_storage, 'filename', None):
        return {'success': False, 'error': 'Chưa chọn file logo'}

    ext = (file_storage.filename.rsplit('.', 1)[-1] if '.' in file_storage.filename else '').lower()
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        return {
            'success': False,
            'error': 'Chỉ chấp nhận ảnh PNG, JPG, WEBP hoặc GIF',
        }

    raw = file_storage.read()
    if not raw:
        return {'success': False, 'error': 'File logo rỗng'}
    if len(raw) > MAX_LOGO_BYTES:
        return {'success': False, 'error': 'Logo tối đa 2 MB'}

    key = branding_tenant_key(tenant_id)
    dest_dir = os.path.join(_branding_root(), key)
    os.makedirs(dest_dir, exist_ok=True)

    for name in os.listdir(dest_dir):
        if name.startswith('logo.'):
            try:
                os.remove(os.path.join(dest_dir, name))
            except OSError:
                pass

    filename = f'logo.{ext}'
    abs_path = os.path.join(dest_dir, filename)
    with open(abs_path, 'wb') as fh:
        fh.write(raw)

    rel = f'branding/{key}/{filename}'.replace('\\', '/')
    return {'success': True, 'logo_path': rel, 'logo_url': resolve_business_logo_url(rel)}


def remove_business_logo_file(logo_path: str | None) -> None:
    rel = (logo_path or '').strip().lstrip('/')
    if not rel or rel.startswith('http') or rel == DEFAULT_LOGO_REL:
        return
    abs_path = os.path.join(BASE_DIR, 'static', rel.replace('/', os.sep))
    if os.path.isfile(abs_path):
        try:
            os.remove(abs_path)
        except OSError:
            pass
