"""Sinh product_code / barcode F&B: ready_made (Mxxx), raw_materials (NVLxxx)."""
import sqlite3


def _next_prefixed_code(cursor, prefix, table_sql):
    """prefix ví dụ 'M' hoặc 'NVL' — số bắt đầu sau len(prefix)."""
    cursor.execute(table_sql)
    row = cursor.fetchone()
    if row:
        raw = row[0] if not isinstance(row, sqlite3.Row) else row[0]
        try:
            num = int(''.join(ch for ch in str(raw)[len(prefix):] if ch.isdigit()) or '0')
            return f"{prefix}{str(num + 1).zfill(3)}"
        except ValueError:
            pass
    return f"{prefix}001"


def get_next_menu_code(cursor):
    """Sinh mã Mxxx tiếp theo từ max item_code trong bảng menu."""
    return _next_prefixed_code(cursor, "M", """
        SELECT item_code FROM menu
        WHERE item_code LIKE 'M%'
          AND LENGTH(item_code) >= 2
        ORDER BY CAST(SUBSTR(item_code, 2) AS INTEGER) DESC, item_code DESC
        LIMIT 1
    """)


def get_next_raw_material_code(cursor):
    """Sinh mã NVLxxx tiếp theo từ max product_code NVL trong bảng products."""
    return _next_prefixed_code(cursor, "NVL", """
        SELECT product_code FROM products
        WHERE product_code LIKE 'NVL%'
          AND LENGTH(product_code) >= 4
        ORDER BY CAST(SUBSTR(product_code, 4) AS INTEGER) DESC, product_code DESC
        LIMIT 1
    """)


def assign_ready_made_product_codes(cursor, product_id, has_wholesale=False):
    """ready_made: product_code=Mxxx, barcode=Mxxx01, barcode1=Mxxx02."""
    menu_row = cursor.execute(
        "SELECT item_code FROM menu WHERE product_id = ?", (product_id,)
    ).fetchone()
    prod_row = cursor.execute(
        "SELECT product_code FROM products WHERE id = ?", (product_id,)
    ).fetchone()

    code = None
    if menu_row:
        code = menu_row['item_code']
    elif prod_row and prod_row['product_code'] and str(prod_row['product_code']).upper().startswith('M'):
        code = prod_row['product_code']

    if not code:
        code = get_next_menu_code(cursor)

    barcode = f"{code}01"
    barcode1 = f"{code}02" if has_wholesale else None

    cursor.execute(
        "UPDATE products SET product_code = ?, barcode = ?, barcode1 = ? WHERE id = ?",
        (code, barcode, barcode1, product_id),
    )
    return code, barcode, barcode1


def assign_raw_material_product_codes(cursor, product_id, has_wholesale=False):
    """raw_materials: product_code=NVLxxx, barcode=NVLxxx01, barcode1=NVLxxx02."""
    prod_row = cursor.execute(
        "SELECT product_code FROM products WHERE id = ?", (product_id,)
    ).fetchone()

    code = None
    if prod_row and prod_row['product_code']:
        pc = str(prod_row['product_code']).upper()
        if pc.startswith('NVL'):
            code = prod_row['product_code']

    if not code:
        code = get_next_raw_material_code(cursor)

    barcode = f"{code}01"
    barcode1 = f"{code}02" if has_wholesale else None

    cursor.execute(
        "UPDATE products SET product_code = ?, barcode = ?, barcode1 = ? WHERE id = ?",
        (code, barcode, barcode1, product_id),
    )
    return code, barcode, barcode1
