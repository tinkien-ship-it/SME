"""Danh mục nhà cung cấp HĐĐT tại Việt Nam — metadata & trạng thái tích hợp."""

# status: ready | beta | planned
EINVOICE_PROVIDERS = {
    'matbao': {
        'code': 'matbao',
        'label': 'Mắt Bão Invoice & eSign',
        'status': 'ready',
        'supports_draft': True,
        'supports_replacement': True,
        'supports_portal_sync': True,
        'supports_purchase_sync': True,
        # API-Proxy-HDDT (phát hành HĐ bán): /api/auth/login {MST, TDNhap, MKhau}
        'default_api_url': 'https://api-hddt.matbao.in:11443',
        'demo_api_url': 'https://demo-api-hddt.matbao.in:11443',
        # Purchase Inv API (HĐ đầu vào): /auth/token {token: ApiKey}
        'default_purchase_api_url': 'https://api-hoadondauvao.matbao.in',
        'demo_purchase_api_url': 'https://demo-api-hoadondauvao.matbao.in',
        'doc_hint': (
            'HĐ bán: api-hddt.matbao.in:11443 (MST + TDNhap + MKhau). '
            'HĐ mua: api-hoadondauvao.matbao.in (Api Key/Token → /auth/token). '
            'CQT: MST + mật khẩu hoadondientu.gdt.gov.vn + captcha.'
        ),
    },
    'misa': {
        'code': 'misa',
        'label': 'MISA meInvoice',
        'status': 'beta',
        'supports_draft': True,
        'supports_replacement': False,
        'supports_portal_sync': False,
        'supports_purchase_sync': False,
        'default_api_url': 'https://testapi.meinvoice.vn/api/v3',
        'doc_hint': 'Tài liệu: doc.meinvoice.vn — App ID + MST + SignedService (port 12019) để ký số.',
    },
    'viettel': {
        'code': 'viettel',
        'label': 'Viettel S-Invoice',
        'status': 'beta',
        'supports_draft': False,
        'supports_replacement': False,
        'supports_portal_sync': False,
        'supports_purchase_sync': False,
        'default_api_url': 'https://sinvoice.viettel.vn:8443/InvoiceAPI/InvoiceWS',
        'doc_hint': 'Viettel S-Invoice → Tài liệu API createInvoice',
    },
    'vnpt': {
        'code': 'vnpt',
        'label': 'VNPT Invoice',
        'status': 'beta',
        'supports_draft': True,
        'supports_replacement': True,
        'supports_portal_sync': True,
        'supports_purchase_sync': False,
        'default_api_url': (
            'https://vnpthcmc-tt78admindemo.vnpt-invoice.com.vn/PublishService.asmx'
        ),
        'doc_hint': (
            'SOAP PublishService.asmx (TT78). Username/Password = ServiceRole (vd. vnpthcmc_service). '
            'Api Key/ACPass = Account nhân viên phát hành. Portal demo: vnpthcmc-tt78admindemo.vnpt-invoice.com.vn. '
            'Nháp SME (loai_hdon=0) → ImportInvByPattern — VNPT không có mã type cho nháp. '
            'Chính thức (loai_hdon=1) → ImportAndPublishInv hoặc PublishInvFkey (tương đương VNPT type=0). '
            'Thay thế → BusinessService.replaceInv (VNPT type=1, TCHDon=1 trong XML TT78).'
        ),
    },
    'fpt': {
        'code': 'fpt',
        'label': 'FPT eInvoice',
        'status': 'planned',
        'supports_draft': True,
        'supports_replacement': True,
        'supports_portal_sync': False,
        'supports_purchase_sync': False,
        'default_api_url': 'https://einvoice.fpt.com.vn',
        'doc_hint': 'FPT eInvoice → Tích hợp hệ thống',
    },
    'bkav': {
        'code': 'bkav',
        'label': 'BKAV eHoadon',
        'status': 'planned',
        'supports_draft': True,
        'supports_replacement': True,
        'supports_portal_sync': False,
        'supports_purchase_sync': False,
        'default_api_url': 'https://van.ehoadon.vn',
        'doc_hint': 'BKAV eHoadon API',
    },
    'easyinvoice': {
        'code': 'easyinvoice',
        'label': 'EasyInvoice (Softdreams)',
        'status': 'beta',
        'supports_draft': True,
        'supports_replacement': False,
        'supports_portal_sync': False,
        'supports_purchase_sync': False,
        'default_api_url': 'https://api.easyinvoice.vn/api/publish/importInvoice',
        'doc_hint': (
            'REST importInvoice (domain chung từ 01/2026). '
            'App Secret = Partner Key ký HMAC; Convert=0 nháp, Convert=1 phát hành.'
        ),
    },
    'mobifone': {
        'code': 'mobifone',
        'label': 'M-Invoice (Mobifone)',
        'status': 'beta',
        'supports_draft': True,
        'supports_replacement': True,
        'supports_portal_sync': False,
        'supports_purchase_sync': False,
        'default_api_url': 'https://hoadon.minvoice.com.vn',
        'doc_hint': 'API v4.7 — Login JWT, SaveListHoadon78. Tài liệu: wiki.minvoice.com.vn',
    },
    'fast': {
        'code': 'fast',
        'label': 'FAST e-Invoice',
        'status': 'planned',
        'supports_draft': True,
        'supports_replacement': False,
        'supports_portal_sync': False,
        'supports_purchase_sync': False,
        'default_api_url': 'https://einvoice.fast.com.vn',
        'doc_hint': 'FAST Accounting e-Invoice API',
    },
    'thaison': {
        'code': 'thaison',
        'label': 'Thái Sơn / TS24 E-Invoice',
        'status': 'planned',
        'supports_draft': True,
        'supports_replacement': False,
        'supports_portal_sync': False,
        'supports_purchase_sync': False,
        'default_api_url': 'https://hoadon.thaison.vn',
        'doc_hint': 'TS24 / Thái Sơn E-Invoice',
    },
    'cyberlotus': {
        'code': 'cyberlotus',
        'label': 'CyberLotus / Hilo Invoice',
        'status': 'planned',
        'supports_draft': True,
        'supports_replacement': False,
        'supports_portal_sync': False,
        'supports_purchase_sync': False,
        'default_api_url': 'https://api.hilo.com.vn',
        'doc_hint': 'Hilo / CyberLotus CyberBill',
    },
    'hilo': {
        'code': 'hilo',
        'label': 'Hilo Invoice (CyberLotus)',
        'status': 'planned',
        'supports_draft': True,
        'supports_replacement': False,
        'supports_portal_sync': False,
        'supports_purchase_sync': False,
        'default_api_url': 'https://api.hilo.com.vn',
        'doc_hint': 'Alias Hilo — cùng nền tảng CyberLotus',
        'alias_of': 'cyberlotus',
    },
    'efyc': {
        'code': 'efyc',
        'label': 'EFY eInvoice',
        'status': 'planned',
        'supports_draft': True,
        'supports_replacement': False,
        'supports_portal_sync': False,
        'supports_purchase_sync': False,
        'default_api_url': 'https://efy.com.vn',
        'doc_hint': 'EFY eInvoice API',
    },
    'cmc': {
        'code': 'cmc',
        'label': 'CMC eInvoice',
        'status': 'planned',
        'supports_draft': True,
        'supports_replacement': False,
        'supports_portal_sync': False,
        'supports_purchase_sync': False,
        'default_api_url': 'https://einvoice.cmc.com.vn',
        'doc_hint': 'CMC TS eInvoice',
    },
    'newinvoice': {
        'code': 'newinvoice',
        'label': 'NewInvoice',
        'status': 'planned',
        'supports_draft': True,
        'supports_replacement': False,
        'supports_portal_sync': False,
        'supports_purchase_sync': False,
        'default_api_url': 'https://newinvoice.vn',
        'doc_hint': 'NewInvoice API',
    },
}


