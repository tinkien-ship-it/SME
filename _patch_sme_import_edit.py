from pathlib import Path

# --- inventory.py: SME redirect on /import/edit ---
inv = Path(r"C:\SME\routes\inventory.py")
text = inv.read_text(encoding="utf-8")
needle = "    def import_edit(import_id):\n        conn = get_db_connection()"
repl = """    def import_edit(import_id):
        from Services.tenant_profile import get_current_tenant_profile, is_sme_regime

        profile = get_current_tenant_profile() or {}
        if is_sme_regime(profile.get('accounting_regime')):
            return redirect(url_for('SME_import', import_id=import_id))

        conn = get_db_connection()"""
if needle not in text:
    raise SystemExit("import_edit needle not found")
inv.write_text(text.replace(needle, repl, 1), encoding="utf-8")
print("inventory redirect OK")

# --- ketoan_sme.py: replace INSERT header with create/edit ---
sme = Path(r"C:\SME\routes\ketoan_sme.py")
text = sme.read_text(encoding="utf-8")

old_insert = '''            c.execute('PRAGMA table_info(import)')
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
            if 'import_type' in import_cols:
                import_fields.append('import_type')
                import_values.append(import_type)
            if 'currency' in import_cols:
                import_fields.append('currency')
                import_values.append(currency)
            if 'exchange_rate' in import_cols:
                import_fields.append('exchange_rate')
                import_values.append(float(exchange_rate))
            if 'import_tax_amount' in import_cols:
                import_fields.append('import_tax_amount')
                import_values.append(0)

            placeholders = ', '.join(['?'] * len(import_fields))
            c.execute(
                f'INSERT INTO import ({", ".join(import_fields)}) VALUES ({placeholders})',
                import_values,
            )
            import_id = c.lastrowid
'''

new_insert = '''            c.execute('PRAGMA table_info(import)')
            import_cols = {col[1] for col in c.fetchall()}
            pay_method_db = (
                'cash' if payment_method == 'CASH'
                else ('bank' if payment_method == 'BANK_TRANSFER' else None)
            )

            if edit_id:
                c.execute("SELECT * FROM import WHERE id = ?", (edit_id,))
                old_imp = c.fetchone()
                if not old_imp:
                    return jsonify({"error": f"Không tìm thấy phiếu nhập #{edit_id}"}), 404
                import_id = int(edit_id)
                import_no = old_imp['import_no'] or import_no

                c.execute(
                    "SELECT COUNT(*) AS cnt FROM return_import WHERE import_id = ?",
                    (import_id,),
                )
                if int(c.fetchone()['cnt'] or 0) > 0:
                    return jsonify({
                        "error": "Phiếu nhập đã phát sinh trả hàng NCC, không thể sửa.",
                    }), 403

                fa_act, tools_act = count_active_assets_by_import_id(c, import_id)
                if fa_act or tools_act:
                    return jsonify({
                        "error": "Không thể sửa: TSCĐ/CCDC từ phiếu nhập đã đưa vào sử dụng.",
                    }), 403

                # Chặn sửa nếu đã có xuất kho sau phiếu nhập (cùng SP)
                c.execute(
                    """
                    SELECT DISTINCT d.product_id
                    FROM import_details d
                    WHERE d.import_id = ? AND d.product_id IS NOT NULL
                    """,
                    (import_id,),
                )
                old_pids = [r['product_id'] for r in c.fetchall() if r['product_id']]
                if old_pids:
                    outbound_types = (
                        'export', 'sale', 'SALE', 'adjustment_out', 'ADJUSTMENT_OUT',
                        'RETURN_IMPORT',
                    )
                    ph_p = ','.join('?' * len(old_pids))
                    ph_t = ','.join('?' * len(outbound_types))
                    c.execute(
                        f"""
                        SELECT COUNT(*) AS cnt FROM stock_moves
                        WHERE product_id IN ({ph_p})
                          AND type IN ({ph_t})
                          AND date >= ?
                          AND NOT (type = 'import' AND ref_id = ?)
                        """,
                        (*old_pids, *outbound_types, str(old_imp['date'] or '')[:10], import_id),
                    )
                    if int((c.fetchone()['cnt'] or 0)) > 0:
                        return jsonify({
                            "error": "Không thể sửa: hàng đã phát sinh xuất/bán sau phiếu nhập này.",
                        }), 403

                sync_pids = set(old_pids)
                reverse_import_moves_wac(c, import_id)
                c.execute("DELETE FROM import_details WHERE import_id = ?", (import_id,))
                c.execute(
                    "DELETE FROM stock_moves WHERE ref_id = ? AND type IN ('import', 'RETURN_IMPORT')",
                    (import_id,),
                )
                c.execute(
                    "DELETE FROM inventory_transactions WHERE reference_id = ? AND reference_type = 'import'",
                    (import_id,),
                )
                try:
                    c.execute("DELETE FROM chi_tiet_phieu_nhap_kho WHERE import_id = ?", (import_id,))
                except sqlite3.Error:
                    pass
                delete_assets_by_import_id(c, import_id)
                if sync_pids:
                    sync_inventory_quantities(c, list(sync_pids))

                set_parts = [
                    'date = ?', 'supplier_id = ?', 'bill_no = ?', 'bill_date = ?',
                    'note = ?', 'payment_status = ?', 'extra_cost = ?',
                    'total_value = ?', 'paid_amount = ?',
                ]
                set_vals = [
                    import_date, supplier_id, bill_no, bill_date,
                    note, payment_status_input, float(extra_cost), 0, 0,
                ]
                if 'warehouse_code' in import_cols:
                    set_parts.append('warehouse_code = ?')
                    set_vals.append(default_warehouse)
                if 'from_invoice_id' in import_cols and from_invoice_id:
                    set_parts.append('from_invoice_id = ?')
                    set_vals.append(int(from_invoice_id))
                if 'payment_method' in import_cols:
                    set_parts.append('payment_method = ?')
                    set_vals.append(pay_method_db)
                if 'import_type' in import_cols:
                    set_parts.append('import_type = ?')
                    set_vals.append(import_type)
                if 'currency' in import_cols:
                    set_parts.append('currency = ?')
                    set_vals.append(currency)
                if 'exchange_rate' in import_cols:
                    set_parts.append('exchange_rate = ?')
                    set_vals.append(float(exchange_rate))
                if 'import_tax_amount' in import_cols:
                    set_parts.append('import_tax_amount = ?')
                    set_vals.append(0)
                set_vals.append(import_id)
                c.execute(
                    f"UPDATE import SET {', '.join(set_parts)} WHERE id = ?",
                    set_vals,
                )
            else:
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
                    import_values.append(pay_method_db)
                if 'import_type' in import_cols:
                    import_fields.append('import_type')
                    import_values.append(import_type)
                if 'currency' in import_cols:
                    import_fields.append('currency')
                    import_values.append(currency)
                if 'exchange_rate' in import_cols:
                    import_fields.append('exchange_rate')
                    import_values.append(float(exchange_rate))
                if 'import_tax_amount' in import_cols:
                    import_fields.append('import_tax_amount')
                    import_values.append(0)

                placeholders = ', '.join(['?'] * len(import_fields))
                c.execute(
                    f'INSERT INTO import ({", ".join(import_fields)}) VALUES ({placeholders})',
                    import_values,
                )
                import_id = c.lastrowid
'''

