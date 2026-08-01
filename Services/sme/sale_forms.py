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


def list_products_brief(
    conn: sqlite3.Connection,
    *,
    limit: int = 500,
    branch_code: str | None = None,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "products"):
        return []
    has_inv = _table_exists(conn, "inventory")
    has_moves = _table_exists(conn, "stock_moves")
    sm_cols = (
        {r[1] for r in conn.execute("PRAGMA table_info(stock_moves)").fetchall()}
        if has_moves
        else set()
    )
    use_moves_stock = bool(
        branch_code
        and (branch_code or '').strip().upper() not in ('', 'ALL')
        and has_moves
        and 'warehouse_code' in sm_cols
    )

    if use_moves_stock:
        from Services.sme.branches import warehouse_branch_filter_sql
        wh_filter, wh_params = warehouse_branch_filter_sql(
            conn, branch_code, table='stock_moves', alias='sm',
        )
        sql = f"""
            SELECT p.id, p.product_code, p.name, p.unit, COALESCE(p.price,0) AS price,
                   COALESCE((
                       SELECT SUM(sm.quantity)
                       FROM stock_moves sm
                       WHERE sm.product_id = p.id
                       {wh_filter}
                   ), 0) AS stock
            FROM products p
            ORDER BY p.name
            LIMIT ?
        """
        rows = conn.execute(sql, [*wh_params, limit]).fetchall()
    else:
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


def ensure_agent_delivery_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_agent_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_code TEXT NOT NULL DEFAULT '01-BH',
            doc_no TEXT NOT NULL UNIQUE,
            delivery_date TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'posted',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_agent_delivery_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_id INTEGER NOT NULL,
            product_id INTEGER,
            product_code TEXT,
            product_name TEXT,
            unit TEXT,
            quantity REAL NOT NULL DEFAULT 0,
            unit_price REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(delivery_id) REFERENCES sme_agent_deliveries(id)
        )
        """
    )
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, 'sme_agent_deliveries')
    if commit:
        conn.commit()


def _next_delivery_no(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT doc_no FROM sme_agent_deliveries WHERE doc_no LIKE 'GDL%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return 'GDL000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['doc_no']
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'GDL{int(digits) + 1:06d}'


def create_agent_delivery(
    conn: sqlite3.Connection,
    *,
    agent_name: str,
    delivery_date: str,
    items: list[dict],
    notes: str = '',
    created_by: str | None = None,
    branch_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Phiếu giao hàng đại lý/ký gửi — nguồn cột «Nhận trong kỳ» của 01-BH."""
    ensure_agent_delivery_schema(conn, commit=False)
    agent = (agent_name or '').strip()
    date_s = str(delivery_date or '')[:10]
    if not agent or not date_s:
        raise ValueError('Thiếu đại lý / ngày giao')
    if not items:
        raise ValueError('Không có dòng hàng giao')
    doc_no = _next_delivery_no(conn)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_agent_deliveries
            (form_code, doc_no, delivery_date, agent_name, notes, status, created_by, created_at)
        VALUES ('01-BH', ?, ?, ?, ?, 'posted', ?, ?)
        """,
        (doc_no, date_s, agent, notes or '', created_by, datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
    )
    did = cur.lastrowid
    n = 0
    for raw in items:
        qty = _f(raw.get('quantity'))
        if qty <= 0:
            continue
        pid = int(raw.get('product_id') or 0) or None
        cur.execute(
            """
            INSERT INTO sme_agent_delivery_lines
                (delivery_id, product_id, product_code, product_name, unit, quantity, unit_price)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                did, pid,
                (raw.get('product_code') or '')[:64],
                (raw.get('product_name') or raw.get('name') or 'Hàng giao')[:255],
                (raw.get('unit') or 'Cái')[:32],
                qty, _f(raw.get('unit_price') or raw.get('price')),
            ),
        )
        n += 1
    if n == 0:
        conn.execute('DELETE FROM sme_agent_deliveries WHERE id = ?', (did,))
        raise ValueError('Không có dòng hợp lệ')
    from Services.sme.branch_filter import stamp_row_branch
    stamp_row_branch(conn, 'sme_agent_deliveries', did, branch_code=branch_code)
    if commit:
        conn.commit()
    return get_agent_delivery(conn, did)


