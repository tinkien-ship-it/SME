"""Hàm tiện ích dùng chung — tách khỏi app.py."""
import os
import re
import sqlite3
from datetime import date, datetime

from db_utils import get_db_connection

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def thuần_thục_tên_file(filename):
    if '.' in filename:
        name_part, ext_part = filename.rsplit('.', 1)
        ext_part = ext_part.lower()
    else:
        name_part, ext_part = filename, 'jpg'

    name_part = name_part.lower()
    co_dau = "áàảãạăắằẳẵặâấầẩẫậéèẻẽẹêếềểễệíìỉĩịóòỏõọôốồổỗộơớờởỡợúùủũụưứừửữựýỳỷỹỵđ"
    khong_dau = "aaaaaaaaaaaaaaaaaeeeeeeeeeeeiiiiiooooooooooooooooouuuuuuuuuuuyyyyyd"
    name_part = name_part.translate(str.maketrans(co_dau, khong_dau))
    name_part = re.sub(r'[^\w\s-]', '', name_part)
    name_part = re.sub(r'[\s_]+', '-', name_part)
    name_part = name_part.strip('-')
    if not name_part:
        name_part = "uploaded_file"
    return f"{name_part}.{ext_part}"


def so_thanh_chu(so):
    don_vi = ['', 'một', 'hai', 'ba', 'bốn', 'năm', 'sáu', 'bảy', 'tám', 'chín']
    hang_chuc = ['', 'mười', 'hai mươi', 'ba mươi', 'bốn mươi', 'năm mươi', 'sáu mươi', 'bảy mươi', 'tám mươi', 'chín mươi']
    nhom_lon = ['', 'nghìn', 'triệu', 'tỉ']

    def doc_ba_chu_so(n):
        if n == 0:
            return ''
        chuoi = ''
        hang_tram = n // 100
        n %= 100
        if hang_tram > 0:
            chuoi += don_vi[hang_tram] + ' trăm'
            if n > 0:
                chuoi += ' '
        if n > 0:
            if n < 10:
                chuoi += ('linh ' if hang_tram > 0 else '') + don_vi[n]
            elif n < 20:
                hang_dv = n % 10
                if hang_dv == 0:
                    chuoi += 'mười'
                elif hang_dv == 5:
                    chuoi += 'mười lăm'
                else:
                    chuoi += 'mười ' + don_vi[hang_dv]
            else:
                hang_ch = n // 10
                hang_dv = n % 10
                chuoi += hang_chuc[hang_ch]
                if hang_dv > 0:
                    chuoi += ' '
                    if hang_dv == 1:
                        chuoi += 'mốt'
                    elif hang_dv == 5:
                        chuoi += 'lăm'
                    else:
                        chuoi += don_vi[hang_dv]
        return chuoi

    try:
        so = int(so)
    except (TypeError, ValueError):
        return "Không đồng chẵn"

    if so == 0:
        return "Không đồng chẵn"
    if so < 0:
        return "âm " + so_thanh_chu(-so)

    ket_qua = []
    i = 0
    while so > 0:
        nhom = so % 1000
        so //= 1000
        chuoi_nhom = doc_ba_chu_so(nhom)
        if chuoi_nhom:
            chuoi_nhom += (' ' + nhom_lon[i]) if i > 0 else ''
            ket_qua.append(chuoi_nhom.strip())
        i += 1

    ket_qua = ' '.join(reversed(ket_qua)).strip() + " đồng chẵn"
    if ket_qua:
        ket_qua = ket_qua[0].upper() + ket_qua[1:]
    return ket_qua


def parse_number_vn(value):
    """Parse chuỗi số VN (1.234.567,89) → float."""
    if value is None or value == '':
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(' ', '').replace('₫', '').replace('đ', '')
    if not s:
        return 0.0
    s = s.replace('.', '').replace(',', '.')
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def format_number(value, decimals=2):
    """Định dạng số VN: dấu chấm (.) nghìn, dấu phẩy (,) thập phân — mặc định 2 số lẻ."""
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        n = 0.0
    dec = max(0, int(decimals))
    sign = '-' if n < 0 else ''
    n = abs(n)
    if dec == 0:
        int_part = str(int(round(n)))
        dec_part = None
    else:
        parts = f"{n:.{dec}f}".split('.')
        int_part, dec_part = parts[0], parts[1]
    grouped = ''
    for i, ch in enumerate(reversed(int_part)):
        if i and i % 3 == 0:
            grouped = '.' + grouped
        grouped = ch + grouped
    if dec == 0:
        return sign + grouped
    return sign + grouped + ',' + dec_part