if old_insert not in text:
    raise SystemExit("INSERT block not found")
text = text.replace(old_insert, new_insert, 1)

# journals replace_existing
old_j = '''            journal_result = sync_import_journals(
                conn,
                import_id,
                accounting_regime='SME',
                created_by=(session.get('user') or {}).get('username'),
                replace_existing=False,
                payment_method=payment_method,
                import_type=import_type,
                import_tax_amount=total_import_tax_vnd,
                exchange_rate=exchange_rate,
            )'''
new_j = '''            journal_result = sync_import_journals(
                conn,
                import_id,
                accounting_regime='SME',
                created_by=(session.get('user') or {}).get('username'),
                replace_existing=bool(edit_id),
                payment_method=payment_method,
                import_type=import_type,
                import_tax_amount=total_import_tax_vnd,
                exchange_rate=exchange_rate,
            )'''
if old_j not in text:
    raise SystemExit("journal block not found")
text = text.replace(old_j, new_j, 1)

# phieu_nhap_kho: delete then insert on edit
old_pnk = '''            items_json_str = json.dumps(items_for_json, ensure_ascii=False)
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
            )'''
new_pnk = '''            items_json_str = json.dumps(items_for_json, ensure_ascii=False)
            if edit_id:
                c.execute("DELETE FROM phieu_nhap_kho WHERE import_id = ?", (import_id,))
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
            )'''
if old_pnk not in text:
    raise SystemExit("pnk block not found")
text = text.replace(old_pnk, new_pnk, 1)

# skip PO receipt on edit
old_po = '''            po_result = None
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
                )'''
new_po = '''            po_result = None
            if po_id and not edit_id:
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
                )'''
if old_po not in text:
    raise SystemExit("po block not found")
text = text.replace(old_po, new_po, 1)

# success payload include edited flag
old_ok = '''            conn.commit()
            return jsonify({
                "success": True,
                "import_id": import_id,
                "voucher_no": import_no,'''
new_ok = '''            conn.commit()
            return jsonify({
                "success": True,
                "edited": bool(edit_id),
                "import_id": import_id,
                "voucher_no": import_no,'''
if old_ok not in text:
    raise SystemExit("success block not found")
text = text.replace(old_ok, new_ok, 1)

sme.write_text(text, encoding="utf-8")
print("ketoan_sme edit flow OK")