def get_agent_delivery(conn: sqlite3.Connection, delivery_id: int) -> dict[str, Any] | None:
    ensure_agent_delivery_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_agent_deliveries WHERE id = ?', (delivery_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['lines'] = [dict(x) for x in conn.execute(
        'SELECT * FROM sme_agent_delivery_lines WHERE delivery_id = ? ORDER BY id',
        (delivery_id,),
    ).fetchall()]
    return d


def list_agent_deliveries(
    conn: sqlite3.Connection,
    *,
    agent_name: str | None = None,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_agent_delivery_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where
    bf, bp = branch_where(branch_code)
    sql = "SELECT * FROM sme_agent_deliveries WHERE status != 'void'"
    params: list[Any] = []
    if agent_name:
        sql += ' AND TRIM(agent_name) = ?'
        params.append(agent_name.strip())
    sql += bf
    params.extend(bp)
    sql += ' ORDER BY delivery_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def void_agent_delivery(
    conn: sqlite3.Connection,
    delivery_id: int,
    *,
    reason: str = 'Hủy phiếu giao đại lý',
    commit: bool = False,
) -> dict[str, Any]:
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_agent_deliveries', delivery_id, label='Phiếu giao đại lý')
    doc = get_agent_delivery(conn, delivery_id)
    if not doc:
        raise ValueError('Không tìm thấy phiếu giao')
    if doc.get('status') == 'void':
        raise ValueError('Đã hủy')
    conn.execute(
        "UPDATE sme_agent_deliveries SET status = 'void', notes = ? WHERE id = ?",
        ((doc.get('notes') or '') + f' | {reason}', delivery_id),
    )
    if commit:
        conn.commit()
    return get_agent_delivery(conn, delivery_id)


