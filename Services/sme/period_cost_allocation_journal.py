"""Journal phân bổ 622/627 → 154 và dưới công suất → 632."""
from __future__ import annotations

import sqlite3
from typing import Any

from db_utils import sqlite_commit
from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    post_journal_entry,
    resolve_postable_account,
)


def _money(v) -> float:
    return round(float(v or 0), 2)


def _cogs_account(conn: sqlite3.Connection) -> str:
    try:
        return resolve_postable_account(conn, '632')
    except Exception:
        return '632'


def _oh_fixed_account(conn: sqlite3.Connection) -> str:
    try:
        return resolve_postable_account(conn, '6271')
    except Exception:
        try:
            return resolve_postable_account(conn, '627')
        except Exception:
            return '6271'


def _oh_variable_account(conn: sqlite3.Connection) -> str:
    try:
        return resolve_postable_account(conn, '6272')
    except Exception:
        try:
            return resolve_postable_account(conn, '627')
        except Exception:
            return '6272'


def post_allocation_journals(
    conn: sqlite3.Connection,
    allocation_id: int,
    data: dict[str, Any],
    *,
    close_idle_now: bool = False,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """
    1) Tập hợp: Nợ 622/6271/6272 · Có 3341/1111 (chi phí phát sinh trong phạm vi).
    2) Phân bổ vào GT: Nợ 154 · Có 622/6271/6272 (theo từng lệnh/SP).
    3) (Tùy chọn) Idle ngay: Nợ 632 · Có 622/6271.
    """
    ensure_sme_journal_ready(conn, commit=False)
    date_s = str(data.get('date_to') or '')[:10]
    fy = data.get('fiscal_year')
    period = data.get('period')
    doc = f"PBGT{fy}{int(period):02d}-{allocation_id}"
    acc_6271 = _oh_fixed_account(conn)
    acc_6272 = _oh_variable_account(conn)

    labor = _money(data.get('labor_amount'))
    oh_f = _money(data.get('oh_fixed_amount'))
    oh_v = _money(data.get('oh_variable_amount'))
    labor_alloc = _money(data.get('labor_allocated'))
    oh_f_alloc = _money(data.get('oh_fixed_allocated'))
    oh_v_alloc = _money(data.get('oh_variable_allocated'))
    labor_idle = _money(data.get('labor_idle'))
    oh_f_idle = _money(data.get('oh_fixed_idle'))

    result: dict[str, Any] = {}

    # --- 1. Collect ---
    collect_lines: list[dict] = []
    seq = 1
    if labor > 0:
        collect_lines.extend([
            {
                'sequence': seq, 'account_code': '622',
                'debit': labor, 'credit': 0,
                'description': f'{doc}: tập hợp nhân công trực tiếp',
            },
            {
                'sequence': seq + 1, 'account_code': '3341',
                'debit': 0, 'credit': labor,
                'description': f'{doc}: phải trả người lao động',
            },
        ])
        seq += 2
    if oh_f > 0:
        collect_lines.extend([
            {
                'sequence': seq, 'account_code': acc_6271,
                'debit': oh_f, 'credit': 0,
                'description': f'{doc}: tập hợp SXC định phí ({acc_6271})',
            },
            {
                'sequence': seq + 1, 'account_code': '1111',
                'debit': 0, 'credit': oh_f,
                'description': f'{doc}: đối ứng SXC định phí',
            },
        ])
        seq += 2
    if oh_v > 0:
        collect_lines.extend([
            {
                'sequence': seq, 'account_code': acc_6272,
                'debit': oh_v, 'credit': 0,
                'description': f'{doc}: tập hợp SXC biến phí ({acc_6272})',
            },
            {
                'sequence': seq + 1, 'account_code': '1111',
                'debit': 0, 'credit': oh_v,
                'description': f'{doc}: đối ứng SXC biến phí',
            },
        ])
        seq += 2

    if collect_lines:
        collect = post_journal_entry(
            conn,
            posting_date=date_s,
            document_date=date_s,
            document_type='PBGT',
            document_no=f'{doc}-TH',
            document_id=allocation_id,
            business_type='TAP_HOP_CP_KY',
            description=f'Tập hợp 622/6271/6272 kỳ phân bổ #{allocation_id}',
            reference_document=doc,
            created_by=created_by,
            lines=collect_lines,
        )
        result['collect_journal_entry_id'] = collect['id']
        result['collect_entry_no'] = collect.get('entry_no')

    # --- 2. Allocate to 154 by order/product ---
    alloc_lines: list[dict] = []
    seq = 1
    for ln in data.get('lines') or []:
        pid = ln.get('finished_product_id')
        lab = _money(ln.get('labor_allocated'))
        ofx = _money(ln.get('oh_fixed_allocated'))
        ov = _money(ln.get('oh_variable_allocated'))
        voucher = ln.get('voucher_no') or ''
        if lab > 0:
            alloc_lines.append({
                'sequence': seq, 'account_code': '154',
                'debit': lab, 'credit': 0,
                'product_id': pid,
                'description': f'{doc}: NC → 154 {voucher}',
            })
            seq += 1
            alloc_lines.append({
                'sequence': seq, 'account_code': '622',
                'debit': 0, 'credit': lab,
                'product_id': pid,
                'description': f'{doc}: KC 622 → 154 {voucher}',
            })
            seq += 1
        if ofx > 0:
            alloc_lines.append({
                'sequence': seq, 'account_code': '154',
                'debit': ofx, 'credit': 0,
                'product_id': pid,
                'description': f'{doc}: SXC định phí → 154 {voucher}',
            })
            seq += 1
            alloc_lines.append({
                'sequence': seq, 'account_code': acc_6271,
                'debit': 0, 'credit': ofx,
                'product_id': pid,
                'description': f'{doc}: KC {acc_6271} → 154 {voucher}',
            })
            seq += 1
        if ov > 0:
            alloc_lines.append({
                'sequence': seq, 'account_code': '154',
                'debit': ov, 'credit': 0,
                'product_id': pid,
                'description': f'{doc}: SXC biến phí → 154 {voucher}',
            })
            seq += 1
            alloc_lines.append({
                'sequence': seq, 'account_code': acc_6272,
                'debit': 0, 'credit': ov,
                'product_id': pid,
                'description': f'{doc}: KC {acc_6272} → 154 {voucher}',
            })
            seq += 1

    if alloc_lines:
        allocate = post_journal_entry(
            conn,
            posting_date=date_s,
            document_date=date_s,
            document_type='PBGT',
            document_no=f'{doc}-PB',
            document_id=allocation_id,
            business_type='PHAN_BO_GT_LENH',
            description=(
                f'Phân bổ GT vào lệnh SX #{allocation_id} '
                f'({data.get("allocation_method_label") or data.get("allocation_method")})'
            ),
            reference_document=doc,
            created_by=created_by,
            lines=alloc_lines,
        )
        result['allocate_journal_entry_id'] = allocate['id']
        result['allocate_entry_no'] = allocate.get('entry_no')

    if close_idle_now and (labor_idle > 0 or oh_f_idle > 0):
        idle = post_idle_to_cogs(
            conn, allocation_id, data, created_by=created_by, commit=False,
        )
        result['idle_journal_entry_id'] = idle.get('journal_entry_id')
        result['idle_entry_no'] = idle.get('entry_no')

    _ = (labor_alloc, oh_f_alloc, oh_v_alloc)

    if commit:
        sqlite_commit(conn, label='period_cost_allocation_journal')
    return result


def post_idle_to_cogs(
    conn: sqlite3.Connection,
    allocation_id: int,
    data: dict[str, Any],
    *,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Nợ 632 / Có 622·6271 — chi phí dưới công suất bình thường (chỉ định phí SXC)."""
    ensure_sme_journal_ready(conn, commit=False)
    labor_idle = _money(data.get('labor_idle'))
    oh_f_idle = _money(data.get('oh_fixed_idle'))
    if labor_idle <= 0 and oh_f_idle <= 0:
        return {'skipped': True, 'amount': 0}

    date_s = str(data.get('date_to') or '')[:10]
    fy = data.get('fiscal_year')
    period = data.get('period')
    doc = f"PBGT{fy}{int(period):02d}-{allocation_id}"
    cogs = _cogs_account(conn)
    acc_6271 = _oh_fixed_account(conn)

    lines: list[dict] = []
    seq = 1
    total = _money(labor_idle + oh_f_idle)
    lines.append({
        'sequence': seq, 'account_code': cogs,
        'debit': total, 'credit': 0,
        'description': f'{doc}: chi phí dưới công suất bình thường',
    })
    seq += 1
    if labor_idle > 0:
        lines.append({
            'sequence': seq, 'account_code': '622',
            'debit': 0, 'credit': labor_idle,
            'description': f'{doc}: NC dưới công suất → {cogs}',
        })
        seq += 1
    if oh_f_idle > 0:
        lines.append({
            'sequence': seq, 'account_code': acc_6271,
            'debit': 0, 'credit': oh_f_idle,
            'description': f'{doc}: SXC định phí dưới công suất → {cogs}',
        })

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='PBGT',
        document_no=f'{doc}-IDLE',
        document_id=allocation_id,
        business_type='CP_DUOI_CONG_SUAT',
        description=f'Dưới công suất bình thường — phân bổ #{allocation_id}',
        reference_document=doc,
        created_by=created_by,
        lines=lines,
    )
    if commit:
        sqlite_commit(conn, label='period_cost_allocation_journal')
    return {
        'journal_entry_id': entry['id'],
        'entry_no': entry.get('entry_no'),
        'amount': total,
        'cogs_account': cogs,
    }
