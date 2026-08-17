"""4 phương pháp nộp thuế GTGT/TNDN — TT58/2026/TT-BTC Điều 5–8."""
from __future__ import annotations

from typing import Any

# Khóa lưu trong sme_coa_seed_meta.key = 'tt58_tax_method'
TT58_TAX_METHODS: dict[str, dict[str, Any]] = {
    'article5': {
        'code': 'article5',
        'method_no': 1,
        'article': 5,
        'short_label': 'PP1 — GTGT % + TNDN % trên doanh thu',
        'label': (
            'Nộp cả thuế GTGT và thuế TNDN theo tỷ lệ % trên doanh thu'
        ),
        'description': (
            'Đơn giản nhất. Chỉ sổ doanh thu S1-DNSN. '
            'Không bắt buộc lập BCTC nộp cơ quan nhà nước (Điều 10.1.b).'
        ),
        'vat_mode': 'pct_revenue',
        'cit_mode': 'pct_revenue',
        'required_books': ('S1-DNSN',),
        'optional_books': (),  # tối giản sổ bắt buộc
        'show_vouchers': True,  # Điều 9 — chứng từ tùy nghi dùng
        'require_bctc': False,
        'show_bctc': False,
    },
    'article6': {
        'code': 'article6',
        'method_no': 2,
        'article': 6,
        'short_label': 'PP2 — GTGT % + TNDN trên thu nhập tính thuế',
        'label': (
            'Nộp thuế GTGT theo tỷ lệ % trên doanh thu và '
            'thuế TNDN trên thu nhập tính thuế (Doanh thu − Chi phí)'
        ),
        'description': (
            'Phù hợp khi quản lý được chi phí đầu vào nhưng chưa khấu trừ GTGT. '
            'Bắt buộc lập BCTC năm (Điều 10.1.a).'
        ),
        'vat_mode': 'pct_revenue',
        'cit_mode': 'taxable_income',
        'required_books': ('S2a-DNSN', 'S2b-DNSN', 'S2c-DNSN', 'S2d-DNSN'),
        'optional_books': ('S4a-DNSN', 'S4b-DNSN', 'S4c-DNSN', 'S4d-DNSN'),
        'show_vouchers': True,
        'require_bctc': True,
        'show_bctc': True,
    },
    'article7': {
        'code': 'article7',
        'method_no': 3,
        'article': 7,
        'short_label': 'PP3 — GTGT khấu trừ + TNDN % trên doanh thu',
        'label': (
            'Nộp thuế GTGT theo phương pháp khấu trừ và '
            'thuế TNDN theo tỷ lệ % trên doanh thu'
        ),
        'description': (
            'Có hóa đơn đầu vào GTGT đầy đủ; TNDN vẫn theo % doanh thu. '
            'Không bắt buộc nộp BCTC cho CQNN (Điều 10.1.b).'
        ),
        'vat_mode': 'deduction',
        'cit_mode': 'pct_revenue',
        'required_books': ('S3a-DNSN', 'S3b-DNSN'),
        'optional_books': ('S4a-DNSN', 'S4b-DNSN', 'S4c-DNSN', 'S4d-DNSN'),
        'show_vouchers': True,
        'require_bctc': False,
        'show_bctc': False,
    },
    'article8': {
        'code': 'article8',
        'method_no': 4,
        'article': 8,
        'short_label': 'PP4 — GTGT khấu trừ + TNDN trên thu nhập tính thuế',
        'label': (
            'Nộp thuế GTGT theo phương pháp khấu trừ và '
            'thuế TNDN trên thu nhập tính thuế'
        ),
        'description': (
            'Bộ sổ đầy đủ nhất, phản ánh sát thực tế kinh doanh. '
            'Bắt buộc lập BCTC năm (Điều 10.1.a).'
        ),
        'vat_mode': 'deduction',
        'cit_mode': 'taxable_income',
        'required_books': ('S2b-DNSN', 'S2c-DNSN', 'S2d-DNSN', 'S3b-DNSN'),
        'optional_books': ('S4a-DNSN', 'S4b-DNSN', 'S4c-DNSN', 'S4d-DNSN'),
        'show_vouchers': True,
        'require_bctc': True,
        'show_bctc': True,
    },
}

DEFAULT_TT58_TAX_METHOD = 'article5'

_ALIASES = {
    '1': 'article5', 'pp1': 'article5', 'method1': 'article5',
    'article_5': 'article5', 'dieu5': 'article5', 'điều5': 'article5',
    '2': 'article6', 'pp2': 'article6', 'method2': 'article6',
    'article_6': 'article6', 'dieu6': 'article6',
    '3': 'article7', 'pp3': 'article7', 'method3': 'article7',
    'article_7': 'article7', 'dieu7': 'article7',
    '4': 'article8', 'pp4': 'article8', 'method4': 'article8',
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
