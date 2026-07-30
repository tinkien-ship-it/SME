"""Checkout trả hàng NCC — luồng giống POS: sale + HĐ + kho + PT/công nợ."""

import json
from datetime import datetime

from Services.hkd_sector import requires_stock_check
from Services.inventory_stock_helpers import (
    apply_wac_outbound,
    import_base_qty,
    import_cost_to_base,
    sync_inventory_quantities,
)
from Services.sale_helpers import table_has_column


def _next_voucher_no(cursor, prefix, table, col='voucher_no'):
    cursor.execute(
        f"SELECT {col} FROM {table} WHERE {col} LIKE ? ORDER BY id DESC LIMIT 1",
        (f'{prefix}%',),
    )
    last = cursor.fetchone()
    if last and last[0] and len(last[0]) > len(prefix):
        try:
            return f"{prefix}{int(last[0][len(prefix):]) + 1:06d}"
        except ValueError:
            pass
    return f"{prefix}000001"


def _line_sale_amount(qty, price, discount_pct, tax_pct):
    line_sub = qty * price
    line_disc = round(line_sub * (discount_pct / 100))
    line_taxable = line_sub - line_disc
    line_tax = round(line_taxable * (tax_pct / 100))
    return line_taxable + line_tax, line_disc, line_tax


def _load_import_line(cursor, import_id, product_id):
    cursor.execute(
        """
        SELECT ii.qty, ii.buyprice, ii.unit_type,
               COALESCE(ii.discount, 0) AS discount_amount,
               COALESCE(ii.tax, 0) AS tax_amount,
               COALESCE(ii.discount_pct, 0) AS discount_pct,
               COALESCE(ii.tax_pct, 0) AS tax_pct,
               ii.cost_price,
               p.name, p.barcode, p.unit AS base_unit, p.unit1 AS wholesale_unit,
               COALESCE(p.unit_ratio, 1) AS unit_ratio,
               COALESCE(p.product_type, 'goods') AS product_type
        FROM import_details ii
        JOIN products p ON p.id = ii.product_id
        WHERE ii.import_id = ? AND ii.product_id = ?
        """,
        (import_id, product_id),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def _remaining_qty(cursor, import_id, product_id, original_qty):
    cursor.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM return_import WHERE import_id = ? AND product_id = ?",
        (import_id, product_id),
    )
    returned = float(cursor.fetchone()[0] or 0)
    return max(0.0, float(original_qty) - returned)


