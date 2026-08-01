# -*- coding: utf-8 -*-
"""Patch import_sme.html: LINE_TYPE_LABEL + upsert payload/response."""
from pathlib import Path

p = Path(r"C:\SME\templates\KeToanSME\import_sme.html")
t = p.read_text(encoding="utf-8")

old_label_anchor = """const DEFAULT_LINE_TYPE = 'goods';
const NEXT_CODE_API = '/api/products/next-code';
"""
new_label_anchor = """const DEFAULT_LINE_TYPE = 'goods';
const LINE_TYPE_LABEL = Object.fromEntries(LINE_TYPE_OPTIONS.map(o => [o.value, o.label]));
const NEXT_CODE_API = '/api/products/next-code';
"""
if "LINE_TYPE_LABEL =" not in t:
    if old_label_anchor not in t:
        raise SystemExit("label anchor missing")
    t = t.replace(old_label_anchor, new_label_anchor, 1)

old_upsert = """        for (const it of items) {
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
"""

new_upsert = """        for (const it of items) {
            if (it.line_type === 'service') continue;
            const upsertRes = await fetch('/api/products/upsert', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    id: it.product_id || null,
                    name: it.name,
                    unit: it.base_unit || it.unit || 'Cái',
                    base_price: it.base_sale_price || 0,
                    buyprice: it.buyprice,
                    unit1: it.wholesale_unit || null,
                    unit_ratio: it.ratio || 1,
                    price: it.sale_price || 0,
                    product_type: it.line_type || 'goods'
                })
            });
            const upsertData = await upsertRes.json();
            if (!upsertData.success) {
                throw new Error(upsertData.error || `Không lưu được sản phẩm: ${it.name}`);
            }
            it.product_id = upsertData.product?.id || upsertData.id;
            if (upsertData.product?.product_code) it.product_code = upsertData.product.product_code;
        }
"""

if old_upsert not in t:
    raise SystemExit("upsert block not found")
t = t.replace(old_upsert, new_upsert, 1)
p.write_text(t, encoding="utf-8")
print("import_sme.html patched")
