"""Chuẩn hóa phương thức thanh toán dùng chung cho POS / accounting / reconciler."""
from __future__ import annotations
import unicodedata

def _fold(value: object) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return " ".join(text.split())

_ALIASES = {
    "111": "111", "cash": "111", "tien mat": "111", "tm": "111",
    "112": "112", "bank": "112", "bank transfer": "112", "transfer": "112",
    "chuyen khoan": "112", "ck": "112", "vietqr": "112", "qr": "112",
    "131": "131", "credit": "131", "cong no": "131", "ghi no": "131", "phai thu": "131",
}

def normalize_sale_payment_method(value: object, *, default: str = "111") -> str:
    if value is None or str(value).strip() == "":
        return default
    code = _ALIASES.get(_fold(value))
    if not code:
        raise ValueError(f"Phương thức thanh toán bán hàng không hỗ trợ: {value}")
    return code

def payment_method_label(code: object) -> str:
    return {"111":"Tiền mặt","112":"Chuyển khoản","131":"Công nợ"}[normalize_sale_payment_method(code)]
