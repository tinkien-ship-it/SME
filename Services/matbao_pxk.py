# -*- coding: utf-8 -*-
"""Phiếu xuất kho điện tử Mắt Bão (TT78 mẫu số 6).

Hai loại chứng từ (ký hiệu hóa đơn — ký tự thứ 4):
  - N: phiếu xuất kho kiêm vận chuyển nội bộ điện tử
  - B: phiếu xuất kho hàng gửi bán đại lý điện tử

API: cùng endpoint create-invoice / sign-invoice như HĐ bán.
Tham chiếu: tài liệu Mat Bao (KHMSHDon=6, LDDNBo / HDKTSo+HDKTNgay, PTVChuyen, …).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)

PXK_KHMS = '6'
PxkKind = Literal['internal', 'agency']

REQUIRED_INTERNAL = ('LDDNBo', 'PTVChuyen')
REQUIRED_AGENCY = ('HDKTSo', 'HDKTNgay', 'PTVChuyen')


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _s(v, maxlen: int | None = None) -> str:
    out = str(v or '').strip()
    if maxlen is not None and len(out) > maxlen:
        return out[:maxlen]
    return out


def _date_iso(v) -> str:
    raw = str(v or '').strip()
    if not raw:
        return datetime.now().strftime('%Y-%m-%dT00:00:00')
    if 'T' in raw:
        return raw[:19]
    return f'{raw[:10]}T00:00:00'


def series_kind_letter(kind: PxkKind) -> str:
    return 'N' if kind == 'internal' else 'B'


def validate_pxk_series(series: str, kind: PxkKind) -> str:
    """Ký hiệu 6 ký tự; ký tự thứ 4 phải là N (nội bộ) hoặc B (đại lý)."""
    s = _s(series)
    if len(s) < 4:
        raise ValueError(
            f'Ký hiệu PXK không hợp lệ «{s}». Cần dạng C26{series_kind_letter(kind)}YY '
            f'(đã đăng ký trên Mắt Bão).'
        )
    expected = series_kind_letter(kind)
    actual = s[3].upper()
    if actual != expected:
        raise ValueError(
            f'Ký hiệu PXK «{s}»: ký tự thứ 4 phải là «{expected}» '
            f'({"vận chuyển nội bộ" if kind == "internal" else "gửi bán đại lý"}), '
            f'hiện là «{actual}».'
        )
    return s


def ensure_pxk_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_pxk_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pxk_kind TEXT NOT NULL,
            source_type TEXT,
            source_id INTEGER,
            document_ref TEXT,
            invoice_no TEXT,
            invoice_id TEXT,
            series TEXT,
            pdf_url TEXT,
            xml_url TEXT,
            status TEXT,
            loai_hdon INTEGER DEFAULT 1,
            payload_json TEXT,
            error_message TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sme_pxk_source
        ON sme_pxk_documents(source_type, source_id)
        """
    )
    if commit:
        from db_utils import sqlite_commit
        sqlite_commit(conn, label='sme_pxk_schema')


def resolve_pxk_series(config: dict, kind: PxkKind) -> str:
    """Lấy ký hiệu từ invoice_settings (cột riêng hoặc suy ra từ HĐ bán)."""
    if kind == 'internal':
        raw = (
            config.get('pxk_internal_series')
            or config.get('invoice_series_pxk_n')
            or ''
        )
    else:
        raw = (
            config.get('pxk_agency_series')
            or config.get('invoice_series_pxk_b')
            or ''
        )
    raw = _s(raw)
    if raw:
        return validate_pxk_series(raw, kind)

    # Suy ra từ ký hiệu HĐ bán: thay ký tự thứ 4
    base = _s(config.get('invoice_series') or '')
    if len(base) >= 4:
        letter = series_kind_letter(kind)
        derived = base[:3] + letter + base[4:]
        logger.info('PXK series derived from invoice_series: %s → %s', base, derived)
        return validate_pxk_series(derived, kind)

    raise ValueError(
        f'Chưa cấu hình ký hiệu PXK {"nội bộ (N)" if kind == "internal" else "đại lý (B)"} '
        'trong Settings → HĐĐT. Đăng ký mẫu số 6 trên Mắt Bão rồi nhập ký hiệu '
        f'(vd C26{series_kind_letter(kind)}YY).'
    )


