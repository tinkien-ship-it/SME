"""Chuẩn hóa tên khách lẻ / người tiêu dùng trên hóa đơn điện tử."""
import re

from Services.customer_utils import (
    format_budget_unit_code_for_einvoice,
    format_passport_for_einvoice,
    format_tax_code_for_einvoice,
)

DEFAULT_RETAIL_BUYER_NAME = 'Bán cho người tiêu dùng'

_RETAIL_ALIASES = frozenset({
    '',
    'khách lẻ',
    'khach le',
    'khách lẻ không lấy hóa đơn',
    'khach le khong lay hoa don',
    'bán cho người tiêu dùng',
    'ban cho nguoi tieu dung',
})


def _norm_key(text):
    return (text or '').strip().lower()


def is_retail_buyer_name(name):
    """True nếu là khách lẻ / người tiêu dùng (không lấy HĐ doanh nghiệp)."""
    return _norm_key(name) in _RETAIL_ALIASES


def normalize_retail_buyer_name(name):
    """Trả tên hiển thị mặc định cho khách lẻ trên UI và DB."""
    if is_retail_buyer_name(name):
        return DEFAULT_RETAIL_BUYER_NAME
    return (name or '').strip()


def normalize_buyer_identity_fields(budget_raw, passport_raw):
    """
    Phân tách Mã ĐVQHNS (7 số) và Số hộ chiếu — tránh gửi nhầm sang XML TT78.
    """
    budget_raw = (budget_raw or '').strip()
    passport_raw = (passport_raw or '').strip()

    budget = format_budget_unit_code_for_einvoice(budget_raw)
    passport = format_passport_for_einvoice(passport_raw)

    if budget_raw and not budget:
        if re.search(r'[A-Za-z]', budget_raw):
            if not passport or len(passport) < len(budget_raw):
                passport = budget_raw
        budget = ''

    if passport_raw and not budget:
        maybe_budget = format_budget_unit_code_for_einvoice(passport_raw)
        if maybe_budget and not re.search(r'[A-Za-z]', passport_raw):
            budget = maybe_budget
            passport = ''

    return budget, passport


def enrich_sale_buyer_identity(sale_data, customer_row=None):
    """Chuẩn hóa + bổ sung Mã ĐVQHNS / hộ chiếu từ hồ sơ khách (nếu đơn lưu nhầm)."""
    sale = dict(sale_data or {})
    raw_budget = (sale.get('budget_unit_code') or '').strip()
    raw_passport = (sale.get('passport_no') or '').strip()

    budget, passport = normalize_buyer_identity_fields(raw_budget, raw_passport)
    sale['budget_unit_code'] = budget or None
    sale['passport_no'] = passport or None

    if not customer_row:
        return sale

    cust_budget = format_budget_unit_code_for_einvoice(customer_row.get('budget_unit_code'))
    cust_passport = format_passport_for_einvoice(customer_row.get('passport_no'))

    if raw_budget and raw_budget == cust_passport and cust_budget:
        sale['budget_unit_code'] = cust_budget
        sale['passport_no'] = cust_passport or passport or None
    elif raw_budget == cust_passport and not cust_budget:
        sale['budget_unit_code'] = None
        sale['passport_no'] = cust_passport or None
    elif not budget and cust_budget:
        sale['budget_unit_code'] = cust_budget
    if not (sale.get('passport_no') or '').strip() and cust_passport:
        sale['passport_no'] = cust_passport

    return sale


def extract_buyer_invoice_fields(sale_data):
    """Các trường định danh người mua luôn truyền sang HĐ nếu có giá trị."""
    sd = sale_data or {}
    raw_tax = (
        sd.get('tax_code')
        or sd.get('customer_tax_code')
        or ''
    )
    budget, passport = normalize_buyer_identity_fields(
        sd.get('budget_unit_code'),
        sd.get('passport_no'),
    )
    return {
        'tax_code': format_tax_code_for_einvoice(raw_tax),
        'budget_unit_code': budget,
        'passport_no': passport,
    }


def resolve_vnpt_buyer_fields(sale_data):
    """
    NMua TT78 VNPT:
    - Bán lẻ: unit_name để trống (adapter VNPT gán Ten = buyer_full_name vì portal bắt buộc),
      HVTNMHang = Bán cho người tiêu dùng, DChi để trống
    - Bán cho DN: Ten = tên công ty, DChi = địa chỉ, HVTNMHang = họ tên người mua
    - MST / Mã ĐVQHNS / Số hộ chiếu: luôn điền nếu người bán đã nhập (kể cả bán lẻ)
    """
    sale_data = sale_data or {}
    extras = extract_buyer_invoice_fields(sale_data)
    company = (sale_data.get('company_name') or '').strip()
    address = (sale_data.get('address') or '').strip()
    customer = (sale_data.get('customer_name') or '').strip()

    has_business = (
        (company and not is_retail_buyer_name(company))
        or bool(extras['tax_code'])
        or bool(extras['budget_unit_code'])
    )

    if has_business:
        unit_name = company if company and not is_retail_buyer_name(company) else customer
        if is_retail_buyer_name(unit_name) and company and not is_retail_buyer_name(company):
            unit_name = company
        unit_address = '' if is_retail_buyer_name(address) else address
        if customer and not is_retail_buyer_name(customer):
            buyer_full_name = customer
        elif unit_name and not is_retail_buyer_name(unit_name):
            buyer_full_name = unit_name
        else:
            buyer_full_name = DEFAULT_RETAIL_BUYER_NAME
        if is_retail_buyer_name(unit_name):
            unit_name = ''
        return {
            'unit_name': unit_name,
            'unit_address': unit_address,
            'buyer_full_name': buyer_full_name,
            **extras,
        }

    return {
        'unit_name': '',
        'unit_address': '',
        'buyer_full_name': DEFAULT_RETAIL_BUYER_NAME,
        **extras,
    }
