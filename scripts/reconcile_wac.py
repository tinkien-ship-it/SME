#!/usr/bin/env python3
"""Đối soát tồn kho (SL) và rebuild WAC từ stock_moves cho dữ liệu cũ."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db_utils import get_db_connection
from Services.inventory_stock_helpers import rebuild_all_wac_from_moves, reconcile_all_inventory


def main():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        print('Đối soát số lượng tồn ← stock_moves...')
        qty_fixes = reconcile_all_inventory(c)
        print(f'  → {len(qty_fixes)} sản phẩm lệch SL đã sửa')

        print('Rebuild WAC từ lịch sử stock_moves...')
        wac_fixes = rebuild_all_wac_from_moves(c)
        print(f'  → {len(wac_fixes)} sản phẩm WAC đã cập nhật')
        for f in wac_fixes[:20]:
            print(f"    SP #{f['product_id']}: {f['old_wac']:.4f} → {f['new_wac']:.4f}")

        conn.commit()
        print('Hoàn tất.')
    except Exception as e:
        conn.rollback()
        print('Lỗi:', e)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