def build_pxk_line_items(items: list[dict]) -> tuple[list[dict], float, float]:
    """Dòng hàng PXK — thuế thường 0% (chứng từ kho, không phải HĐ GTGT)."""
    rows: list[dict] = []
    total_qty = 0.0
    total_amt = 0.0
    for i, item in enumerate(items):
        qty = _f(item.get('quantity') or item.get('qty'))
        price = _f(item.get('price') or item.get('unit_cost') or item.get('unit_price'))
        if qty <= 0:
            continue
        amount = round(qty * price, 2)
        tax_pct = _f(item.get('tax_pct'))
        tax_val = round(amount * tax_pct / 100.0, 2) if tax_pct > 0 else 0.0
        rows.append({
            'TChat': 1,
            'STT': i + 1,
            'MHHDVu': _s(item.get('product_code') or item.get('code'), 50),
            'THHDVu': _s(item.get('name') or item.get('product_name') or 'Hàng hóa', 500),
            'DVTinh': _s(item.get('unit') or 'Cái', 50),
            'SLuong': qty,
            'DGia': price,
            'ThTienChuaCK': amount,
            'TLCKhau': 0,
            'STCKhau': 0,
            'ThTien': amount,
            'TSuat': int(tax_pct) if tax_pct else 0,
            'TThue': tax_val,
            'TgTien': round(amount + tax_val, 2),
        })
        total_qty += qty
        total_amt += amount
    if not rows:
        raise ValueError('PXK cần ít nhất một dòng hàng có số lượng > 0')
    return rows, round(total_qty, 6), round(total_amt, 2)


def build_pxk_payload(
    *,
    kind: PxkKind,
    series: str,
    header: dict[str, Any],
    items: list[dict],
    loai_hdon: int = 1,
) -> dict[str, Any]:
    """Tạo body create-invoice cho PXK theo tài liệu Mắt Bão."""
    series = validate_pxk_series(series, kind)
    dsh, tong_sl, total_amt = build_pxk_line_items(items)

    if kind == 'internal':
        for key in REQUIRED_INTERNAL:
            if not _s(header.get(key)):
                raise ValueError(
                    f'Thiếu trường bắt buộc PXK vận chuyển nội bộ: {key} '
                    f'({"Lệnh điều động nội bộ" if key == "LDDNBo" else "Phương tiện vận chuyển"})'
                )
    else:
        for key in REQUIRED_AGENCY:
            if not _s(header.get(key)):
                label = {
                    'HDKTSo': 'Số hợp đồng kinh tế đại lý',
                    'HDKTNgay': 'Ngày hợp đồng kinh tế',
                    'PTVChuyen': 'Phương tiện vận chuyển',
                }.get(key, key)
                raise ValueError(f'Thiếu trường bắt buộc PXK gửi đại lý: {label} ({key})')

    nlap = _date_iso(header.get('NLap') or header.get('date') or header.get('NgayXuat'))
    payload: dict[str, Any] = {
        'KHMSHDon': PXK_KHMS,
        'KHHDon': series,
        'LoaiHDon': int(loai_hdon),
        'TCHDon': 0,
        'NLap': nlap,
        'DVTTe': 704,
        'TGia': 1.0,
        'HTTToan': _s(header.get('HTTToan') or 'TM/CK', 50) or 'TM/CK',
        'NMua_HVTNMHang': _s(header.get('NMua_HVTNMHang') or header.get('receiver_name'), 100),
        'NMua_Ten': _s(header.get('NMua_Ten') or header.get('receiver_org'), 400),
        'NMua_MST': _s(header.get('NMua_MST') or header.get('tax_code'), 14),
        'NMua_DChi': _s(header.get('NMua_DChi') or header.get('dest_address'), 400),
        'NMua_SDThoai': _s(header.get('NMua_SDThoai') or header.get('phone'), 20),
        'NMua_DCTDTu': _s(header.get('NMua_DCTDTu') or header.get('email'), 50),
        'PTVChuyen': _s(header.get('PTVChuyen'), 50),
        'TNVChuyen': _s(header.get('TNVChuyen'), 100),
        'HDSo': _s(header.get('HDSo'), 255),
        'XuatKhoTai': _s(header.get('XuatKhoTai') or header.get('from_warehouse'), 400),
        'NgayXuat': _date_iso(header.get('NgayXuat') or header.get('date')),
        'NgayNhap': _date_iso(header.get('NgayNhap') or header.get('NgayXuat') or header.get('date')),
        'TongSoLuong': tong_sl,
        'DSHHDVu': dsh,
        'TgThTien': total_amt,
        'TgTThue': 0,
        'TgTTTBSo': total_amt,
        'TgTTTBChu': '',
        'KTraMTChieuTrung': int(header.get('KTraMTChieuTrung') or 1),
        'MTChieu': _s(header.get('MTChieu') or header.get('document_ref'), 50),
    }

    if kind == 'internal':
        payload['LDDNBo'] = _s(header.get('LDDNBo'), 255)
        payload['Cua'] = _s(header.get('Cua'))
        payload['VeViec'] = _s(header.get('VeViec'))
    else:
        payload['HDKTSo'] = _s(header.get('HDKTSo'), 255)
        payload['HDKTNgay'] = _date_iso(header.get('HDKTNgay'))

    # Bỏ field rỗng (trừ số)
    cleaned = {}
    for k, v in payload.items():
        if v is None:
            continue
        if isinstance(v, str) and not v and k not in ('TgTTTBChu',):
            continue
        cleaned[k] = v
    return cleaned


