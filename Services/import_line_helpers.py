"""Mã sản phẩm theo loại hàng (HKD import)."""
import sqlite3


def _max_seq_with_prefix(c, prefix, digit_width=4):
    px = prefix.upper()
    c.execute(
        "SELECT product_code FROM products WHERE UPPER(product_code) LIKE ? ORDER BY product_code DESC",
        (f"{px}%",),
    )
    max_num = 0
    plen = len(px)
    for row in c.fetchall():
        code = (row[0] or '').strip().upper()
        if code.startswith(px):
            suffix = code[plen:]
            if suffix.isdigit():
                max_num = max(max_num, int(suffix))
    return f"{px}{max_num + 1:0{digit_width}d}"


def peek_next_product_code(c, product_type):
    pt = (product_type or 'goods').strip().lower()
    if pt == 'materials':
        return _max_seq_with_prefix(c, 'VT', 4)
    if pt == 'fixed_asset':
        return _max_seq_with_prefix(c, 'TSCD', 4)
    if pt == 'tools':
        return _max_seq_with_prefix(c, 'CCDC', 4)
    if pt == 'service':
        return _max_seq_with_prefix(c, 'DV', 3)
    return None


def assign_product_codes(c, product_id, product_type, unit1=None):
    """Gán product_code + barcode sau INSERT. Trả về (code, barcode, barcode1)."""
    pt = (product_type or 'goods').strip().lower()
    if pt == 'materials':
        code = _max_seq_with_prefix(c, 'VT', 4)
        barcode = f"{code}01"
        barcode1 = f"{code}02" if unit1 else None
    elif pt == 'fixed_asset':
        code = _max_seq_with_prefix(c, 'TSCD', 4)
        barcode = code
        barcode1 = None
    elif pt == 'tools':
        code = _max_seq_with_prefix(c, 'CCDC', 4)
        barcode = code
        barcode1 = None
    elif pt == 'service':
        code = _max_seq_with_prefix(c, 'DV', 3)
        barcode = code
        barcode1 = None
    elif pt == 'goods':
        code = f"SP{product_id:04d}"
        barcode = f"{code}01"
        barcode1 = f"{code}02" if unit1 else None
    else:
        code = f"SP{product_id:04d}"
        barcode = f"{code}01"
        barcode1 = f"{code}02" if unit1 else None

    c.execute(
        "UPDATE products SET product_code=?, barcode=?, barcode1=? WHERE id=?",
        (code, barcode, barcode1, product_id),
    )
    return code, barcode, barcode1


DEFAULT_WAREHOUSES = (
    ('KHO_001', 'Kho mặc định', 1),
    ('KHO_002', 'Kho chi nhánh 2', 0),
    ('KHO_003', 'Kho chi nhánh 3', 0),
)

# Dòng ghi nhận tồn kho bán hàng / WAC (156, 152…)
INVENTORY_TRACKED_LINE_TYPES = frozenset({'goods', 'materials'})

# TSCĐ / CCDC: sổ riêng, không qua tồn POS
ASSET_REGISTER_LINE_TYPES = frozenset({'fixed_asset', 'tools'})


def tracks_retail_inventory(line_type):
    return (line_type or 'goods').strip().lower() in INVENTORY_TRACKED_LINE_TYPES


def ensure_warehouse_schema(conn):
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS warehouses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            branch_name TEXT,
            address TEXT,
            is_default INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    count = c.execute("SELECT COUNT(*) FROM warehouses").fetchone()[0]
    if count == 0:
        for code, name, is_def in DEFAULT_WAREHOUSES:
            c.execute(
                "INSERT INTO warehouses (code, name, is_default, is_active) VALUES (?, ?, ?, 1)",
                (code, name, is_def),
            )

    for table, col in (('import', 'warehouse_code'), ('import_details', 'warehouse_code')):
        c.execute(f'PRAGMA table_info({table})')
        cols = {r[1] for r in c.fetchall()}
        if 'warehouse_code' not in cols:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN warehouse_code TEXT DEFAULT 'KHO_001'")
            except sqlite3.OperationalError:
                pass

    c.execute('PRAGMA table_info(stock_moves)')
    sm_cols = {r[1] for r in c.fetchall()}
    if 'warehouse_code' not in sm_cols:
        try:
            c.execute("ALTER TABLE stock_moves ADD COLUMN warehouse_code TEXT DEFAULT 'KHO_001'")
        except sqlite3.OperationalError:
            pass


