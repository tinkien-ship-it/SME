# -*- coding: utf-8 -*-
"""Đọc cấu hình HĐĐT từ invoice_settings (đúng provider đang chọn ở Settings)."""
from __future__ import annotations

import sqlite3
from typing import Any

from db_utils import get_db_connection
from Services.einvoice_registry import get_provider_meta, normalize_provider_code


def _row_to_dict(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def get_active_invoice_config() -> dict[str, Any] | None:
    """Provider đang kích hoạt (is_active=1) — nguồn sự thật cho bán & mua."""
    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM invoice_settings WHERE is_active = 1 LIMIT 1"
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def get_invoice_config_by_provider(provider_name: str | None) -> dict[str, Any] | None:
    """Lấy cấu hình theo mã provider (không phụ thuộc is_active)."""
    key = normalize_provider_code(provider_name or '')
    if not key:
        return None
    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM invoice_settings WHERE LOWER(TRIM(provider_name)) = ? LIMIT 1",
            (key,),
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def provider_supports_purchase_sync(provider_name: str | None) -> bool:
    meta = get_provider_meta(normalize_provider_code(provider_name or '')) or {}
    return bool(meta.get('supports_purchase_sync'))


def get_purchase_sync_config() -> dict[str, Any]:
    """
    Cấu hình dùng đồng bộ HĐ đầu vào / GDT.
    Bắt buộc theo provider đang active ở Settings.
    """
    cfg = get_active_invoice_config()
    if not cfg:
        raise ValueError(
            "Chưa cấu hình nhà cung cấp HĐĐT. Vào Hệ thống → Thiết lập → chọn provider và lưu."
        )
    key = normalize_provider_code(cfg.get('provider_name') or cfg.get('provider') or '')
    label = (get_provider_meta(key) or {}).get('label') or key or 'HĐĐT'
    if not provider_supports_purchase_sync(key):
        raise ValueError(
            f"Nhà cung cấp đang chọn ({label}) chưa hỗ trợ đồng bộ hóa đơn mua hàng từ CQT. "
            f"Vào Settings chọn nhà cung cấp có hỗ trợ HĐ đầu vào (ví dụ Mắt Bão), rồi thử lại."
        )
    api_url = (cfg.get('api_url') or '').strip().rstrip('/')
    api_key = (cfg.get('api_key') or '').strip()
    if not api_url or not api_key:
        raise ValueError(
            f"Thiếu API URL hoặc Api Key cho {label}. Kiểm tra lại cấu hình ở Settings."
        )
    # Chuẩn hóa key dùng cho purchase provider
    out = dict(cfg)
    out['provider_name'] = key
    out['name'] = key
    out['api_url'] = api_url
    out['api_key'] = api_key
    return out


def require_active_provider(*allowed: str) -> dict[str, Any]:
    """Lấy config active; nếu allowed được truyền thì bắt buộc thuộc danh sách."""
    cfg = get_active_invoice_config()
    if not cfg:
        raise ValueError("Chưa cấu hình nhà cung cấp HĐĐT (Settings).")
    key = normalize_provider_code(cfg.get('provider_name') or '')
    if allowed:
        allowed_norm = {normalize_provider_code(a) for a in allowed}
        if key not in allowed_norm:
            label = (get_provider_meta(key) or {}).get('label') or key
            raise ValueError(
                f"Chức năng này chỉ dùng với {', '.join(sorted(allowed_norm))}. "
                f"Provider đang chọn: {label}."
            )
    return cfg
