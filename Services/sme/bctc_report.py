"""Lập BCTC SME (B01 / B02) từ sổ nhật ký + map bctc_line_code."""
from __future__ import annotations

import ast
import operator
import re
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.bctc_lines import B01_BALANCE_SHEET, B02_INCOME_STATEMENT, B03_CASH_FLOW
from Services.sme.general_ledger import period_bounds
from Services.sme.journal_engine import ensure_sme_journal_ready

MONEY_Q = Decimal('0.01')
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
}


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _eval_formula(expr: str, values: dict[str, Decimal]) -> Decimal:
    """Công thức chỉ tiêu: chỉ + - * / ; mọi token số/chữ đều là mã chỉ tiêu (không literal)."""
    expr = (expr or '0').strip()
    safe_parts: list[str] = []
    for match in re.finditer(r'[0-9A-Za-z_]+|[+\-*/()]|\s+', expr):
        token = match.group(0)
        if token.isspace():
            safe_parts.append(token)
            continue
        if token in '+-*/()':
            safe_parts.append(token)
            continue
        # Không dùng literal số — tránh '222' thành integer khi mã calc chưa có trong values
        safe_parts.append(f'v_{token}')
    safe = ''.join(safe_parts)
    env = {f'v_{k}': _money(v) for k, v in values.items()}
    for match in re.finditer(r'\bv_[0-9A-Za-z_]+\b', safe):
        env.setdefault(match.group(0), Decimal('0.00'))
    try:
        tree = ast.parse(safe, mode='eval')
    except SyntaxError as exc:
        raise ValueError(f'Công thức không hợp lệ: {expr}') from exc
    return _money(_eval_node(tree.body, env))


def _eval_node(node: ast.AST, env: dict[str, Decimal]) -> Decimal:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return _money(node.value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.operand, env))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval_node(node.left, env), _eval_node(node.right, env))
    if isinstance(node, ast.Name):
        return _money(env.get(node.id, 0))
    raise ValueError('Công thức BCTC không hợp lệ')


def _closing_balances(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period_to: int,
    *,
    branch_code: str | None = None,
) -> dict[str, dict[str, Decimal]]:
    from Services.sme.branches import branch_sql_filter

    sql = """
        SELECT jl.account_code,
               SUM(jl.debit) AS debit,
               SUM(jl.credit) AS credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND (
              je.fiscal_year < ?
              OR (je.fiscal_year = ? AND je.period <= ?)
          )
    """
    params: list[Any] = [fiscal_year, fiscal_year, period_to]
    bf, bp = branch_sql_filter(branch_code, alias='je')
    sql += bf
    params.extend(bp)
    sql += ' GROUP BY jl.account_code'
    rows = conn.execute(sql, params).fetchall()
    return {
        r[0]: {'debit': _money(r[1]), 'credit': _money(r[2])}
        for r in rows
    }


def _period_activity(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period_from: int,
    period_to: int,
    *,
    exclude_document_types: tuple[str, ...] = (),
    branch_code: str | None = None,
) -> dict[str, dict[str, Decimal]]:
    from Services.sme.branches import branch_sql_filter

    sql = """
        SELECT jl.account_code,
               SUM(jl.debit) AS debit,
               SUM(jl.credit) AS credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND je.fiscal_year = ?
          AND je.period >= ? AND je.period <= ?
    """
    params: list[Any] = [fiscal_year, period_from, period_to]
    if exclude_document_types:
        placeholders = ','.join('?' for _ in exclude_document_types)
        sql += f' AND je.document_type NOT IN ({placeholders})'
        params.extend(exclude_document_types)
    bf, bp = branch_sql_filter(branch_code, alias='je')
    sql += bf
    params.extend(bp)
    sql += ' GROUP BY jl.account_code'
    rows = conn.execute(sql, params).fetchall()
    return {
        r[0]: {'debit': _money(r[1]), 'credit': _money(r[2])}
        for r in rows
    }