def list_active_warehouses(conn):
    ensure_warehouse_schema(conn)
    c = conn.cursor()
    c.execute("""
        SELECT id, code, name, branch_name, address, is_default
        FROM warehouses WHERE is_active = 1
        ORDER BY is_default DESC, code ASC
    """)
    cols = ['id', 'code', 'name', 'branch_name', 'address', 'is_default']
    return [dict(zip(cols, row)) for row in c.fetchall()]


def insert_import_detail_row(c, import_id, fields):
    """INSERT động vào import_details theo cột có sẵn."""
    c.execute('PRAGMA table_info(import_details)')
    allowed = {row[1] for row in c.fetchall()}
    cols, vals = [], []
    for key, val in fields.items():
        if key in allowed:
            cols.append(key)
            vals.append(val)
    if not cols:
        return
    ph = ', '.join(['?'] * len(vals))
    c.execute(f"INSERT INTO import_details ({', '.join(cols)}) VALUES ({ph})", vals)


def is_service_detail_row(row):
    """Dòng dịch vụ: chỉ lưu trên import_details (product_id NULL hoặc line_type=service)."""
    lt = (row.get('line_type') or '').strip().lower()
    if lt == 'service':
        return True
    pid = row.get('product_id')
    return pid is None or pid == '' or pid == 0


def detect_service_import(imp, detail_rows):
    """Phiếu mua dịch vụ thuần — lấy chi tiết trực tiếp từ import_details."""
    if (imp.get('doc_type') or '').strip().lower() == 'service':
        return True
    if not detail_rows:
        return False
    return all(is_service_detail_row(r if isinstance(r, dict) else dict(r)) for r in detail_rows)


def fetch_import_details_raw(c, import_id):
    """Luồng 2: SELECT trực tiếp từ import_details, không JOIN products."""
    c.execute('PRAGMA table_info(import_details)')
    detail_cols = {col[1] for col in c.fetchall()}

    select_parts = [
        'id', 'import_id', 'product_id', 'qty', 'buyprice',
        'COALESCE(discount, 0) AS discount',
        'COALESCE(tax, 0) AS tax',
        'COALESCE(subtotal, 0) AS subtotal',
        'COALESCE(payment_amt, 0) AS payment_amt',
        'COALESCE(unit_type, 0) AS unit_type',
        'COALESCE(tax_pct, 0) AS tax_pct',
        'COALESCE(discount_pct, 0) AS discount_pct',
    ]
    if 'product_name' in detail_cols:
        select_parts.append("COALESCE(product_name, '') AS product_name")
    if 'unit' in detail_cols:
        select_parts.append("COALESCE(unit, '') AS unit")
    if 'line_type' in detail_cols:
        select_parts.append("COALESCE(line_type, 'goods') AS line_type")
    if 'warehouse_code' in detail_cols:
        select_parts.append('warehouse_code')

    sql = f"""
        SELECT {', '.join(select_parts)}
        FROM import_details
        WHERE import_id = ?
        ORDER BY id
    """
    c.execute(sql, (import_id,))
    col_names = [d[0] for d in (c.description or [])]
    rows = []
    for row in c.fetchall():
        if isinstance(row, dict):
            rows.append(dict(row))
        elif hasattr(row, 'keys'):
            rows.append(dict(row))
        else:
            rows.append(dict(zip(col_names, row)))
    return rows


def _calc_line_financials(row):
    """Tính % chiết khấu/thuế và tổng thanh toán dòng từ import_details."""
    qty = float(row.get('qty') or 0)
    buyprice = float(row.get('buyprice') or 0)
    discount_amount = float(row.get('discount') or 0)
    tax_amount = float(row.get('tax') or 0)

    line_total = qty * buyprice
    after_discount = line_total - discount_amount
    payment_amt = float(row.get('payment_amt') or 0)
    if not payment_amt:
        payment_amt = after_discount + tax_amount

    discount_pct = float(row.get('discount_pct') or 0)
    if not discount_pct and line_total:
        discount_pct = discount_amount / line_total * 100
    tax_pct = float(row.get('tax_pct') or 0)
    if not tax_pct and after_discount:
        tax_pct = tax_amount / after_discount * 100

    return {
        'qty': qty,
        'buyprice': buyprice,
        'line_total': line_total,
        'subtotal': after_discount,
        'payment_amount': payment_amt,
        'discount_pct': round(discount_pct, 2),
        'tax_pct': round(tax_pct, 2),
        'discount': discount_amount,
        'tax': tax_amount,
    }


