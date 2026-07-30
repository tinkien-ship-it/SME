# -*- coding: utf-8 -*-
"""Mẫu chứng từ bán hàng TT99: 01-BH (đại lý/ký gửi) và 02-BH (thẻ quầy)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return bool(row)


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _company(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        row = conn.execute("SELECT * FROM business_info LIMIT 1").fetchone()
    except Exception:
        row = None
    if not row:
        return {"business_name": "", "address": "", "tax_code": "", "representative_name": ""}
    d = dict(row)
    return {
        "business_name": (d.get("business_name") or "").strip(),
        "address": (d.get("address") or "").strip(),
        "tax_code": (d.get("tax_code") or "").strip(),
        "representative_name": (d.get("representative_name") or "").strip(),
    }


def list_sale_customers(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict[str, Any]]:
    if not _table_exists(conn, "sale"):
        return []
    rows = conn.execute(
        """
        SELECT TRIM(customer_name) AS name, COUNT(*) AS sale_count,
               COALESCE(SUM(total_amount),0) AS total_amount
        FROM sale
        WHERE customer_name IS NOT NULL AND TRIM(customer_name) != ''
          AND LOWER(TRIM(customer_name)) NOT IN ('khách lẻ', 'khach le', 'retail')
        GROUP BY TRIM(customer_name)
        ORDER BY sale_count DESC, name ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "name": r[0] if not isinstance(r, sqlite3.Row) else r["name"],
            "sale_count": int((r[1] if not isinstance(r, sqlite3.Row) else r["sale_count"]) or 0),
            "total_amount": _f(r[2] if not isinstance(r, sqlite3.Row) else r["total_amount"]),
        }
        for r in rows
    ]