def persist_pxk_result(
    conn: sqlite3.Connection,
    *,
    kind: PxkKind,
    source_type: str,
    source_id: int | None,
    document_ref: str,
    series: str,
    result: dict,
    payload: dict,
    created_by: str | None = None,
    loai_hdon: int = 1,
) -> int:
    ensure_pxk_schema(conn, commit=False)
    ok = bool(result.get('success'))
    cur = conn.execute(
        """
        INSERT INTO sme_pxk_documents (
            pxk_kind, source_type, source_id, document_ref,
            invoice_no, invoice_id, series, pdf_url, xml_url,
            status, loai_hdon, payload_json, error_message, created_by
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            kind,
            source_type,
            source_id,
            document_ref,
            result.get('invoice_no') or '',
            result.get('invoice_id') or '',
            series,
            result.get('pdf_url') or '',
            result.get('xml_url') or '',
            'issued' if ok and not result.get('is_draft') else ('draft' if ok else 'error'),
            loai_hdon,
            json.dumps(payload, ensure_ascii=False)[:8000],
            '' if ok else _s(result.get('error'), 500),
            created_by,
        ),
    )
    return int(cur.lastrowid or 0)


def issue_pxk_via_provider(
    provider,
    *,
    kind: PxkKind,
    header: dict[str, Any],
    items: list[dict],
    loai_hdon: int = 1,
    series: str | None = None,
) -> dict[str, Any]:
    """Gọi MatbaoProvider.issue_pxk (hoặc tương thích)."""
    config = getattr(provider, 'config', None) or {}
    ser = validate_pxk_series(series or resolve_pxk_series(config, kind), kind)
    payload = build_pxk_payload(
        kind=kind, series=ser, header=header, items=items, loai_hdon=loai_hdon,
    )
    if hasattr(provider, 'issue_pxk'):
        result = provider.issue_pxk(payload)
    else:
        raise ValueError('Nhà cung cấp HĐĐT hiện tại không hỗ trợ phát hành PXK điện tử (cần Mắt Bão).')
    result = dict(result or {})
    result['pxk_kind'] = kind
    result['series'] = ser
    result['payload'] = payload
    return result


def _get_matbao_provider():
    from flask import current_app
    from Services.einvoice_factory import create_einvoice_service
    from Services.invoice_config import get_active_invoice_config

    cfg = get_active_invoice_config() or {}
    if str(cfg.get('provider_name') or '').lower() != 'matbao':
        raise ValueError('PXK điện tử hiện chỉ hỗ trợ nhà cung cấp Mắt Bão.')
    matbao_cls = current_app.config.get('MatbaoProvider')
    if matbao_cls is None:
        raise ValueError('MatbaoProvider chưa được đăng ký trên ứng dụng.')
    svc = create_einvoice_service(cfg, matbao_cls=matbao_cls)
    # Wrapper có issue_pxk → dùng trực tiếp
    if hasattr(svc, 'issue_pxk'):
        return svc, cfg
    inner = getattr(svc, '_inner', None)
    if inner and hasattr(inner, 'issue_pxk'):
        if not getattr(inner, 'config', None):
            inner.config = cfg
        return inner, cfg
    raise ValueError('Không khởi tạo được MatbaoProvider.issue_pxk — kiểm tra cấu hình HĐĐT.')


def issue_and_persist_pxk(
    conn: sqlite3.Connection,
    *,
    kind: PxkKind,
    header: dict[str, Any],
    items: list[dict],
    source_type: str,
    source_id: int | None,
    loai_hdon: int = 1,
    created_by: str | None = None,
) -> dict[str, Any]:
    provider, cfg = _get_matbao_provider()
    result = issue_pxk_via_provider(
        provider, kind=kind, header=header, items=items, loai_hdon=loai_hdon,
    )
    doc_ref = str(header.get('MTChieu') or header.get('document_ref') or '')
    pxk_id = persist_pxk_result(
        conn,
        kind=kind,
        source_type=source_type,
        source_id=source_id,
        document_ref=doc_ref,
        series=result.get('series') or '',
        result=result,
        payload=result.get('payload') or {},
        created_by=created_by,
        loai_hdon=loai_hdon,
    )
    result['pxk_document_id'] = pxk_id
    result['config_series_hint'] = {
        'internal': cfg.get('pxk_internal_series'),
        'agency': cfg.get('pxk_agency_series'),
    }
    return result


def build_internal_header_from_sale(sale: dict, extra: dict | None = None) -> dict[str, Any]:
    """Map đơn XK / điều chuyển → header PXK nội bộ."""
    extra = extra or {}
    sale_no = str(sale.get('sale_no') or '').strip()
    return {
        'document_ref': sale_no,
        'MTChieu': sale_no,
        'date': str(sale.get('date') or '')[:10],
        'NgayXuat': str(sale.get('date') or '')[:10],
        'NgayNhap': str(extra.get('NgayNhap') or sale.get('date') or '')[:10],
        'LDDNBo': extra.get('LDDNBo') or sale.get('internal_transfer_doc_no') or f'LĐĐ-{sale_no}',
        'VeViec': extra.get('VeViec') or f'Xuất kho vận chuyển nội bộ {sale_no}',
        'Cua': extra.get('Cua') or sale.get('company_name') or '',
        'PTVChuyen': extra.get('PTVChuyen') or sale.get('transport_vehicle') or 'Xe tải',
        'TNVChuyen': extra.get('TNVChuyen') or sale.get('transporter_name') or '',
        'HDSo': extra.get('HDSo') or '',
        'XuatKhoTai': extra.get('XuatKhoTai') or sale.get('warehouse_code') or '',
        'NMua_DChi': extra.get('NMua_DChi') or extra.get('dest_address') or sale.get('address') or '',
        'receiver_name': extra.get('receiver_name') or sale.get('customer_name') or '',
        'receiver_org': extra.get('receiver_org') or sale.get('company_name') or '',
        'tax_code': extra.get('tax_code') or '',
    }


def build_agency_header_from_delivery(delivery: dict, extra: dict | None = None) -> dict[str, Any]:
    """Map phiếu gửi đại lý → header PXK-B."""
    extra = extra or {}
    doc = str(
        delivery.get('delivery_no')
        or delivery.get('doc_no')
        or delivery.get('id')
        or ''
    ).strip()
    return {
        'document_ref': doc,
        'MTChieu': doc,
        'date': str(delivery.get('delivery_date') or '')[:10],
        'NgayXuat': str(delivery.get('delivery_date') or '')[:10],
        'NgayNhap': str(extra.get('NgayNhap') or delivery.get('delivery_date') or '')[:10],
        'HDKTSo': extra.get('HDKTSo') or delivery.get('contract_no') or delivery.get('hdkt_so') or '',
        'HDKTNgay': extra.get('HDKTNgay') or delivery.get('contract_date') or delivery.get('hdkt_ngay') or delivery.get('delivery_date') or '',
        'PTVChuyen': extra.get('PTVChuyen') or delivery.get('transport_vehicle') or 'Xe tải',
        'TNVChuyen': extra.get('TNVChuyen') or delivery.get('transporter_name') or '',
        'HDSo': extra.get('HDSo') or '',
        'XuatKhoTai': extra.get('XuatKhoTai') or delivery.get('warehouse_code') or '',
        'NMua_DChi': extra.get('NMua_DChi') or delivery.get('agent_address') or '',
        'receiver_name': delivery.get('agent_name') or '',
        'receiver_org': delivery.get('agent_name') or '',
        'tax_code': delivery.get('agent_tax_code') or '',
        'phone': delivery.get('agent_phone') or '',
        'email': delivery.get('agent_email') or '',
    }


def sale_items_as_pxk_lines(conn: sqlite3.Connection, sale_id: int) -> list[dict]:
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sale_items)').fetchall()}
    cost_expr = 'COALESCE(si.cost_price, 0)' if 'cost_price' in cols else '0'
    price_expr = 'COALESCE(si.price, 0)' if 'price' in cols else '0'
    unit_expr = 'si.unit' if 'unit' in cols else "''"
    rows = conn.execute(
        f"""
        SELECT si.quantity, {price_expr} AS price, {unit_expr} AS unit,
               p.name AS product_name, p.product_code, p.unit AS p_unit,
               {cost_expr} AS unit_cost
        FROM sale_items si
        LEFT JOIN products p ON p.id = si.product_id
        WHERE si.sale_id = ?
        """,
        (int(sale_id),),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            'quantity': d.get('quantity'),
            'price': d.get('unit_cost') or d.get('price') or 0,
            'unit': d.get('unit') or d.get('p_unit') or 'Cái',
            'name': d.get('product_name') or 'Hàng hóa',
            'product_code': d.get('product_code') or '',
            'tax_pct': 0,
        })
    return out


def delivery_items_as_pxk_lines(conn: sqlite3.Connection, delivery_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT l.quantity, l.unit_cost, p.name AS product_name, p.product_code, p.unit
        FROM sme_agent_delivery_lines l
        LEFT JOIN products p ON p.id = l.product_id
        WHERE l.delivery_id = ?
        """,
        (int(delivery_id),),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            'quantity': d.get('quantity'),
            'price': d.get('unit_cost') or 0,
            'unit': d.get('unit') or 'Cái',
            'name': d.get('product_name') or 'Hàng hóa',
            'product_code': d.get('product_code') or '',
            'tax_pct': 0,
        })
    return out


def maybe_auto_issue_pxk(
    conn: sqlite3.Connection,
    *,
    kind: PxkKind,
    header: dict,
    items: list[dict],
    source_type: str,
    source_id: int,
    created_by: str | None = None,
) -> dict[str, Any] | None:
    """Phát hành PXK nếu Settings bật auto_issue_pxk_*."""
    try:
        from Services.invoice_config import get_active_invoice_config
        cfg = get_active_invoice_config() or {}
        flag = (
            'auto_issue_pxk_internal' if kind == 'internal' else 'auto_issue_pxk_agency'
        )
        if not int(cfg.get(flag) or 0):
            return None
        if str(cfg.get('provider_name') or '').lower() != 'matbao':
            return None
        return issue_and_persist_pxk(
            conn,
            kind=kind,
            header=header,
            items=items,
            source_type=source_type,
            source_id=source_id,
            loai_hdon=1,
            created_by=created_by,
        )
    except Exception as exc:
        logger.warning('auto issue PXK %s failed: %s', kind, exc)
        return {'success': False, 'error': str(exc), 'auto': True}