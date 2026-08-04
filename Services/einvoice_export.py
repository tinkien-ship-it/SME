"""Payload HĐĐT cho bán hàng xuất khẩu (ngoại tệ, VAT 0%, ghi chú TKHQ)."""
from __future__ import annotations

from typing import Any

# ISO 4217 numeric — Mắt Bão dùng mã số; MISA/VNPT dùng mã chữ.
CURRENCY_NUMERIC = {
    'VND': 704,
    'USD': 840,
    'EUR': 978,
    'JPY': 392,
    'CNY': 156,
    'GBP': 826,
    'AUD': 36,
    'SGD': 702,
    'KRW': 410,
    'THB': 764,
}


def is_export_sale(sale: dict | None) -> bool:
    if not sale:
        return False
    if str(sale.get('sale_type') or '').strip().upper() == 'EXPORT':
        return True
    currency = str(sale.get('currency') or 'VND').strip().upper()
    return bool(currency and currency != 'VND' and sale.get('amount_fc') is not None)


def sale_has_draft_invoice(sale: dict | None) -> bool:
    if not sale:
        return False
    status = str(sale.get('invoice_status') or '').lower()
    inv_no = str(sale.get('invoice_number') or '').strip()
    return status == 'draft' or (inv_no == '0' and bool(sale.get('invoice_id')))


def sale_has_official_invoice(sale: dict | None) -> bool:
    if not sale or sale_has_draft_invoice(sale):
        return False
    inv_no = str(sale.get('invoice_number') or '').strip()
    status = str(sale.get('invoice_status') or '').lower()
    return bool(inv_no) and inv_no not in ('0', '---', 'None', 'null') and status == 'issued'


def resolve_invoice_currency(sale: dict | None) -> dict[str, Any]:
    """Trả currency ISO, mã số, tỷ giá cho payload HĐĐT."""
    sale = sale or {}
    if not is_export_sale(sale):
        return {
            'is_export': False,
            'currency': 'VND',
            'currency_numeric': 704,
            'exchange_rate': 1.0,
        }
    code = str(sale.get('currency') or 'USD').strip().upper() or 'USD'
    try:
        rate = float(sale.get('exchange_rate') or 1)
    except (TypeError, ValueError):
        rate = 1.0
    if rate <= 0:
        rate = 1.0
    return {
        'is_export': True,
        'currency': code,
        'currency_numeric': CURRENCY_NUMERIC.get(code, 840),
        'exchange_rate': rate,
    }


def build_export_invoice_notes(sale: dict | None) -> str:
    sale = sale or {}
    if not is_export_sale(sale):
        return str(sale.get('note') or '').strip()

    parts: list[str] = []
    note = str(sale.get('note') or '').strip()
    if note:
        parts.append(note)

    contract = str(
        sale.get('export_contract_no') or sale.get('contract_no') or ''
    ).strip()
    if contract:
        parts.append(f'Xuất khẩu theo Hợp đồng số: {contract}.')

    customs = str(sale.get('customs_decl_no') or '').strip()
    risk = str(sale.get('risk_transfer_date') or sale.get('date') or '')[:10]
    if customs:
        line = f'Tờ khai Hải quan số: {customs}'
        if risk:
            line += f' ngày {risk}'
        parts.append(line + '.')

    bl = str(sale.get('bl_no') or '').strip()
    if bl:
        parts.append(f'Số B/L: {bl}.')

    incoterms = str(sale.get('incoterms') or '').strip()
    if incoterms:
        parts.append(f'Incoterms: {incoterms}.')

    cur = resolve_invoice_currency(sale)
    try:
        amount_fc = float(sale.get('amount_fc') or 0)
    except (TypeError, ValueError):
        amount_fc = 0.0
    rate = cur['exchange_rate']
    if amount_fc > 0 and rate > 0:
        vnd = int(round(amount_fc * rate))
        vnd_txt = f'{vnd:,}'.replace(',', '.')
        fc_txt = f'{amount_fc:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
        parts.append(
            f'Quy đổi doanh thu tính thuế (VND): {vnd_txt} VND '
            f'({fc_txt} {cur["currency"]} x tỷ giá {rate:g} VND/{cur["currency"]}).'
        )

    return ' '.join(parts).strip()