def list_products_brief(conn: sqlite3.Connection, *, limit: int = 500) -> list[dict[str, Any]]:
    if not _table_exists(conn, "products"):
        return []
    has_inv = _table_exists(conn, "inventory")
    sql = """
        SELECT p.id, p.product_code, p.name, p.unit, COALESCE(p.price,0) AS price
        {inv}
        FROM products p
        {join}
        ORDER BY p.name
        LIMIT ?
    """.format(
        inv=", COALESCE(i.quantity,0) AS stock" if has_inv else ", 0 AS stock",
        join="LEFT JOIN inventory i ON i.product_id = p.id" if has_inv else "",
    )
    rows = conn.execute(sql, (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r) if isinstance(r, sqlite3.Row) else {
            "id": r[0], "product_code": r[1], "name": r[2], "unit": r[3], "price": r[4], "stock": r[5],
        }
        out.append({
            "id": d["id"],
            "product_code": d.get("product_code") or "",
            "name": d.get("name") or "",
            "unit": d.get("unit") or "Cái",
            "price": _f(d.get("price")),
            "stock": _f(d.get("stock")),
        })
    return out


def form_01_bh(
    conn: sqlite3.Connection,
    *,
    agent_name: str,
    date_from: str,
    date_to: str,
    contract_no: str = "",
    contract_date: str = "",
    opening_debt: float = 0.0,
    commission: float = 0.0,
    tax_paid_for: float = 0.0,
    other_cost: float = 0.0,
    paid_cash: float = 0.0,
    paid_cheque: float = 0.0,
) -> dict[str, Any]:
    """
    Bảng thanh toán hàng đại lý, ký gửi (01-BH).

    Nguồn: sale + sale_items theo khách hàng (đại lý) trong kỳ.
    Tồn đầu/cuối: ước lượng từ tồn hiện tại + xuất bán trong kỳ (không có sổ ký gửi riêng).
    """
    agent = (agent_name or "").strip()
    if not agent:
        raise ValueError("Chọn đại lý / khách hàng nhận bán")
    if not date_from or not date_to:
        raise ValueError("Thiếu khoảng ngày")

    lines: list[dict[str, Any]] = []
    if _table_exists(conn, "sale") and _table_exists(conn, "sale_items"):
        rows = conn.execute(
            """
            SELECT p.id AS product_id,
                   COALESCE(p.product_code,'') AS product_code,
                   COALESCE(p.name, 'SP #' || si.product_id) AS product_name,
                   COALESCE(p.unit, 'Cái') AS unit,
                   COALESCE(SUM(si.quantity),0) AS qty_sold,
                   COALESCE(AVG(si.price), COALESCE(p.price,0)) AS unit_price,
                   COALESCE(SUM(si.quantity * si.price),0) AS amount
            FROM sale_items si
            JOIN sale s ON s.id = si.sale_id
            LEFT JOIN products p ON p.id = si.product_id
            WHERE TRIM(COALESCE(s.customer_name,'')) = ?
              AND substr(s.date,1,10) >= ?
              AND substr(s.date,1,10) <= ?
              AND COALESCE(s.status,'') NOT IN ('cancelled','draft','void')
            GROUP BY si.product_id
            ORDER BY product_name
            """,
            (agent, date_from[:10], date_to[:10]),
        ).fetchall()
        has_inv = _table_exists(conn, "inventory")
        for r in rows:
            d = dict(r)
            pid = d.get("product_id")
            sold = _f(d.get("qty_sold"))
            stock = 0.0
            if has_inv and pid:
                inv = conn.execute(
                    "SELECT COALESCE(quantity,0) FROM inventory WHERE product_id=?",
                    (pid,),
                ).fetchone()
                stock = _f(inv[0] if inv else 0)
            # Ước lượng: tồn cuối ≈ tồn hiện tại; tồn đầu ≈ tồn cuối + đã bán trong kỳ
            closing = max(0.0, stock)
            opening = closing + sold
            received = 0.0  # không có phiếu giao đại lý riêng → để 0, user chỉnh tay khi in
            total = opening + received
            price = _f(d.get("unit_price"))
            amount = _f(d.get("amount")) or round(sold * price, 2)
            lines.append({
                "product_id": pid,
                "product_code": d.get("product_code") or "",
                "product_name": d.get("product_name") or "",
                "unit": d.get("unit") or "Cái",
                "qty_opening": opening,
                "qty_received": received,
                "qty_total": total,
                "qty_sold": sold,
                "unit_price": price,
                "amount": amount,
                "qty_closing": max(0.0, total - sold),
            })

    sold_amount = sum(_f(x["amount"]) for x in lines)
    section_ii = _f(opening_debt)
    section_iii = section_ii + sold_amount
    section_iv = _f(commission) + _f(tax_paid_for) + _f(other_cost)
    section_v = _f(paid_cash) + _f(paid_cheque)
    section_vi = section_iii - section_iv - section_v

    return {
        "form": "01-BH",
        "title": "Bảng thanh toán hàng đại lý, ký gửi",
        "company": _company(conn),
        "agent_name": agent,
        "date_from": date_from[:10],
        "date_to": date_to[:10],
        "contract_no": contract_no or "",
        "contract_date": contract_date or "",
        "lines": lines,
        "totals": {
            "sold_amount": sold_amount,
            "section_ii": section_ii,
            "section_iii": section_iii,
            "commission": _f(commission),
            "tax_paid_for": _f(tax_paid_for),
            "other_cost": _f(other_cost),
            "section_iv": section_iv,
            "paid_cash": _f(paid_cash),
            "paid_cheque": _f(paid_cheque),
            "section_v": section_v,
            "section_vi": section_vi,
        },
        "notes": [
            "Mẫu số 01-BH theo TT99/2025/TT-BTC — lập từ doanh số bán theo khách hàng đại lý.",
            "Cột nhận trong kỳ mặc định 0 nếu chưa có phiếu giao hàng đại lý riêng; có thể điền tay trước khi in.",
            "Tồn đầu/cuối ước lượng từ tồn kho hiện tại + số lượng đã bán trong kỳ.",
        ],
        "printed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def form_02_bh(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    """Thẻ quầy hàng (02-BH) — theo dõi nhập/xuất/tồn theo ngày từ stock_moves / sale."""
    if not product_id:
        raise ValueError("Chọn mặt hàng")
    if not date_from or not date_to:
        raise ValueError("Thiếu khoảng ngày")

    prod = None
    if _table_exists(conn, "products"):
        row = conn.execute(
            "SELECT id, product_code, name, unit, COALESCE(price,0) AS price FROM products WHERE id=?",
            (product_id,),
        ).fetchone()
        if row:
            prod = dict(row)

    if not prod:
        raise ValueError("Không tìm thấy sản phẩm")

    # Opening balance before date_from
    opening = 0.0
    if _table_exists(conn, "stock_moves"):
        row = conn.execute(
            """
            SELECT COALESCE(SUM(in_quantity),0) - COALESCE(SUM(out_quantity),0)
            FROM stock_moves
            WHERE product_id=? AND substr(date,1,10) < ?
            """,
            (product_id, date_from[:10]),
        ).fetchone()
        opening = _f(row[0] if row else 0)
    elif _table_exists(conn, "inventory"):
        row = conn.execute(
            "SELECT COALESCE(quantity,0) FROM inventory WHERE product_id=?",
            (product_id,),
        ).fetchone()
        opening = _f(row[0] if row else 0)

    days: list[dict[str, Any]] = []
    bal = opening

    if _table_exists(conn, "stock_moves"):
        rows = conn.execute(
            """
            SELECT substr(date,1,10) AS d,
                   COALESCE(SUM(in_quantity),0) AS qty_in,
                   COALESCE(SUM(out_quantity),0) AS qty_out,
                   GROUP_CONCAT(DISTINCT type) AS types
            FROM stock_moves
            WHERE product_id=?
              AND substr(date,1,10) >= ?
              AND substr(date,1,10) <= ?
            GROUP BY substr(date,1,10)
            ORDER BY d
            """,
            (product_id, date_from[:10], date_to[:10]),
        ).fetchall()
        for r in rows:
            d = dict(r)
            qty_in = _f(d.get("qty_in"))
            qty_out = _f(d.get("qty_out"))
            # Phân loại nhập kho vs khác / xuất bán vs khác thô
            types = (d.get("types") or "").upper()
            in_wh = qty_in if "IMPORT" in types or "ADJUST" in types else qty_in
            in_other = 0.0
            out_sale = qty_out
            out_other = 0.0
            if "RETURN" in types and qty_in:
                in_other = qty_in
                in_wh = 0.0
            start = bal
            total = start + in_wh + in_other
            end = total - out_sale - out_other
            price = _f(prod.get("price"))
            days.append({
                "date": d.get("d"),
                "seller": "",
                "qty_open": start,
                "qty_in_wh": in_wh,
                "qty_in_other": in_other,
                "qty_total": total,
                "qty_out_sale": out_sale,
                "amt_out_sale": round(out_sale * price, 2),
                "qty_out_other": out_other,
                "amt_out_other": 0.0,
                "qty_close": end,
            })
            bal = end
    elif _table_exists(conn, "sale") and _table_exists(conn, "sale_items"):
        # Fallback: chỉ có xuất bán theo ngày
        rows = conn.execute(
            """
            SELECT substr(s.date,1,10) AS d,
                   COALESCE(SUM(si.quantity),0) AS qty_out,
                   COALESCE(SUM(si.quantity * si.price),0) AS amt
            FROM sale_items si
            JOIN sale s ON s.id = si.sale_id
            WHERE si.product_id=?
              AND substr(s.date,1,10) >= ? AND substr(s.date,1,10) <= ?
              AND COALESCE(s.status,'') NOT IN ('cancelled','draft','void')
            GROUP BY substr(s.date,1,10)
            ORDER BY d
            """,
            (product_id, date_from[:10], date_to[:10]),
        ).fetchall()
        for r in rows:
            d = dict(r)
            qty_out = _f(d.get("qty_out"))
            start = bal
            total = start
            end = total - qty_out
            days.append({
                "date": d.get("d"),
                "seller": "",
                "qty_open": start,
                "qty_in_wh": 0.0,
                "qty_in_other": 0.0,
                "qty_total": total,
                "qty_out_sale": qty_out,
                "amt_out_sale": _f(d.get("amt")),
                "qty_out_other": 0.0,
                "amt_out_other": 0.0,
                "qty_close": end,
            })
            bal = end

    return {
        "form": "02-BH",
        "title": "Thẻ quầy hàng",
        "company": _company(conn),
        "product": {
            "id": prod["id"],
            "product_code": prod.get("product_code") or "",
            "name": prod.get("name") or "",
            "unit": prod.get("unit") or "Cái",
            "price": _f(prod.get("price")),
        },
        "date_from": date_from[:10],
        "date_to": date_to[:10],
        "opening_qty": opening,
        "closing_qty": bal,
        "days": days,
        "notes": [
            "Mẫu số 02-BH theo TT99/2025/TT-BTC — lập từ stock_moves / bán hàng.",
            "Tồn đầu kỳ = phát sinh nhập−xuất trước ngày bắt đầu.",
        ],
        "printed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