def _coa_line_map(conn: sqlite3.Connection) -> list[dict]:
    """Mọi TK active; bctc_line_code kế thừa từ cha nếu lá chưa gán."""
    conn.row_factory = sqlite3.Row
    rows = [
        dict(r) for r in conn.execute(
            """
            SELECT code, name, parent_code, bctc_line_code, normal_balance,
                   account_class, is_postable
            FROM sme_chart_of_accounts
            WHERE is_active = 1
            """
        ).fetchall()
    ]
    by_code = {r['code']: r for r in rows}

    def resolve(code: str) -> str | None:
        seen = set()
        cur = code
        while cur and cur not in seen:
            seen.add(cur)
            row = by_code.get(cur)
            if not row:
                return None
            val = (row.get('bctc_line_code') or '').strip()
            if val:
                return val
            cur = row.get('parent_code')
        return None

    out = []
    for r in rows:
        line = resolve(r['code'])
        if not line:
            continue
        item = dict(r)
        item['bctc_line_code'] = line
        out.append(item)
    return out


def _signed_amount(debit: Decimal, credit: Decimal, sign_role: str) -> Decimal:
    role = sign_role or 'asset'
    if role == 'asset':
        return debit - credit
    if role in ('liability', 'equity', 'contra_asset'):
        return credit - debit
    if role == 'revenue':
        return credit - debit
    if role == 'expense':
        return debit - credit
    return debit - credit


def _aggregate_leaf_amounts(
    accounts: list[dict],
    bal_map: dict[str, dict[str, Decimal]],
    *,
    line_defs: list[dict],
) -> dict[str, Decimal]:
    """Gom số dư/phát sinh theo mã chỉ tiêu báo cáo.

    Hỗ trợ ``coa_line`` (TT99) hoặc ``coa_lines`` / ``coa_contra_lines`` (TT58 DNSN).
    """
    leaf_meta = {}
    for line in line_defs:
        if line.get('kind') != 'leaf':
            continue
        codes = line.get('coa_lines')
        if not codes:
            codes = [line.get('coa_line') or line['code']]
        leaf_meta[line['code']] = {
            'coa_lines': list(codes),
            'coa_contra_lines': list(line.get('coa_contra_lines') or []),
            'coa_contra_role': line.get('coa_contra_role') or (
                'contra_asset' if (line.get('sign_role') or 'asset') == 'asset' else 'expense'
            ),
            'sign_role': line.get('sign_role') or 'asset',
        }

    raw: dict[str, dict[str, Decimal]] = {}
    for acc in accounts:
        if not acc.get('is_postable'):
            continue
        line = acc.get('bctc_line_code')
        if not line:
            continue
        bal = bal_map.get(acc['code'])
        if not bal:
            continue
        bucket = raw.setdefault(line, {'debit': Decimal('0.00'), 'credit': Decimal('0.00')})
        bucket['debit'] += bal['debit']
        bucket['credit'] += bal['credit']

    empty = {'debit': Decimal('0.00'), 'credit': Decimal('0.00')}
    out: dict[str, Decimal] = {}
    for report_code, meta in leaf_meta.items():
        amt = Decimal('0.00')
        role = meta['sign_role']
        for coa_line in meta['coa_lines']:
            bucket = raw.get(coa_line, empty)
            amt += _signed_amount(bucket['debit'], bucket['credit'], role)
        contra_role = meta['coa_contra_role']
        for coa_line in meta['coa_contra_lines']:
            bucket = raw.get(coa_line, empty)
            amt -= _signed_amount(bucket['debit'], bucket['credit'], contra_role)
        out[report_code] = _money(amt)
    return out


