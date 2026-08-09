"""Chuẩn hóa và tra cứu mã vạch POS — tem NSX hoặc mã nội bộ hệ thống."""
from __future__ import annotations

import re

_INTERNAL_PREFIXES = ('SP', 'VT', 'TP', 'CCDC', 'TSCD', 'DV', 'NVL', 'M')


def normalize_scan_code(raw) -> str:
    return str(raw or '').strip()


def scan_candidates(raw) -> list[str]:
    """Các biến thể cùng một lần quét (UPC-A 12 số ↔ EAN-13 thêm 0)."""
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
    digits = re.sub(r'\D', '', code)
    if digits:
        add(digits)
        if len(digits) == 12:
            add('0' + digits)
        if len(digits) == 13 and digits.startswith('0'):
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
