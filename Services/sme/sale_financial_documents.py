"""Reconcile chứng từ liên quan đến sale completed cho SME.

Không ghi vào bảng HKD phieu_thu.
Phiếu Thu SME phải nằm trong sme_vouchers (01-TT).

Luồng:
- 111/112: đảm bảo SME receipt voucher (document-only, không post journal lần hai).
- 131: không tạo Phiếu Thu cho đến khi thực thu.
- Hàng có xuất kho: đảm bảo Phiếu Xuất Kho 02-VT theo sale_id.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from db.dialect import table_exists
from Services.sme.payment_method import normalize_sale_payment_method
from Services.sme.vouchers import ensure_sale_receipt_voucher
from Services.sme.stock_vouchers import upsert_stock_out_voucher_for_sale

logger = logging.getLogger(__name__)


def _row_dict(row) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return {}


def _sale_items_for_stock_voucher(
    conn,
    sale_id: int,
) -> list[dict[str, Any]]:
    """Lấy dòng hàng của sale để bù PXK 02-VT.

    Chỉ lấy hàng thực sự có stock_move SALE/SALE_RECIPE để tránh tạo PXK
    cho dịch vụ thuần.
    """
    try:
        rows = conn.execute(
            """
            SELECT
                si.product_id,
                COALESCE(p.name, '') AS product_name,
                COALESCE(p.barcode, '') AS product_code,
                COALESCE(p.unit, '') AS unit,
                COALESCE(si.quantity, 0) AS quantity,
                COALESCE(si.price, 0) AS price
            FROM sale_items si
            LEFT JOIN products p ON p.id = si.product_id
            WHERE si.sale_id = ?
              AND EXISTS (
                  SELECT 1
                  FROM stock_moves sm
                  WHERE sm.ref_id = ?
                    AND sm.product_id = si.product_id
                    AND UPPER(COALESCE(sm.type, '')) IN (
                        'SALE',
                        'SALE_RECIPE'
                    )
              )
            ORDER BY si.id
            """,
            (sale_id, sale_id),
        ).fetchall()
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    for row in rows:
        d = _row_dict(row)
        qty = float(d.get("quantity") or 0)
        price = float(d.get("price") or 0)
        if qty <= 0:
            continue
        items.append(
            {
                "product_id": d.get("product_id"),
                "product_name": d.get("product_name") or "",
                "product_code": d.get("product_code") or "",
                "unit": d.get("unit") or "",
                "quantity": qty,
                "qty": qty,
                "price": price,
                "amount": qty * price,
            }
        )
    return items


def ensure_sale_related_documents(
    conn,
    sale_id: int,
    *,
    created_by: str | None = None,
) -> dict[str, Any]:
    sale = _row_dict(
        conn.execute(
            "SELECT * FROM sale WHERE id = ?",
            (int(sale_id),),
        ).fetchone()
    )
    if not sale:
        raise ValueError(f"Không tìm thấy sale #{sale_id}")

    if str(sale.get("status") or "").strip().lower() != "completed":
        return {
            "sale_id": int(sale_id),
            "created": [],
            "existing": [],
            "skipped": ["sale_not_completed"],
            "errors": [],
        }

    created: list[str] = []
    existing: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    payment_code = normalize_sale_payment_method(
        sale.get("payment_method"),
    )

    # 1) Phiếu Thu SME 01-TT cho khoản đã thực thu.
    if payment_code in ("111", "112"):
        try:
            receipt = ensure_sale_receipt_voucher(
                conn,
                int(sale_id),
                created_by=created_by,
                commit=False,
            )
            if receipt.get("created"):
                created.append("sme_receipt")
            elif receipt.get("existing"):
                existing.append("sme_receipt")
            elif receipt.get("reason"):
                skipped.append(
                    f"sme_receipt:{receipt['reason']}"
                )
        except Exception as exc:
            errors.append(f"sme_receipt:{exc}")
            logger.warning(
                "sale integrity receipt sale=%s: %s",
                sale_id,
                exc,
                exc_info=True,
            )
    elif payment_code == "131":
        skipped.append("sme_receipt:credit_sale")

    # 2) Phiếu Xuất Kho 02-VT nếu sale có hàng xuất kho.
    try:
        items = _sale_items_for_stock_voucher(
            conn,
            int(sale_id),
        )
        if items:
            existed_px = False
            if table_exists(conn, "phieu_xuat_kho"):
                existed_px = bool(
                    conn.execute(
                        """
                        SELECT 1
                        FROM phieu_xuat_kho
                        WHERE sale_id = ?
                        LIMIT 1
                        """,
                        (int(sale_id),),
                    ).fetchone()
                )

            upsert_stock_out_voucher_for_sale(
                conn,
                sale_id=int(sale_id),
                sale_date=str(
                    sale.get("date")
                    or sale.get("created_at")
                    or ""
                )[:10],
                customer_name=str(
                    sale.get("customer_name")
                    or sale.get("company_name")
                    or ""
                ),
                items=items,
                total_amount=float(
                    sale.get("total_amount") or 0
                ),
                note=f"Xuất kho bán hàng {sale.get('sale_no') or sale_id}",
                address=str(sale.get("address") or ""),
                reuse_voucher_no=True,
            )

            if existed_px:
                existing.append("stock_out_voucher")
            else:
                created.append("stock_out_voucher")
        else:
            skipped.append("stock_out_voucher:no_stock_items")
    except Exception as exc:
        errors.append(f"stock_out_voucher:{exc}")
        logger.warning(
            "sale integrity stock voucher sale=%s: %s",
            sale_id,
            exc,
            exc_info=True,
        )

    return {
        "sale_id": int(sale_id),
        "payment_method": payment_code,
        "created": created,
        "existing": existing,
        "skipped": skipped,
        "errors": errors,
    }


def reconcile_completed_sale_documents(
    conn,
    *,
    batch_size: int = 100,
    created_by: str = "scheduler_reconcile",
) -> dict[str, Any]:
    """Quét sale completed và bù chứng từ SME còn thiếu."""
    try:
        batch_size = max(
            1,
            min(int(batch_size or 100), 1000),
        )
    except (TypeError, ValueError):
        batch_size = 100

    if not table_exists(conn, "sale"):
        return {
            "scanned": 0,
            "created": 0,
            "existing": 0,
            "errors": 0,
            "details": [],
            "reason": "sale_table_missing",
        }

    rows = conn.execute(
        """
        SELECT id
        FROM sale
        WHERE LOWER(TRIM(COALESCE(status, ''))) = 'completed'
        ORDER BY id ASC
        LIMIT ?
        """,
        (batch_size,),
    ).fetchall()

    total_created = 0
    total_existing = 0
    total_errors = 0
    details: list[dict[str, Any]] = []

    for row in rows:
        sale_id = int(
            row["id"]
            if hasattr(row, "keys")
            else row[0]
        )

        result = ensure_sale_related_documents(
            conn,
            sale_id,
            created_by=created_by,
        )

        total_created += len(result.get("created") or [])
        total_existing += len(result.get("existing") or [])
        total_errors += len(result.get("errors") or [])

        if (
            result.get("created")
            or result.get("errors")
            or result.get("skipped")
        ):
            details.append(result)

    conn.commit()

    return {
        "scanned": len(rows),
        "created": total_created,
        "existing": total_existing,
        "errors": total_errors,
        "details": details[:100],
    }