def _build_rows(
    line_defs: list[dict],
    leaf_values: dict[str, Decimal],
    *,
    opening_values: dict[str, Decimal] | None = None,
) -> list[dict]:
    values: dict[str, Decimal] = dict(leaf_values)
    open_vals: dict[str, Decimal] = dict(opening_values or {})
    rows = []
    for line in line_defs:
        code = line['code']
        kind = line['kind']
        amount = None
        amount_opening = None
        if kind == 'leaf':
            amount = _money(values.get(code, 0))
            values[code] = amount
            if opening_values is not None:
                amount_opening = _money(open_vals.get(code, 0))
                open_vals[code] = amount_opening
        elif kind == 'calc':
            amount = _eval_formula(line.get('formula') or '0', values)
            values[code] = amount
            if opening_values is not None:
                amount_opening = _eval_formula(line.get('formula') or '0', open_vals)
                open_vals[code] = amount_opening
        row = {
            'code': code,
            'name': line['name'],
            'kind': kind,
            'level': line.get('level', 1),
            'bold': bool(line.get('bold')),
            'highlight': bool(line.get('highlight')),
            'amount': None if amount is None else float(amount),
            'formula': line.get('formula'),
        }
        if opening_values is not None:
            row['amount_opening'] = None if amount_opening is None else float(amount_opening)
        rows.append(row)
    return rows


def _is_tt58_forms(conn: sqlite3.Connection) -> bool:
    try:
        from Services.sme.regime_profile import get_ledger_profile
        return bool(get_ledger_profile(conn).get('is_tt58_micro'))
    except Exception:
        return False


def _year_opening_balances(conn: sqlite3.Connection, fiscal_year: int) -> dict[str, dict[str, Decimal]]:
    """Số dư đầu năm = số dư cuối năm trước."""
    if fiscal_year <= 1:
        return {}
    return _closing_balances(conn, fiscal_year - 1, 12)


def balance_sheet(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_to: int,
    include_current_profit: bool = True,
) -> dict[str, Any]:
    """Bảng CĐKT / Báo cáo tình hình TC — B01-DN (TT99) hoặc B01-DNSN (TT58)."""
    ensure_sme_journal_ready(conn, commit=False)
    if period_to < 1 or period_to > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')

    tt58 = _is_tt58_forms(conn)
    if tt58:
        from Services.sme.bctc_lines_tt58 import B01_DNSN_BALANCE_SHEET as line_defs
        report_code = 'B01-DNSN'
        title = 'Báo cáo tình hình tài chính'
        profit_line = '420'
        total_asset_code = '200'
        total_source_code = '500'
    else:
        line_defs = B01_BALANCE_SHEET
        report_code = 'B01-DN'
        title = 'Bảng cân đối kế toán'
        profit_line = '421'
        total_asset_code = '270'
        total_source_code = '440'

    accounts = _coa_line_map(conn)
    bal_map = _closing_balances(conn, fiscal_year, period_to)
    leaf_vals = _aggregate_leaf_amounts(accounts, bal_map, line_defs=line_defs)

    opening_leaf = None
    if tt58:
        open_map = _year_opening_balances(conn, fiscal_year)
        opening_leaf = _aggregate_leaf_amounts(accounts, open_map, line_defs=line_defs)

    current_profit = Decimal('0.00')
    if include_current_profit:
        # Chỉ cộng LN các kỳ chưa kết chuyển KCKQ (tránh cộng trùng vào 421/420)
        closed_rows = conn.execute(
            """
            SELECT DISTINCT period FROM sme_journal_entries
            WHERE fiscal_year = ? AND period <= ?
              AND document_type = 'KCKQ' AND status = 'posted'
              AND reverses_id IS NULL
            """,
            (fiscal_year, period_to),
        ).fetchall()
        closed = {int(r[0]) for r in closed_rows}
        open_periods = [p for p in range(1, period_to + 1) if p not in closed]
        if open_periods:
            is_rep = income_statement(
                conn,
                fiscal_year=fiscal_year,
                period_from=min(open_periods),
                period_to=max(open_periods),
            )
            current_profit = _money(is_rep['totals']['profit_after_tax'])
            leaf_vals[profit_line] = _money(leaf_vals.get(profit_line, 0)) + current_profit

    rows = _build_rows(line_defs, leaf_vals, opening_values=opening_leaf)
    by_code = {r['code']: r['amount'] for r in rows if r['amount'] is not None}
    total_assets = _money(by_code.get(total_asset_code, 0))
    total_equity_liab = _money(by_code.get(total_source_code, 0))
    _, as_of = period_bounds(fiscal_year, period_to)

    return {
        'report': report_code,
        'form_set': 'tt58_dnsn' if tt58 else 'tt99_dn',
        'title': title,
        'fiscal_year': fiscal_year,
        'period_to': period_to,
        'as_of_date': as_of,
        'include_current_profit': include_current_profit,
        'current_year_profit': float(current_profit),
        'has_opening_column': bool(tt58),
        'rows': rows,
        'totals': {
            'total_assets': float(total_assets),
            'total_equity_and_liabilities': float(total_equity_liab),
            'balanced': total_assets == total_equity_liab,
            'difference': float(total_assets - total_equity_liab),
        },
    }


