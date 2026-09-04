"""Sổ cái / bảng cân đối phát sinh SME — từ nhật ký bút toán kép."""
from __future__ import annotations

import calendar
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _period_bounds(year: int, period: int) -> tuple[str, str]:
    if period < 1 or period > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')
    last = calendar.monthrange(year, period)[1]
    return f'{year:04d}-{period:02d}-01', f'{year:04d}-{period:02d}-{last:02d}'


def _net_balance(debit: Decimal, credit: Decimal, normal: str) -> dict[str, float]:
    """Số dư theo tính chất TK — chỉ một bên có số."""
    if (normal or 'debit') == 'credit':
        net = credit - debit
        if net >= 0:
            return {'debit': 0.0, 'credit': float(net), 'net': float(net), 'side': 'credit'}
        return {'debit': float(-net), 'credit': 0.0, 'net': float(net), 'side': 'debit'}
    net = debit - credit
    if net >= 0:
        return {'debit': float(net), 'credit': 0.0, 'net': float(net), 'side': 'debit'}
    return {'debit': 0.0, 'credit': float(-net), 'net': float(net), 'side': 'credit'}


def _activity_before_period(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period: int,
    branch_code: str | None = None,
) -> dict[str, dict[str, Decimal]]:
    from Services.sme.branches import branch_sql_filter
    bf, bp = branch_sql_filter(branch_code, alias='je')
    rows = conn.execute(
        f"""
        SELECT jl.account_code,
               SUM(jl.debit) AS debit,
               SUM(jl.credit) AS credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND (
              je.fiscal_year < ?
              OR (je.fiscal_year = ? AND je.period < ?)
          )
          {bf}
        GROUP BY jl.account_code
        """,
        (fiscal_year, fiscal_year, period, *bp),
    ).fetchall()
    return {
        r[0]: {'debit': _money(r[1]), 'credit': _money(r[2])}
        for r in rows
    }


def _activity_in_periods(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period_from: int,
    period_to: int,
    branch_code: str | None = None,
) -> dict[str, dict[str, Decimal]]:
    from Services.sme.branches import branch_sql_filter
    bf, bp = branch_sql_filter(branch_code, alias='je')
    rows = conn.execute(
        f"""
        SELECT jl.account_code,
               SUM(jl.debit) AS debit,
               SUM(jl.credit) AS credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND je.fiscal_year = ?
          AND je.period >= ? AND je.period <= ?
          {bf}
        GROUP BY jl.account_code
        """,
        (fiscal_year, period_from, period_to, *bp),
    ).fetchall()
    return {
        r[0]: {'debit': _money(r[1]), 'credit': _money(r[2])}
        for r in rows
    }


def period_bounds(year: int, period: int) -> tuple[str, str]:
    """Ngày đầu/cuối tháng YYYY-MM-DD."""
    return _period_bounds(year, period)


