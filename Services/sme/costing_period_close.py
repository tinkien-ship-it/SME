"""Chốt giá thành cuối kỳ — 3 phương án + công thức DD TT99 + điều chỉnh giá tạm."""
from __future__ import annotations

import calendar
import sqlite3
from datetime import datetime
from typing import Any

from db_utils import sqlite_commit
from Services.sme.costing_policy import (
    METHOD_ACTUAL,
    METHOD_NORMAL,
    METHOD_STANDARD,
    ensure_costing_policy_schema,
    get_costing_policy,
    get_period_close,
)
from Services.sme.period_cost_allocation import (
    METHOD_ACTUAL as PA_ACTUAL,
    METHOD_NORMAL as PA_NORMAL,
    preview_allocation,
)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _money(v) -> float:
    return round(float(v or 0), 2)


def _f(v) -> float:
    return float(v or 0)


def _period_bounds(fiscal_year: int, period: int) -> tuple[str, str]:
    last = calendar.monthrange(int(fiscal_year), int(period))[1]
    return (
        f'{int(fiscal_year):04d}-{int(period):02d}-01',
        f'{int(fiscal_year):04d}-{int(period):02d}-{last:02d}',
    )


def collect_622_627_from_gl(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
) -> dict[str, float]:
    """Lấy phát sinh Nợ thuần 622 / 6271 (định phí) / 6272 (biến phí) trong kỳ.

    - 6271* → oh_fixed
    - 6272* → oh_variable
    - 627 (đúng mã cha, phát sinh còn sót) → gộp vào oh_fixed (an toàn TT99)
    Trừ bút toán PBGT / PBGTADJ.
    """
    ensure_costing_policy_schema(conn)
    labor = 0.0
    oh_f = 0.0
    oh_v = 0.0
    try:
        rows = conn.execute(
            """
            SELECT jl.account_code,
                   SUM(COALESCE(jl.debit,0) - COALESCE(jl.credit,0)) AS net
            FROM journal_lines jl
            JOIN journal_entries je ON je.id = jl.journal_entry_id
            WHERE COALESCE(je.is_reversed, 0) = 0
              AND COALESCE(je.status, 'posted') = 'posted'
              AND date(je.posting_date) >= date(?)
              AND date(je.posting_date) <= date(?)
              AND (
                    jl.account_code = '622' OR jl.account_code LIKE '622%'
                 OR jl.account_code = '627' OR jl.account_code LIKE '627%'
              )
              AND COALESCE(je.document_type, '') NOT IN ('PBGT', 'PBGTADJ')
            GROUP BY jl.account_code
            """,
            (date_from[:10], date_to[:10]),
        ).fetchall()
        for r in rows:
            code = str(r[0] or '').strip()
            net = _money(r[1])
            if net <= 0:
                continue
            if code.startswith('622'):
                labor = _money(labor + net)
            elif code.startswith('6271'):
                oh_f = _money(oh_f + net)
            elif (
                code.startswith('6272')
                or code.startswith('6273')  # legacy 08f (đã ngừng mở)
                or code.startswith('6274')
                or code.startswith('6278')
            ):
                oh_v = _money(oh_v + net)
            elif code == '627' or code.startswith('627'):
                # 627 cha hoặc tiểu khoản SXC khác → mặc định định phí
                oh_f = _money(oh_f + net)
    except sqlite3.Error:
        pass
    return {
        'labor_amount': labor,
        'oh_fixed_amount': oh_f,
        'oh_variable_amount': oh_v,
    }