def process_return_import_checkout(cursor, data):
    """
    Xử lý trả NCC nhiều dòng.
    Trả về dict {sale_id, sale_no, total_amount, px_voucher, pt_voucher, ...}
    """
    import_id = int(data['import_id'])
    lines = data.get('items') or []
    if not lines:
        raise ValueError('Chưa chọn hàng trả')

    payment_method = str(data.get('payment_method') or '111')
    if payment_method not in ('111', '112', '131'):
        payment_method = '111'

    reason = (data.get('reason') or 'Trả hàng NCC').strip()
    return_date = data.get('date') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    cursor.execute(
        """
        SELECT i.id, i.import_no, i.bill_no, i.supplier_id,
               s.name AS supplier_name, s.address AS supplier_address,
               s.tax_code AS supplier_tax_code,
               COALESCE(s.phone, '') AS supplier_phone,
               COALESCE(s.email, '') AS supplier_email
        FROM import i
        LEFT JOIN suppliers s ON s.id = i.supplier_id
        WHERE i.id = ?
        """,
        (import_id,),
    )
    imp = cursor.fetchone()
    if not imp:
        raise ValueError('Phiếu nhập không tồn tại')
    imp = dict(imp)

    supplier_name = imp.get('supplier_name') or 'NCC'
    supplier_address = imp.get('supplier_address') or ''
    tax_code = imp.get('supplier_tax_code') or ''
    customer_name = data.get('customer_name') or supplier_name
    company_name = data.get('company_name') or supplier_name
    address = data.get('address') or supplier_address
    email = data.get('email') or imp.get('supplier_email') or ''
    customer_phone = data.get('customer_phone') or imp.get('supplier_phone') or ''

    import_no = imp.get('import_no') or f"PN{str(import_id).zfill(6)}"
    ref_doc = f"TR{str(import_id).zfill(6)}"
    note = f"Trả hàng NCC theo {import_no} | import_id={import_id} | {reason}"

    total_amount = 0.0
    parsed_lines = []

    for raw in lines:
        qty_input = float(raw.get('quantity') or 0)
        if qty_input <= 0:
            continue
        pid = int(raw['product_id'])
        detail = _load_import_line(cursor, import_id, pid)
        if not detail:
            raise ValueError(f'SP #{pid} không có trong phiếu nhập')

        original_qty = float(detail['qty'])
        remain = _remaining_qty(cursor, import_id, pid, original_qty)
        if qty_input > remain + 0.0001:
            raise ValueError(
                f"{detail['name']}: số lượng trả ({qty_input}) vượt còn lại ({remain})"
            )

        unit_type = int(raw.get('unit_type') if raw.get('unit_type') is not None else detail['unit_type'] or 0)
        ratio = float(detail['unit_ratio'] or 1)
        buyprice = float(raw.get('buyprice') if raw.get('buyprice') is not None else detail['buyprice'])
        discount_pct = float(raw.get('discount_pct') if raw.get('discount_pct') is not None else detail['discount_pct'] or 0)
        tax_pct = float(raw.get('tax_pct') if raw.get('tax_pct') is not None else detail['tax_pct'] or 0)

        line_total, _, _ = _line_sale_amount(qty_input, buyprice, discount_pct, tax_pct)
        total_amount += line_total

        qty_base = import_base_qty(qty_input, unit_type, ratio)
        if requires_stock_check(detail.get('product_type')) and qty_base > 0:
            from Services.inventory_stock_helpers import ledger_quantity
            stock = ledger_quantity(cursor, pid)
            if stock < qty_base - 0.0001:
                raise ValueError(f"{detail['name']}: không đủ tồn ({stock} < {qty_base})")

        parsed_lines.append({
            'product_id': pid,
            'qty_input': qty_input,
            'qty_base': qty_base,
            'unit_type': unit_type,
            'ratio': ratio,
            'buyprice': buyprice,
            'discount_pct': discount_pct,
            'tax_pct': tax_pct,
            'line_total': line_total,
            'detail': detail,
        })

    if not parsed_lines:
        raise ValueError('Không có dòng hàng hợp lệ')

    total_amount = round(total_amount)

    if table_has_column(cursor, 'sale', 'business_line'):
        cursor.execute(
            """
            INSERT INTO sale
            (date, total_amount, payment_method, customer_name, company_name, tax_code,
             customer_phone, address, note, status, email, business_line, sale_no)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, 'return_import', ?)
            """,
            (
                return_date, total_amount, payment_method, customer_name, company_name,
                tax_code, customer_phone, address, note, email, ref_doc,
            ),
        )
    else:
        cursor.execute(
            """
            INSERT INTO sale
            (date, total_amount, payment_method, customer_name, company_name, tax_code,
             customer_phone, address, note, status, email, sale_no)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)
            """,
            (
                return_date, total_amount, payment_method, customer_name, company_name,
                tax_code, customer_phone, address, note, email, ref_doc,
            ),
        )
    sale_id = cursor.lastrowid

    px_items = []
    sync_pids = set()
    pn_ref = import_no

    for pl in parsed_lines:
        pid = pl['product_id']
        detail = pl['detail']
        qty_input = pl['qty_input']
        qty_base = pl['qty_base']
        unit_type = pl['unit_type']
        ratio = pl['ratio']

        display_unit = (
            detail['wholesale_unit'] if unit_type == 1 else detail['base_unit']
        ) or 'Cái'

        import_cost_base = import_cost_to_base(detail.get('cost_price'), unit_type, ratio)

        cost_out = 0.0
        if requires_stock_check(detail.get('product_type')) and qty_base > 0:
            _, cost_used = apply_wac_outbound(cursor, pid, qty_base, import_cost_base)
            cost_out = qty_base * cost_used
            sync_pids.add(pid)

            cursor.execute(
                """
                INSERT INTO stock_moves
                (product_id, date, type, ref_id, quantity, cost_price, note, ref_document, ref_type, type1)
                VALUES (?, ?, 'RETURN_IMPORT', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid, return_date, import_id, -qty_base, import_cost_base,
                    f"Trả NCC {ref_doc} - {reason}", pn_ref, 'export', 'Trả HN',
                ),
            )
            cursor.execute(
                """
                INSERT INTO inventory_transactions
                (product_id, type, type1, quantity, cost_price, reference_id, reference_type, note, created_at)
                VALUES (?, 'export', 'Trả HN', ?, ?, ?, 'return_import', ?, ?)
                """,
                (
                    pid, -qty_base, import_cost_base, sale_id,
                    f"Trả NCC {ref_doc}", return_date,
                ),
            )

            px_unit_price = import_cost_base * (ratio if unit_type == 1 else 1)
            px_items.append({
                'product_id': pid,
                'product_name': detail['name'],
                'product_code': detail.get('barcode') or str(pid),
                'unit': display_unit,
                'quantity': qty_input,
                'price': px_unit_price,
                'amount': cost_out,
            })

        cursor.execute(
            """
            INSERT INTO sale_items
            (sale_id, product_id, quantity, price, cost_price, UseSaleUnit, unit_ratio, discount_pct, tax_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sale_id, pid, qty_input, pl['buyprice'], import_cost_base,
                1 if unit_type == 1 else 0, ratio,
                pl['discount_pct'], pl['tax_pct'],
            ),
        )

        refund_line = pl['line_total']
        if table_has_column(cursor, 'return_import', 'refund_amount'):
            cursor.execute(
                """
                INSERT INTO return_import
                (import_id, product_id, quantity, cost_price, date, reason, refund_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (import_id, pid, qty_input, import_cost_base, return_date, reason, refund_line),
            )
        else:
            cursor.execute(
                """
                INSERT INTO return_import
                (import_id, product_id, quantity, cost_price, date, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (import_id, pid, qty_input, import_cost_base, return_date, reason),
            )

    if sync_pids:
        sync_inventory_quantities(cursor, list(sync_pids))

    px_voucher = None
    if px_items:
        px_voucher = _next_voucher_no(cursor, 'PX', 'phieu_xuat_kho')
        px_total = sum(i['amount'] for i in px_items)
        cursor.execute(
            """
            INSERT INTO phieu_xuat_kho
            (voucher_no, date, customer_name, items_json, total_amount, note, sale_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                px_voucher, return_date, supplier_name,
                json.dumps(px_items, ensure_ascii=False), px_total,
                f"Xuất trả NCC {ref_doc}", sale_id,
            ),
        )

    pt_voucher = None
    if payment_method in ('111', '112') and total_amount > 0:
        pt_voucher = _next_voucher_no(cursor, 'PT', 'phieu_thu')
        pay_label = 'tiền mặt' if payment_method == '111' else 'chuyển khoản'
        cursor.execute(
            """
            INSERT INTO phieu_thu
            (voucher_no, payer_name, address, tax_code, amount, debit_account, credit_account,
             reason, reference_document, sale_id, date)
            VALUES (?, ?, ?, ?, ?, ?, '511', ?, ?, ?, ?)
            """,
            (
                pt_voucher, company_name or customer_name, address, tax_code,
                total_amount, payment_method,
                f"Thu tiền NCC hoàn trả ({pay_label}) - {ref_doc}",
                ref_doc, sale_id, return_date,
            ),
        )
    elif payment_method == '131' and total_amount > 0:
        cursor.execute(
            """
            INSERT INTO cong_no
            (customer_name, company_name, address, tax_code, debit_account, credit_account,
             date_of_debt, unpaid_amount, sale_id, sale_no)
            VALUES (?, ?, ?, ?, '131', '511', ?, ?, ?, ?)
            """,
            (
                customer_name, company_name, address, tax_code,
                return_date, total_amount, sale_id, ref_doc,
            ),
        )

    return {
        'sale_id': sale_id,
        'sale_no': ref_doc,
        'total_amount': total_amount,
        'px_voucher': px_voucher,
        'pt_voucher': pt_voucher,
        'import_no': import_no,
    }