def validate_export_for_einvoice(sale: dict | None) -> str | None:
    """Trả thông báo lỗi nếu chưa đủ điều kiện xuất HĐ XK; None nếu OK."""
    if not is_export_sale(sale):
        return None
    status = str((sale or {}).get('export_status') or '').strip().lower()
    # Phiếu cũ không có export_status nhưng đã có TKHQ vẫn cho xuất
    if status == 'shipped':
        return (
            'Chưa thông quan — chỉ xuất HĐĐT sau khi xác nhận thông quan '
            '(TKHQ + B/L; bút toán Nợ 632/Có 157 và Nợ 131/Có 511).'
        )
    customs = str((sale or {}).get('customs_decl_no') or '').strip()
    if not customs:
        return (
            'Xuất hóa đơn điện tử hàng xuất khẩu bắt buộc có số tờ khai hải quan (TKHQ).'
        )
    bl = str((sale or {}).get('bl_no') or '').strip()
    if not bl:
        return 'Xuất HĐĐT XK cần số vận đơn Bill of Lading (B/L) sau thông quan.'
    return None


def prepare_invoice_items_for_sale(sale: dict | None, items: list[dict] | None) -> list[dict]:
    """XK: ép VAT 0% trên từng dòng (đơn giá đã là ngoại tệ trên sale_items)."""
    rows = [dict(it) for it in (items or [])]
    if not is_export_sale(sale):
        return rows
    for row in rows:
        row['tax_pct'] = 0
    return rows


def enrich_sale_for_einvoice(sale: dict | None) -> dict:
    """Gắn metadata tiền tệ / ghi chú để adapter đọc khi phát hành / thay thế."""
    out = dict(sale or {})
    cur = resolve_invoice_currency(out)
    out['_einvoice_is_export'] = cur['is_export']
    out['_einvoice_currency'] = cur['currency']
    out['_einvoice_currency_numeric'] = cur['currency_numeric']
    out['_einvoice_exchange_rate'] = cur['exchange_rate']
    notes = build_export_invoice_notes(out)
    out['_einvoice_notes'] = notes
    if cur['is_export']:
        # Người mua nước ngoài: không ép MST VN
        tax = str(out.get('tax_code') or '').strip()
        if tax in ('-', '—', 'N/A', 'n/a'):
            out['tax_code'] = ''
        pay = str(out.get('payment_method') or '').strip()
        if not pay or pay in ('111', '112', '131'):
            out['payment_method'] = 'CK'
            out['_einvoice_payment_method'] = 'CK (Bank Transfer)'
        else:
            out['_einvoice_payment_method'] = pay
    else:
        out['_einvoice_payment_method'] = str(
            out.get('payment_method') or 'TM/CK'
        )
    return out


def apply_currency_to_matbao_payload(payload: dict, sale_data: dict | None) -> dict:
    sale = enrich_sale_for_einvoice(sale_data) if sale_data else {}
    payload = dict(payload)
    payload['DVTTe'] = sale.get('_einvoice_currency_numeric', 704)
    payload['TGia'] = float(sale.get('_einvoice_exchange_rate') or 1)
    notes = sale.get('_einvoice_notes') or ''
    if notes:
        payload['GChu'] = notes
    pay = sale.get('_einvoice_payment_method')
    if pay:
        payload['HTTToan'] = pay
    return payload


def invoice_currency_fields(sale_data: dict | None) -> tuple[str, float]:
    """(ISO currency, exchange rate) cho MISA / VNPT / Viettel."""
    sale = enrich_sale_for_einvoice(sale_data) if sale_data else {}
    return (
        str(sale.get('_einvoice_currency') or 'VND'),
        float(sale.get('_einvoice_exchange_rate') or 1),
    )
