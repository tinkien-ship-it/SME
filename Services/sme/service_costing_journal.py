"""Hạch toán giá vốn dịch vụ TT99: 621/622/627 → 154 → (nghiệm thu) 6323."""
from __future__ import annotations

import sqlite3
from typing import Any

from db_utils import sqlite_commit
from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    post_journal_entry,
    reverse_journal_entry,
)


def _link_step(conn: sqlite3.Connection, job_id: int, step: str, journal_entry_id: int) -> None:
    conn.execute(
        """
        INSERT INTO service_job_journals (job_id, step, journal_entry_id)
        VALUES (?, ?, ?)
        ON CONFLICT(job_id, step) DO UPDATE SET journal_entry_id = excluded.journal_entry_id
        """,
        (job_id, step, journal_entry_id),
    )


def _cogs_account(conn: sqlite3.Connection) -> str:
    from Services.sme.journal_engine import resolve_postable_account
    try:
        return resolve_postable_account(conn, 'cogs.service.processing')
    except Exception:
        return resolve_postable_account(conn, '6323')


def post_service_cost_line(
    conn: sqlite3.Connection,
    job_id: int,
    cost_id: int,
    *,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any] | None:
    """Ghi một dòng CP: trong mức → collect+KC 154; vượt mức → Nợ 6323 / Có nguồn."""
    ensure_sme_journal_ready(conn, commit=False)
    from Services.sme.service_costing import ensure_service_costing_schema, get_service_job
    ensure_service_costing_schema(conn)

    row = conn.execute(
        "SELECT * FROM service_job_costs WHERE id = ? AND job_id = ?",
        (cost_id, job_id),
    ).fetchone()
    if not row:
        return None
    keys = row.keys() if isinstance(row, sqlite3.Row) else None
    cost = dict(row) if keys else {
        'id': row[0], 'job_id': row[1], 'cost_date': row[2], 'cost_type': row[3],
        'description': row[4], 'amount': row[5], 'in_norm': row[6],
        'product_id': row[7], 'credit_account': row[9],
        'debit_collect_account': row[10],
        'journal_entry_id': row[13] if len(row) > 13 else None,
        'posted_to_wip': row[14] if len(row) > 14 else 0,
        'posted_to_cogs': row[15] if len(row) > 15 else 0,
    }
    if cost.get('journal_entry_id'):
        return {'skipped': True, 'journal_entry_id': cost['journal_entry_id']}

    job = get_service_job(conn, job_id)
    if not job:
        raise ValueError('Không tìm thấy lệnh dịch vụ')

    amount = float(cost.get('amount') or 0)
    if amount <= 0:
        return None

    voucher = job.get('voucher_no') or f'DVGT{job_id}'
    date_s = str(cost.get('cost_date') or job.get('job_date') or '')[:10]
    credit = str(cost.get('credit_account') or '1111')
    collect = str(cost.get('debit_collect_account') or '627')
    desc_base = cost.get('description') or cost.get('cost_type') or 'CP dịch vụ'
    product_id = cost.get('product_id') or job.get('service_product_id')

    in_norm = int(cost.get('in_norm') if cost.get('in_norm') is not None else 1) == 1

    if not in_norm:
        # Vượt mức → thẳng giá vốn 6323
        cogs = _cogs_account(conn)
        entry = post_journal_entry(
            conn,
            posting_date=date_s,
            document_date=date_s,
            document_type='DVGT',
            document_no=f'{voucher}-VN{cost_id}',
            document_id=job_id,
            business_type='GV_DV_VUOT_MUC',
            description=f'{voucher}: vượt mức — {desc_base}',
            reference_document=voucher,
            created_by=created_by,
            lines=[
                {
                    'sequence': 1, 'account_code': cogs,
                    'debit': amount, 'credit': 0,
                    'product_id': product_id,
                    'description': f'Vượt mức {desc_base}',
                },
                {
                    'sequence': 2, 'account_code': credit,
                    'debit': 0, 'credit': amount,
                    'product_id': cost.get('product_id'),
                    'description': f'Đối ứng vượt mức {desc_base}',
                },
            ],
        )
        conn.execute(
            """
            UPDATE service_job_costs
            SET journal_entry_id = ?, posted_to_cogs = 1, posted_to_wip = 0
            WHERE id = ?
            """,
            (entry['id'], cost_id),
        )
        _link_step(conn, job_id, f'overnorm:{cost_id}', entry['id'])
        if commit:
            sqlite_commit(conn, label='service_costing_journal')
        return {
            'journal_entry_id': entry['id'],
            'entry_no': entry.get('entry_no'),
            'mode': 'overnorm',
            'amount': amount,
        }

    # Trong mức: Nợ collect / Có nguồn + Nợ 154 / Có collect
    lines = [
        {
            'sequence': 1, 'account_code': collect,
            'debit': amount, 'credit': 0,
            'product_id': product_id,
            'description': f'{voucher}: {desc_base} ({collect})',
        },
        {
            'sequence': 2, 'account_code': credit,
            'debit': 0, 'credit': amount,
            'product_id': cost.get('product_id'),
            'description': f'{voucher}: đối ứng {credit}',
        },
        {
            'sequence': 3, 'account_code': '154',
            'debit': amount, 'credit': 0,
            'product_id': product_id,
            'description': f'{voucher}: KC {collect} → 154',
        },
        {
            'sequence': 4, 'account_code': collect,
            'debit': 0, 'credit': amount,
            'product_id': product_id,
            'description': f'{voucher}: KC {collect} → 154',
        },
    ]
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='DVGT',
        document_no=f'{voucher}-CP{cost_id}',
        document_id=job_id,
        business_type='TAP_HOP_CP_DV',
        description=f'Tập hợp CP DV {voucher}: {desc_base}',
        reference_document=voucher,
        created_by=created_by,
        lines=lines,
    )
    conn.execute(
        """
        UPDATE service_job_costs
        SET journal_entry_id = ?, posted_to_wip = 1
        WHERE id = ?
        """,
        (entry['id'], cost_id),
    )
    _link_step(conn, job_id, f'cost:{cost_id}', entry['id'])
    if commit:
        sqlite_commit(conn, label='service_costing_journal')
    return {
        'journal_entry_id': entry['id'],
        'entry_no': entry.get('entry_no'),
        'mode': 'wip',
        'amount': amount,
    }


