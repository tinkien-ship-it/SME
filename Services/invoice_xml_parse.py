# -*- coding: utf-8 -*-
"""Parse XML hóa đơn điện tử (TT78) — dùng chung nhập kho / HĐ đầu vào."""
from __future__ import annotations

import xml.etree.ElementTree as ET


def decode_xml_bytes(raw) -> str:
    """Giải mã file XML HĐĐT (BOM / UTF-8 / UTF-16 / Windows-1258)."""
    if raw is None:
        return ''
    if isinstance(raw, str):
        return raw.strip().encode('utf-8').decode('utf-8-sig', errors='replace').strip()
    if not isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw)
    if not raw:
        return ''
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return raw.decode('utf-16', errors='replace').strip()
    for enc in ('utf-8-sig', 'utf-8', 'cp1258', 'latin-1'):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace').strip()


def strip_xml_namespaces(root):
    """Bỏ namespace để .//NDHDon / .//HHDVu tìm được trên XML ký số."""
    for el in root.iter():
        if isinstance(el.tag, str) and '}' in el.tag:
            el.tag = el.tag.rsplit('}', 1)[-1]
        attrib = getattr(el, 'attrib', None)
        if attrib:
            for key in list(attrib.keys()):
                if isinstance(key, str) and '}' in key:
                    attrib[key.rsplit('}', 1)[-1]] = attrib.pop(key)
    return root


def find_invoice_payload_node(root):
    """
    Node chứa nội dung HĐ (ưu tiên NDHDon — có NBan/HHDVu; fallback DLHDon; cuối cùng root).
    Dùng `is not None` (không dùng `or` với Element — Python 3.14+).
    """
    ndhdon = root.find('.//NDHDon')
    if ndhdon is not None:
        return ndhdon
    dlhdon = root.find('.//DLHDon')
    if dlhdon is not None:
        return dlhdon
    # Một số file chỉ có HDon / Invoice root kèm HHDVu
    if root.find('.//HHDVu') is not None or root.find('.//NBan') is not None:
        return root
    return None


def parse_invoice_xml_root(xml_content):
    """Decode + parse + strip namespace → Element root."""
    text = decode_xml_bytes(xml_content)
    root = ET.fromstring(text)
    strip_xml_namespaces(root)
    return root
