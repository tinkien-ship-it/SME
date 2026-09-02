# -*- coding: utf-8 -*-
"""Hoàn tất đơn bán từ CRM (báo giá / hợp đồng) — kho + sổ như POS SME.

HKD: caller có thể chỉ tạo đơn pending; fulfill vẫn hỗ trợ trừ kho + phiếu xuất,
nhưng ghi sổ SME chỉ chạy khi regime = sme.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def _map_payment_method(raw: str | None) -> str:
    s = (raw or '').strip().lower()
    if not s:
        return '131'
    if s in ('111', '112', '131'):
        return s
    if 'tiền mặt' in s or 'tien mat' in s or s in ('tm', 'cash'):
        return '111'
    if 'chuyển khoản' in s or 'chuyen khoan' in s or s in ('ck', 'bank', 'transfer'):
        return '112'
    if 'công nợ' in s or 'cong no' in s or 'debt' in s or 'credit' in s:
        return '131'
    return '131'


def fulfill_sale_like_pos(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    payment_method: str | None = None,
    created_by: str | None = None,
    accounting_regime: str | None = None,
    features: dict | None = None,
    warehouse_codes: list[str] | None = None,
) -> dict[str, Any]:
    """
    Đưa sale pending/draft → completed: trừ kho, phiếu xuất, ghi sổ SME (nếu có).

    Idempotent: sale đã completed thì chỉ đảm bảo accounting (không trừ kho lần 2).
    """
    from Services.hkd_sector import requires_stock_check
    from Services.sale_helpers import deduct_inventory_for_sale, fetch_product_for_checkout
    from Services.tenant_profile import is_sme_regime

    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    sale = cur.execute('SELECT * FROM sale WHERE id = ?', (int(sale_id),)).fetchone()
    if not sale:
        raise ValueError('Không tìm thấy đơn bán')
    sale = dict(sale)
    status = str(sale.get('status') or '').strip().lower()
    sale_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    ref_doc = (sale.get('sale_no') or '').strip() or f"ĐH{str(sale_id).zfill(6)}"
    pay = _map_payment_method(payment_method or sale.get('payment_method'))

    out: dict[str, Any] = {
        'sale_id': int(sale_id),
        'sale_no': ref_doc,
        'status': status,
        'stock_deducted': False,
        'accounting': None,
        'already_completed': status == 'completed',
    }

    if status == 'completed':
        if is_sme_regime(accounting_regime):
            out['accounting'] = _post_sale_accounting(
                conn, int(sale_id),
                accounting_regime=accounting_regime,
                features=features,
                created_by=created_by,
                replace_existing=False,
            )
        return out

    if status in ('cancelled', 'deleted', 'void'):
        raise ValueError('Đơn đã hủy — không hoàn tất được')

    items = cur.execute(
        'SELECT * FROM sale_items WHERE sale_id = ?', (int(sale_id),)
    ).fetchall()
    if not items:
        raise ValueError('Đơn bán chưa có dòng hàng')

    # Gán sale_no + payment + completed
    cur.execute(
        """
        UPDATE sale SET
            status = 'completed',
            sale_no = ?,
            payment_method = COALESCE(NULLIF(?, ''), payment_method),
            date = COALESCE(NULLIF(date, ''), ?)
        WHERE id = ?
        """,
        (ref_doc, pay, sale_date, int(sale_id)),
    )
    # Đồng bộ ngày nếu trống
    if not (sale.get('date') or '').strip():
        cur.execute('UPDATE sale SET date = ? WHERE id = ?', (sale_date, int(sale_id)))

    px_items: list[dict] = []
    for row in items:
        it = dict(row)
        pid = it.get('product_id')
        if not pid:
            continue
        qty = float(it.get('quantity') or 0)
        if qty <= 0:
            continue
        prod = fetch_product_for_checkout(cur, int(pid), warehouse_codes=warehouse_codes)
        if not prod:
            raise ValueError(f'Sản phẩm ID {pid} không tồn tại')
        prod = dict(prod)
        product_type = prod.get('product_type') or 'goods'
        if not requires_stock_check(product_type):
            continue
        use_unit1 = bool(int(it.get('UseSaleUnit') or 0))
        ratio = float(prod.get('unit_ratio') or 1) or 1.0
        deduct_qty = qty * ratio if use_unit1 else qty
        stock = float(prod.get('stock') or 0)
        if stock + 1e-9 < deduct_qty:
            name = prod.get('name') or f'ID {pid}'
            raise ValueError(
                f'Không đủ tồn kho cho «{name}» (cần {deduct_qty:g}, còn {stock:g})'
            )
        avg_cost = float(prod.get('avg_cost') or it.get('cost_price') or 0)
        deduct_inventory_for_sale(
            cur, int(pid), deduct_qty, avg_cost, int(sale_id), sale_date, ref_doc,
        )
        px_items.append({
            'product_id': int(pid),
            'product_name': prod.get('name'),
            'unit': (prod.get('unit1') if use_unit1 else prod.get('unit')) or 'Cái',
            'quantity': qty,
            'price': float(it.get('price') or 0),
            'amount': qty * float(it.get('price') or 0),
        })

    out['stock_deducted'] = bool(px_items)

    if px_items:
        last_px = cur.execute(
            "SELECT voucher_no FROM phieu_xuat_kho WHERE voucher_no LIKE 'PX%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        px_num = 1
        if last_px:
            try:
                px_num = int(str(last_px[0] if not isinstance(last_px, sqlite3.Row) else last_px['voucher_no'])[2:]) + 1
            except ValueError:
                px_num = 1
        px_no = f'PX{px_num:06d}'
        total_amount = float(sale.get('total_amount') or 0)
        cur.execute(
            """
            INSERT INTO phieu_xuat_kho (voucher_no, date, customer_name, items_json, total_amount, sale_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                px_no,
                sale_date,
                sale.get('customer_name') or '',
                json.dumps(px_items, ensure_ascii=False),
                total_amount,
                int(sale_id),
            ),
        )
        out['export_voucher_no'] = px_no

    # CRM khách đã mua
    cid = sale.get('customer_id')
    if cid:
        try:
            from Services.crm import mark_customer_purchased
            # mark_customer_purchased expects cursor in some places — use SQL
            cur.execute(
                "UPDATE customers SET crm_lifecycle='active', crm_updated_at=? WHERE id=?",
                (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), int(cid)),
            )
        except Exception:
            pass

    out['status'] = 'completed'
    out['payment_method'] = pay

    if is_sme_regime(accounting_regime):
        out['accounting'] = _post_sale_accounting(
            conn, int(sale_id),
            accounting_regime=accounting_regime,
            features=features,
            created_by=created_by,
            replace_existing=False,
        )

    return out