def _received_qty_map(
    conn: sqlite3.Connection,
    *,
    agent_name: str,
    date_from: str,
    date_to: str,
) -> dict[int | None, float]:
    """Tổng SL giao đại lý theo product_id trong kỳ (None = không gắn SP)."""
    ensure_agent_delivery_schema(conn, commit=False)
    rows = conn.execute(
        """
        SELECT l.product_id, COALESCE(SUM(l.quantity),0) AS qty
        FROM sme_agent_delivery_lines l
        JOIN sme_agent_deliveries d ON d.id = l.delivery_id
        WHERE d.status != 'void'
          AND TRIM(d.agent_name) = ?
          AND date(d.delivery_date) >= date(?)
          AND date(d.delivery_date) <= date(?)
        GROUP BY l.product_id
        """,
        (agent_name.strip(), date_from[:10], date_to[:10]),
    ).fetchall()
    out: dict[int | None, float] = {}
    for r in rows:
        d = dict(r)
        pid = d.get('product_id')
        out[int(pid) if pid is not None else None] = _f(d.get('qty'))
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
    Nhận trong kỳ: phiếu giao sme_agent_deliveries.
    Tồn đầu/cuối: ước lượng từ tồn hiện tại + xuất bán + nhận giao.
    """
    agent = (agent_name or "").strip()
    if not agent:
        raise ValueError("Chọn đại lý / khách hàng nhận bán")
    if not date_from or not date_to:
        raise ValueError("Thiếu khoảng ngày")

    received_map = _received_qty_map(
        conn, agent_name=agent, date_from=date_from, date_to=date_to,
    )
    lines: list[dict[str, Any]] = []
    sold_by_pid: dict[Any, dict] = {}
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
        for r in rows:
            d = dict(r)
            sold_by_pid[d.get("product_id")] = d

    # Gộp cả SP chỉ có giao, chưa bán
    all_pids = set(sold_by_pid.keys()) | {k for k in received_map if k is not None}
    has_inv = _table_exists(conn, "inventory")
    for pid in sorted(all_pids, key=lambda x: (x is None, x or 0)):
        d = sold_by_pid.get(pid) or {}
        if not d and pid:
            prow = None
            if _table_exists(conn, "products"):
                prow = conn.execute(
                    "SELECT id, product_code, name, unit, COALESCE(price,0) AS price FROM products WHERE id=?",
                    (pid,),
                ).fetchone()
            if prow:
                pd = dict(prow)
                d = {
                    "product_id": pid,
                    "product_code": pd.get("product_code") or "",
                    "product_name": pd.get("name") or "",
                    "unit": pd.get("unit") or "Cái",
                    "qty_sold": 0,
                    "unit_price": pd.get("price") or 0,
                    "amount": 0,
                }
            else:
                continue
        sold = _f(d.get("qty_sold"))
        received = _f(received_map.get(pid if pid is not None else None))
        stock = 0.0
        if has_inv and pid:
            inv = conn.execute(
                "SELECT COALESCE(quantity,0) FROM inventory WHERE product_id=?",
                (pid,),
            ).fetchone()
            stock = _f(inv[0] if inv else 0)
        # Tồn cuối ≈ tồn kho DN; tồn đại lý ≈ nhận − bán (ước lượng)
        closing_agent = max(0.0, received - sold) if received or sold else max(0.0, stock)
        opening = max(0.0, closing_agent + sold - received)
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
            "qty_received_total": sum(_f(x["qty_received"]) for x in lines),
        },
        "notes": [
            "Mẫu số 01-BH theo TT99/2025/TT-BTC — lập từ doanh số bán theo khách hàng đại lý.",
            "Cột nhận trong kỳ lấy từ phiếu giao đại lý (sme_agent_deliveries); lập phiếu giao trước khi đối soát.",
            "Tồn đầu/cuối ước lượng từ giao − bán trong kỳ (và tồn kho DN nếu không có giao).",
        ],
        "printed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def form_02_bh(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    date_from: str,
    date_to: str,
    branch_code: str | None = None,
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

    sm_wh_filter = ''
    sm_wh_params: list[Any] = []
    if _table_exists(conn, "stock_moves"):
        sm_cols = {r[1] for r in conn.execute("PRAGMA table_info(stock_moves)").fetchall()}
        if 'warehouse_code' in sm_cols:
            from Services.sme.branches import warehouse_branch_filter_sql
            sm_wh_filter, sm_wh_params = warehouse_branch_filter_sql(
                conn, branch_code, table='stock_moves', alias='sm',
            )

    # Opening / phát sinh: ưu tiên quantity có dấu (chuẩn SME);
    # fallback in_quantity - out_quantity nếu quantity = 0 (dữ liệu cũ).
    qty_net_expr = """
        CASE
            WHEN COALESCE(sm.quantity, 0) != 0 THEN sm.quantity
            ELSE COALESCE(sm.in_quantity, 0) - COALESCE(sm.out_quantity, 0)
        END
    """
    qty_in_expr = """
        CASE
            WHEN COALESCE(sm.quantity, 0) > 0 THEN sm.quantity
            WHEN COALESCE(sm.quantity, 0) < 0 THEN 0
            ELSE COALESCE(sm.in_quantity, 0)
        END
    """
    qty_out_expr = """
        CASE
            WHEN COALESCE(sm.quantity, 0) < 0 THEN -sm.quantity
            WHEN COALESCE(sm.quantity, 0) > 0 THEN 0
            ELSE COALESCE(sm.out_quantity, 0)
        END
    """

    # Opening balance before date_from
    opening = 0.0
    if _table_exists(conn, "stock_moves"):
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM({qty_net_expr}), 0)
            FROM stock_moves sm
            WHERE product_id=? AND substr(date,1,10) < ?
            {sm_wh_filter}
            """,
            [product_id, date_from[:10], *sm_wh_params],
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
            f"""
            SELECT substr(date,1,10) AS d,
                   COALESCE(SUM({qty_in_expr}), 0) AS qty_in,
                   COALESCE(SUM({qty_out_expr}), 0) AS qty_out,
                   GROUP_CONCAT(DISTINCT type) AS types
            FROM stock_moves sm
            WHERE product_id=?
              AND substr(date,1,10) >= ?
              AND substr(date,1,10) <= ?
              {sm_wh_filter}
            GROUP BY substr(date,1,10)
            ORDER BY d
            """,
            [product_id, date_from[:10], date_to[:10], *sm_wh_params],
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
                "amt_out_sale": round(_f(d.get("amt")), 2),
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
