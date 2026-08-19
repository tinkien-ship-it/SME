"""Danh mục POS cho cache offline — HKD & SME dùng chung products."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from Services.scale_service import get_scale_config


def fetch_pos_catalog(
    conn: sqlite3.Connection,
    *,
    include_menu: bool = False,
    warehouse_codes: list[str] | None = None,
) -> dict:
    """
    warehouse_codes: nếu chỉ định, chỉ trả sản phẩm có tồn kho trong các kho này.
    None = không lọc (tất cả sản phẩm).
    """
    if warehouse_codes:
        placeholders = ','.join('?' * len(warehouse_codes))
        sql = f"""
            SELECT
                p.id, p.name, p.product_code, p.barcode, p.base_price, p.unit,
                p.unit1, p.unit_ratio, p.price AS sale_price,
                p.barcode1, p.sell_by_weight, p.weight_plu,
                COALESCE(p.product_type, 'goods') AS product_type,
                COALESCE(SUM(i.quantity), 0) AS quantity,
                COALESCE(AVG(CASE WHEN i.quantity > 0 THEN i.avg_cost END), 0) AS avg_cost
            FROM products p
            LEFT JOIN inventory i ON i.product_id = p.id
                AND i.warehouse_code IN ({placeholders})
            GROUP BY p.id
            ORDER BY p.id
            LIMIT 12000
        """
        cur = conn.execute(sql, warehouse_codes)
    else:
        cur = conn.execute(
            """
            SELECT
                p.id, p.name, p.product_code, p.barcode, p.base_price, p.unit,
                p.unit1, p.unit_ratio, p.price AS sale_price,
                p.barcode1, p.sell_by_weight, p.weight_plu,
                COALESCE(p.product_type, 'goods') AS product_type,
                COALESCE(i.quantity, 0) AS quantity,
                COALESCE(i.avg_cost, 0) AS avg_cost
            FROM products p
            LEFT JOIN inventory i ON i.product_id = p.id
            ORDER BY p.id
            LIMIT 12000
            """
        )
    products = [dict(r) if isinstance(r, sqlite3.Row) else dict(zip([d[0] for d in cur.description], r))
                for r in cur.fetchall()]

    menu: list[dict] = []
    if include_menu:
        try:
            mcur = conn.execute(
                """
                SELECT id, item_code, name, category, unit, unit1, base_price, price,
                       is_active, product_type, product_id
                FROM menu
                WHERE COALESCE(is_active, 1) = 1
                ORDER BY name
                """
            )
            menu = [dict(r) if isinstance(r, sqlite3.Row) else dict(zip([d[0] for d in mcur.description], r))
                    for r in mcur.fetchall()]
        except sqlite3.Error:
            menu = []

    return {
        'products': products,
        'menu': menu,
        'scale_config': get_scale_config(),
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'count': len(products),
    }