def normalize_provider_code(code):
    return (code or '').strip().lower()


def get_provider_meta(code):
    key = normalize_provider_code(code)
    meta = EINVOICE_PROVIDERS.get(key)
    if meta:
        return dict(meta)
    alias = next((m for m in EINVOICE_PROVIDERS.values() if m.get('alias_of') == key), None)
    if alias:
        merged = dict(alias)
        merged['code'] = key
        return merged
    return None


def list_providers_for_ui():
    """Danh sách provider cho dropdown (bỏ alias trùng nền tảng)."""
    out = []
    seen_labels = set()
    for code, meta in EINVOICE_PROVIDERS.items():
        if meta.get('alias_of'):
            continue
        label = meta['label']
        if label in seen_labels:
            continue
        seen_labels.add(label)
        out.append({
            'code': code,
            'label': label,
            'status': meta['status'],
            'supports_draft': meta.get('supports_draft', False),
            'supports_purchase_sync': meta.get('supports_purchase_sync', False),
            'default_api_url': meta.get('default_api_url', ''),
            'default_purchase_api_url': meta.get('default_purchase_api_url', ''),
            'doc_hint': meta.get('doc_hint', ''),
        })
    return sorted(out, key=lambda x: (0 if x['status'] == 'ready' else 1 if x['status'] == 'beta' else 2, x['label']))


def list_providers_api():
    return [
        {
            'code': code,
            **{k: v for k, v in meta.items() if k != 'code'},
        }
        for code, meta in EINVOICE_PROVIDERS.items()
        if not meta.get('alias_of')
    ]
