# -*- coding: utf-8 -*-
"""Replace api_import_sme in ketoan_sme.py with multi line-type + FA/CCDC + sync journals."""
from pathlib import Path

p = Path(r"C:\SME\routes\ketoan_sme.py")
t = p.read_text(encoding="utf-8")

start = t.find("    @app.route('/api/import_sme', methods=['POST'])")
if start < 0:
    raise SystemExit("api route not found")

# end at next section comment
end_marker = "    # --------------------------------------------------------------------------\n    # Danh mục tài khoản SME (TT99) — tách biệt HKD"
end = t.find(end_marker, start)
if end < 0:
    raise SystemExit("end marker not found")

new_fn = r'''    @app.route('/api/import_sme', methods=['POST'])
    @login_required
    @require_sme_regime
    def api_fb_import_post():
        """Nhập mua SME: HH/VT (kho) + DV/TSCĐ/CCDC; định khoản TT99."""
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        try:
            from Services.inward_invoice_helpers import ensure_import_service_schema
            from Services.import_line_helpers import (
                ensure_warehouse_schema,
                insert_import_detail_row,
                tracks_retail_inventory,
            )
            from Services.fixed_assets_helpers import (
                ensure_fixed_assets_schema,
                register_fixed_asset_from_import,
                register_tool_from_import,
            )
            from Services.inventory_stock_helpers import (
                apply_wac_inbound,
                sync_inventory_quantity_from_moves,
            )
            from Services.sme.import_journal import (
                BUSINESS_TYPE_LABELS,
                _business_type_for_line,
                sync_import_journals,
            )
            from Services.sme.journal_engine import ensure_sme_journal_ready

            ensure_import_service_schema(conn)
            ensure_warehouse_schema(conn)
            ensure_fixed_assets_schema(conn)
            ensure_sme_journal_ready(conn, commit=False)

            data = request.get_json()
            if not data:
                return jsonify({"error": "Dữ liệu gửi lên không hợp lệ"}), 400

            def _normalize_line_type(raw):
                t = str(raw or 'goods').strip().lower()
                if t in ('raw_materials', 'nvl'):
                    return 'materials'
                if t in ('ready_made', 'hang_hoa'):
                    return 'goods'
                if t in ('goods', 'materials', 'fixed_asset', 'tools', 'service', 'finished_goods'):
                    return t
                return 'goods'

            import_type = data.get('import_type', 'DOMESTIC')
            currency = (data.get('currency') or 'VND').strip().upper()
            exchange_rate = (
                Decimal(str(data.get('exchange_rate', 1.0)))
                if import_type == 'IMPORT'
                else Decimal('1.0')
            )

            items = data.get('items') or []
            supplier_id = data.get('supplier_id')
            import_date = data.get('date')
            bill_date = data.get('bill_date')
            import_no = data.get('import_no')
            bill_no = data.get('bill_no')
            tax_code = data.get('tax_code')
            note = data.get('note')
            extra_cost = round_money(data.get('extra_cost', 0))
            payment_status_input = data.get('payment_status', 'Chưa thanh toán')
            from_invoice_id = data.get('from_invoice_id')
            default_warehouse = (data.get('warehouse_code') or 'KHO_001').strip()

            payment_method_raw = str(data.get('payment_method', 'cash')).strip().upper()
            if payment_status_input == 'Chưa thanh toán':
                payment_method = 'CREDIT'
            else:
                payment_method = 'CASH' if payment_method_raw == 'CASH' else 'BANK_TRANSFER'

            po_id = data.get('po_id')
            try:
                po_id = int(po_id) if po_id not in (None, '', 0, '0') else None
            except (TypeError, ValueError):
                po_id = None

            c.execute("SELECT name, address, tax_code FROM suppliers WHERE id = ?", (supplier_id,))
            sup_row = c.fetchone()
            supplier_name = sup_row['name'] if sup_row else f"NCC ID {supplier_id}"
            if not tax_code and sup_row and 'tax_code' in sup_row.keys() and sup_row['tax_code']:
                tax_code = sup_row['tax_code']

            # Validate warehouse for HH/VT
            for item in items:
                lt = _normalize_line_type(
                    item.get('line_type') or item.get('invoice_product_type') or item.get('product_type')
                )
                if tracks_retail_inventory(lt):
                    wh = (item.get('warehouse_code') or default_warehouse or '').strip()
                    if not wh:
                        name = (item.get('name') or item.get('invoice_name') or '').strip() or 'dòng hàng'
                        return jsonify({
                            "error": f'Dòng "{name}" (Hàng hóa/Vật tư) bắt buộc chọn kho.',
                        }), 400

            total_base_vnd = Decimal('0.00')
            for i in items:
                qty = round_money(i.get('qty', 0))
                price_vnd = round_money(round_money(i.get('buyprice', 0)) * exchange_rate)
                total_base_vnd += round_money(qty * price_vnd)
            total_base_safe = total_base_vnd if total_base_vnd > 0 else Decimal('1.00')

            c.execute('PRAGMA table_info(import)')
            import_cols = {col[1] for col in c.fetchall()}
            import_fields = [
                'date', 'supplier_id', 'import_no', 'bill_no', 'bill_date',
                'note', 'payment_status', 'extra_cost', 'total_value', 'paid_amount',
            ]
            import_values = [
                import_date, supplier_id, import_no, bill_no, bill_date,
                note, payment_status_input, float(extra_cost), 0, 0,
            ]
            if 'warehouse_code' in import_cols:
                import_fields.append('warehouse_code')
                import_values.append(default_warehouse)
            if 'from_invoice_id' in import_cols and from_invoice_id:
                import_fields.append('from_invoice_id')
                import_values.append(int(from_invoice_id))
            if 'payment_method' in import_cols:
                import_fields.append('payment_method')
                import_values.append(
                    'cash' if payment_method == 'CASH'
                    else ('bank' if payment_method == 'BANK_TRANSFER' else None)
                )

            placeholders = ', '.join(['?'] * len(import_fields))
            c.execute(
                f'INSERT INTO import ({", ".join(import_fields)}) VALUES ({placeholders})',
                import_values,
            )
            import_id = c.lastrowid

            c.execute('PRAGMA table_info(stock_moves)')
            sm_has_wh = 'warehouse_code' in {col[1] for col in c.fetchall()}

            p_ids = [i.get('product_id') for i in items if i.get('product_id')]
            p_map = {}
            if p_ids:
                ph = ','.join(['?'] * len(p_ids))
                c.execute(
                    f"SELECT id, name, unit, unit1, unit_ratio, barcode, product_code, product_type "
                    f"FROM products WHERE id IN ({ph})",
                    p_ids,
                )
                p_map = {row['id']: row for row in c.fetchall()}

            items_for_json = []
            fixed_assets_created = 0
            tools_created = 0

            for item in items:
                line_type = _normalize_line_type(
                    item.get('line_type')
                    or item.get('invoice_product_type')
                    or item.get('product_type')
                )
                warehouse_code = (item.get('warehouse_code') or default_warehouse or 'KHO_001').strip()
                if not tracks_retail_inventory(line_type):
                    warehouse_code = warehouse_code or 'KHO_001'

                qty_in = round_money(item.get('qty', 0))
                if qty_in <= 0:
                    continue

                price_original = round_money(item.get('buyprice', 0))
                price_vnd = round_money(price_original * exchange_rate)
                tax_p = Decimal(str(item.get('tax_pct', 0) or 0))
                disc_p = Decimal(str(item.get('discount_pct', 0) or item.get('discountPct', 0) or 0))
                import_tax_p = (
                    Decimal(str(item.get('import_tax_pct', 0) or 0))
                    if import_type == 'IMPORT'
                    else Decimal('0.00')
                )
                unit_in = str(item.get('unit') or 'Cái').strip()
                inv_name = (
                    item.get('invoice_name')
                    or item.get('name')
                    or ''
                ).strip()

                line_subtotal_vnd = round_money(qty_in * price_vnd)
                line_disc_vnd = round_money(line_subtotal_vnd * (disc_p / Decimal('100.00')))
                line_net_vnd = line_subtotal_vnd - line_disc_vnd
                line_import_tax_vnd = Decimal('0.00')
                if import_type == 'IMPORT' and import_tax_p > 0:
                    line_import_tax_vnd = round_money(line_net_vnd * (import_tax_p / Decimal('100.00')))
                line_extra_vnd = round_money((line_subtotal_vnd / total_base_safe) * extra_cost)
                tax_base_vnd = line_net_vnd + line_import_tax_vnd if import_type == 'IMPORT' else line_net_vnd
                line_vat_vnd = round_money(tax_base_vnd * (tax_p / Decimal('100.00')))
                line_inventory_value_vnd = line_net_vnd + line_import_tax_vnd + line_extra_vnd
                line_total_payment_vnd = line_net_vnd + line_vat_vnd + line_extra_vnd + (
                    line_import_tax_vnd if import_type == 'IMPORT' else Decimal('0.00')
                )
                # Domestic: payable = net + VAT + extra (import tax already in net base above for IMPORT VAT)
                if import_type != 'IMPORT':
                    line_total_payment_vnd = line_net_vnd + line_vat_vnd + line_extra_vnd
                else:
                    line_total_payment_vnd = line_net_vnd + line_import_tax_vnd + line_vat_vnd + line_extra_vnd

                b_type = _business_type_for_line(line_type)
                desc_label = BUSINESS_TYPE_LABELS.get(b_type or '', line_type)

                if line_type == 'service':
                    insert_import_detail_row(c, import_id, {
                        'import_id': import_id,
                        'product_id': None,
                        'qty': float(qty_in),
                        'buyprice': float(price_original),
                        'subtotal': float(line_subtotal_vnd),
                        'discount': float(line_disc_vnd),
                        'tax': float(line_vat_vnd),
                        'cost_price': float(line_total_payment_vnd / qty_in) if qty_in else 0,
                        'tax_pct': float(tax_p),
                        'discount_pct': float(disc_p),
                        'payment_amt': float(line_total_payment_vnd),
                        'product_name': inv_name or 'Dịch vụ',
                        'unit': unit_in,
                        'line_type': 'service',
                        'warehouse_code': warehouse_code,
                    })
                    items_for_json.append({
                        'product_name': inv_name or 'Dịch vụ',
                        'unit': unit_in,
                        'qty': float(qty_in),
                        'buyprice': float(price_original),
                        'discount_pct': float(disc_p),
                        'tax_pct': float(tax_p),
                        'line_type': 'service',
                        'invoice_product_type': 'service',
                        'warehouse_code': warehouse_code,
                        'line_total': float(line_total_payment_vnd),
                    })
                    continue

                pid = item.get('product_id')
                p_info = p_map.get(pid)
                if not p_info:
                    continue

                if inv_name:
                    try:
                        c.execute(
                            """
                            INSERT OR IGNORE INTO product_aliases (product_id, invoice_name, supplier_id)
                            VALUES (?, ?, ?)
                            """,
                            (pid, inv_name, supplier_id),
                        )
                    except sqlite3.Error:
                        pass

                product_name = p_info['name']
                retail_unit = str(p_info['unit'] or 'Cái').strip()
                wholesale_unit = str(p_info['unit1'] or '').strip().lower()
                ratio = Decimal(str(p_info['unit_ratio'] or 1))
                unit_in_lower = unit_in.lower()
                is_wholesale = bool(wholesale_unit and unit_in_lower == wholesale_unit)
                qty_retail = qty_in * ratio if is_wholesale else qty_in
                cost_per_retail = (
                    round_money(line_inventory_value_vnd / qty_retail)
                    if qty_retail > 0
                    else Decimal('0.00')
                )

                items_for_json.append({
                    'product_id': pid,
                    'product_name': product_name,
                    'barcode': p_info['barcode'] or '',
                    'product_code': p_info['product_code'] or '',
                    'unit': item.get('unit'),
                    'qty': float(qty_in),
                    'buyprice': float(price_original),
                    'discount_pct': float(disc_p),
                    'tax_pct': float(tax_p),
                    'import_tax_pct': float(import_tax_p),
                    'line_type': line_type,
                    'invoice_product_type': line_type,
                    'warehouse_code': warehouse_code,
                    'line_total': float(line_total_payment_vnd),
                })

                insert_import_detail_row(c, import_id, {
                    'import_id': import_id,
                    'product_id': pid,
                    'qty': float(qty_in),
                    'buyprice': float(price_original),
                    'subtotal': float(line_subtotal_vnd),
                    'discount': float(line_disc_vnd),
                    'tax': float(line_vat_vnd),
                    'cost_price': float(cost_per_retail),
                    'unit_type': 1 if is_wholesale else 0,
                    'tax_pct': float(tax_p),
                    'discount_pct': float(disc_p),
                    'payment_amt': float(line_total_payment_vnd),
                    'product_name': product_name,
                    'unit': unit_in,
                    'line_type': line_type,
                    'warehouse_code': warehouse_code,
                })
                detail_id = c.lastrowid

                if line_type == 'fixed_asset':
                    register_fixed_asset_from_import(
                        c,
                        import_id=import_id,
                        import_detail_id=detail_id,
                        product_id=pid,
                        product_code=p_info['product_code'] or '',
                        product_name=product_name,
                        import_no=import_no,
                        import_date=import_date,
                        warehouse_code=warehouse_code,
                        qty=float(qty_in),
                        buyprice=float(price_original),
                        tax_amount=float(line_vat_vnd),
                        discount_amount=float(line_disc_vnd),
                        line_total=float(line_total_payment_vnd),
                        subtotal=float(line_subtotal_vnd),
                    )
                    fixed_assets_created += 1
                elif line_type == 'tools':
                    register_tool_from_import(
                        c,
                        import_id=import_id,
                        import_detail_id=detail_id,
                        product_id=pid,
                        product_code=p_info['product_code'] or '',
                        product_name=product_name,
                        import_no=import_no,
                        import_date=import_date,
                        warehouse_code=warehouse_code,
                        qty=float(qty_in),
                        buyprice=float(price_original),
                        tax_amount=float(line_vat_vnd),
                        line_total=float(line_total_payment_vnd),
                        subtotal=float(line_subtotal_vnd),
                        discount_amount=float(line_disc_vnd),
                    )
                    tools_created += 1

                if tracks_retail_inventory(line_type):
                    params_detail = (
                        import_id, pid, float(qty_in), float(price_original), float(line_subtotal_vnd),
                        float(line_disc_vnd), float(line_vat_vnd), float(cost_per_retail),
                        1 if is_wholesale else 0, float(tax_p), float(disc_p),
                    )
                    try:
                        c.execute(
                            "INSERT INTO chi_tiet_phieu_nhap_kho "
                            "(import_id, product_id, quantity, buyprice, subtotal, discount_amount, "
                            "tax_amount, cost_price, unit_type, tax_pct, discount_pct) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            params_detail,
                        )
                    except sqlite3.Error:
                        pass

                    apply_wac_inbound(c, pid, float(qty_retail), float(line_inventory_value_vnd))
                    move_note = (
                        f"Nhập kho từ {supplier_name} ({desc_label}: {product_name}) — {warehouse_code}"
                    )
                    if sm_has_wh:
                        c.execute(
                            """
                            INSERT INTO stock_moves (
                                product_id, date, type, ref_id, quantity, cost_price, note,
                                ref_document, ref_type, type1, unit, unit1, unit_ratio, warehouse_code
                            ) VALUES (?, ?, 'import', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                pid, import_date, import_id, float(qty_retail), float(cost_per_retail),
                                move_note, import_no, 'import', 'Nhập', retail_unit, wholesale_unit,
                                float(ratio), warehouse_code,
                            ),
                        )
                    else:
                        c.execute(
                            """
                            INSERT INTO stock_moves (
                                product_id, date, type, ref_id, quantity, cost_price, note,
                                ref_document, ref_type, type1, unit, unit1, unit_ratio
                            ) VALUES (?, ?, 'import', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                pid, import_date, import_id, float(qty_retail), float(cost_per_retail),
                                move_note, import_no, 'import', 'Nhập', retail_unit, wholesale_unit,
                                float(ratio),
                            ),
                        )
                    try:
                        c.execute(
                            """
                            INSERT INTO inventory_transactions
                            (product_id, type, type1, quantity, cost_price, reference_id, reference_type, note, created_at)
                            VALUES (?, 'import', 'Nhập', ?, ?, ?, 'import', ?, ?)
                            """,
                            (
                                pid, float(qty_retail), float(cost_per_retail), import_id,
                                f"Nhập kho - PN#{import_no} ({warehouse_code})", import_date,
                            ),
                        )
                    except sqlite3.Error:
                        pass
                    sync_inventory_quantity_from_moves(c, pid)

            if not items_for_json:
                raise ValueError('Không có dòng hàng hợp lệ để lưu phiếu nhập.')

            total_overall_payment_vnd = sum(
                round_money(x.get('line_total') or 0) for x in items_for_json
            )
            total_final_float = float(total_overall_payment_vnd)
            final_paid = total_final_float if payment_status_input == 'Đã thanh toán' else 0.0
            c.execute(
                "UPDATE import SET total_value = ?, paid_amount = ? WHERE id = ?",
                (total_final_float, final_paid, import_id),
            )

            items_json_str = json.dumps(items_for_json, ensure_ascii=False)
            c.execute(
                """
                INSERT INTO phieu_nhap_kho (
                    import_no, date, bill_no, bill_date, supplier_name, supplier_id,
                    items_json, total_amount, import_id, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    import_no, import_date, bill_no, bill_date, supplier_name, supplier_id,
                    items_json_str, total_final_float, import_id, note,
                ),
            )

            journal_result = sync_import_journals(
                conn,
                import_id,
                accounting_regime='SME',
                created_by=(session.get('user') or {}).get('username'),
                replace_existing=False,
                payment_method=payment_method,
            )
            accounting_tx_ids = list(journal_result.get('entry_ids') or [])

            po_result = None
            if po_id:
                from Services.sme.purchase_order import apply_po_receipt
                receipt_lines = []
                for i in items:
                    receipt_lines.append({
                        'product_id': i.get('product_id'),
                        'product_name': i.get('name') or i.get('invoice_name'),
                        'qty': i.get('qty'),
                    })
                po_result = apply_po_receipt(
                    conn, po_id, receipt_lines, import_id=import_id,
                )

            conn.commit()
            return jsonify({
                "success": True,
                "import_id": import_id,
                "voucher_no": import_no,
                "total_payment_vnd": total_final_float,
                "journal_entry_ids": accounting_tx_ids,
                "accounting_tx_ids": accounting_tx_ids,
                "journal_sync": journal_result,
                "fixed_assets_created": fixed_assets_created,
                "tools_created": tools_created,
                "purchase_order": po_result,
                "currency": currency,
                "tax_code": tax_code,
            })

        except Exception as e:
            conn.rollback()
            logging.error(f"LỖI import_sme: {str(e)}", exc_info=True)
            return jsonify({"error": f"Lỗi xử lý: {str(e)}"}), 500
        finally:
            conn.close()

'''

p.write_text(t[:start] + new_fn + t[end:], encoding="utf-8")
print("api_import_sme replaced, bytes", len(new_fn))