def income_statement(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_from: int = 1,
    period_to: int | None = None,
) -> dict[str, Any]:
    """KQHĐKD — B02-DN (TT99) hoặc B02-DNSN (TT58)."""
    ensure_sme_journal_ready(conn, commit=False)
    period_to = period_to or period_from
    if period_from > period_to:
        raise ValueError('period_from không được lớn hơn period_to')

    tt58 = _is_tt58_forms(conn)
    if tt58:
        from Services.sme.bctc_lines_tt58 import B02_DNSN_INCOME_STATEMENT as line_defs
        report_code = 'B02-DNSN'
        title = 'Báo cáo kết quả hoạt động kinh doanh'
        net_code, gross_code, pbt_code, pat_code = '01', '03', '03', '20'
    else:
        line_defs = B02_INCOME_STATEMENT
        report_code = 'B02-DN'
        title = 'Báo cáo kết quả hoạt động kinh doanh'
        net_code, gross_code, pbt_code, pat_code = '10', '20', '50', '60'

    accounts = _coa_line_map(conn)
    # Loại KCKQ: kết chuyển làm phát sinh DT/CP về 0 — B02 cần số trước kết chuyển
    bal_map = _period_activity(
        conn, fiscal_year, period_from, period_to,
        exclude_document_types=('KCKQ',),
    )
    leaf_vals = _aggregate_leaf_amounts(accounts, bal_map, line_defs=line_defs)

    prior_leaf = None
    if tt58 and fiscal_year > 1:
        prior_map = _period_activity(
            conn, fiscal_year - 1, period_from, period_to,
            exclude_document_types=('KCKQ',),
        )
        prior_leaf = _aggregate_leaf_amounts(accounts, prior_map, line_defs=line_defs)

    rows = _build_rows(
        line_defs, leaf_vals,
        opening_values=prior_leaf if prior_leaf is not None else None,
    )
    # Với B02-DNSN: amount_opening = năm trước (cột Năm trước)
    if prior_leaf is not None:
        for r in rows:
            if 'amount_opening' in r:
                r['amount_prior'] = r.pop('amount_opening')

    date_from, _ = period_bounds(fiscal_year, period_from)
    _, date_to = period_bounds(fiscal_year, period_to)
    by_code = {r['code']: r['amount'] for r in rows if r['amount'] is not None}

    return {
        'report': report_code,
        'form_set': 'tt58_dnsn' if tt58 else 'tt99_dn',
        'title': title,
        'fiscal_year': fiscal_year,
        'period_from': period_from,
        'period_to': period_to,
        'date_from': date_from,
        'date_to': date_to,
        'has_prior_column': bool(tt58),
        'rows': rows,
        'totals': {
            'revenue_net': float(_money(by_code.get(net_code, 0))),
            'gross_profit': float(_money(by_code.get(gross_code, 0))),
            'profit_before_tax': float(_money(by_code.get(pbt_code, 0))),
            'profit_after_tax': float(_money(by_code.get(pat_code, 0))),
        },
    }


# ---------------------------------------------------------------------------
# B03 — Lưu chuyển tiền tệ
# ---------------------------------------------------------------------------

