"""Factory chọn adapter HĐĐT theo cấu hình invoice_settings."""
from Services.einvoice_adapters import (
    EasyInvoiceInvoiceAdapter,
    MatbaoAdapterWrapper,
    MisaInvoiceAdapter,
    MobifoneInvoiceAdapter,
    PendingProviderAdapter,
    ViettelInvoiceAdapter,
    VNPTInvoiceAdapter,
)
from Services.einvoice_registry import get_provider_meta, normalize_provider_code
from Services.invoice_xml import normalize_invoice_config


def create_einvoice_service(config, matbao_cls=None):
    """
    Tạo service xuất HĐ theo provider_name trong invoice_settings.
    matbao_cls: class MatbaoProvider (định nghĩa trong routes/invoice.py).
    """
    cfg = normalize_invoice_config(config or {})
    key = normalize_provider_code(cfg.get('provider_name') or cfg.get('provider') or 'matbao')

    if key == 'matbao':
        if matbao_cls is None:
            raise ValueError('MatbaoProvider class is required for provider matbao')
        return MatbaoAdapterWrapper(cfg, matbao_cls)

    if key == 'misa':
        return MisaInvoiceAdapter(cfg)

    if key == 'viettel':
        return ViettelInvoiceAdapter(cfg)

    if key == 'easyinvoice':
        return EasyInvoiceInvoiceAdapter(cfg)

    if key == 'mobifone':
        return MobifoneInvoiceAdapter(cfg)

    if key == 'vnpt':
        return VNPTInvoiceAdapter(cfg)

    meta = get_provider_meta(key)
    if meta and meta.get('alias_of'):
        alias = PendingProviderAdapter(cfg)
        alias.provider_key = key
        return alias

    if meta:
        adapter = PendingProviderAdapter(cfg)
        adapter.provider_key = key
        return adapter

    adapter = PendingProviderAdapter(cfg)
    adapter.provider_key = key or 'unknown'
    return adapter


def merge_esign_config_from_request(data):
    """Gộp cấu hình form với giá trị đã lưu DB (password rỗng → giữ cũ)."""
    from db.init import get_db_connection
    import sqlite3

    cfg = dict(data or {})
    provider = (cfg.get('provider_name') or cfg.get('provider') or '').strip()
    if not provider:
        return cfg

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        'SELECT * FROM invoice_settings WHERE provider_name = ?',
        (provider,),
    ).fetchone()
    conn.close()
    if not row:
        return cfg

    old = dict(row)
    for key, val in old.items():
        if key not in cfg or cfg.get(key) in (None, ''):
            cfg[key] = val

    for field in ('password', 'app_secret', 'esign_pin', 'etax_password'):
        new_val = (data or {}).get(field)
        if new_val is not None and str(new_val).strip():
            cfg[field] = str(new_val).strip()

    return cfg


def test_einvoice_connection(config, matbao_cls=None):
    cfg = normalize_invoice_config(merge_esign_config_from_request(config or {}))
    service = create_einvoice_service(cfg, matbao_cls=matbao_cls)
    if hasattr(service, 'test_connection') and callable(service.test_connection):
        return service.test_connection()
    provider = (cfg.get('provider_name') or cfg.get('provider') or '').strip()
    meta = get_provider_meta(provider) or {}
    label = meta.get('label', provider or 'HĐĐT')
    if not cfg.get('username') or not cfg.get('password'):
        return {'success': False, 'error': f'{label}: thiếu username hoặc password.'}
    if not cfg.get('api_url'):
        return {'success': False, 'error': f'{label}: thiếu API URL.'}
    return {
        'success': True,
        'message': f'{label}: đã nhận cấu hình (chưa có kiểm tra sâu cho provider này).',
    }


def provider_supports_draft(config):
    cfg = normalize_invoice_config(config or {})
    key = normalize_provider_code(cfg.get('provider_name') or cfg.get('provider'))
    meta = get_provider_meta(key)
    if meta:
        return bool(meta.get('supports_draft'))
    service = create_einvoice_service(cfg)
    return bool(getattr(service, 'supports_draft', False))
