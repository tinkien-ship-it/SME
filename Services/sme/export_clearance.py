"""Bút toán XK 2 bước: xuất kho ra cảng (157) → thông quan (632/157 + 131/511/3333)."""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.export_payment import (
    PAYMENT_LC_USANCE,
    PAYMENT_PREPAID_FULL,
    PAYMENT_PREPAID_PARTIAL,
    REVENUE_ACCOUNT_DEFAULT,
    compute_split_fx_revenue_vnd,
    ensure_export_sale_schema,
    list_sale_advances,
    normalize_payment_mode,
)
from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    post_journal_entry,
    reverse_journal_entry,
    resolve_postable_account,
)

MONEY_Q = Decimal('0.01')

EXPORT_STATUS_SHIPPED = 'shipped'
EXPORT_STATUS_CLEARED = 'cleared'
STOCK_TYPE_EXPORT_SHIP = 'EXPORT_SHIP'
DOC_TYPES_SHIP = ('EXPORT_SHIP',)
DOC_TYPES_CLEARANCE = ('EXPORT_REVENUE', 'EXPORT_COGS', 'EXPORT_TAX')
DOC_TYPES_ALL = DOC_TYPES_SHIP + DOC_TYPES_CLEARANCE


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _fx(val) -> Decimal:
    from Services.sme.export_payment import _fx as fx
    return fx(val)


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def export_status_of(sale: dict) -> str:
    st = str(sale.get('export_status') or '').strip().lower()
    if st in (EXPORT_STATUS_SHIPPED, EXPORT_STATUS_CLEARED):
        return st
    # Phiếu cũ đã có DT → coi như đã thông quan
    return EXPORT_STATUS_SHIPPED


def inventory_role_for_line(line_type: str | None) -> str:
    from Services.sme.cogs_accounts import cogs_accounts_for_line
    _deb, cred, _ = cogs_accounts_for_line(line_type=line_type, channel='export')
    return cred or 'inv.goods'


def cogs_role_for_line(line_type: str | None) -> str:
    from Services.sme.cogs_accounts import cogs_accounts_for_line
    deb, _cred, _ = cogs_accounts_for_line(line_type=line_type, channel='export')
    return deb


def active_entries(
    conn: sqlite3.Connection,
    sale_id: int,
    doc_types: tuple[str, ...] = DOC_TYPES_ALL,
) -> list[int]:
    ph = ','.join('?' * len(doc_types))
    rows = conn.execute(
        f"""
        SELECT id FROM sme_journal_entries
        WHERE document_id = ?
          AND document_type IN ({ph})
          AND status = 'posted' AND reverses_id IS NULL
        ORDER BY id
        """,
        (sale_id, *doc_types),
    ).fetchall()
    return [int(r[0]) for r in rows]


def reverse_export_journals(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    posting_date: str | None = None,
    created_by: str | None = None,
    reason: str = 'Thay thế bút toán xuất khẩu',
    doc_types: tuple[str, ...] = DOC_TYPES_ALL,
) -> list[int]:
    out = []
    for eid in active_entries(conn, sale_id, doc_types):
        rev = reverse_journal_entry(
            conn, eid, posting_date=posting_date, created_by=created_by, reason=reason,
        )
        out.append(int(rev['id']))
    return out


def ship_cost_rows(conn: sqlite3.Connection, sale_id: int) -> list:
    sm_cols = _cols(conn, 'stock_moves')
    cost_expr = 'sm.cost_price' if 'cost_price' in sm_cols else (
        'sm.avg_cost' if 'avg_cost' in sm_cols else '0'
    )
    return conn.execute(
        f"""
        SELECT
            COALESCE(p.product_type, 'goods') AS product_type,
            SUM(ABS(COALESCE(sm.quantity, 0)) * COALESCE({cost_expr}, 0)) AS amount
        FROM stock_moves sm
        LEFT JOIN products p ON p.id = sm.product_id
        WHERE sm.ref_id = ?
          AND UPPER(sm.type) IN ('EXPORT_SHIP', 'SALE')
        GROUP BY COALESCE(p.product_type, 'goods')
        """,
        (sale_id,),
    ).fetchall()


