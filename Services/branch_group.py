"""
Chi nhánh hạch toán độc lập (MST riêng) — mô hình tenant group.

Mỗi chi nhánh hạch toán độc lập là một tenant riêng (DB riêng), nhưng liên kết
với tenant mẹ (trụ sở chính) qua bảng `tenant_branch_links` trên registry DB.

Luồng:
1. Tạo chi nhánh độc lập → tạo tenant mới + link vào parent.
2. Báo cáo hợp nhất: parent query aggregate từ các child tenant DB.
3. Nhân viên có thể được gán quyền ở 1+ tenant (parent hoặc child).

Registry DB (REGISTRY_PATH) chứa bảng:
  - tenants: danh sách tenant (đã có sẵn)
  - tenant_branch_links: quan hệ parent-child
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Any

from db_utils import REGISTRY_PATH, open_sqlite, BASE_DIR


def ensure_branch_group_schema() -> None:
    with open_sqlite(REGISTRY_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tenant_branch_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_tenant_id TEXT NOT NULL,
                child_tenant_id TEXT NOT NULL,
                branch_type TEXT NOT NULL DEFAULT 'independent',
                child_tax_code TEXT,
                child_name TEXT,
                notes TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(parent_tenant_id, child_tenant_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tbl_parent ON tenant_branch_links(parent_tenant_id)"
        )
        conn.commit()


def create_independent_branch(
    parent_tenant_id: str,
    *,
    branch_name: str,
    tax_code: str,
    phone: str = '',
    business_line: str = 'pos',
    contact_email: str = '',
    notes: str = '',
) -> dict[str, Any]:
    """
    Tạo chi nhánh hạch toán độc lập (tenant mới) và liên kết với parent.
    Trả về thông tin tenant mới + link.
    """
    from tenant_middleware import init_tenant_database

    child_tenant_id = phone.strip() or tax_code.strip().replace('-', '')
    if not child_tenant_id:
        raise ValueError('Cần số điện thoại hoặc MST làm ID chi nhánh')

    init_tenant_database(
        tenant_id=child_tenant_id,
        business_name=branch_name,
        phone=phone,
        tax_code=tax_code,
        contact_email=contact_email,
        business_line=business_line,
        accounting_regime='SME',
    )

    ensure_branch_group_schema()
    with open_sqlite(REGISTRY_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO tenant_branch_links
            (parent_tenant_id, child_tenant_id, branch_type, child_tax_code, child_name, notes, is_active, created_at)
            VALUES (?, ?, 'independent', ?, ?, ?, 1, ?)
            """,
            (parent_tenant_id, child_tenant_id, tax_code, branch_name, notes, datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        )
        conn.commit()

    return {
        'parent_tenant_id': parent_tenant_id,
        'child_tenant_id': child_tenant_id,
        'branch_name': branch_name,
        'tax_code': tax_code,
        'db_path': os.path.join('tenants', f'{child_tenant_id}.db'),
    }


def list_child_branches(parent_tenant_id: str, *, active_only: bool = True) -> list[dict]:
    ensure_branch_group_schema()
    with open_sqlite(REGISTRY_PATH) as conn:
        conn.row_factory = sqlite3.Row
        sql = """
            SELECT l.*, t.db_path, t.business_name
            FROM tenant_branch_links l
            LEFT JOIN tenants t ON t.tenant_id = l.child_tenant_id
            WHERE l.parent_tenant_id = ?
        """
        if active_only:
            sql += " AND l.is_active = 1"
        sql += " ORDER BY l.created_at"
        rows = conn.execute(sql, (parent_tenant_id,)).fetchall()
        return [dict(r) for r in rows]


def get_parent_tenant(child_tenant_id: str) -> str | None:
    ensure_branch_group_schema()
    with open_sqlite(REGISTRY_PATH) as conn:
        row = conn.execute(
            "SELECT parent_tenant_id FROM tenant_branch_links WHERE child_tenant_id = ? AND is_active = 1",
            (child_tenant_id,),
        ).fetchone()
        return row[0] if row else None


def deactivate_child_branch(parent_tenant_id: str, child_tenant_id: str) -> bool:
    ensure_branch_group_schema()
    with open_sqlite(REGISTRY_PATH) as conn:
        conn.execute(
            "UPDATE tenant_branch_links SET is_active = 0 WHERE parent_tenant_id = ? AND child_tenant_id = ?",
            (parent_tenant_id, child_tenant_id),
        )
        conn.commit()
    return True


def list_group_db_paths(parent_tenant_id: str) -> list[str]:
    """Trả về danh sách DB path của parent + tất cả child (cho báo cáo hợp nhất)."""
    children = list_child_branches(parent_tenant_id)
    paths = []
    with open_sqlite(REGISTRY_PATH) as conn:
        row = conn.execute(
            "SELECT db_path FROM tenants WHERE tenant_id = ?", (parent_tenant_id,)
        ).fetchone()
        if row and row[0]:
            paths.append(os.path.join(BASE_DIR, row[0]) if not os.path.isabs(row[0]) else row[0])
    for child in children:
        db = child.get('db_path')
        if db:
            full = os.path.join(BASE_DIR, db) if not os.path.isabs(db) else db
            if os.path.isfile(full):
                paths.append(full)
    return paths
