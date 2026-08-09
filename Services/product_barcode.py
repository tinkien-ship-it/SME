"""Chuẩn hóa và tra cứu mã vạch POS — tem NSX hoặc mã nội bộ hệ thống."""
from __future__ import annotations

import re

_INTERNAL_PREFIXES = ('SP', 'VT', 'TP', 'CCDC', 'TSCD', 'DV', 'NVL', 'M')
_GTIN_LENS = (8, 12, 13, 14)


def normalize_scan_code(raw) -> str:
    return str(raw or '').strip()


def _normalize_gtin(digits: str) -> str:
    d = re.sub(r'\D', '', digits or '')
    if len(d) == 14 and d.startswith('0'):
        return d[1:]
    return d


def extract_gtin_from_payload(raw) -> str:
    """Rút GTIN/EAN từ QR (URL, GS1 Digital Link) hoặc chuỗi số thuần."""
    text = normalize_scan_code(raw)
    if not text:
        return ''

    for pat in (
        r'\(01\)(\d{8,14})',
        r'/01/(\d{8,14})',
        r'[?&](?:gtin|ean13|ean|barcode|g)=(\d{8,14})',
    ):
        m = re.search(pat, text, re.I)
        if m:
            return _normalize_gtin(m.group(1))

    compact = re.sub(r'[\s-]+', '', text)
    if re.fullmatch(r'\d{8,14}', compact):
        return compact

    if re.fullmatch(r'[A-Za-z]{1,8}\d{3,}', text):
        return ''

    if '://' in text or len(text) > 18:
        tokens = re.findall(r'(?<!\d)(\d{8,14})(?!\d)', text)
        if not tokens:
            return ''

        def score(t: str):
            pref = {13: 4, 14: 3, 12: 2, 8: 1}.get(len(t), 0)
            body = t[1:] if len(t) == 14 and t.startswith('0') else t
            vn = 2 if body.startswith('893') else 0
            return (pref, vn)

        tokens.sort(key=score, reverse=True)
        return _normalize_gtin(tokens[0])
    return ''


def canonical_scan_code(raw) -> str:
    """Mã lưu trên SP: ưu tiên GTIN rút từ QR, không lưu cả URL."""
    text = normalize_scan_code(raw)
    extracted = extract_gtin_from_payload(text)
    if extracted:
        return extracted
    return text[:120] if len(text) > 120 else text


def scan_candidates(raw) -> list[str]:
    """Các biến thể cùng một lần quét (QR URL → GTIN, UPC-A ↔ EAN-13)."""
    code = normalize_scan_code(raw)
    if not code:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(val):
        v = str(val or '').strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)

    add(code)
    add(code.upper())
    extracted = extract_gtin_from_payload(code)
    if extracted:
        add(extracted)
        add(extracted.upper())
        digits = extracted
    elif re.fullmatch(r'[A-Za-z]{1,8}\d{3,}', code):
        digits = ''
    else:
        digits = re.sub(r'\D', '', code)
        if '://' in code or len(digits) > 14:
            digits = ''
    if digits:
        add(digits)
        if len(digits) == 12:
            add('0' + digits)
        if len(digits) == 13 and digits.startswith('0'):
            add(digits[1:])
        if len(digits) == 14 and digits.startswith('0'):
            add(digits[1:])
    return out


def is_internal_barcode(code, product_code=None) -> bool:
    c = normalize_scan_code(code).upper()
    if not c:
        return False
    pc = normalize_scan_code(product_code).upper()
    if pc and c in (pc, pc + '01', pc + '02'):
        return True
    for px in _INTERNAL_PREFIXES:
        if not c.startswith(px):
            continue
        rest = c[len(px):]
        if rest.isdigit():
            return True
        if len(rest) >= 3 and rest[:-2].isdigit() and rest[-2:] in ('01', '02'):
            return True
    return False


def barcode_owned_by_other(conn, raw, exclude_id=None):
    """SP khác đã dùng mã này ở barcode / barcode1. Không khớp product_code."""
    candidates = scan_candidates(raw)
    if not candidates:
        return None
    ph = ','.join('?' * len(candidates))
    sql = (
        f"SELECT id, name, barcode, barcode1, product_code FROM products "
        f"WHERE barcode IN ({ph}) OR barcode1 IN ({ph})"
    )
    params = list(candidates) + list(candidates)
    if exclude_id:
        sql += " AND id != ?"
        params.append(exclude_id)
    return conn.execute(sql, params).fetchone()


def find_product_by_scan(conn, raw, exclude_id=None):
    """Tìm SP theo barcode / barcode1 / product_code (kèm tồn kho)."""
    candidates = scan_candidates(raw)
    if not candidates:
        return None
    ph = ','.join('?' * len(candidates))
    uppers = [c.upper() for c in candidates]
    sql = f"""
        SELECT p.*, COALESCE(i.quantity, 0) AS quantity
        FROM products p
        LEFT JOIN inventory i ON i.product_id = p.id
        WHERE p.barcode IN ({ph})
           OR p.barcode1 IN ({ph})
           OR UPPER(COALESCE(p.product_code, '')) IN ({ph})
    """
    params = list(candidates) + list(candidates) + uppers
    if exclude_id:
        sql += " AND p.id != ?"
        params.append(exclude_id)
    sql += " LIMIT 1"
    return conn.execute(sql, params).fetchone()


def scan_matches_barcode1(scanned, barcode1) -> bool:
    if not barcode1:
        return False
    b1 = set(scan_candidates(barcode1))
    return any(c in b1 for c in scan_candidates(scanned))