def post_unposted_costs_to_wip(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id FROM service_job_costs
        WHERE job_id = ?
          AND COALESCE(journal_entry_id, 0) = 0
        ORDER BY id
        """,
        (job_id,),
    ).fetchall()
    posted = []
    for r in rows:
        cid = r[0] if not isinstance(r, sqlite3.Row) else r['id']
        info = post_service_cost_line(
            conn, job_id, int(cid), created_by=created_by, commit=False,
        )
        if info:
            posted.append(info)
    if commit:
        sqlite_commit(conn, label='service_costing_journal')
    return {'posted_count': len(posted), 'entries': posted}


def post_service_delivery_cogs(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    deliver_date: str,
    amount: float | None = None,
    percent: float | None = None,
    note: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any] | None:
    """Nghiệm thu (toàn bộ hoặc một phần): Nợ 6323 / Có 154."""
    ensure_sme_journal_ready(conn, commit=False)
    from Services.sme.service_costing import get_service_job
    job = get_service_job(conn, job_id)
    if not job:
        raise ValueError('Không tìm thấy lệnh dịch vụ')

    wip_available = float(job.get('wip_balance') or 0)
    if amount is not None:
        cogs_amt = round(float(amount), 2)
    else:
        cogs_amt = wip_available
    if cogs_amt <= 0:
        return {'skipped': True, 'reason': 'no_wip', 'amount': 0}
    if cogs_amt > wip_available + 0.01:
        raise ValueError('Số tiền nghiệm thu vượt số dư chi phí dở dang trên tài khoản 154')

    voucher = job.get('voucher_no') or f'DVGT{job_id}'
    cogs = _cogs_account(conn)
    pct_label = f' {float(percent):g}%' if percent is not None else ''
    note_s = (note or '').strip()
    desc = (
        f"Nghiệm thu dịch vụ {voucher}{pct_label} — "
        f"{job.get('service_name') or ''}"
        + (f' ({note_s})' if note_s else '')
    )
    seq = conn.execute(
        "SELECT COUNT(*) FROM service_job_deliveries WHERE job_id = ?",
        (job_id,),
    ).fetchone()[0] or 0
    entry = post_journal_entry(
        conn,
        posting_date=deliver_date[:10],
        document_date=deliver_date[:10],
        document_type='DVGT',
        document_no=f'{voucher}-GV{int(seq) + 1}',
        document_id=job_id,
        business_type='GIA_VON_DICH_VU',
        description=desc,
        reference_document=voucher,
        created_by=created_by,
        lines=[
            {
                'sequence': 1, 'account_code': cogs,
                'debit': cogs_amt, 'credit': 0,
                'product_id': job.get('service_product_id'),
                'description': desc,
            },
            {
                'sequence': 2, 'account_code': '154',
                'debit': 0, 'credit': cogs_amt,
                'product_id': job.get('service_product_id'),
                'description': f'Kết chuyển 154 sang {cogs} {voucher}',
            },
        ],
    )
    if commit:
        sqlite_commit(conn, label='service_costing_journal')
    return {
        'journal_entry_id': entry['id'],
        'entry_no': entry.get('entry_no'),
        'amount': cogs_amt,
        'percent': percent,
        'cogs_account': cogs,
    }


def reverse_service_job_journals(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    reason: str = 'Hủy lệnh DV',
    created_by: str | None = None,
) -> list[int]:
    rows = conn.execute(
        """
        SELECT step, journal_entry_id FROM service_job_journals
        WHERE job_id = ? ORDER BY id DESC
        """,
        (job_id,),
    ).fetchall()
    reversed_ids = []
    for r in rows:
        jid = r[1] if not isinstance(r, sqlite3.Row) else r['journal_entry_id']
        if not jid:
            continue
        try:
            reverse_journal_entry(conn, int(jid), created_by=created_by, reason=reason)
            reversed_ids.append(int(jid))
        except ValueError:
            pass
    conn.execute('DELETE FROM service_job_journals WHERE job_id = ?', (job_id,))
    conn.execute(
        """
        UPDATE service_job_costs
        SET journal_entry_id = NULL, posted_to_wip = 0, posted_to_cogs = 0
        WHERE job_id = ?
        """,
        (job_id,),
    )
    return reversed_ids


def post_outsource_variance_credit(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    amount: float,
    cost_date: str,
    invoice_no: str = '',
    provisional_cost_id: int | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """HĐ thuê ngoài < dự kiến: giảm 154 và giảm phải trả 331 ước tính.

    Nợ 331 / Có 627 + Nợ 627 / Có 154 (cùng số tiền giảm).
    """
    ensure_sme_journal_ready(conn, commit=False)
    from Services.sme.service_costing import ensure_service_costing_schema, get_service_job
    ensure_service_costing_schema(conn)
    amt = float(amount or 0)
    if amt <= 0:
        raise ValueError('Số tiền điều chỉnh giảm phải > 0')
    job = get_service_job(conn, job_id)
    if not job:
        raise ValueError('Không tìm thấy lệnh dịch vụ')
    voucher = job.get('voucher_no') or f'DVGT{job_id}'
    date_s = str(cost_date or job.get('job_date') or '')[:10]
    inv = (invoice_no or '').strip()
    desc = f'{voucher}: giảm thuê ngoài theo HĐ {inv}'.strip()
    product_id = job.get('service_product_id')
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='DVGT',
        document_no=f'{voucher}-OSADJ{provisional_cost_id or ""}',
        document_id=job_id,
        business_type='DIEU_CHINH_THUE_NGOAI',
        description=desc,
        reference_document=voucher,
        created_by=created_by,
        lines=[
            {
                'sequence': 1, 'account_code': '331',
                'debit': amt, 'credit': 0,
                'description': f'Giảm phải trả ước tính — HĐ {inv}',
            },
            {
                'sequence': 2, 'account_code': '627',
                'debit': 0, 'credit': amt,
                'product_id': product_id,
                'description': f'Giảm CP thuê ngoài — HĐ {inv}',
            },
            {
                'sequence': 3, 'account_code': '627',
                'debit': amt, 'credit': 0,
                'product_id': product_id,
                'description': f'Điều chỉnh giảm 154 — HĐ {inv}',
            },
            {
                'sequence': 4, 'account_code': '154',
                'debit': 0, 'credit': amt,
                'product_id': product_id,
                'description': f'Giảm dở dang DV — HĐ {inv}',
            },
        ],
    )
    step = f'osadj:{provisional_cost_id or entry["id"]}'
    _link_step(conn, job_id, step, entry['id'])
    if commit:
        sqlite_commit(conn, label='service_costing_journal')
    return {
        'journal_entry_id': entry['id'],
        'entry_no': entry.get('entry_no'),
        'amount': amt,
        'mode': 'outsource_variance_credit',
    }
