# -*- coding: utf-8 -*-
from pathlib import Path

p = Path(r"C:\SME\templates\KeToanSME\import_sme.html")
text = p.read_text(encoding="utf-8")
marker = "// ====================== GET CONSOLIDATED ITEMS"
idx = text.find(marker)
if idx < 0:
    raise SystemExit("marker not found")

new_tail = r'''function getConsolidatedItems() {
    const rows = document.querySelectorAll('#itemsTable tbody tr');
    const consolidatedMap = new Map();

    rows.forEach(tr => {
        const name = tr.querySelector('.product-name').value.trim();
        if (!name) return;

        const lineType = normalizeLineType(tr.querySelector('.line-type-select')?.value);
        const wh = (tr.querySelector('.warehouse-select')?.value || '').trim();
        const key = `${name.toLowerCase()}|${lineType}|${wh}`;

        const itemData = {
            product_id: parseInt(tr.querySelector('.p-id').value) || null,
            name: name,
            invoice_name: tr.querySelector('.invoice-name-hidden').value.trim() || name,
            line_type: lineType,
            invoice_product_type: lineType,
            warehouse_code: wh || null,
            unit: tr.querySelector('.invoice-unit').value.trim(),
            qty: parseNumber(tr.querySelector('.qty').value),
            buyprice: parseNumber(tr.querySelector('.buy-price').value),
            tax_pct: parseFloat(tr.querySelector('.tax-pct').value) || 0,
            discount_pct: parseFloat(tr.querySelector('.disc-pct').value) || 0,
            base_unit: tr.querySelector('.base-unit').value.trim(),
            wholesale_unit: tr.querySelector('.wholesale-unit').value.trim(),
            ratio: parseFloat(tr.querySelector('.ratio').value) || 1,
            base_sale_price: parseNumber(tr.querySelector('.base-sale-price').value),
            sale_price: parseNumber(tr.querySelector('.sale-price').value)
        };

        if (consolidatedMap.has(key)) {
            const exist = consolidatedMap.get(key);
            exist.qty += itemData.qty;
            exist.buyprice = itemData.buyprice;
        } else {
            consolidatedMap.set(key, itemData);
        }
    });

    return Array.from(consolidatedMap.values());
}

document.getElementById('btnSaveImport').addEventListener('click', async function(e) {
    e.preventDefault();
    const btn = this;

    const paymentStatus = document.getElementById('paymentStatus').value;
    const paymentMethod = document.getElementById('paymentMethod').value;
    const totalAmount = parseNumber(document.getElementById('totalImport').textContent);

    if (paymentStatus === 'Đã thanh toán') {
        await loadQuySoDu();
        const check = checkSoDu(totalAmount, paymentMethod);
        if (!check.ok) {
            return Swal.fire('Không đủ tiền', check.message, 'warning');
        }
    }

    const confirmResult = await Swal.fire({
        title: 'Xác nhận lưu phiếu nhập mua',
        html: 'Kiểm tra <b>Loại hàng</b>, <b>Kho</b> (HH/VT) và định khoản trước khi lưu.',
        icon: 'question',
        showCancelButton: true,
        confirmButtonText: 'Lưu phiếu',
        cancelButtonText: 'Hủy',
        confirmButtonColor: '#2563eb'
    });

    if (!confirmResult.isConfirmed) return;

    try {
        const items = getConsolidatedItems();
        if (items.length === 0) throw new Error('Vui lòng nhập ít nhất một mặt hàng!');

        for (const it of items) {
            if (needsWarehouse(it.line_type) && !(it.warehouse_code || '').trim()) {
                throw new Error(`Dòng "${it.name}" (${LINE_TYPE_LABEL[it.line_type] || it.line_type}) bắt buộc chọn kho.`);
            }
        }

        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Đang lưu...';

        for (const it of items) {
            if (it.line_type === 'service') continue;
            const upsertRes = await fetch('/api/products/upsert', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    id: it.product_id || null,
                    name: it.name,
                    invoice_name: it.invoice_name || it.name,
                    unit: it.unit || 'Cái',
                    buyprice: it.buyprice,
                    sale_price: it.sale_price || 0,
                    base_unit: it.base_unit || it.unit || 'Cái',
                    wholesale_unit: it.wholesale_unit || '',
                    conversion_ratio: it.ratio || 1,
                    base_sale_price: it.base_sale_price || 0,
                    product_type: it.line_type,
                    tax_rate: it.tax_pct || 0
                })
            });
            const upsertData = await upsertRes.json();
            if (!upsertData.success) {
                throw new Error(upsertData.error || `Không lưu được sản phẩm: ${it.name}`);
            }
            it.product_id = upsertData.id;
            if (upsertData.code) it.product_code = upsertData.code;
        }

        const payload = {
            import_no: document.getElementById('import_no').value,
            supplier_id: parseInt(document.getElementById('supplierId').value) || null,
            date: document.getElementById('importDate').value.split('/').reverse().join('-'),
            bill_no: document.getElementById('bill_no').value,
            bill_date: document.getElementById('bill_date').value
                ? document.getElementById('bill_date').value.split('/').reverse().join('-')
                : null,
            note: document.getElementById('note').value || '',
            extra_cost: parseNumber(document.getElementById('extraCost').value),
            from_invoice_id: parseInt(document.getElementById('fromInvoiceId')?.value) || null,
            po_id: parseInt(document.getElementById('fromPoId')?.value) || null,
            po_no: document.getElementById('fromPoNo')?.value || null,
            payment_status: paymentStatus,
            payment_method: (paymentStatus === 'Đã thanh toán')
                ? (paymentMethod === 'cash' ? 'cash' : 'bank')
                : null,
            items: items
        };

        const res = await fetch('/api/import_sme', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const result = await res.json();

        if (result.success) {
            const parts = [];
            if (result.purchase_order) {
                parts.push(`ĐĐH ${result.purchase_order.po_no || ''} → ${result.purchase_order.status}`);
            }
            if (result.fixed_assets_created) parts.push(`TSCĐ: ${result.fixed_assets_created}`);
            if (result.tools_created) parts.push(`CCDC: ${result.tools_created}`);
            toast('Lưu phiếu nhập mua thành công!' + (parts.length ? ' · ' + parts.join(' · ') : ''), 'success');
            setTimeout(() => {
                if (result.purchase_order && result.purchase_order.id) {
                    location.href = '/SME_purchase_order_list';
                } else {
                    location.reload();
                }
            }, 1500);
        } else {
            throw new Error(result.error || result.message || 'Lưu thất bại');
        }

    } catch (err) {
        console.error(err);
        toast(err.message, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<i class="bi bi-floppy-fill"></i> LƯU PHIẾU NHẬP`;
    }
});

function togglePaymentMethod() {
    const status = document.getElementById('paymentStatus').value;
    document.getElementById('methodWrapper').classList.toggle('hidden-field', status === 'Chưa thanh toán');
    if (status !== 'Chưa thanh toán') loadQuySoDu();
}
</script>
{% endblock %}
'''

p.write_text(text[:idx] + new_tail, encoding="utf-8")
print("patched ok, new len", len(text[:idx] + new_tail))