def _post_sale_accounting(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    accounting_regime: str | None,
    features: dict | None,
    created_by: str | None,
    replace_existing: bool,
) -> dict:
    from Services.accounting_queue import ensure_sale_accounting_posted

    try:
        return ensure_sale_accounting_posted(
            conn,
            sale_id,
            accounting_regime=accounting_regime,
            features=features,
            created_by=created_by,
            replace_existing=replace_existing,
            sync_now=True,
        ) or {}
    except Exception as exc:
        logger.warning('CRM fulfill accounting sale %s: %s', sale_id, exc, exc_info=True)
        return {'posted': False, 'error': str(exc)}


def try_issue_einvoice(sale_id: int, *, loai_hdon: int = 1) -> dict[str, Any]:
    """Phát hành HĐĐT sau khi đơn completed — best-effort, không rollback đơn."""
    try:
        from flask import current_app
        fn = current_app.config.get('issue_invoice_for_sale')
        if not callable(fn):
            return {'success': False, 'error': 'Chưa đăng ký dịch vụ xuất HĐĐT'}
        return fn(int(sale_id), loai_hdon=int(loai_hdon or 1)) or {}
    except Exception as exc:
        logger.warning('try_issue_einvoice sale %s: %s', sale_id, exc, exc_info=True)
        return {'success': False, 'error': str(exc)}