_CASH_BCTC = '111'
_RECV_LINES = {'131', '132', '133', '136'}
_INV_LINES = {'141'}
_PAY_LINES = {'311', '313', '314', '315', '316', '318', '319', '320', '322'}
_PREPAID_LINES = {'155', '242'}

_INVESTING_PREFIXES = (
    '211', '212', '213', '217', '221', '222', '228', '241', '242',
)
_FINANCING_PREFIXES = (
    '341', '343', '344', '411', '412', '414', '418', '419', '421', '441',
)


def _account_net(bal: dict[str, Decimal] | None) -> Decimal:
    if not bal:
        return Decimal('0.00')
    return bal['debit'] - bal['credit']


def _cash_account_codes(conn: sqlite3.Connection) -> set[str]:
    return {
        a['code'] for a in _coa_line_map(conn)
        if a.get('is_postable') and a.get('bctc_line_code') == _CASH_BCTC
    }


def _group_net_by_bctc(
    accounts: list[dict],
    bal_map: dict[str, dict[str, Decimal]],
    line_codes: set[str],
    *,
    as_liability: bool = False,
) -> Decimal:
    total = Decimal('0.00')
    for acc in accounts:
        if not acc.get('is_postable'):
            continue
        if acc.get('bctc_line_code') not in line_codes:
            continue
        net = _account_net(bal_map.get(acc['code']))
        total += (-net if as_liability else net)
    return total


def _balances_before(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period_from: int,
) -> dict[str, dict[str, Decimal]]:
    """Số dư ngay trước period_from (= đầu kỳ báo cáo)."""
    return _closing_balances(conn, fiscal_year, period_from - 1) if period_from > 1 else (
        _closing_balances(conn, fiscal_year - 1, 12) if fiscal_year > 1 else {}
    )


def _classify_prefix(code: str) -> str | None:
    for p in _INVESTING_PREFIXES:
        if code.startswith(p):
            return 'investing'
    for p in _FINANCING_PREFIXES:
        if code.startswith(p):
            return 'financing'
    return None


def _classify_cash_entries(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_from: int,
    period_to: int,
    cash_codes: set[str],
) -> dict[str, Decimal]:
    """Phân loại dòng tiền thuần theo đối ứng trong từng chứng từ."""
    conn.row_factory = sqlite3.Row
    entries = conn.execute(
        """
        SELECT id, document_type, business_type FROM sme_journal_entries
        WHERE status IN ('posted', 'reversed')
          AND fiscal_year = ? AND period >= ? AND period <= ?
        """,
        (fiscal_year, period_from, period_to),
    ).fetchall()

    buckets = {
        'operating': Decimal('0.00'),
        'investing_out': Decimal('0.00'),
        'investing_in': Decimal('0.00'),
        'investing_lend_out': Decimal('0.00'),
        'investing_lend_in': Decimal('0.00'),
        'investing_equity_out': Decimal('0.00'),
        'investing_equity_in': Decimal('0.00'),
        'investing_income': Decimal('0.00'),
        'financing_equity_in': Decimal('0.00'),
        'financing_equity_out': Decimal('0.00'),
        'financing_borrow_in': Decimal('0.00'),
        'financing_borrow_out': Decimal('0.00'),
        'financing_lease_out': Decimal('0.00'),
        'financing_dividend': Decimal('0.00'),
        'depreciation': Decimal('0.00'),
        'interest_paid': Decimal('0.00'),
        'tax_paid': Decimal('0.00'),
    }

    for ent in entries:
        lines = conn.execute(
            """
            SELECT account_code, debit, credit FROM sme_journal_lines
            WHERE entry_id = ?
            """,
            (ent['id'],),
        ).fetchall()
        cash_net = Decimal('0.00')
        other: list[tuple[str, Decimal]] = []
        for ln in lines:
            code = str(ln['account_code'])
            d, cr = _money(ln['debit']), _money(ln['credit'])
            amt = d - cr
            if code in cash_codes:
                cash_net += amt
            else:
                if abs(amt) > 0:
                    other.append((code, amt))
                if code.startswith('214'):
                    # Có 214 = khấu hao tăng (điều chỉnh dương)
                    buckets['depreciation'] += cr - d

        if cash_net == 0:
            continue

        # Điểm theo nhóm đối ứng
        scores = {'operating': Decimal('0.00'), 'investing': Decimal('0.00'), 'financing': Decimal('0.00')}
        for code, amt in other:
            cat = _classify_prefix(code) or 'operating'
            scores[cat] += abs(amt)
            if code.startswith('635') and cash_net < 0:
                buckets['interest_paid'] += cash_net  # đã âm
            if code.startswith('333') and cash_net < 0:
                # nộp thuế (ước lượng khi đối ứng thuế)
                buckets['tax_paid'] += cash_net

        category = max(scores, key=lambda k: scores[k])
        if scores[category] == 0:
            category = 'operating'

        if category == 'operating':
            buckets['operating'] += cash_net
        elif category == 'investing':
            if cash_net < 0:
                buckets['investing_out'] += cash_net
            else:
                buckets['investing_in'] += cash_net
        else:
            # financing — tách vay vs vốn theo đối ứng
            eq = any(c.startswith(('411', '412', '414', '418', '419', '441')) for c, _ in other)
            borrow = any(c.startswith(('341', '343', '344')) for c, _ in other)
            if eq:
                if cash_net > 0:
                    buckets['financing_equity_in'] += cash_net
                else:
                    buckets['financing_equity_out'] += cash_net
            elif borrow:
                if cash_net > 0:
                    buckets['financing_borrow_in'] += cash_net
                else:
                    buckets['financing_borrow_out'] += cash_net
            else:
                if cash_net > 0:
                    buckets['financing_borrow_in'] += cash_net
                else:
                    buckets['financing_borrow_out'] += cash_net

    return buckets