def _wip_opening(conn: sqlite3.Connection, fiscal_year: int, period: int) -> float:
    """DD đầu kỳ = DD cuối kỳ trước đã chốt, hoặc ước lượng từ lệnh chưa nhập đủ."""
    prev_y, prev_p = (fiscal_year, period - 1) if period > 1 else (fiscal_year - 1, 12)
    prev = get_period_close(conn, prev_y, prev_p)
    if prev:
        return _money(prev.get('wip_closing'))
    # Fallback: tổng chi phí lệnh chưa nhập đủ trước kỳ
    date_from, _ = _period_bounds(fiscal_year, period)
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE
                  WHEN COALESCE(qty_received,0) <= 0 THEN COALESCE(total_cost, total_material_cost, 0)
                  ELSE COALESCE(total_cost, total_material_cost, 0)
                       * (1.0 - COALESCE(qty_received,0)
                          / NULLIF(COALESCE(qty_planned, qty_completed, 0), 0))
                END
            ), 0)
            FROM production_orders
            WHERE COALESCE(status,'') != 'cancelled'
              AND production_date < ?
              AND COALESCE(qty_received,0) < COALESCE(qty_planned, qty_completed, 0)
            """,
            (date_from,),
        ).fetchone()
        return _money(row[0] if row else 0)
    except sqlite3.Error:
        return 0.0


def _wip_closing_from_orders(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
    lines: list[dict],
) -> float:
    """DD cuối kỳ = chi phí còn lại trên lệnh chưa nhập đủ trong phạm vi."""
    by_id = {int(ln['production_order_id']): ln for ln in lines}
    total = 0.0
    rows = conn.execute(
        """
        SELECT id, COALESCE(qty_planned, qty_completed, 0) AS qty,
               COALESCE(qty_received, 0) AS qty_recv,
               COALESCE(total_material_cost, 0) AS mat
        FROM production_orders
        WHERE COALESCE(status,'') != 'cancelled'
          AND production_date >= ? AND production_date <= ?
        """,
        (date_from, date_to),
    ).fetchall()
    for r in rows:
        oid = int(r[0])
        qty = _f(r[1])
        recv = _f(r[2])
        ln = by_id.get(oid)
        order_total = _money(ln['total_cost']) if ln else _money(r[3])
        if qty <= 0:
            continue
        remain_ratio = max(0.0, (qty - recv) / qty)
        total = _money(total + order_total * remain_ratio)
    return total


def _fg_qty_received(conn: sqlite3.Connection, date_from: str, date_to: str) -> float:
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(r.qty), 0)
            FROM production_fg_receipts r
            JOIN production_orders o ON o.id = r.order_id
            WHERE r.cancelled_at IS NULL
              AND r.receipt_date >= ? AND r.receipt_date <= ?
              AND COALESCE(o.status,'') != 'cancelled'
            """,
            (date_from, date_to),
        ).fetchone()
        return _f(row[0] if row else 0)
    except sqlite3.Error:
        return 0.0