def trial_balance(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_from: int = 1,
    period_to: int | None = None,
    postable_only: bool = False,
    include_zero: bool = True,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Bảng cân đối phát sinh theo hệ thống TK pháp định.

    - Hiển thị tài khoản đang hiệu lực; số liệu vẫn tổng hợp cả TK con đã ngừng sử dụng.
    - Số liệu TK cha = cộng dồn phát sinh/dư của toàn bộ cấp con (+ ghi trực tiếp nếu có).
    - Dòng Tổng chỉ cộng các TK cấp 1 để tránh đếm trùng.
    - branch_code: lọc theo chi nhánh (None/ALL = hợp nhất pháp nhân).
    """
    ensure_sme_journal_ready(conn, commit=False)
    period_to = period_to or period_from
    if period_from > period_to:
        raise ValueError('period_from không được lớn hơn period_to')

    conn.row_factory = sqlite3.Row

    # -------------------------------------------------------------
    # Tách "cây COA để tổng hợp" và "danh sách tài khoản để hiển thị".
    #
    # QUAN TRỌNG:
    # - Tài khoản đã ngừng sử dụng (is_active=0) vẫn phải nằm trong cây
    #   để số liệu lịch sử của nó tiếp tục roll-up lên tài khoản cha.
    # - Chỉ danh sách hiển thị mới lọc is_active/postable_only.
    #
    # Ví dụ: 5111 từng có phát sinh, sau đó người dùng tắt 5111 và hệ
    # thống cho phép ghi trực tiếp 511. Bảng CĐPS phải vẫn cộng số cũ
    # của 5111 vào 511 mà không sửa bút toán lịch sử.
    # -------------------------------------------------------------
    all_accounts = [
        dict(r)
        for r in conn.execute(
            """
            SELECT code, name, normal_balance, is_postable, is_active,
                   account_class, level, parent_code, legal_source
            FROM sme_chart_of_accounts
            ORDER BY code
            """
        ).fetchall()
    ]
    all_by_code = {a['code']: a for a in all_accounts}

    accounts = [
        dict(a)
        for a in all_accounts
        if int(a.get('is_active') or 0) == 1
        and (not postable_only or int(a.get('is_postable') or 0) == 1)
    ]
    by_code = {a['code']: a for a in accounts}

    opening_direct = _activity_before_period(
        conn, fiscal_year, period_from, branch_code=branch_code,
    )
    period_direct = _activity_in_periods(
        conn, fiscal_year, period_from, period_to, branch_code=branch_code,
    )

    # TK có phát sinh nhưng thật sự không tồn tại trong COA mới là orphan.
    # TK chỉ bị inactive KHÔNG được xem là orphan, vì vẫn cần parent_code
    # để cộng dồn lịch sử lên tài khoản cha.
    orphan_codes = sorted(
        (set(opening_direct) | set(period_direct)) - set(all_by_code)
    )
    for code in orphan_codes:
        orphan = {
            'code': code,
            'name': f'(Ngoài danh mục) {code}',
            'normal_balance': 'debit',
            'is_postable': 1,
            'is_active': 1,
            'account_class': None,
            'level': max(1, len(str(code)) - 2),
            'parent_code': None,
            'legal_source': 'orphan',
        }
        all_accounts.append(orphan)
        all_by_code[code] = orphan
        accounts.append(dict(orphan))
        by_code[code] = accounts[-1]

    # Cây quan hệ phải xây từ TOÀN BỘ COA, kể cả tài khoản inactive.
    children: dict[str, list[str]] = {}
    for acc in all_accounts:
        parent = acc.get('parent_code')
        if parent:
            children.setdefault(parent, []).append(acc['code'])

    def _rollup(direct: dict[str, dict[str, Decimal]]) -> dict[str, dict[str, Decimal]]:
        memo: dict[str, dict[str, Decimal]] = {}

        def total(code: str) -> dict[str, Decimal]:
            if code in memo:
                return memo[code]
            base = direct.get(code) or {'debit': Decimal('0.00'), 'credit': Decimal('0.00')}
            debit = _money(base['debit'])
            credit = _money(base['credit'])
            for child in children.get(code, ()):
                child_tot = total(child)
                debit += child_tot['debit']
                credit += child_tot['credit']
            memo[code] = {'debit': debit, 'credit': credit}
            return memo[code]

        # Tính roll-up trên toàn bộ cây COA, kể cả tài khoản inactive.
        for acc in all_accounts:
            total(acc['code'])
        return memo

    opening_all = _rollup(opening_direct)
    period_map = _rollup(period_direct)

    rows_out = []
    sum_open_d = sum_open_c = Decimal('0.00')
    sum_per_d = sum_per_c = Decimal('0.00')
    sum_close_d = sum_close_c = Decimal('0.00')

    for acc in accounts:
        code = acc['code']
        level = int(acc.get('level') or 1)
        op = opening_all.get(code) or {'debit': Decimal('0.00'), 'credit': Decimal('0.00')}
        pe = period_map.get(code) or {'debit': Decimal('0.00'), 'credit': Decimal('0.00')}
        has_children = bool(children.get(code))

        is_zero = (
            op['debit'] == 0 and op['credit'] == 0
            and pe['debit'] == 0 and pe['credit'] == 0
        )

        normal = acc.get('normal_balance') or 'debit'
        close_d = op['debit'] + pe['debit']
        close_c = op['credit'] + pe['credit']
        open_bal = _net_balance(op['debit'], op['credit'], normal)
        close_bal = _net_balance(close_d, close_c, normal)

        rows_out.append({
            'code': code,
            'name': acc['name'],
            'normal_balance': normal,
            'account_class': acc.get('account_class'),
            'level': level,
            'parent_code': acc.get('parent_code'),
            'is_postable': int(acc.get('is_postable') or 0),
            'has_children': has_children,
            'legal_source': acc.get('legal_source'),
            'opening_debit': open_bal['debit'],
            'opening_credit': open_bal['credit'],
            'period_debit': float(pe['debit']),
            'period_credit': float(pe['credit']),
            'closing_debit': close_bal['debit'],
            'closing_credit': close_bal['credit'],
            'is_zero': is_zero,
        })

    if not include_zero:
        # Giữ mọi TK cấp 1; giữ TK con nếu bản thân/hậu duệ có số liệu (và tổ tiên của chúng).
        keep: set[str] = set()
        codes = {r['code'] for r in rows_out}
        for r in rows_out:
            if r['level'] == 1 or not r['is_zero']:
                keep.add(r['code'])
                parent = r.get('parent_code')
                while parent and parent in codes:
                    keep.add(parent)
                    parent = (by_code.get(parent) or {}).get('parent_code')
        rows_out = [r for r in rows_out if r['code'] in keep]

    for r in rows_out:
        # Tổng chỉ theo TK cấp 1 (quan trọng nhất theo pháp luật / tránh trùng số).
        if int(r.get('level') or 1) != 1:
            continue
        sum_open_d += _money(r['opening_debit'])
        sum_open_c += _money(r['opening_credit'])
        sum_per_d += _money(r['period_debit'])
        sum_per_c += _money(r['period_credit'])
        sum_close_d += _money(r['closing_debit'])
        sum_close_c += _money(r['closing_credit'])

    date_from, _ = _period_bounds(fiscal_year, period_from)
    _, date_to = _period_bounds(fiscal_year, period_to)
    return {
        'fiscal_year': fiscal_year,
        'period_from': period_from,
        'period_to': period_to,
        'date_from': date_from,
        'date_to': date_to,
        'include_zero': include_zero,
        'postable_only': postable_only,
        'branch_code': branch_code or 'ALL',
        'totals_basis': 'level1',
        'rows': rows_out,
        'totals': {
            'opening_debit': float(sum_open_d),
            'opening_credit': float(sum_open_c),
            'period_debit': float(sum_per_d),
            'period_credit': float(sum_per_c),
            'closing_debit': float(sum_close_d),
            'closing_credit': float(sum_close_c),
            'period_balanced': sum_per_d == sum_per_c,
            'opening_balanced': sum_open_d == sum_open_c,
            'closing_balanced': sum_close_d == sum_close_c,
        },
    }


def _level1_account(conn: sqlite3.Connection, account_code: str) -> sqlite3.Row | None:
    """Leo cây COA tới tài khoản cấp 1 (Sổ cái chỉ theo TK cấp 1)."""
    code = (account_code or '').strip()
    if not code:
        return None
    row = conn.execute(
        """
        SELECT code, name, normal_balance, is_postable, level, parent_code
        FROM sme_chart_of_accounts WHERE code = ?
        """,
        (code,),
    ).fetchone()
    if not row:
        root = code[:3] if len(code) >= 3 else code
        row = conn.execute(
            """
            SELECT code, name, normal_balance, is_postable, level, parent_code
            FROM sme_chart_of_accounts WHERE code = ?
            """,
            (root,),
        ).fetchone()
        return row
    seen = set()
    while row and int(row['level'] or 1) > 1 and row['parent_code']:
        if row['code'] in seen:
            break
        seen.add(row['code'])
        parent = conn.execute(
            """
            SELECT code, name, normal_balance, is_postable, level, parent_code
            FROM sme_chart_of_accounts WHERE code = ?
            """,
            (row['parent_code'],),
        ).fetchone()
        if not parent:
            break
        row = parent
    return row


def _descendant_account_filter(code: str) -> tuple[str, list[Any]]:
    """Khớp TK cấp 1 và mọi TK con (111 → 111, 1111; không khớp 112)."""
    return (
        '(jl.account_code = ? OR jl.account_code LIKE ?)',
        [code, f'{code}%'],
    )


def account_ledger(
    conn: sqlite3.Connection,
    account_code: str,
    *,
    date_from: str,
    date_to: str,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Sổ cái chi tiết theo tài khoản cấp 1 (gộp phát sinh mọi TK con)."""
    from Services.sme.branches import branch_sql_filter

    ensure_sme_journal_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    code_in = (account_code or '').strip()
    if not code_in:
        raise ValueError('Thiếu mã tài khoản')

    acc = _level1_account(conn, code_in)
    if not acc:
        raise ValueError(f'Không tìm thấy tài khoản {code_in}')
    code = acc['code']

    d_from = date_from[:10]
    d_to = date_to[:10]
    normal = acc['normal_balance'] or 'debit'
    match_sql, match_params = _descendant_account_filter(code)
    bf, bp = branch_sql_filter(branch_code, alias='je')

    op = conn.execute(
        f"""
        SELECT COALESCE(SUM(jl.debit), 0), COALESCE(SUM(jl.credit), 0)
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE {match_sql}
          AND je.status IN ('posted', 'reversed')
          AND je.posting_date < ?
          {bf}
        """,
        (*match_params, d_from, *bp),
    ).fetchone()
    open_d, open_c = _money(op[0]), _money(op[1])
    open_bal = _net_balance(open_d, open_c, normal)

    lines = conn.execute(
        f"""
        SELECT jl.id AS line_id, jl.sequence, jl.debit, jl.credit, jl.description,
               jl.account_code AS line_account_code,
               jl.partner_id, jl.partner_type, jl.product_id, jl.warehouse_code,
               je.id AS entry_id, je.entry_no, je.posting_date, je.document_type,
               je.document_no, je.document_id, je.business_type, je.status,
               je.description AS entry_description, je.branch_code
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE {match_sql}
          AND je.status IN ('posted', 'reversed')
          AND je.posting_date >= ? AND je.posting_date <= ?
          {bf}
        ORDER BY je.posting_date, je.id, jl.sequence, jl.id
        """,
        (*match_params, d_from, d_to, *bp),
    ).fetchall()

    run_d, run_c = open_d, open_c
    detail = []
    period_d = Decimal('0.00')
    period_c = Decimal('0.00')
    for ln in lines:
        d = _money(ln['debit'])
        c = _money(ln['credit'])
        period_d += d
        period_c += c
        run_d += d
        run_c += c
        run_bal = _net_balance(run_d, run_c, normal)
        line_acc = ln['line_account_code'] or code
        desc = ln['description'] or ln['entry_description'] or ''
        if line_acc != code:
            desc = f'[{line_acc}] {desc}'.strip()
        detail.append({
            'line_id': ln['line_id'],
            'entry_id': ln['entry_id'],
            'entry_no': ln['entry_no'],
            'posting_date': ln['posting_date'],
            'document_type': ln['document_type'],
            'document_no': ln['document_no'],
            'document_id': ln['document_id'],
            'business_type': ln['business_type'],
            'status': ln['status'],
            'description': desc,
            'account_code': line_acc,
            'debit': float(d),
            'credit': float(c),
            'balance_debit': run_bal['debit'],
            'balance_credit': run_bal['credit'],
            'partner_type': ln['partner_type'],
            'partner_id': ln['partner_id'],
        })

    close_bal = _net_balance(open_d + period_d, open_c + period_c, normal)
    return {
        'account': {
            'code': acc['code'],
            'name': acc['name'],
            'normal_balance': normal,
            'is_postable': acc['is_postable'],
            'level': int(acc['level'] or 1),
            'includes_children': True,
        },
        'date_from': d_from,
        'date_to': d_to,
        'branch_code': branch_code or 'ALL',
        'opening': open_bal,
        'period_debit': float(period_d),
        'period_credit': float(period_c),
        'closing': close_bal,
        'lines': detail,
        'line_count': len(detail),
    }


def accounts_with_activity(
    conn: sqlite3.Connection,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Danh sách tài khoản cấp 1 có phát sinh (gồm TK con) — chọn nhanh sổ cái."""
    ensure_sme_journal_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row

    d_from = (date_from or '').strip()[:10] or None
    d_to = (date_to or '').strip()[:10] or None

    params: list[Any] = []
    date_sql = ''
    if d_from:
        date_sql += ' AND je.posting_date >= ?'
        params.append(d_from)
    if d_to:
        date_sql += ' AND je.posting_date <= ?'
        params.append(d_to)

    leaf_rows = conn.execute(
        f"""
        SELECT jl.account_code AS code,
               COALESCE(SUM(jl.debit), 0) AS period_debit,
               COALESCE(SUM(jl.credit), 0) AS period_credit,
               COUNT(*) AS line_count
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          {date_sql}
        GROUP BY jl.account_code
        HAVING SUM(jl.debit) <> 0 OR SUM(jl.credit) <> 0
        """,
        params,
    ).fetchall()

    l1_map: dict[str, str] = {}
    agg: dict[str, dict[str, Any]] = {}

    for r in leaf_rows:
        leaf = (r['code'] or '').strip()
        if not leaf:
            continue
        if leaf not in l1_map:
            root = _level1_account(conn, leaf)
            l1_map[leaf] = root['code'] if root else (leaf[:3] if len(leaf) >= 3 else leaf)
        l1 = l1_map[leaf]
        bucket = agg.get(l1)
        if not bucket:
            root_acc = _level1_account(conn, l1)
            bucket = {
                'code': l1,
                'name': (root_acc['name'] if root_acc else l1),
                'level': 1,
                'period_debit': Decimal('0.00'),
                'period_credit': Decimal('0.00'),
                'line_count': 0,
            }
            agg[l1] = bucket
        bucket['period_debit'] += _money(r['period_debit'])
        bucket['period_credit'] += _money(r['period_credit'])
        bucket['line_count'] += int(r['line_count'] or 0)

    return [
        {
            'code': agg[code]['code'],
            'name': agg[code]['name'],
            'level': 1,
            'period_debit': float(agg[code]['period_debit']),
            'period_credit': float(agg[code]['period_credit']),
            'line_count': agg[code]['line_count'],
        }
        for code in sorted(agg.keys())
    ]
