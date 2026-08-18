"""4 trường hợp áp dụng chế độ kế toán DNSN — TT58/2026/TT-BTC (thay TT 132/2018/TT-BTC).

Hiệu lực: 01/07/2026. Mã lưu DB giữ article5–8 (Điều 5–8) để tương thích.
"""
from __future__ import annotations

from typing import Any

# Khóa lưu trong sme_coa_seed_meta.key = 'tt58_tax_method'
TT58_TAX_METHODS: dict[str, dict[str, Any]] = {
    'article5': {
        'code': 'article5',
        'case_no': 1,
        'method_no': 1,
        'article': 5,
        'short_label': 'Trường hợp 1 — GTGT % DT + TNDN % DT',
        'label': (
            'Nộp thuế GTGT theo tỷ lệ % trên doanh thu và '
            'thuế TNDN theo tỷ lệ % trên doanh thu'
        ),
        'description': (
            'Đơn giản nhất. Chỉ sổ doanh thu bán hàng hóa, dịch vụ S1-DNSN. '
            'Không bắt buộc lập Báo cáo tài chính.'
        ),
        'vat_mode': 'pct_revenue',
        # GTGT % DT: không khấu trừ đầu vào → vốn hóa vào giá hàng/TSCĐ/chi phí
        'input_vat_in_cost': True,
        'cit_mode': 'pct_revenue',
        'vat_label': 'Tỷ lệ % trên doanh thu',
        'cit_label': 'Tỷ lệ % trên doanh thu',
        'required_books': ('S1-DNSN',),
        'optional_books': (),
        'show_vouchers': True,
        'require_bctc': False,
        'show_bctc': False,
        'bctc_deadline_days': None,
    },
    'article6': {
        'code': 'article6',
        'case_no': 2,
        'method_no': 2,
        'article': 6,
        'short_label': 'Trường hợp 2 — GTGT % DT + TNDN trên thu nhập',
        'label': (
            'Nộp thuế GTGT theo tỷ lệ % trên doanh thu và '
            'thuế TNDN trên thu nhập tính thuế (Doanh thu − Chi phí) × thuế suất'
        ),
        'description': (
            'Bắt buộc 4 sổ: S2a (doanh thu), S2b (chi phí — 6 nhóm), '
            'S2c (vật tư/hàng hóa), S2d (tiền mặt/tiền gửi). '
            'Bắt buộc lập BCTC năm (nộp trong 90 ngày sau khi kết thúc năm tài chính).'
        ),
        'vat_mode': 'pct_revenue',
        'input_vat_in_cost': True,
        'cit_mode': 'taxable_income',
        'vat_label': 'Tỷ lệ % trên doanh thu',
        'cit_label': 'Trên thu nhập tính thuế (DT − CP) × thuế suất',
        'required_books': ('S2a-DNSN', 'S2b-DNSN', 'S2c-DNSN', 'S2d-DNSN'),
        'optional_books': ('S4a-DNSN', 'S4b-DNSN', 'S4c-DNSN', 'S4d-DNSN'),
        'show_vouchers': True,
        'require_bctc': True,
        'show_bctc': True,
        'bctc_deadline_days': 90,
    },
    'article7': {
        'code': 'article7',
        'case_no': 3,
        'method_no': 3,
        'article': 7,
        'short_label': 'Trường hợp 3 — GTGT khấu trừ + TNDN % DT',
        'label': (
            'Nộp thuế GTGT theo phương pháp khấu trừ (đầu ra − đầu vào) và '
            'thuế TNDN theo tỷ lệ % trên doanh thu'
        ),
        'description': (
            'Bắt buộc 2 sổ: S3a-DNSN (doanh thu bán hàng) và S3b-DNSN '
            '(theo dõi nghĩa vụ thuế GTGT). Không bắt buộc lập BCTC.'
        ),
        'vat_mode': 'deduction',
        'input_vat_in_cost': False,
        'cit_mode': 'pct_revenue',
        'vat_label': 'Phương pháp khấu trừ (đầu ra − đầu vào)',
        'cit_label': 'Tỷ lệ % trên doanh thu',
        'required_books': ('S3a-DNSN', 'S3b-DNSN'),
        'optional_books': ('S4a-DNSN', 'S4b-DNSN', 'S4c-DNSN', 'S4d-DNSN'),
        'show_vouchers': True,
        'require_bctc': False,
        'show_bctc': False,
        'bctc_deadline_days': None,
    },
    'article8': {
        'code': 'article8',
        'case_no': 4,
        'method_no': 4,
        'article': 8,
        'short_label': 'Trường hợp 4 — GTGT khấu trừ + TNDN trên thu nhập',
        'label': (
            'Nộp thuế GTGT theo phương pháp khấu trừ và '
            'thuế TNDN trên thu nhập tính thuế (Doanh thu − Chi phí) × thuế suất'
        ),
        'description': (
            'Bắt buộc 4 sổ: S2b (chi phí), S2c (vật tư/hàng hóa), S2d (tiền), '
            'S3b-DNSN (nghĩa vụ GTGT). Bắt buộc lập BCTC năm '
            '(nộp trong 90 ngày sau khi kết thúc năm tài chính).'
        ),
        'vat_mode': 'deduction',
        'input_vat_in_cost': False,
        'cit_mode': 'taxable_income',
        'vat_label': 'Phương pháp khấu trừ (đầu ra − đầu vào)',
        'cit_label': 'Trên thu nhập tính thuế (DT − CP) × thuế suất',
        'required_books': ('S2b-DNSN', 'S2c-DNSN', 'S2d-DNSN', 'S3b-DNSN'),
        'optional_books': ('S4a-DNSN', 'S4b-DNSN', 'S4c-DNSN', 'S4d-DNSN'),
        'show_vouchers': True,
        'require_bctc': True,
        'show_bctc': True,
        'bctc_deadline_days': 90,
    },
}

