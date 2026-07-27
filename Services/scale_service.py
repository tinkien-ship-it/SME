"""Đọc mã vạch cân / cấu hình cân điện tử cho POS."""
import re

from db_utils import get_db_connection
from helpers import get_setting

WEIGHT_UNIT_ALIASES = {'kg', 'kilogram', 'kilograms', 'g', 'gram', 'grams', 'gr'}


def get_scale_config():
    return {
        'enabled': get_setting('scale_enabled', '0') == '1',
        'protocol': (get_setting('scale_protocol', 'generic') or 'generic').strip().lower(),
        'auto_add': get_setting('scale_auto_add', '0') == '1',
        'stable_reads': int(get_setting('scale_stable_reads', '3') or 3),
        'decimal_places': int(get_setting('scale_decimal_places', '3') or 3),
        'barcode_prefix': (get_setting('scale_barcode_prefix', '2') or '2').strip(),
    }


def save_scale_settings(data):
    mapping = {
        'scale_enabled': '1' if str(data.get('scale_enabled', '0')) in ('1', 'true', True) else '0',
        'scale_protocol': str(data.get('scale_protocol', 'generic') or 'generic').strip().lower(),
        'scale_auto_add': '1' if str(data.get('scale_auto_add', '0')) in ('1', 'true', True) else '0',
        'scale_stable_reads': str(int(data.get('scale_stable_reads', 3) or 3)),
        'scale_decimal_places': str(int(data.get('scale_decimal_places', 3) or 3)),
        'scale_barcode_prefix': str(data.get('scale_barcode_prefix', '2') or '2').strip()[:2],
    }
    conn = get_db_connection()
    try:
        for key, val in mapping.items():
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val))
        conn.commit()
    finally:
        conn.close()
    return mapping


def _digits_only(code):
    return re.sub(r'\D', '', str(code or ''))


def parse_weight_barcode(barcode, prefix='2'):
    """
    Giải mã mã vạch cân EAN-13 (prefix 2x).
    Cấu trúc phổ biến VN: 2 + PLU(5) + khối lượng gram(5) + checksum(1)
    """
    code = _digits_only(barcode)
    if len(code) != 13:
        return None
    if prefix and not code.startswith(str(prefix)[0]):
        return None
    if code[0] != '2':
        return None

    plu = code[1:6]
    weight_raw = code[6:11]
    try:
        grams = int(weight_raw)
    except ValueError:
        return None
    if grams <= 0:
        return None

    weight_kg = round(grams / 1000.0, 4)
    return {
        'barcode': code,
        'plu': plu,
        'weight_kg': weight_kg,
        'weight_grams': grams,
    }


def parse_scale_serial_line(line, protocol='generic'):
    """Trích khối lượng (kg) từ chuỗi cân gửi qua cổng Serial."""
    if not line:
        return None
    text = str(line).strip()
    protocol = (protocol or 'generic').lower()

    if protocol == 'cas':
        m = re.search(r'(?:W|T)?\s*([+-]?\d+[.,]?\d*)\s*(kg|g)?', text, re.I)
    elif protocol == 'mettler':
        m = re.search(r'(?:ST,GS,?\s*)?([+-]?\d+[.,]?\d*)\s*(kg|g)?', text, re.I)
    else:
        m = re.search(r'([+-]?\d+[.,]?\d*)\s*(kg|kilogram|g|gram|gr)?', text, re.I)

    if not m:
        return None

    num_str = m.group(1).replace(',', '.')
    try:
        val = float(num_str)
    except ValueError:
        return None

    unit = (m.group(2) or 'kg').lower()
    if unit in ('g', 'gram', 'grams', 'gr'):
        val = val / 1000.0
    if val <= 0:
        return None
    return round(val, 4)


def lookup_weight_product(conn, plu):
    """Tìm sản phẩm bán theo cân theo PLU hoặc mã vạch."""
    cursor = conn.cursor()
    plu = str(plu or '').strip()
    if not plu:
        return None

    row = cursor.execute("""
        SELECT p.id, p.name, p.unit, p.base_price, p.price, p.unit_ratio, p.unit1,
               p.barcode, p.barcode1, p.sell_by_weight, p.weight_plu,
               COALESCE(i.quantity, 0) AS stock_qty
        FROM products p
        LEFT JOIN inventory i ON p.id = i.product_id
        WHERE p.weight_plu = ? OR p.weight_plu = ?
        LIMIT 1
    """, (plu, plu.lstrip('0'))).fetchone()

    if row:
        return dict(row)

    row = cursor.execute("""
        SELECT p.id, p.name, p.unit, p.base_price, p.price, p.unit_ratio, p.unit1,
               p.barcode, p.barcode1, p.sell_by_weight, p.weight_plu,
               COALESCE(i.quantity, 0) AS stock_qty
        FROM products p
        LEFT JOIN inventory i ON p.id = i.product_id
        WHERE p.sell_by_weight = 1 AND (
            p.barcode LIKE ? OR p.product_code LIKE ?
        )
        LIMIT 1
    """, (f'%{plu}%', f'%{plu}%')).fetchone()

    if row:
        return dict(row)
    return None


def build_weight_cart_item(product, weight_kg):
    weight_kg = round(float(weight_kg or 0), 4)
    if weight_kg <= 0:
        return None
    price_per_unit = float(product.get('base_price') or 0)
    unit = product.get('unit') or 'kg'
    stock = float(product.get('stock_qty') or 0)
    return {
        'id': product['id'],
        'name': product['name'],
        'price': price_per_unit,
        'unit': unit,
        'useUnit1': False,
        'maxQty': stock if stock > 0 else 9999,
        'qty': weight_kg,
        'sellByWeight': True,
        'line_total': round(price_per_unit * weight_kg),
    }


def resolve_weight_scan(barcode):
    cfg = get_scale_config()
    parsed = parse_weight_barcode(barcode, cfg.get('barcode_prefix', '2'))
    if not parsed:
        return {'success': False, 'error': 'Không phải mã vạch cân hợp lệ'}

    conn = get_db_connection()
    try:
        product = lookup_weight_product(conn, parsed['plu'])
        if not product:
            return {
                'success': False,
                'error': f'Không tìm thấy sản phẩm PLU {parsed["plu"]}. Hãy cấu hình PLU cân trong danh mục.',
                'parsed': parsed,
            }
        item = build_weight_cart_item(product, parsed['weight_kg'])
        if not item:
            return {'success': False, 'error': 'Khối lượng không hợp lệ', 'parsed': parsed}

        return {
            'success': True,
            'source': 'scale_barcode',
            'parsed': parsed,
            'data': item,
        }
    finally:
        conn.close()