def map_service_detail_for_edit(row):
    """Map dòng dịch vụ (luồng 2) cho API edit / in phiếu."""
    fin = _calc_line_financials(row)
    unit = str(row.get('unit') or 'Lần').strip() or 'Lần'
    name = str(row.get('product_name') or '').strip()
    return {
        'id': row.get('id'),
        'import_id': row.get('import_id'),
        'product_id': None,
        'product_name': name,
        'name': name,
        'unit': unit,
        'invoice_unit': unit,
        'base_unit': unit,
        'wholesale_unit': '—',
        'line_type': 'service',
        'unit_type': 0,
        'base_sale_price': 0,
        'sale_price': 0,
        'unit_ratio': 1,
        **fin,
    }


def enrich_stock_detail_for_edit(c, row):
    """Luồng 1: bổ sung thông tin từ products khi có product_id."""
    item = dict(row)
    fin = _calc_line_financials(item)
    item.update(fin)

    pid = item.get('product_id')
    p_row = None
    if pid:
        c.execute('PRAGMA table_info(products)')
        product_cols = {col[1] for col in c.fetchall()}
        p_select = ['name']
        for col in ('unit', 'unit1', 'base_price', 'price', 'unit_ratio', 'barcode'):
            if col in product_cols:
                p_select.append(col)
        c.execute(
            f"SELECT {', '.join(p_select)} FROM products WHERE id = ?",
            (pid,),
        )
        p_row = c.fetchone()

    detail_unit = str(item.get('unit') or '').strip()
    if p_row:
        p = dict(p_row)
        b_unit = str(p.get('unit') or detail_unit or 'Cái').strip() or 'Cái'
        w_unit = str(p.get('unit1') or '').strip()
        if not detail_unit:
            detail_unit = b_unit
        if not str(item.get('product_name') or '').strip():
            item['product_name'] = p.get('name') or ''
    else:
        b_unit = detail_unit or 'Cái'
        w_unit = ''
        if not detail_unit:
            detail_unit = b_unit

    unit_type = int(item.get('unit_type') or 0)
    if unit_type == 1 and w_unit:
        invoice_unit = w_unit
    else:
        invoice_unit = detail_unit or b_unit

    p_data = dict(p_row) if p_row else {}
    item.update({
        'invoice_unit': invoice_unit,
        'unit': invoice_unit,
        'base_unit': b_unit,
        'wholesale_unit': w_unit or '—',
        'base_sale_price': float(p_data.get('base_price') or 0),
        'sale_price': float(p_data.get('price') or 0),
        'unit_ratio': float(p_data.get('unit_ratio') or 1),
        'line_type': (item.get('line_type') or 'goods').strip().lower(),
    })
    if p_data.get('barcode') is not None:
        item['barcode'] = p_data['barcode']
    return item


def build_service_line_insert_fields(import_id, item, extra_cost, total_base_for_allocation):
    """Chuẩn hóa payload dòng dịch vụ khi cập nhật phiếu nhập."""
    from decimal import Decimal

    qty = Decimal(str(item.get('qty', 0) or 0))
    if qty <= 0:
        return None, Decimal('0')

    name = (
        item.get('name')
        or item.get('invoice_name')
        or item.get('product_name')
        or ''
    ).strip()
    buyprice = Decimal(str(item.get('buyprice', 0) or 0))
    discount_pct = Decimal(str(
        item.get('discount_pct')
        if item.get('discount_pct') is not None
        else item.get('discountPct', 0)
    ))
    tax_pct = Decimal(str(
        item.get('tax_pct')
        if item.get('tax_pct') is not None
        else item.get('taxPct', 0)
    ))
    unit_in = str(item.get('unit') or 'Lần').strip() or 'Lần'

    line_subtotal = qty * buyprice
    line_disc = (
        Decimal(str(item.get('discount_amount')))
        if item.get('discount_amount') is not None
        else line_subtotal * discount_pct / Decimal('100')
    )
    line_after_disc = line_subtotal - line_disc
    line_tax = (
        Decimal(str(item.get('tax_amount')))
        if item.get('tax_amount') is not None
        else line_after_disc * tax_pct / Decimal('100')
    )
    allocated_extra = (
        extra_cost * (line_after_disc / total_base_for_allocation)
        if extra_cost > 0
        else Decimal('0')
    )
    line_total = line_after_disc + line_tax + allocated_extra
    per_unit_cost = line_total / qty if qty > 0 else Decimal('0')

    fields = {
        'import_id': import_id,
        'product_id': None,
        'qty': float(qty),
        'buyprice': float(buyprice),
        'subtotal': float(line_subtotal),
        'discount': float(line_disc),
        'tax': float(line_tax),
        'cost_price': float(per_unit_cost),
        'tax_pct': float(tax_pct),
        'discount_pct': float(discount_pct),
        'payment_amt': float(line_total),
        'product_name': name,
        'unit': unit_in,
        'line_type': 'service',
        'unit_type': 0,
    }
    wh = (item.get('warehouse_code') or '').strip()
    if wh:
        fields['warehouse_code'] = wh
    return fields, line_total


