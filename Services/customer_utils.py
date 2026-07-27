"""Kiểm tra và chuẩn hóa mã số thuế khách hàng."""
import re

VALID_TAX_CODE_LENGTHS = (10, 12, 13)


def normalize_tax_code_digits(value):
    return re.sub(r'\D', '', str(value or '').strip())


def normalize_budget_unit_code_digits(value):
    return re.sub(r'\D', '', str(value or '').strip())


_PASSPORT_GARBAGE = frozenset({
    'pos', 'test', 'na', 'n/a', 'xxx', 'none', 'null', '-', '.',
})


def format_passport_for_einvoice(value):
    """Số hộ chiếu — bỏ giá trị rác (vd. 'pos' do lỗi SQL cũ gán nhầm cột)."""
    text = (value or '').strip()
    if not text:
        return ''
    if text.lower() in _PASSPORT_GARBAGE:
        return ''
    if len(text) <= 3 and not any(ch.isdigit() for ch in text):
        return ''
    return text


def format_budget_unit_code_for_einvoice(value):
    """
    Mã ĐVQHNS (MDVQHNSach) TT78 — đúng 7 chữ số.
    Giá trị không hợp lệ → bỏ qua (không gửi thẻ XML).
    """
    digits = normalize_budget_unit_code_digits(value)
    if len(digits) == 7:
        return digits
    return ''


def budget_unit_code_validation_message(value):
    text = str(value or '').strip()
    if not text:
        return None
    digits = normalize_budget_unit_code_digits(text)
    if len(digits) == 7:
        return None
    return (
        'Mã ĐVQHNS phải đúng 7 chữ số theo TT78 '
        f'(giá trị hiện tại: {text}).'
    )


def format_tax_code_for_einvoice(value):
    """
    Chuẩn hóa MST trước khi gửi HĐĐT.
    13 số (MST + mã chi nhánh) → dạng XXXXXXXXXX-XXX theo TT78.
    """
    digits = normalize_tax_code_digits(value)
    if not digits:
        return ''
    if len(digits) == 13:
        return f'{digits[:10]}-{digits[10:]}'
    return digits


def is_valid_tax_code(value):
    """MST hợp lệ: rỗng (khách lẻ) hoặc đúng 10 / 12 / 13 chữ số."""
    digits = normalize_tax_code_digits(value)
    if not digits:
        return True
    return len(digits) in VALID_TAX_CODE_LENGTHS


def tax_code_validation_message(value):
    digits = normalize_tax_code_digits(value)
    if not digits:
        return None
    if len(digits) in VALID_TAX_CODE_LENGTHS:
        return None
    return (
        f'Mã số thuế phải có 10, 12 hoặc 13 chữ số (hiện có {len(digits)} số). '
        'Vui lòng kiểm tra lại.'
    )