def sync_export_ship_journals(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    created_by: str | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Bước 1: Nợ 157 / Có 155·156 — hàng gửi đi bán (chờ thông quan)."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_export_sale_schema(conn, commit=False)

    sale = conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone()
    if not sale:
        raise ValueError(f'Không tìm thấy phiếu bán #{sale_id}')
    s = dict(sale)
    if str(s.get('sale_type') or '').upper() != 'EXPORT':
        return {'posted': False, 'reason': 'not_export', 'entry_ids': []}
    if export_status_of(s) == EXPORT_STATUS_CLEARED and active_entries(conn, sale_id, DOC_TYPES_CLEARANCE):
        return {'posted': False, 'reason': 'already_cleared', 'entry_ids': []}

    active = active_entries(conn, sale_id, DOC_TYPES_SHIP)
    reversed_ids: list[int] = []
    posting_date = str(s.get('date') or '')[:10] or None
    if active and replace_existing:
        reversed_ids = reverse_export_journals(
            conn, sale_id, posting_date=posting_date, created_by=created_by,
            reason='Thay thế bút toán xuất kho ra cảng',
            doc_types=DOC_TYPES_SHIP,
        )
        active = []
    if active:
        return {
            'posted': False,
            'reason': 'already_posted',
            'phase': 'ship',
            'entry_ids': active,
            'reversed_entry_ids': reversed_ids,
        }

    from Services.sme.branch_filter import warehouse_branch_or_session

    branch = warehouse_branch_or_session(conn, s.get('warehouse_code'))
    sale_no = s.get('sale_no') or f'#{sale_id}'
    consign = resolve_postable_account(conn, 'inv.consignment')
    lines: list[dict] = []
    seq = 1
    total_cost = Decimal('0.00')
    for row in ship_cost_rows(conn, sale_id):
        amt = _money(row[1] if not isinstance(row, sqlite3.Row) else row['amount'])
        if amt <= 0:
            continue
        pt = row[0] if not isinstance(row, sqlite3.Row) else row['product_type']
        inv_acct = resolve_postable_account(conn, inventory_role_for_line(pt))
        lines.extend([
            {
                'sequence': seq,
                'account_code': consign,
                'debit': float(amt),
                'credit': 0,
                'description': f'Hàng gửi đi bán {sale_no}',
            },
            {
                'sequence': seq + 1,
                'account_code': inv_acct,
                'debit': 0,
                'credit': float(amt),
                'description': f'Xuất kho ra cảng {sale_no}',
            },
        ])
        seq += 2
        total_cost += amt

    posted: list[dict] = []
    if lines:
        posted.append(post_journal_entry(
            conn,
            posting_date=posting_date or '',
            document_date=str(s.get('date') or posting_date or '')[:10],
            document_type='EXPORT_SHIP',
            document_no=sale_no,
            document_id=sale_id,
            business_type='XUAT_KHAU_GUI_BAN',
            currency='VND',
            exchange_rate=1,
            description=f'Xuất kho ra cảng {sale_no}',
            reference_document=s.get('internal_transfer_doc_no') or sale_no,
            created_by=created_by,
            branch_code=branch,
            lines=lines,
        ))

    scols = _cols(conn, 'sale')
    if 'export_status' in scols:
        conn.execute(
            'UPDATE sale SET export_status = ? WHERE id = ?',
            (EXPORT_STATUS_SHIPPED, sale_id),
        )

    return {
        'posted': bool(posted),
        'phase': 'ship',
        'entry_ids': [p['id'] for p in posted],
        'reversed_entry_ids': reversed_ids,
        'consignment_vnd': float(total_cost),
        'export_status': EXPORT_STATUS_SHIPPED,
    }


def sync_export_clearance_journals(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    created_by: str | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Bước 2: Nợ 632/Có 157 + Nợ 131/Có 511·3333 (ngày thông quan)."""
    ensure_sme_journal_ready(conn, commit=False)
    ensure_export_sale_schema(conn, commit=False)

    sale = conn.execute('SELECT * FROM sale WHERE id = ?', (sale_id,)).fetchone()
    if not sale:
        raise ValueError(f'Không tìm thấy phiếu bán #{sale_id}')
    s = dict(sale)
    if str(s.get('sale_type') or '').upper() != 'EXPORT':
        return {'posted': False, 'reason': 'not_export', 'entry_ids': []}

    customs = str(s.get('customs_decl_no') or '').strip()
    bl_no = str(s.get('bl_no') or '').strip()
    if not customs:
        raise ValueError('Thiếu số tờ khai hải quan đã thông quan (TKHQ)')
    if not bl_no:
        raise ValueError('Thiếu số vận đơn Bill of Lading (B/L)')

    if not active_entries(conn, sale_id, DOC_TYPES_SHIP):
        ship = sync_export_ship_journals(conn, sale_id, created_by=created_by)
        if not ship.get('entry_ids') and not any(
            _money(r[1] if not isinstance(r, sqlite3.Row) else r['amount']) > 0
            for r in ship_cost_rows(conn, sale_id)
        ):
            raise ValueError(
                'Chưa có bút toán xuất kho ra cảng (Nợ 157). '
                'Hãy lưu phiếu xuất kho ra cảng trước.'
            )

    has_cogs = bool(active_entries(conn, sale_id, ('EXPORT_COGS',)))
    has_rev = bool(active_entries(conn, sale_id, ('EXPORT_REVENUE',)))
    has_tax = bool(active_entries(conn, sale_id, ('EXPORT_TAX',)))

    reversed_ids: list[int] = []
    posting_date = (
        str(s.get('risk_transfer_date') or s.get('date') or '')[:10] or None
    )
    if replace_existing and (has_cogs or has_rev or has_tax):
        reversed_ids = reverse_export_journals(
            conn, sale_id, posting_date=posting_date, created_by=created_by,
            reason='Thay thế bút toán thông quan XK',
            doc_types=DOC_TYPES_CLEARANCE,
        )
        has_cogs = has_rev = has_tax = False

    if has_cogs and has_rev and not replace_existing:
        return {
            'posted': False,
            'reason': 'already_posted',
            'phase': 'clearance',
            'entry_ids': active_entries(conn, sale_id, DOC_TYPES_CLEARANCE),
            'reversed_entry_ids': reversed_ids,
        }

    from Services.sme.branch_filter import warehouse_branch_or_session

    currency = (s.get('currency') or 'USD').upper()
    clearance_rate = _fx(s.get('customs_fx_rate') or s.get('exchange_rate') or 1)
    total_fc = _money(s.get('amount_fc') or 0)
    mode = normalize_payment_mode(s.get('payment_mode'), sale_type='EXPORT')

    advances = []
    try:
        advances = list_sale_advances(conn, sale_id)
    except sqlite3.OperationalError:
        advances = []
    if not advances and _money(s.get('advance_fc')) > 0:
        advances = [{
            'amount_fc': float(s.get('advance_fc') or 0),
            'exchange_rate': float(s.get('exchange_rate') or clearance_rate),
            'amount_vnd': float(s.get('advance_vnd') or 0),
        }]
    use_advances = mode in (PAYMENT_PREPAID_FULL, PAYMENT_PREPAID_PARTIAL)
    split = compute_split_fx_revenue_vnd(
        total_fc=total_fc,
        revenue_rate=clearance_rate,
        advances=advances if use_advances else [],
    )
    revenue_vnd = _money(split['revenue_vnd'])
    export_tax_vnd = _money(s.get('export_tax_vnd') or 0)
    if export_tax_vnd <= 0 and _money(s.get('export_tax_fc')) > 0:
        export_tax_vnd = _money(_money(s.get('export_tax_fc')) * clearance_rate)
    net_revenue = _money(revenue_vnd - export_tax_vnd)
    if net_revenue < 0:
        net_revenue = Decimal('0.00')

    branch = warehouse_branch_or_session(conn, s.get('warehouse_code'))
    sale_no = s.get('sale_no') or f'#{sale_id}'
    rev_acct = resolve_postable_account(conn, REVENUE_ACCOUNT_DEFAULT)
    ar_acct = resolve_postable_account(conn, 'ar.customer')
    consign = resolve_postable_account(conn, 'inv.consignment')
    posted: list[dict] = []

    # --- a) Giá vốn: Nợ 632 / Có 157 (bổ sung nếu thiếu) ---
    if not has_cogs:
        cogs_lines: list[dict] = []
        seq = 1
        for row in ship_cost_rows(conn, sale_id):
            amt = _money(row[1] if not isinstance(row, sqlite3.Row) else row['amount'])
            if amt <= 0:
                continue
            pt = row[0] if not isinstance(row, sqlite3.Row) else row['product_type']
            cogs_lines.extend([
                {
                    'sequence': seq,
                    'account_code': resolve_postable_account(conn, cogs_role_for_line(pt)),
                    'debit': float(amt),
                    'credit': 0,
                    'description': f'Giá vốn {sale_no}',
                },
                {
                    'sequence': seq + 1,
                    'account_code': consign,
                    'debit': 0,
                    'credit': float(amt),
                    'description': f'Tất toán hàng gửi đi bán {sale_no}',
                },
            ])
            seq += 2
        if cogs_lines:
            posted.append(post_journal_entry(
                conn,
                posting_date=posting_date or '',
                document_date=str(s.get('date') or posting_date or '')[:10],
                document_type='EXPORT_COGS',
                document_no=sale_no,
                document_id=sale_id,
                business_type='XUAT_KHAU_GV',
                currency='VND',
                exchange_rate=1,
                description=f'Giá vốn {sale_no}',
                reference_document=customs or bl_no,
                created_by=created_by,
                branch_code=branch,
                lines=cogs_lines,
            ))

    # --- b) Doanh thu: Nợ 131 / Có 511·3333 (bổ sung nếu thiếu) ---
    if not has_rev:
        rev_lines = [
            {
                'sequence': 1,
                'account_code': ar_acct,
                'debit': float(revenue_vnd),
                'credit': 0,
                'debit_fc': float(total_fc) if currency != 'VND' else 0,
                'credit_fc': 0,
                'partner_type': 'customer',
                'description': f'Phải thu {sale_no}',
            },
            {
                'sequence': 2,
                'account_code': rev_acct,
                'debit': 0,
                'credit': float(net_revenue if export_tax_vnd > 0 else revenue_vnd),
                'debit_fc': 0,
                'credit_fc': float(total_fc) if currency != 'VND' and export_tax_vnd <= 0 else 0,
                'description': f'Doanh thu {sale_no}',
            },
        ]
        if export_tax_vnd > 0:
            rev_lines.append({
                'sequence': 3,
                'account_code': resolve_postable_account(conn, '3333'),
                'debit': 0,
                'credit': float(export_tax_vnd),
                'description': f'Thuế xuất khẩu {sale_no}',
            })
        if revenue_vnd > 0:
            posted.append(post_journal_entry(
                conn,
                posting_date=posting_date or '',
                document_date=str(s.get('date') or posting_date or '')[:10],
                document_type='EXPORT_REVENUE',
                document_no=sale_no,
                document_id=sale_id,
                business_type='XUAT_KHAU_DT',
                currency=currency,
                exchange_rate=float(clearance_rate),
                description=f'Thông quan {sale_no}',
                reference_document=bl_no or customs or sale_no,
                created_by=created_by,
                branch_code=branch,
                lines=rev_lines,
            ))
        elif total_fc > 0:
            raise ValueError(
                f'Không tính được doanh thu VND cho {sale_no} '
                f'(amount_fc={float(total_fc)}, tỷ giá={float(clearance_rate)})'
            )

    if not posted and has_cogs and has_rev:
        return {
            'posted': False,
            'reason': 'already_posted',
            'phase': 'clearance',
            'entry_ids': active_entries(conn, sale_id, DOC_TYPES_CLEARANCE),
            'reversed_entry_ids': reversed_ids,
        }

    ar_status = 'open'
    if mode == PAYMENT_PREPAID_FULL or (
        mode == PAYMENT_PREPAID_PARTIAL and _money(split['remain_fc']) <= 0
    ):
        ar_status = 'settled'
    elif mode == PAYMENT_LC_USANCE:
        ar_status = 'accepted'

    # Bổ sung bút toán thiếu (GV/DT) không được reset công nợ đã thu
    was_partial_repair = (has_cogs or has_rev or has_tax) and not replace_existing
    if was_partial_repair:
        settled_fc = _money(s.get('settle_amount_fc') or 0)
        adv_fc = _money(s.get('advance_fc') or 0)
        if total_fc > 0 and settled_fc + adv_fc + Decimal('0.00005') >= total_fc:
            ar_status = 'settled'
        else:
            existing_ar = str(s.get('ar_status') or '').strip()
            if existing_ar:
                ar_status = existing_ar

    scols = _cols(conn, 'sale')
    sets, vals = [], []
    if 'ar_status' in scols:
        sets.append('ar_status = ?')
        vals.append(ar_status)
    if 'export_status' in scols:
        sets.append('export_status = ?')
        vals.append(EXPORT_STATUS_CLEARED)
    if 'exchange_rate' in scols:
        sets.append('exchange_rate = ?')
        vals.append(float(clearance_rate))
    if sets:
        vals.append(sale_id)
        conn.execute(f"UPDATE sale SET {', '.join(sets)} WHERE id = ?", vals)

    return {
        'posted': bool(posted),
        'phase': 'clearance',
        'entry_ids': [p['id'] for p in posted] + (
            active_entries(conn, sale_id, DOC_TYPES_CLEARANCE) if not posted else []
        ),
        'reversed_entry_ids': reversed_ids,
        'revenue_vnd': float(revenue_vnd),
        'net_revenue_vnd': float(net_revenue),
        'export_tax_vnd': float(export_tax_vnd),
        'split': split,
        'ar_status': ar_status,
        'export_status': EXPORT_STATUS_CLEARED,
        'repaired_revenue': (not has_rev) and bool(posted),
    }