DEFAULT_TT58_TAX_METHOD = 'article5'

_ALIASES = {
    '1': 'article5', 'pp1': 'article5', 'method1': 'article5',
    'th1': 'article5', 'case1': 'article5', 'truonghop1': 'article5',
    'article_5': 'article5', 'dieu5': 'article5', 'điều5': 'article5',
    '2': 'article6', 'pp2': 'article6', 'method2': 'article6',
    'th2': 'article6', 'case2': 'article6', 'truonghop2': 'article6',
    'article_6': 'article6', 'dieu6': 'article6',
    '3': 'article7', 'pp3': 'article7', 'method3': 'article7',
    'th3': 'article7', 'case3': 'article7', 'truonghop3': 'article7',
    'article_7': 'article7', 'dieu7': 'article7',
    '4': 'article8', 'pp4': 'article8', 'method4': 'article8',
    'th4': 'article8', 'case4': 'article8', 'truonghop4': 'article8',
    'article_8': 'article8', 'dieu8': 'article8',
}


def normalize_tt58_tax_method(value: str | None) -> str:
    raw = (value or '').strip().lower().replace('-', '').replace(' ', '')
    if not raw:
        return DEFAULT_TT58_TAX_METHOD
    if raw in TT58_TAX_METHODS:
        return raw
    mapped = _ALIASES.get(raw) or _ALIASES.get(raw.replace('_', ''))
    if mapped:
        return mapped
    return DEFAULT_TT58_TAX_METHOD


def get_tt58_tax_method_def(code: str | None) -> dict[str, Any]:
    key = normalize_tt58_tax_method(code)
    return dict(TT58_TAX_METHODS[key])


def list_tt58_tax_methods() -> list[dict[str, Any]]:
    return [dict(TT58_TAX_METHODS[k]) for k in (
        'article5', 'article6', 'article7', 'article8',
    )]


def tt58_input_vat_in_inventory_cost(conn) -> bool:
    """TH1/TH2 (đã chọn): VAT đầu vào không khấu trừ, cộng vào giá vốn / nguyên giá.

    Chưa chọn trường hợp → False (giữ như khấu trừ, không vốn hóa nhầm).
    """
    if conn is None:
        return False
    try:
        from Services.sme.regime_profile import get_ledger_profile
        profile = get_ledger_profile(conn)
    except Exception:
        return False
    tax_def = profile.get('tt58_tax_method_def') or {}
    if tax_def.get('input_vat_in_cost') is not None:
        return bool(tax_def.get('input_vat_in_cost'))
    return bool(
        profile.get('is_tt58_micro')
        and tax_def.get('vat_mode') == 'pct_revenue'
    )