def is_service_line_payload(item):
    """Nhận diện dòng dịch vụ từ payload frontend."""
    line_type = (item.get('line_type') or '').strip().lower()
    if line_type == 'service':
        return True
    pid = item.get('product_id')
    try:
        pid_int = int(pid) if pid not in (None, '') else 0
    except (TypeError, ValueError):
        pid_int = 0
    return pid_int <= 0


def calc_import_detail_line_amounts(
    quantity,
    unit_price,
    discount_amount,
    tax_amount,
    subtotal,
    discount_pct=0,
    tax_pct=0,
):
    """Tính chiết khấu, thuế, tổng thanh toán cho một dòng import_details."""
    qty = float(quantity or 0)
    price = float(unit_price or 0)
    disc = float(discount_amount or 0)
    tax = float(tax_amount or 0)
    sub = float(subtotal or 0)

    if disc or tax or sub:
        discount_val = round(disc)
        tax_val = round(tax)
        if sub > 0:
            line_total = round(sub - discount_val + tax_val)
        else:
            line_total = round(qty * price - discount_val + tax_val)
    else:
        line_sub = round(qty * price)
        discount_val = round(line_sub * float(discount_pct or 0) / 100)
        after_discount = line_sub - discount_val
        tax_val = round(after_discount * float(tax_pct or 0) / 100)
        line_total = after_discount + tax_val

    return {
        'discount_amount': discount_val,
        'tax_amount': tax_val,
        'line_total': line_total,
    }


def sum_import_details_payment_period(cursor, start_date, end_date):
    """Tổng thanh toán + số dòng từ import_details (gồm dịch vụ không có product_id)."""
    cursor.execute('PRAGMA table_info(import_details)')
    detail_cols = {col[1] for col in cursor.fetchall()}

    disc_pct_sel = (
        'COALESCE(ii.discount_pct, 0) AS discount_pct'
        if 'discount_pct' in detail_cols
        else '0 AS discount_pct'
    )
    tax_pct_sel = (
        'COALESCE(ii.tax_pct, 0) AS tax_pct'
        if 'tax_pct' in detail_cols
        else '0 AS tax_pct'
    )

    cursor.execute(
        f"""
        SELECT
            ii.qty AS quantity,
            ii.buyprice AS unit_price,
            COALESCE(ii.discount, 0) AS discount_amount,
            COALESCE(ii.tax, 0) AS tax_amount,
            COALESCE(ii.subtotal, 0) AS subtotal,
            {disc_pct_sel},
            {tax_pct_sel}
        FROM import_details ii
        INNER JOIN import i ON i.id = ii.import_id
        WHERE ii.qty > 0
          AND date(i.date) >= date(?)
          AND date(i.date) <= date(?)
        """,
        (start_date, end_date),
    )

    rows = cursor.fetchall()
    col_names = [d[0] for d in (cursor.description or [])]
    total = 0.0
    count = 0
    for row in rows:
        if isinstance(row, dict):
            r = row
        elif hasattr(row, 'keys'):
            r = dict(row)
        else:
            r = dict(zip(col_names, row))
        amounts = calc_import_detail_line_amounts(
            r['quantity'],
            r['unit_price'],
            r['discount_amount'],
            r['tax_amount'],
            r['subtotal'],
            r.get('discount_pct'),
            r.get('tax_pct'),
        )
        total += amounts['line_total']
        count += 1
    return total, count


def _json_safe(value):
    """Chuyển giá trị SQLite/Python sang kiểu an toàn cho JSON response."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            return 0.0
        return value
    if isinstance(value, str):
        return value
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    try:
        from decimal import Decimal
        if isinstance(value, Decimal):
            return float(value)
    except ImportError:
        pass
    return str(value)


def prepare_import_edit_json(imp):
    """Chuẩn hóa payload GET /api/import/<id>/edit cho frontend."""
    if not imp:
        return imp
    out = {}
    for key, val in imp.items():
        if key == 'items':
            continue
        out[key] = _json_safe(val)

    out['items'] = []
    for item in imp.get('items') or []:
        row = {k: _json_safe(v) for k, v in dict(item).items()}
        pid = row.get('product_id')
        if pid in (0, '0', ''):
            row['product_id'] = None
        if not row.get('product_name') and row.get('name'):
            row['product_name'] = row['name']
        out['items'].append(row)
    return out