def format_price(price, decimals=2):
    if price is None:
        return format_number(0, decimals)
    try:
        return format_number(float(price), decimals)
    except (TypeError, ValueError):
        return format_number(0, decimals)


def format_date(value, fmt='%d/%m/%Y'):
    if not value:
        return '—'
    if isinstance(value, str):
        value = value.strip()
        for f in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y'):
            try:
                return datetime.strptime(value, f).strftime(fmt)
            except ValueError:
                pass
        return value
    return value.strftime(fmt) if hasattr(value, 'strftime') else str(value)


def vnd(value, decimals=2):
    try:
        return format_number(float(value or 0), decimals) + " ₫"
    except (TypeError, ValueError):
        return format_number(0, decimals) + " ₫"


def parse_date(date_str):
    if not date_str:
        return None
    date_str = str(date_str).strip()
    formats = [
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d',
        '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y',
        '%d-%m-%Y %H:%M:%S',
        '%d-%m-%Y',
        '%Y/%m/%d %H:%M:%S',
        '%Y/%m/%d',
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    print(f"[ERROR PARSE DATE] Không parse được: '{date_str}'")
    return None


def convert_date(val):
    if val is None:
        return None
    if isinstance(val, bytes):
        val = val.decode('utf-8', errors='ignore').strip()
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        try:
            return date.fromisoformat(val[:10])
        except ValueError:
            return None
    return None


def convert_datetime(val):
    if val is None:
        return None
    if isinstance(val, bytes):
        val = val.decode('utf-8', errors='ignore').strip()
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        try:
            return datetime.fromisoformat(val.replace(' ', 'T')[:19])
        except ValueError:
            return None
    return None


def register_sqlite_converters():
    sqlite3.register_converter("DATE", convert_date)
    sqlite3.register_converter("TIMESTAMP", convert_datetime)


def format_vn_date(date_obj):
    if not date_obj:
        return '—'
    if isinstance(date_obj, str):
        date_obj = parse_date(date_obj)
    return date_obj.strftime('%d/%m/%Y') if date_obj else '—'


def format_vn_number(num, decimals=2):
    return format_number(num, decimals)


def format_currency(value, decimals=2):
    return format_number(value, decimals)


def format_date_for_frontend(date_str):
    if not date_str:
        return ""
    formats = ['%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y', '%Y/%m/%d']
    for fmt in formats:
        try:
            dt = datetime.strptime(str(date_str), fmt)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            continue
    return date_str


def get_setting(key, default=""):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else default


def get_next_voucher_no(prefix):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO voucher_seq (type, seq) VALUES (?, 0)
            ON CONFLICT(type) DO UPDATE SET seq = seq + 1
        """, (prefix,))
        c.execute("SELECT seq FROM voucher_seq WHERE type=?", (prefix,))
        seq = c.fetchone()[0]
        conn.commit()
        return f"{prefix}{seq:06d}"


def validate_json(required_fields, data):
    for f in required_fields:
        if f not in data or data[f] in [None, '', []]:
            return f"Thiếu hoặc không hợp lệ trường: {f}"
    return None


def get_product_stock(product_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT quantity, avg_cost FROM inventory WHERE product_id=?", (product_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {'quantity': row['quantity'] or 0, 'avg_cost': row['avg_cost'] or 0}
    return {'quantity': 0, 'avg_cost': 0}


def register_jinja_filters(app):
    app.jinja_env.filters['so_thanh_chu'] = so_thanh_chu
    app.jinja_env.filters['format_number'] = format_number
    app.jinja_env.filters['format_date'] = format_date
    app.jinja_env.filters['vnd'] = vnd
    app.jinja_env.filters['format_currency'] = format_currency
    app.jinja_env.filters['format_vn_date'] = format_vn_date
    app.jinja_env.filters['format_vn_number'] = format_vn_number
    app.config['parse_date'] = parse_date