def cash_flow_statement(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period_from: int = 1,
    period_to: int | None = None,
) -> dict[str, Any]:
    """
    Báo cáo LCTT (B03-DN) — chỉ áp dụng TT99.
    TT58 siêu nhỏ không có mẫu B03-DNSN theo Thông tư 58/2026/TT-BTC.
    """
    if _is_tt58_forms(conn):
        raise ValueError(
            'Doanh nghiệp siêu nhỏ (TT58) không lập Báo cáo lưu chuyển tiền tệ (B03). '
            'Chỉ lập B01-DNSN và B02-DNSN.'
        )
    ensure_sme_journal_ready(conn, commit=False)
    period_to = period_to or period_from
    if period_from > period_to:
        raise ValueError('period_from không được lớn hơn period_to')

    accounts = _coa_line_map(conn)
    cash_codes = _cash_account_codes(conn)
    open_map = _balances_before(conn, fiscal_year, period_from)
    close_map = _closing_balances(conn, fiscal_year, period_to)

    cash_opening = sum((_account_net(open_map.get(c)) for c in cash_codes), Decimal('0.00'))
    cash_closing = sum((_account_net(close_map.get(c)) for c in cash_codes), Decimal('0.00'))

    is_rep = income_statement(
        conn, fiscal_year=fiscal_year, period_from=period_from, period_to=period_to,
    )
    profit_bt = _money(is_rep['totals']['profit_before_tax'])

    # Biến động vốn lưu động (dấu theo quy ước LCTT gián tiếp)
    open_recv = _group_net_by_bctc(accounts, open_map, _RECV_LINES)
    close_recv = _group_net_by_bctc(accounts, close_map, _RECV_LINES)
    delta_receivables = -(close_recv - open_recv)

    open_inv = _group_net_by_bctc(accounts, open_map, _INV_LINES)
    close_inv = _group_net_by_bctc(accounts, close_map, _INV_LINES)
    delta_inventory = -(close_inv - open_inv)

    open_pay = _group_net_by_bctc(accounts, open_map, _PAY_LINES, as_liability=True)
    close_pay = _group_net_by_bctc(accounts, close_map, _PAY_LINES, as_liability=True)
    # as_liability=True đã lấy credit-debit; tăng phải trả → dương
    delta_payables = close_pay - open_pay

    open_pp = _group_net_by_bctc(accounts, open_map, _PREPAID_LINES)
    close_pp = _group_net_by_bctc(accounts, close_map, _PREPAID_LINES)
    delta_prepaid = -(close_pp - open_pp)

    classified = _classify_cash_entries(
        conn,
        fiscal_year=fiscal_year,
        period_from=period_from,
        period_to=period_to,
        cash_codes=cash_codes,
    )

    # Tránh đếm kép interest/tax trong operating bucket: interest/tax đã nằm trong operating
    # khi phân loại — giữ dòng riêng chỉ mang tính thuyết minh nếu tách được, còn operating
    # dùng tổng classified['operating'].
    interest_paid = Decimal('0.00')
    tax_paid = Decimal('0.00')

    depreciation = classified['depreciation']
    other_adj = Decimal('0.00')

    indirect_core = (
        profit_bt + depreciation + other_adj
        + delta_receivables + delta_inventory + delta_payables + delta_prepaid
        + interest_paid + tax_paid
    )
    operating_actual = classified['operating']
    plug = operating_actual - indirect_core
    other_in = plug if plug > 0 else Decimal('0.00')
    other_out = plug if plug < 0 else Decimal('0.00')

    cf_values = {
        'profit_before_tax': profit_bt,
        'depreciation': depreciation,
        'other_adjustments': other_adj,
        'delta_receivables': delta_receivables,
        'delta_inventory': delta_inventory,
        'delta_payables': delta_payables,
        'delta_prepaid': delta_prepaid,
        'interest_paid': interest_paid,
        'tax_paid': tax_paid,
        'other_operating_in': other_in,
        'other_operating_out': other_out,
        'investing_out': classified['investing_out'],
        'investing_in': classified['investing_in'],
        'investing_lend_out': classified['investing_lend_out'],
        'investing_lend_in': classified['investing_lend_in'],
        'investing_equity_out': classified['investing_equity_out'],
        'investing_equity_in': classified['investing_equity_in'],
        'investing_income': classified['investing_income'],
        'financing_equity_in': classified['financing_equity_in'],
        'financing_equity_out': classified['financing_equity_out'],
        'financing_borrow_in': classified['financing_borrow_in'],
        'financing_borrow_out': classified['financing_borrow_out'],
        'financing_lease_out': classified['financing_lease_out'],
        'financing_dividend': classified['financing_dividend'],
        'cash_opening': cash_opening,
    }

    leaf_vals: dict[str, Decimal] = {}
    for line in B03_CASH_FLOW:
        if line.get('kind') == 'leaf' and line.get('cf_key'):
            leaf_vals[line['code']] = _money(cf_values.get(line['cf_key'], 0))

    rows = _build_rows(B03_CASH_FLOW, leaf_vals)
    by_code = {r['code']: r['amount'] for r in rows if r['amount'] is not None}
    net_change = _money(by_code.get('50', 0))
    end_cash = _money(by_code.get('70', 0))
    expected_change = cash_closing - cash_opening

    date_from, _ = period_bounds(fiscal_year, period_from)
    _, date_to = period_bounds(fiscal_year, period_to)
    return {
        'report': 'B03-DN',
        'title': 'Báo cáo lưu chuyển tiền tệ',
        'fiscal_year': fiscal_year,
        'period_from': period_from,
        'period_to': period_to,
        'date_from': date_from,
        'date_to': date_to,
        'cash_accounts': sorted(cash_codes),
        'rows': rows,
        'totals': {
            'operating': float(_money(by_code.get('20', 0))),
            'investing': float(_money(by_code.get('30', 0))),
            'financing': float(_money(by_code.get('40', 0))),
            'net_change': float(net_change),
            'cash_opening': float(cash_opening),
            'cash_closing': float(cash_closing),
            'cash_closing_reported': float(end_cash),
            'balanced': net_change == expected_change and end_cash == cash_closing,
            'difference': float(end_cash - cash_closing),
        },
    }