def preview_period_close(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    labor_amount: float | None = None,
    oh_fixed_amount: float | None = None,
    oh_variable_amount: float | None = None,
    cost_reductions: float = 0,
) -> dict[str, Any]:
    policy = get_costing_policy(conn)
    method = policy['allocation_method']
    date_from, date_to = _period_bounds(fiscal_year, period)

    collected = collect_622_627_from_gl(conn, date_from=date_from, date_to=date_to)
    labor = _money(labor_amount if labor_amount is not None else collected['labor_amount'])
    oh_f = _money(oh_fixed_amount if oh_fixed_amount is not None else collected['oh_fixed_amount'])
    oh_v = _money(oh_variable_amount if oh_variable_amount is not None else collected['oh_variable_amount'])

    # PA3 chốt thực tế: dùng engine PA1 (công suất) để đúng TT99 khi dưới CS;
    # PA2 giữ chia hết. Giá tạm chỉ là giữa kỳ.
    engine_method = PA_NORMAL if method in (METHOD_NORMAL, METHOD_STANDARD) else PA_ACTUAL
    if method == METHOD_ACTUAL:
        engine_method = PA_ACTUAL

    alloc_preview = None
    if labor + oh_f + oh_v > 0:
        try:
            alloc_preview = preview_allocation(
                conn,
                fiscal_year=fiscal_year,
                period=period,
                date_from=date_from,
                date_to=date_to,
                labor_amount=labor,
                oh_fixed_amount=oh_f,
                oh_variable_amount=oh_v,
                allocation_method=engine_method,
                normal_capacity_month=policy.get('normal_capacity_month'),
                working_days_month=policy.get('working_days_month'),
            )
        except ValueError as exc:
            # Không có lệnh trong kỳ — vẫn cho xem DD
            alloc_preview = {'error': str(exc), 'lines': [], 'labor_idle': 0, 'oh_fixed_idle': 0,
                             'labor_allocated': 0, 'oh_fixed_allocated': 0, 'oh_variable_allocated': 0}

    lines = (alloc_preview or {}).get('lines') or []
    wip_open = _wip_opening(conn, fiscal_year, period)
    # CPSX phát sinh trong kỳ ≈ NVL lệnh trong kỳ + NC/SXC phân bổ (không gồm idle)
    mat_period = 0.0
    for ln in lines:
        mat_period = _money(mat_period + _f(ln.get('material_cost')))
    if not lines:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(total_material_cost),0) FROM production_orders
            WHERE COALESCE(status,'') != 'cancelled'
              AND production_date >= ? AND production_date <= ?
            """,
            (date_from, date_to),
        ).fetchone()
        mat_period = _money(row[0] if row else 0)

    allocated_conv = _money(
        _f((alloc_preview or {}).get('labor_allocated'))
        + _f((alloc_preview or {}).get('oh_fixed_allocated'))
        + _f((alloc_preview or {}).get('oh_variable_allocated'))
    )
    period_prod = _money(mat_period + allocated_conv)
    reductions = _money(cost_reductions)
    wip_close = _wip_closing_from_orders(
        conn, date_from=date_from, date_to=date_to, lines=lines,
    )
    fg_qty = _fg_qty_received(conn, date_from, date_to)
    actual_total = _money(wip_open + period_prod - wip_close - reductions)
    unit = round(actual_total / fg_qty, 4) if fg_qty > 0 else 0.0

    variance_lines = []
    for ln in lines:
        oid = int(ln['production_order_id'])
        row = conn.execute(
            """
            SELECT COALESCE(provisional_labor,0), COALESCE(provisional_oh_fixed,0),
                   COALESCE(provisional_oh_variable,0), COALESCE(qty_received,0),
                   COALESCE(qty_planned, qty_completed, 0),
                   COALESCE(provisional_unit_cost, unit_cost, 0)
            FROM production_orders WHERE id = ?
            """,
            (oid,),
        ).fetchone()
        if not row:
            continue
        recv = _f(row[3])
        qty = _f(row[4])
        if recv <= 0 or qty <= 0:
            continue
        ratio = min(1.0, recv / qty)
        prov_conv = _money((_f(row[0]) + _f(row[1]) + _f(row[2])) * ratio)
        act_conv = _money(
            (_f(ln.get('labor_allocated')) + _f(ln.get('oh_fixed_allocated'))
             + _f(ln.get('oh_variable_allocated'))) * ratio
        )
        diff = _money(act_conv - prov_conv)
        if abs(diff) < 0.01:
            continue
        variance_lines.append({
            'production_order_id': oid,
            'voucher_no': ln.get('voucher_no'),
            'finished_product_id': ln.get('finished_product_id'),
            'qty_received': recv,
            'provisional_conversion': prov_conv,
            'actual_conversion': act_conv,
            'variance': diff,
            'direction': 'supplement' if diff > 0 else 'reverse',
        })

    existing_close = get_period_close(conn, fiscal_year, period)

    return {
        'fiscal_year': int(fiscal_year),
        'period': int(period),
        'date_from': date_from,
        'date_to': date_to,
        'allocation_method': method,
        'allocation_method_label': policy.get('allocation_method_label'),
        'engine_method': engine_method,
        'collected_from_gl': collected,
        'labor_amount': labor,
        'oh_fixed_amount': oh_f,
        'oh_variable_amount': oh_v,
        'wip_opening': wip_open,
        'period_production_cost': period_prod,
        'wip_closing': wip_close,
        'cost_reductions': reductions,
        'fg_qty_received': fg_qty,
        'actual_total_cost': actual_total,
        'actual_unit_cost': unit,
        'labor_idle': _money((alloc_preview or {}).get('labor_idle')),
        'oh_fixed_idle': _money((alloc_preview or {}).get('oh_fixed_idle')),
        'allocation_preview': alloc_preview,
        'variance_lines': variance_lines,
        'formula': (
            'GT thực tế nhập kho = DD đầu kỳ + CPSX phát sinh trong kỳ '
            '− DD cuối kỳ − các khoản ghi giảm'
        ),
        'already_closed': existing_close is not None,
        'existing_close': existing_close,
        'can_reclose': True,
        'reclose_hint': (
            'Kỳ đã chốt (có thể do tự động ngày 1). '
            'Sau khi hoàn thiện sổ tháng trước / bổ sung chi phí còn thiếu, '
            'hãy «Đảo chốt» rồi «Chốt lại thủ công», hoặc dùng «Chạy lại thủ công».'
            if existing_close else None
        ),
    }


def post_variance_adjust_journals(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    variance_lines: list[dict],
    created_by: str = '',
) -> dict | None:
    if not variance_lines:
        return None
    from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry

    ensure_sme_journal_ready(conn, commit=False)
    date_to = _period_bounds(fiscal_year, period)[1]
    doc = f'PBGTADJ{fiscal_year}{int(period):02d}'
    lines = []
    seq = 1
    for v in variance_lines:
        amt = abs(_money(v.get('variance')))
        if amt < 0.01:
            continue
        pid = v.get('finished_product_id')
        vn = v.get('voucher_no') or ''
        if v.get('direction') == 'supplement':
            lines.append({
                'sequence': seq, 'account_code': '155', 'debit': amt, 'credit': 0,
                'product_id': pid,
                'description': f'{doc}: bổ sung GT tạm {vn}',
            })
            lines.append({
                'sequence': seq + 1, 'account_code': '154', 'debit': 0, 'credit': amt,
                'product_id': pid,
                'description': f'{doc}: bổ sung từ 154 {vn}',
            })
        else:
            lines.append({
                'sequence': seq, 'account_code': '154', 'debit': amt, 'credit': 0,
                'product_id': pid,
                'description': f'{doc}: đảo GT tạm {vn}',
            })
            lines.append({
                'sequence': seq + 1, 'account_code': '155', 'debit': 0, 'credit': amt,
                'product_id': pid,
                'description': f'{doc}: giảm 155 {vn}',
            })
        seq += 2

    if not lines:
        return None
    entry = post_journal_entry(
        conn,
        posting_date=date_to,
        document_date=date_to,
        document_type='PBGTADJ',
        document_no=doc,
        document_id=int(period),
        business_type='DIEU_CHINH_GT_TAM',
        description=f'Điều chỉnh giá thành tạm kỳ {period}/{fiscal_year}',
        reference_document=doc,
        created_by=created_by,
        lines=lines,
    )
    return entry


def reverse_period_close(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    created_by: str = '',
    reason: str = '',
    commit: bool = True,
) -> dict[str, Any]:
    """Đảo lần chốt giá thành kỳ (sau auto ngày 1 hoặc chốt tay) để chạy lại."""
    ensure_costing_policy_schema(conn)
    existing = get_period_close(conn, fiscal_year, period)
    if not existing:
        raise ValueError(f'Kỳ {period}/{fiscal_year} chưa chốt — không có gì để đảo')

    reason_txt = (reason or 'Đảo chốt để chạy lại sau khi hoàn sổ / bổ sung chi phí').strip()

    # Đảo bút toán điều chỉnh giá tạm
    adj_id = existing.get('adjust_journal_entry_id')
    if adj_id:
        try:
            from Services.sme.journal_engine import reverse_journal_entry
            reverse_journal_entry(
                conn, int(adj_id), created_by=created_by,
                reason=reason_txt,
            )
        except ValueError:
            pass

    # Đảo phân bổ 622/627 (+ idle) và trả lệnh về giá tạm
    alloc_id = existing.get('allocation_id')
    if alloc_id:
        from Services.sme.period_cost_allocation import reverse_allocation
        reverse_allocation(
            conn, int(alloc_id),
            created_by=created_by,
            reason=reason_txt,
            commit=False,
        )

    # UNIQUE(fy, period, status): dọn các bản reversed cũ → history
    conn.execute(
        """
        UPDATE sme_costing_period_closes
        SET status = 'history'
        WHERE fiscal_year = ? AND period = ? AND status = 'reversed'
        """,
        (int(fiscal_year), int(period)),
    )
    conn.execute(
        """
        UPDATE sme_costing_period_closes
        SET status = 'reversed',
            note = TRIM(COALESCE(note,'') || ?)
        WHERE id = ?
        """,
        (
            f' | Đảo bởi {created_by or "user"}: {reason_txt}',
            int(existing['id']),
        ),
    )
    if commit:
        sqlite_commit(conn, label='costing_period_close')
    return {
        'reversed': True,
        'close_id': existing['id'],
        'fiscal_year': int(fiscal_year),
        'period': int(period),
        'allocation_id': alloc_id,
        'message': f'Đã đảo chốt kỳ {period}/{fiscal_year} — có thể chốt lại thủ công',
    }


def close_costing_period(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    labor_amount: float | None = None,
    oh_fixed_amount: float | None = None,
    oh_variable_amount: float | None = None,
    cost_reductions: float = 0,
    close_idle_now: bool = True,
    created_by: str = '',
    auto_posted: bool = False,
    replace_existing: bool = False,
    commit: bool = True,
) -> dict[str, Any]:
    """Chốt cuối kỳ: phân bổ 622/627 + điều chỉnh giá tạm + lưu DD.

    ``replace_existing=True``: đảo lần chốt hiện có rồi chốt lại (sau khi hoàn sổ /
    bổ sung chi phí tháng trước chưa kịp vào auto ngày 1).
    """
    ensure_costing_policy_schema(conn)
    existing = get_period_close(conn, fiscal_year, period)
    if existing and not replace_existing:
        raise ValueError(
            f'Kỳ {period}/{fiscal_year} đã chốt giá thành (#{existing["id"]}'
            f'{", tự động" if existing.get("auto_posted") else ""}). '
            f'Dùng «Chạy lại thủ công» (replace) hoặc đảo chốt trước.'
        )
    if existing and replace_existing:
        reverse_period_close(
            conn,
            fiscal_year=fiscal_year,
            period=period,
            created_by=created_by,
            reason='Chạy lại thủ công sau khi hoàn sổ / bổ sung chi phí',
            commit=False,
        )

    preview = preview_period_close(
        conn,
        fiscal_year=fiscal_year,
        period=period,
        labor_amount=labor_amount,
        oh_fixed_amount=oh_fixed_amount,
        oh_variable_amount=oh_variable_amount,
        cost_reductions=cost_reductions,
    )

    allocation_id = None
    ap = preview.get('allocation_preview') or {}
    if not ap.get('error') and (
        preview['labor_amount'] + preview['oh_fixed_amount'] + preview['oh_variable_amount'] > 0
    ):
        allocation_id = _post_allocation_allowing_received(
            conn,
            preview=ap,
            close_idle_now=close_idle_now and preview['allocation_method'] != METHOD_ACTUAL,
            created_by=created_by,
        ).get('id')

    preview2 = preview_period_close(
        conn,
        fiscal_year=fiscal_year,
        period=period,
        labor_amount=preview['labor_amount'],
        oh_fixed_amount=preview['oh_fixed_amount'],
        oh_variable_amount=preview['oh_variable_amount'],
        cost_reductions=cost_reductions,
    )
    variance_lines = preview.get('variance_lines') or []
    adj = post_variance_adjust_journals(
        conn,
        fiscal_year=fiscal_year,
        period=period,
        variance_lines=variance_lines,
        created_by=created_by,
    )

    if allocation_id:
        conn.execute(
            """
            UPDATE production_orders SET cost_basis = 'actual', cost_finalized = 1
            WHERE allocation_id = ?
            """,
            (allocation_id,),
        )

    note = 'Chốt tự động' if auto_posted else (
        'Chốt lại thủ công (sau đảo)' if replace_existing else 'Chốt thủ công'
    )
    conn.execute(
        """
        INSERT INTO sme_costing_period_closes (
            fiscal_year, period, allocation_method, date_from, date_to,
            wip_opening, period_production_cost, wip_closing, cost_reductions,
            fg_qty_received, actual_total_cost, actual_unit_cost,
            labor_amount, oh_fixed_amount, oh_variable_amount,
            labor_idle, oh_fixed_idle, status, auto_posted,
            allocation_id, adjust_journal_entry_id, note, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?, ?, ?)
        """,
        (
            fiscal_year, period, preview['allocation_method'],
            preview['date_from'], preview['date_to'],
            preview['wip_opening'], preview['period_production_cost'],
            preview2['wip_closing'], preview['cost_reductions'],
            preview['fg_qty_received'], preview['actual_total_cost'], preview['actual_unit_cost'],
            preview['labor_amount'], preview['oh_fixed_amount'], preview['oh_variable_amount'],
            preview['labor_idle'], preview['oh_fixed_idle'],
            1 if auto_posted else 0,
            allocation_id,
            (adj or {}).get('id'),
            note,
            created_by, _now(),
        ),
    )
    if commit:
        sqlite_commit(conn, label='costing_period_close')
    return get_period_close(conn, fiscal_year, period) or preview


def reclose_costing_period(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    labor_amount: float | None = None,
    oh_fixed_amount: float | None = None,
    oh_variable_amount: float | None = None,
    cost_reductions: float = 0,
    close_idle_now: bool = True,
    created_by: str = '',
    commit: bool = True,
) -> dict[str, Any]:
    """Tiện ích: đảo (nếu đã chốt) + chốt lại thủ công với số liệu sổ mới nhất."""
    return close_costing_period(
        conn,
        fiscal_year=fiscal_year,
        period=period,
        labor_amount=labor_amount,
        oh_fixed_amount=oh_fixed_amount,
        oh_variable_amount=oh_variable_amount,
        cost_reductions=cost_reductions,
        close_idle_now=close_idle_now,
        created_by=created_by,
        auto_posted=False,
        replace_existing=True,
        commit=commit,
    )


def _post_allocation_allowing_received(
    conn: sqlite3.Connection,
    *,
    preview: dict,
    close_idle_now: bool,
    created_by: str,
) -> dict:
    """Giống post_allocation nhưng cho phép lệnh đã nhập kho (điều chỉnh giá tạm)."""
    from Services.sme.period_cost_allocation import (
        STATUS_IDLE_CLOSED,
        STATUS_POSTED,
        ensure_period_cost_allocation_schema,
        get_allocation,
    )
    from Services.sme.period_cost_allocation_journal import post_allocation_journals

    ensure_period_cost_allocation_schema(conn)
    data = preview
    conflict = conn.execute(
        """
        SELECT id FROM sme_period_cost_allocations
        WHERE fiscal_year = ? AND period = ?
          AND date_from = ? AND date_to = ?
          AND status IN ('posted', 'idle_closed')
        """,
        (data['fiscal_year'], data['period'], data['date_from'], data['date_to']),
    ).fetchone()
    if conflict:
        raise ValueError(f'Đã có phân bổ #{conflict[0]} cho khoảng này')

    c = conn.cursor()
    c.execute(
        """
        INSERT INTO sme_period_cost_allocations (
            fiscal_year, period, date_from, date_to, days_count, allocation_method,
            normal_capacity_month, working_days_month, capacity_in_scope,
            labor_amount, oh_fixed_amount, oh_variable_amount,
            labor_rate, oh_fixed_rate,
            labor_allocated, oh_fixed_allocated, oh_variable_allocated,
            labor_idle, oh_fixed_idle, equivalent_qty_total,
            status, note, created_by, created_at, posted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data['fiscal_year'], data['period'], data['date_from'], data['date_to'],
            data['days_count'], data['allocation_method'],
            data['normal_capacity_month'], data['working_days_month'], data['capacity_in_scope'],
            data['labor_amount'], data['oh_fixed_amount'], data['oh_variable_amount'],
            data['labor_rate'], data['oh_fixed_rate'],
            data['labor_allocated'], data['oh_fixed_allocated'], data['oh_variable_allocated'],
            data['labor_idle'], data['oh_fixed_idle'], data['equivalent_qty_total'],
            STATUS_POSTED, 'Chốt giá thành kỳ', (created_by or '').strip(),
            _now(), _now(),
        ),
    )
    alloc_id = int(c.lastrowid)
    for ln in data.get('lines') or []:
        c.execute(
            """
            INSERT INTO sme_period_cost_allocation_lines (
                allocation_id, production_order_id, finished_product_id, voucher_no,
                qty, equivalent_factor, equivalent_qty, material_cost,
                labor_allocated, oh_fixed_allocated, oh_variable_allocated,
                total_cost, unit_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alloc_id, ln['production_order_id'], ln['finished_product_id'], ln.get('voucher_no'),
                ln['qty'], ln['equivalent_factor'], ln['equivalent_qty'], ln['material_cost'],
                ln['labor_allocated'], ln['oh_fixed_allocated'], ln['oh_variable_allocated'],
                ln['total_cost'], ln['unit_cost'],
            ),
        )
        other = _money(_f(ln['oh_fixed_allocated']) + _f(ln['oh_variable_allocated']))
        c.execute(
            """
            UPDATE production_orders SET
                labor_cost = ?, other_cost = ?,
                oh_fixed_cost = ?, oh_variable_cost = ?,
                total_cost = ?, unit_cost = ?,
                cost_finalized = 1, allocation_id = ?, cost_basis = 'actual'
            WHERE id = ?
            """,
            (
                ln['labor_allocated'], other,
                ln['oh_fixed_allocated'], ln['oh_variable_allocated'],
                ln['total_cost'], ln['unit_cost'],
                alloc_id, ln['production_order_id'],
            ),
        )

    journals = post_allocation_journals(
        conn, alloc_id, data,
        close_idle_now=close_idle_now,
        created_by=created_by,
        commit=False,
    )
    status = STATUS_IDLE_CLOSED if (
        close_idle_now and (_f(data.get('labor_idle')) > 0 or _f(data.get('oh_fixed_idle')) > 0)
    ) else STATUS_POSTED
    c.execute(
        """
        UPDATE sme_period_cost_allocations SET
            collect_journal_entry_id = ?,
            allocate_journal_entry_id = ?,
            idle_journal_entry_id = ?,
            status = ?,
            idle_closed_at = CASE WHEN ? = 'idle_closed' THEN ? ELSE idle_closed_at END
        WHERE id = ?
        """,
        (
            journals.get('collect_journal_entry_id'),
            journals.get('allocate_journal_entry_id'),
            journals.get('idle_journal_entry_id'),
            status, status, _now(), alloc_id,
        ),
    )
    return get_allocation(conn, alloc_id)


def run_auto_close_for_conn(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
) -> dict[str, Any]:
    if not get_costing_policy(conn).get('auto_close'):
        return {'skipped': True, 'reason': 'auto_close_off'}
    if get_period_close(conn, fiscal_year, period):
        return {'skipped': True, 'reason': 'already_closed'}
    try:
        result = close_costing_period(
            conn,
            fiscal_year=fiscal_year,
            period=period,
            close_idle_now=True,
            created_by='system:auto_close',
            auto_posted=True,
            commit=True,
        )
        return {'posted': True, 'data': result}
    except ValueError as exc:
        return {'skipped': True, 'reason': str(exc)}


def run_costing_auto_close_for_all_tenants() -> dict[str, Any]:
    """Ngày 1: chốt giá thành tháng trước cho tenant SME bật auto_close."""
    from datetime import date as date_cls

    from db_utils import get_main_db_connection, get_tenant_db_connection
    from Services.subscription_service import parse_tenant_settings
    from Services.tenant_profile import is_sme_regime, normalize_accounting_regime

    today = date_cls.today()
    if today.month == 1:
        fy, period = today.year - 1, 12
    else:
        fy, period = today.year, today.month - 1

    main = get_main_db_connection()
    try:
        tenants = main.execute(
            "SELECT tenant_id, settings FROM tenants WHERE is_active = 1"
        ).fetchall()
    finally:
        main.close()

    results = []
    for row in tenants:
        tid = row['tenant_id'] if not isinstance(row, sqlite3.Row) else row['tenant_id']
        settings = parse_tenant_settings(
            row['settings'] if not isinstance(row, sqlite3.Row) else row['settings']
        )
        if not isinstance(settings, dict):
            settings = {}
        regime = normalize_accounting_regime(settings.get('accounting_regime'))
        if not is_sme_regime(regime):
            continue
        try:
            conn = get_tenant_db_connection(tid)
            conn.row_factory = sqlite3.Row
            ensure_costing_policy_schema(conn, commit=True)
            r = run_auto_close_for_conn(conn, fiscal_year=fy, period=period)
            r['tenant_id'] = tid
            results.append(r)
            conn.close()
        except Exception as exc:
            results.append({'tenant_id': tid, 'error': str(exc)})

    return {
        'fiscal_year': fy,
        'period': period,
        'tenants': len(results),
        'results': results,
    }
