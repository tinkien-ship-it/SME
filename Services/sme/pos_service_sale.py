"""POS bán dịch vụ B — tự lập lệnh, áp định mức, gắn HĐ (trước sync_sale_journals)."""
from __future__ import annotations

import sqlite3
from typing import Any

from db_utils import sqlite_commit

_SERVICE_TYPES = frozenset({'service', 'services', 'dich_vu', 'dv'})


def _is_service_product_type(product_type: str | None) -> bool:
    return str(product_type or '').strip().lower() in _SERVICE_TYPES


def is_pos_immediate_service_product(conn: sqlite3.Connection, product_id: int) -> bool:
    """Dịch vụ ghi DT ngay (5113) — không phải gói subscription / DT hoãn 3387."""
    from Services.sme.deferred_revenue import ensure_product_revenue_columns
    ensure_product_revenue_columns(conn)
    row = conn.execute(
        """
        SELECT COALESCE(product_type, 'goods') AS product_type,
               COALESCE(is_subscription_plan, 0) AS is_subscription_plan,
               COALESCE(revenue_mode, 'immediate') AS revenue_mode
        FROM products WHERE id = ?
        """,
        (int(product_id),),
    ).fetchone()
    if not row:
        return False
    d = dict(row) if isinstance(row, sqlite3.Row) else {
        'product_type': row[0], 'is_subscription_plan': row[1], 'revenue_mode': row[2],
    }
    if not _is_service_product_type(d.get('product_type')):
        return False
    if int(d.get('is_subscription_plan') or 0) == 1:
        return False
    if str(d.get('revenue_mode') or '').strip().lower() == 'deferred':
        return False
    return True


def _cancel_open_jobs_for_sale(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    created_by: str = '',
) -> int:
    from Services.sme.service_costing import STATUS_CANCELLED, cancel_service_job
    rows = conn.execute(
        """
        SELECT id FROM service_jobs
        WHERE sale_id = ? AND status NOT IN (?, 'delivered')
        """,
        (int(sale_id), STATUS_CANCELLED),
    ).fetchall()
    n = 0
    for r in rows:
        jid = int(r[0] if not isinstance(r, sqlite3.Row) else r['id'])
        try:
            cancel_service_job(
                conn, jid,
                reason='Cập nhật đơn bán POS',
                created_by=created_by,
                commit=False,
            )
            n += 1
        except ValueError:
            pass
    return n


def provision_service_jobs_for_sale(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    created_by: str = '',
    replace_existing: bool = False,
    commit: bool = False,
) -> dict[str, Any]:
    """Tạo lệnh DV + áp định mức + gắn sale_items trước khi ghi sổ bán hàng."""
    from Services.sme.bootstrap import ensure_sme_accounting_ready
    from Services.sme.service_costing import (
        create_service_job,
        ensure_service_costing_schema,
        get_service_cost_standard,
        link_job_to_sale_item,
        _standard_has_content,
    )

    ensure_sme_accounting_ready(conn, commit=False)
    ensure_service_costing_schema(conn)
    conn.row_factory = sqlite3.Row

    sale = conn.execute('SELECT * FROM sale WHERE id = ?', (int(sale_id),)).fetchone()
    if not sale:
        raise ValueError(f'Không tìm thấy hóa đơn bán #{sale_id}')
    if str(sale['status'] or '').lower() != 'completed':
        return {'provisioned': 0, 'skipped': 0, 'reason': 'sale_not_completed'}

    if replace_existing:
        _cancel_open_jobs_for_sale(conn, sale_id, created_by=created_by)

    item_cols = {r[1] for r in conn.execute('PRAGMA table_info(sale_items)').fetchall()}
    has_job_col = 'service_job_id' in item_cols

    rows = conn.execute(
        """
        SELECT COALESCE(si.id, si.rowid) AS sale_item_id, si.product_id, si.quantity,
               COALESCE(p.product_type, 'goods') AS product_type,
               p.name AS product_name
        FROM sale_items si
        JOIN products p ON p.id = si.product_id
        WHERE si.sale_id = ?
        ORDER BY si.rowid
        """,
        (int(sale_id),),
    ).fetchall()

    sale_date = str(sale['date'] or '')[:10]
    customer_name = (
        str(sale['company_name'] or '').strip()
        or str(sale['customer_name'] or '').strip()
        or 'Khách hàng'
    )
    customer_id = None
    if 'customer_id' in sale.keys() and sale['customer_id']:
        try:
            customer_id = int(sale['customer_id'])
        except (TypeError, ValueError):
            customer_id = None
    sale_no = str(sale['sale_no'] or f'ĐH{sale_id}')

    created: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        si_id = int(row['sale_item_id'])
        pid = int(row['product_id'])
        if not is_pos_immediate_service_product(conn, pid):
            skipped += 1
            continue
        if has_job_col:
            existing = conn.execute(
                """
                SELECT service_job_id FROM sale_items
                WHERE COALESCE(id, rowid) = ?
                """,
                (si_id,),
            ).fetchone()
            if existing and existing[0]:
                skipped += 1
                continue

        qty = float(row['quantity'] or 0)
        if qty <= 0:
            skipped += 1
            continue

        std = get_service_cost_standard(conn, pid)
        apply_norms = bool(std and _standard_has_content(std))

        job = create_service_job(
            conn,
            service_product_id=pid,
            job_date=sale_date,
            qty=qty,
            customer_id=customer_id,
            customer_name=customer_name,
            note=f'POS {sale_no} — {row["product_name"] or ""}'.strip(),
            sale_id=int(sale_id),
            apply_norms=apply_norms,
            created_by=created_by,
            commit=False,
        )
        jid = int(job['id'])
        if has_job_col:
            link_job_to_sale_item(
                conn, sale_item_id=si_id, job_id=jid, commit=False,
            )
        else:
            conn.execute(
                'UPDATE service_jobs SET sale_id = ? WHERE id = ?',
                (int(sale_id), jid),
            )
        created.append({
            'job_id': jid,
            'voucher_no': job.get('voucher_no'),
            'sale_item_id': si_id,
            'product_id': pid,
            'applied_norms': apply_norms,
        })

    if commit:
        sqlite_commit(conn, label='pos_service_sale')
    return {
        'provisioned': len(created),
        'skipped': skipped,
        'jobs': created,
    }
