"""Sổ kế toán DNSN — TT58/2026/TT-BTC (S2b, S2d, S4a, S4b, S4d).

Dữ liệu lấy từ nhật ký kép / TSCĐ / vốn hiện có; chỉ đổi khung cột theo mẫu DNSN.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.bctc_lines_tt58 import DNSN_BOOK_CATALOG

MONEY_Q = Decimal('0.01')

BOOK_HANDLERS = {
    'S1-DNSN': 'revenue',
    'S2a-DNSN': 'revenue',
    'S2b-DNSN': 's2b',
    'S2c-DNSN': 's2c',
    'S2d-DNSN': 's2d',
    'S3a-DNSN': 'revenue',
    'S3b-DNSN': 's3b',
    'S4a-DNSN': 's4a',
    'S4b-DNSN': 's4b',
    'S4c-DNSN': 's4c',
    'S4d-DNSN': 's4d',
}

# Fallback nếu chưa có cấu hình DB (giữ tương thích)
DEFAULT_SECTOR_TAX = {
    'goods': {'vat_pct': 1.0, 'cit_pct': 0.3, 'label': 'Phân phối, cung cấp hàng hóa'},
    'production': {
        'vat_pct': 3.0, 'cit_pct': 1.2,
        'label': 'Sản xuất, vận tải, dịch vụ / xây dựng có nguyên vật liệu',
    },
    'service': {
        'vat_pct': 5.0, 'cit_pct': 1.5,
        'label': 'Dịch vụ, xây dựng không gồm nguyên vật liệu',
    },
    'leasing': {
        'vat_pct': 5.0, 'cit_pct': 4.0,
        'label': 'Cho thuê tài sản, đại lý bảo hiểm / xổ số / bán hàng đa cấp',
    },
    'digital': {
        'vat_pct': 5.0, 'cit_pct': 4.0,
        'label': 'Hoạt động nội dung số (nhạc, game, quảng cáo…)',
    },
    'other': {'vat_pct': 2.0, 'cit_pct': 0.5, 'label': 'Hoạt động kinh doanh khác'},
}


def _load_sector_tax(conn: sqlite3.Connection, *, fiscal_year: int) -> dict[str, dict]:
    try:
        from Services.sme.tt58_tax_rates import sector_tax_map
        return sector_tax_map(conn, as_of=f'{fiscal_year:04d}-12-31')
    except Exception:
        return dict(DEFAULT_SECTOR_TAX)



def _money(val) -> Decimal:
    return Decimal(str(val or 0)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def entity_header(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        row = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
        if not row:
            return {'name': '', 'address': '', 'tax_code': ''}
        d = dict(row)
        return {
            'name': d.get('company_name') or d.get('name') or d.get('business_name') or '',
            'address': d.get('address') or '',
            'tax_code': d.get('tax_code') or d.get('mst') or '',
        }
    except Exception:
        return {'name': '', 'address': '', 'tax_code': ''}


def list_dnsn_books(
    *,
    tax_method: str | None = None,
    include_optional: bool = True,
) -> list[dict[str, Any]]:
    """Danh mục sổ DNSN; lọc theo phương pháp thuế TT58 nếu có."""
    from Services.sme.tt58_tax_methods import get_tt58_tax_method_def, normalize_tt58_tax_method

    allowed: set[str] | None = None
    optional: set[str] = set()
    if tax_method:
        td = get_tt58_tax_method_def(normalize_tt58_tax_method(tax_method))
        allowed = set(td.get('required_books') or ())
        optional = set(td.get('optional_books') or ())
        if include_optional:
            allowed |= optional

    out = []
    for b in DNSN_BOOK_CATALOG:
        item = dict(b)
        item['handler'] = BOOK_HANDLERS.get(b['code'])
        item['available'] = item['handler'] in (
            's2d', 's2b', 's4a', 's4b', 's4d', 's2c', 's3b', 's4c', 'revenue',
        )
        code = b['code']
        if allowed is not None and code not in allowed:
            continue
        if allowed is not None:
            item['is_required'] = code in set(td.get('required_books') or ())
            item['is_optional'] = code in optional
        else:
            item['is_required'] = True
            item['is_optional'] = False
        out.append(item)
    return out


def s2d_cash_book(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Sổ chi tiết tiền — S2d-DNSN (tiền mặt + tiền gửi không kỳ hạn)."""
    from Services.sme.cash_books import cash_account_book, list_cash_accounts

    def _section(prefix: str, title: str) -> dict[str, Any]:
        accounts = list_cash_accounts(conn, prefix)
        code = prefix
        if accounts:
            # ưu tiên TK tổng hợp prefix nếu có trong list
            codes = {a['code'] for a in accounts}
            code = prefix if prefix in codes else accounts[0]['code']
        try:
            book = cash_account_book(
                conn,
                fiscal_year=fiscal_year,
                account_prefix=prefix,
                account_code=code,
                branch_code=branch_code,
            )
        except ValueError:
            return {
                'title': title,
                'account_code': code,
                'opening': 0.0,
                'closing': 0.0,
                'total_in': 0.0,
                'total_out': 0.0,
                'rows': [],
                'error': f'Chưa có tài khoản nhóm {prefix}',
            }
        rows = []
        for r in book.get('rows') or []:
            rows.append({
                'document_no': r.get('document_no') or r.get('entry_no') or '',
                'document_date': r.get('document_date') or r.get('posting_date') or '',
                'description': r.get('description') or '',
                'amount_in': r.get('receipt') or 0,
                'amount_out': r.get('payment') or 0,
            })
        return {
            'title': title,
            'account_code': book.get('account_code') or code,
            'opening': book.get('opening_balance') or 0,
            'closing': book.get('closing_balance') or 0,
            'total_in': book.get('total_receipt') or 0,
            'total_out': book.get('total_payment') or 0,
            'rows': rows,
        }

    return {
        'code': 'S2d-DNSN',
        'title': 'Sổ chi tiết tiền',
        'fiscal_year': fiscal_year,
        'sections': [
            _section('111', 'Tiền mặt'),
            _section('112', 'Tiền gửi không kỳ hạn'),
        ],
        'entity': entity_header(conn),
    }


def _partner_label(conn: sqlite3.Connection, partner_type: str | None, partner_id) -> str:
    if not partner_id:
        return 'Không xác định đối tượng'
    pid = int(partner_id)
    pt = (partner_type or '').strip().lower()
    try:
        if pt in ('customer', 'khach_hang', 'ar'):
            row = conn.execute(
                'SELECT name, company_name FROM customers WHERE id = ?', (pid,)
            ).fetchone()
            if row:
                d = dict(row)
                return d.get('company_name') or d.get('name') or f'KH #{pid}'
        if pt in ('supplier', 'ncc', 'ap'):
            row = conn.execute(
                'SELECT name, company_name FROM suppliers WHERE id = ?', (pid,)
            ).fetchone()
            if row:
                d = dict(row)
                return d.get('company_name') or d.get('name') or f'NCC #{pid}'
        if pt in ('employee', 'nv'):
            row = conn.execute(
                'SELECT full_name, name FROM employees WHERE id = ?', (pid,)
            ).fetchone()
            if row:
                d = dict(row)
                return d.get('full_name') or d.get('name') or f'NV #{pid}'
    except Exception:
        pass
    return f'{pt or "ĐT"} #{pid}'


def s4a_partner_book(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    partner_key: str | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Sổ chi tiết thanh toán công nợ — S4a-DNSN.

    partner_key dạng ``customer:12`` / ``supplier:5``.
    Không chọn → trả danh sách đối tượng có phát sinh.
    """
    from Services.sme.branches import branch_sql_filter
    from Services.sme.journal_engine import ensure_sme_journal_ready

    ensure_sme_journal_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    date_from = f'{fiscal_year:04d}-01-01'
    date_to = f'{fiscal_year:04d}-12-31'
    bf, bp = branch_sql_filter(branch_code, alias='je')

    # AR: 131*; AP: 331* (và 338 lương nếu có)
    ar_like = "jl.account_code LIKE '131%'"
    ap_like = "(jl.account_code LIKE '331%' OR jl.account_code LIKE '334%')"

    partners = conn.execute(
        f"""
        SELECT COALESCE(jl.partner_type, '') AS partner_type,
               jl.partner_id,
               SUM(CASE WHEN {ar_like} THEN jl.debit ELSE 0 END) AS ar_debit,
               SUM(CASE WHEN {ar_like} THEN jl.credit ELSE 0 END) AS ar_credit,
               SUM(CASE WHEN {ap_like} THEN jl.debit ELSE 0 END) AS ap_debit,
               SUM(CASE WHEN {ap_like} THEN jl.credit ELSE 0 END) AS ap_credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND je.posting_date >= ? AND je.posting_date <= ?
          AND jl.partner_id IS NOT NULL
          AND ({ar_like} OR {ap_like})
          {bf}
        GROUP BY COALESCE(jl.partner_type, ''), jl.partner_id
        ORDER BY 1, 2
        """,
        (date_from, date_to, *bp),
    ).fetchall()

    partner_list = []
    for p in partners:
        key = f"{p['partner_type'] or 'x'}:{p['partner_id']}"
        partner_list.append({
            'key': key,
            'partner_type': p['partner_type'],
            'partner_id': p['partner_id'],
            'name': _partner_label(conn, p['partner_type'], p['partner_id']),
            'ar_net': _f(_money(p['ar_debit']) - _money(p['ar_credit'])),
            'ap_net': _f(_money(p['ap_credit']) - _money(p['ap_debit'])),
        })

    detail = None
    if partner_key and ':' in partner_key:
        pt, _, pid_s = partner_key.partition(':')
        try:
            pid = int(pid_s)
        except ValueError:
            pid = 0
        if pid:
            # Opening
            op = conn.execute(
                f"""
                SELECT
                  SUM(CASE WHEN {ar_like} THEN jl.debit ELSE 0 END),
                  SUM(CASE WHEN {ar_like} THEN jl.credit ELSE 0 END),
                  SUM(CASE WHEN {ap_like} THEN jl.debit ELSE 0 END),
                  SUM(CASE WHEN {ap_like} THEN jl.credit ELSE 0 END)
                FROM sme_journal_lines jl
                JOIN sme_journal_entries je ON je.id = jl.entry_id
                WHERE je.status IN ('posted', 'reversed')
                  AND je.posting_date < ?
                  AND jl.partner_id = ?
                  AND COALESCE(jl.partner_type, '') = ?
                  AND ({ar_like} OR {ap_like})
                  {bf}
                """,
                (date_from, pid, pt, *bp),
            ).fetchone()
            open_ar = _money(op[0]) - _money(op[1])
            open_ap = _money(op[3]) - _money(op[2])

            lines = conn.execute(
                f"""
                SELECT je.posting_date, je.document_no, je.entry_no,
                       COALESCE(jl.description, je.description, '') AS description,
                       jl.account_code, jl.debit, jl.credit
                FROM sme_journal_lines jl
                JOIN sme_journal_entries je ON je.id = jl.entry_id
                WHERE je.status IN ('posted', 'reversed')
                  AND je.posting_date >= ? AND je.posting_date <= ?
                  AND jl.partner_id = ?
                  AND COALESCE(jl.partner_type, '') = ?
                  AND ({ar_like} OR {ap_like})
                  {bf}
                ORDER BY je.posting_date, je.id, jl.id
                """,
                (date_from, date_to, pid, pt, *bp),
            ).fetchall()

            rows = []
            run_ar, run_ap = open_ar, open_ap
            tot_ar_inc = tot_ar_dec = tot_ap_inc = tot_ap_dec = Decimal('0.00')
            for ln in lines:
                code = ln['account_code'] or ''
                d, c = _money(ln['debit']), _money(ln['credit'])
                ar_inc = ar_dec = ap_inc = ap_dec = Decimal('0.00')
                if code.startswith('131'):
                    ar_inc, ar_dec = d, c
                    run_ar += d - c
                else:
                    ap_inc, ap_dec = c, d
                    run_ap += c - d
                tot_ar_inc += ar_inc
                tot_ar_dec += ar_dec
                tot_ap_inc += ap_inc
                tot_ap_dec += ap_dec
                rows.append({
                    'document_no': ln['document_no'] or ln['entry_no'] or '',
                    'document_date': (ln['posting_date'] or '')[:10],
                    'description': ln['description'] or '',
                    'ar_incurred': _f(ar_inc),
                    'ar_collected': _f(ar_dec),
                    'ar_balance': _f(run_ar),
                    'ap_incurred': _f(ap_inc),
                    'ap_paid': _f(ap_dec),
                    'ap_balance': _f(run_ap),
                })
            detail = {
                'partner_key': partner_key,
                'partner_name': _partner_label(conn, pt, pid),
                'opening': {
                    'ar': _f(open_ar), 'ap': _f(open_ap),
                },
                'totals': {
                    'ar_incurred': _f(tot_ar_inc),
                    'ar_collected': _f(tot_ar_dec),
                    'ap_incurred': _f(tot_ap_inc),
                    'ap_paid': _f(tot_ap_dec),
                },
                'closing': {'ar': _f(run_ar), 'ap': _f(run_ap)},
                'rows': rows,
            }

    return {
        'code': 'S4a-DNSN',
        'title': 'Sổ chi tiết thanh toán công nợ',
        'fiscal_year': fiscal_year,
        'partners': partner_list,
        'detail': detail,
        'entity': entity_header(conn),
    }


def s4b_fixed_assets_book(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Sổ tài sản cố định — S4b-DNSN."""
    from Services.sme.fa_lifecycle import list_active_assets, asset_book_values, list_disposals

    as_of = f'{fiscal_year:04d}-12-31'
    assets = list_active_assets(conn, branch_code=branch_code)
    rows = []
    for a in assets:
        try:
            bv = asset_book_values(conn, int(a['id']), as_of=as_of)
        except Exception:
            fallback = a.get('nguyen_gia_tinh_khau_hao') or a.get('gia_mua_chua_thue') or 0
            bv = {
                'original_cost': fallback,
                'accum_dep': 0,
                'net_book': fallback,
            }
        months = int(a.get('so_thang_khau_hao') or 0) or 1
        cost = _money(
            bv.get('original_cost')
            or bv.get('cost')
            or a.get('nguyen_gia_tinh_khau_hao')
            or 0
        )
        accum = _money(bv.get('accum_dep') or bv.get('accum_depreciation') or 0)
        # Hao mòn trong năm = lũy kế cuối năm − lũy kế đầu năm
        year_dep = Decimal('0.00')
        try:
            bv0 = asset_book_values(conn, int(a['id']), as_of=f'{fiscal_year:04d}-01-01')
            accum0 = _money(bv0.get('accum_dep') or 0)
            # 01-01 may include Jan; approximate year dep from monthly rate
            year_dep = accum - accum0
            if year_dep < 0:
                year_dep = Decimal('0.00')
        except Exception:
            year_dep = (cost / Decimal(str(months))) * Decimal('12') if months else Decimal('0')
            if year_dep > cost:
                year_dep = cost
        net = _money(bv.get('net_book') or bv.get('net_book_value') or (cost - accum))
        annual_rate = round(100.0 * 12.0 / months, 2) if months else 0
        rows.append({
            'code': a.get('ma_tai_san') or '',
            'name': a.get('ten_tai_san') or '',
            'start_date': (a.get('ngay_bat_dau_su_dung') or '')[:10],
            'cost': _f(cost),
            'dep_rate_pct': annual_rate,
            'year_dep': _f(year_dep),
            'accum_dep': _f(accum),
            'net_value': _f(net),
            'status': a.get('tinh_trang') or '',
        })

    disposals = []
    try:
        disposals = list_disposals(
            conn,
            date_from=f'{fiscal_year:04d}-01-01',
            date_to=as_of,
            branch_code=branch_code,
        )
    except TypeError:
        try:
            disposals = list_disposals(conn)  # type: ignore[call-arg]
        except Exception:
            disposals = []
    except Exception:
        disposals = []

    return {
        'code': 'S4b-DNSN',
        'title': 'Sổ tài sản cố định',
        'fiscal_year': fiscal_year,
        'rows': rows,
        'disposals': disposals if isinstance(disposals, list) else [],
        'entity': entity_header(conn),
    }


def s4d_equity_book(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Sổ theo dõi vốn chủ sở hữu — S4d-DNSN."""
    from Services.sme.general_ledger import account_ledger, period_bounds

    date_from, _ = period_bounds(fiscal_year, 1)
    _, date_to = period_bounds(fiscal_year, 12)
    sections_def = (
        ('411', '1. Vốn đầu tư của chủ sở hữu'),
        ('421', '2. Lợi nhuận sau thuế chưa phân phối'),
        ('418', '3. Các quỹ thuộc vốn chủ sở hữu'),
    )
    sections = []
    for code, title in sections_def:
        try:
            led = account_ledger(
                conn, code, date_from=date_from, date_to=date_to, branch_code=branch_code,
            )
        except ValueError:
            sections.append({
                'account_code': code, 'title': title,
                'opening': 0, 'increase': 0, 'decrease': 0, 'closing': 0, 'rows': [],
            })
            continue
        open_bal = _money((led.get('opening') or {}).get('credit', 0)) - _money(
            (led.get('opening') or {}).get('debit', 0)
        )
        # equity normal credit
        rows = []
        run = open_bal
        inc = dec = Decimal('0.00')
        for ln in led.get('lines') or []:
            credit = _money(ln.get('credit'))
            debit = _money(ln.get('debit'))
            inc += credit
            dec += debit
            run += credit - debit
            rows.append({
                'document_no': ln.get('document_no') or ln.get('entry_no') or '',
                'document_date': (ln.get('posting_date') or '')[:10],
                'description': ln.get('description') or '',
                'increase': _f(credit),
                'decrease': _f(debit),
                'balance': _f(run),
            })
        close_bal = _money((led.get('closing') or {}).get('credit', 0)) - _money(
            (led.get('closing') or {}).get('debit', 0)
        )
        sections.append({
            'account_code': code,
            'title': title,
            'opening': _f(open_bal),
            'increase': _f(inc),
            'decrease': _f(dec),
            'closing': _f(close_bal),
            'rows': rows,
        })

    return {
        'code': 'S4d-DNSN',
        'title': 'Sổ theo dõi vốn chủ sở hữu',
        'fiscal_year': fiscal_year,
        'sections': sections,
        'entity': entity_header(conn),
    }


def s2b_pnl_book(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Sổ chi tiết doanh thu, chi phí — S2b-DNSN (mẫu Điều 6/8 TT58)."""
    from Services.sme.branches import branch_sql_filter
    from Services.sme.journal_engine import ensure_sme_journal_ready

    ensure_sme_journal_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    date_from = f'{fiscal_year:04d}-01-01'
    date_to = f'{fiscal_year:04d}-12-31'
    bf, bp = branch_sql_filter(branch_code, alias='je')

    def _lines(where: str, side: str) -> tuple[list[dict], Decimal]:
        rows_db = conn.execute(
            f"""
            SELECT je.posting_date, je.document_no, je.entry_no,
                   COALESCE(jl.description, je.description, '') AS description,
                   jl.account_code, jl.debit, jl.credit
            FROM sme_journal_lines jl
            JOIN sme_journal_entries je ON je.id = jl.entry_id
            WHERE je.status IN ('posted', 'reversed')
              AND je.document_type != 'KCKQ'
              AND je.posting_date >= ? AND je.posting_date <= ?
              AND ({where})
              {bf}
            ORDER BY je.posting_date, je.id
            """,
            (date_from, date_to, *bp),
        ).fetchall()
        rows = []
        total = Decimal('0.00')
        for r in rows_db:
            amt = _money(r['debit'] if side == 'debit' else r['credit']) - _money(
                r['credit'] if side == 'debit' else r['debit']
            )
            if abs(amt) < Decimal('0.005'):
                continue
            total += amt
            rows.append({
                'document_no': (r['document_no'] or r['entry_no'] or ''),
                'document_date': (r['posting_date'] or '')[:10],
                'description': f"[{r['account_code']}] {r['description'] or ''}".strip(),
                'amount': _f(amt),
            })
        return rows, total

    def _account_net(like: str, *, before: bool, normal: str = 'credit') -> Decimal:
        op = '<' if before else '>='
        op2 = '' if before else ' AND je.posting_date <= ?'
        params: list[Any] = [date_from if before else date_from]
        if not before:
            params.append(date_to)
        params.append(like)
        params.extend(bp)
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(jl.debit),0), COALESCE(SUM(jl.credit),0)
            FROM sme_journal_lines jl
            JOIN sme_journal_entries je ON je.id = jl.entry_id
            WHERE je.status IN ('posted','reversed')
              AND je.posting_date {op} ?
              {op2}
              AND jl.account_code LIKE ?
              {bf}
            """,
            params,
        ).fetchone()
        d, c = _money(row[0]), _money(row[1])
        return (c - d) if normal == 'credit' else (d - c)

    # 1. Doanh thu và thu nhập
    rev_rows, rev_total = _lines(
        "(jl.account_code LIKE '511%' OR jl.account_code LIKE '515%' OR jl.account_code LIKE '711%')",
        'credit',
    )
    # 2. Chi phí — nhóm gần mẫu TT58
    cost_buckets = [
        ('materials', 'a) Chi phí NVL, nhiên liệu, hàng hóa',
         "(jl.account_code LIKE '632%' OR jl.account_code LIKE '621%')"),
        ('labor', 'b) Chi phí nhân công / lương',
         "(jl.account_code LIKE '622%' OR jl.account_code LIKE '6272%' OR jl.account_code LIKE '6411%' OR jl.account_code LIKE '6421%')"),
        ('depreciation', 'c) Chi phí khấu hao TSCĐ',
         "(jl.account_code LIKE '6273%' OR jl.account_code LIKE '6412%' OR jl.account_code LIKE '6422%')"),
        ('services', 'd) Chi phí dịch vụ mua ngoài / quản lý, bán hàng khác',
         """((jl.account_code LIKE '627%' OR jl.account_code LIKE '641%' OR jl.account_code LIKE '642%')
             AND jl.account_code NOT LIKE '6272%' AND jl.account_code NOT LIKE '6273%'
             AND jl.account_code NOT LIKE '6411%' AND jl.account_code NOT LIKE '6412%'
             AND jl.account_code NOT LIKE '6421%' AND jl.account_code NOT LIKE '6422%')"""),
        ('interest', 'đ) Chi phí lãi vay',
         "jl.account_code LIKE '635%'"),
        ('other', 'e) Chi phí khác',
         "(jl.account_code LIKE '811%')"),
    ]

    sections = [
        {
            'key': 'revenue',
            'title': '1. Doanh thu và thu nhập',
            'total': _f(rev_total),
            'rows': rev_rows,
        }
    ]
    cost_total = Decimal('0.00')
    for key, title, where in cost_buckets:
        rows, total = _lines(where, 'debit')
        cost_total += total
        sections.append({
            'key': key, 'title': f'2. Chi phí — {title}', 'total': _f(total), 'rows': rows,
        })

    cit_open = _account_net('3334%', before=True, normal='credit')
    cit_incurred_rows, cit_incurred = _lines("jl.account_code LIKE '821%'", 'debit')
    # Đã nộp: Nợ 3334 (giảm phải nộp)
    paid_rows, _paid_wrong = _lines("jl.account_code LIKE '3334%'", 'debit')
    cit_paid = Decimal('0.00')
    for r in paid_rows:
        cit_paid += _money(r['amount'])
    # Phải nộp kỳ có thể lấy từ 821 hoặc PS Có 3334
    _, cit_credit = _lines("jl.account_code LIKE '3334%'", 'credit')
    cit_payable_period = cit_incurred if cit_incurred > 0 else cit_credit
    cit_close = cit_open + cit_payable_period - cit_paid

    return {
        'code': 'S2b-DNSN',
        'title': 'Sổ chi tiết doanh thu, chi phí',
        'fiscal_year': fiscal_year,
        'sections': sections,
        'cit': {
            'opening': _f(cit_open if cit_open > 0 else 0),
            'payable_period': _f(cit_payable_period),
            'paid': _f(cit_paid),
            'closing': _f(cit_close if cit_close > 0 else 0),
            'expense_rows': cit_incurred_rows,
        },
        'summary': {
            'revenue': _f(rev_total),
            'costs': _f(cost_total),
        },
        'entity': entity_header(conn),
    }


def s2c_inventory_summary(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """S2c — danh sách hàng có phát sinh/tồn (chọn mã để xem chi tiết)."""
    conn.row_factory = sqlite3.Row
    date_from = f'{fiscal_year:04d}-01-01'
    date_to = f'{fiscal_year:04d}-12-31'
    products = []
    try:
        has_moves = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_moves'"
        ).fetchone())
        if has_moves:
            rows = conn.execute(
                """
                SELECT p.id, p.product_code, p.name, p.unit,
                       COALESCE(i.quantity, 0) AS qty,
                       COALESCE(i.avg_cost, 0) AS avg_cost,
                       COUNT(sm.id) AS move_count
                FROM products p
                LEFT JOIN inventory i ON i.product_id = p.id
                LEFT JOIN stock_moves sm ON sm.product_id = p.id
                  AND substr(sm.date,1,10) >= ? AND substr(sm.date,1,10) <= ?
                GROUP BY p.id
                HAVING COALESCE(i.quantity, 0) != 0 OR COUNT(sm.id) > 0
                ORDER BY p.name
                LIMIT 800
                """,
                (date_from, date_to),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT p.id, p.product_code, p.name, p.unit,
                       COALESCE(i.quantity, 0) AS qty,
                       COALESCE(i.avg_cost, 0) AS avg_cost,
                       0 AS move_count
                FROM products p
                LEFT JOIN inventory i ON i.product_id = p.id
                WHERE COALESCE(i.quantity, 0) != 0
                ORDER BY p.name
                LIMIT 800
                """
            ).fetchall()
        for r in rows:
            d = dict(r)
            qty = _money(d.get('qty'))
            cost = _money(d.get('avg_cost'))
            products.append({
                'product_id': int(d['id']),
                'product_code': d.get('product_code') or '',
                'name': d.get('name') or '',
                'unit': d.get('unit') or '',
                'quantity': _f(qty),
                'unit_cost': _f(cost),
                'amount': _f(qty * cost),
                'move_count': int(d.get('move_count') or 0),
            })
    except Exception:
        products = []
    return {
        'code': 'S2c-DNSN',
        'title': 'Sổ chi tiết vật liệu, dụng cụ, sản phẩm, hàng hóa',
        'fiscal_year': fiscal_year,
        'products': products,
        'rows': products,
        'detail': None,
        'entity': entity_header(conn),
    }


def s2c_inventory_detail(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    product_id: int,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """S2c chi tiết một mã hàng — nhập/xuất/tồn theo chứng từ."""
    conn.row_factory = sqlite3.Row
    base = s2c_inventory_summary(conn, fiscal_year=fiscal_year, branch_code=branch_code)
    date_from = f'{fiscal_year:04d}-01-01'
    date_to = f'{fiscal_year:04d}-12-31'
    prod = conn.execute(
        "SELECT id, product_code, name, unit FROM products WHERE id = ?",
        (product_id,),
    ).fetchone()
    if not prod:
        raise ValueError('Không tìm thấy hàng hóa')
    pd = dict(prod)

    sm_cols = {r[1] for r in conn.execute('PRAGMA table_info(stock_moves)').fetchall()} \
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_moves'"
        ).fetchone() else set()

    qty_net = """
        CASE WHEN COALESCE(sm.quantity, 0) != 0 THEN sm.quantity
             ELSE COALESCE(sm.in_quantity, 0) - COALESCE(sm.out_quantity, 0) END
    """
    cost_expr = 'COALESCE(sm.cost_price, sm.avg_cost, 0)' if 'cost_price' in sm_cols else 'COALESCE(sm.avg_cost, 0)'

    opening_qty = Decimal('0.00')
    opening_amt = Decimal('0.00')
    if sm_cols:
        op = conn.execute(
            f"""
            SELECT COALESCE(SUM({qty_net}), 0),
                   COALESCE(SUM(({qty_net}) * ({cost_expr})), 0)
            FROM stock_moves sm
            WHERE sm.product_id = ? AND substr(sm.date,1,10) < ?
            """,
            (product_id, date_from),
        ).fetchone()
        opening_qty = _money(op[0] if op else 0)
        opening_amt = _money(op[1] if op else 0)

    rows = []
    run_qty, run_amt = opening_qty, opening_amt
    tot_in_q = tot_in_a = tot_out_q = tot_out_a = Decimal('0.00')
    if sm_cols:
        moves = conn.execute(
            f"""
            SELECT sm.id, substr(sm.date,1,10) AS d, sm.type, sm.ref_document, sm.ref_type,
                   sm.note, {qty_net} AS qty_net, {cost_expr} AS unit_cost
            FROM stock_moves sm
            WHERE sm.product_id = ?
              AND substr(sm.date,1,10) >= ? AND substr(sm.date,1,10) <= ?
            ORDER BY sm.date, sm.id
            """,
            (product_id, date_from, date_to),
        ).fetchall()
        for m in moves:
            md = dict(m)
            qn = _money(md.get('qty_net'))
            uc = _money(md.get('unit_cost'))
            if abs(qn) < Decimal('0.000001') and abs(uc) < Decimal('0.005'):
                continue
            in_q = qn if qn > 0 else Decimal('0')
            out_q = -qn if qn < 0 else Decimal('0')
            in_a = in_q * uc
            out_a = out_q * uc
            run_qty += qn
            # Tồn tiền: nhập cộng, xuất trừ theo đơn giá dòng (đơn giản)
            run_amt += in_a - out_a
            if run_qty < 0:
                run_amt = Decimal('0.00')  # tránh âm bất thường khi thiếu opening
            tot_in_q += in_q
            tot_in_a += in_a
            tot_out_q += out_q
            tot_out_a += out_a
            desc = (md.get('note') or md.get('type') or md.get('ref_type') or '').strip()
            rows.append({
                'document_no': md.get('ref_document') or '',
                'document_date': md.get('d') or '',
                'description': desc,
                'unit': pd.get('unit') or '',
                'unit_price': _f(uc),
                'in_qty': _f(in_q),
                'in_amount': _f(in_a),
                'out_qty': _f(out_q),
                'out_amount': _f(out_a),
                'bal_qty': _f(run_qty),
                'bal_amount': _f(run_amt),
            })

    avg_open = _f(opening_amt / opening_qty) if opening_qty else 0
    base['detail'] = {
        'product_id': product_id,
        'product_code': pd.get('product_code') or '',
        'name': pd.get('name') or '',
        'unit': pd.get('unit') or '',
        'opening': {
            'qty': _f(opening_qty), 'amount': _f(opening_amt), 'unit_price': avg_open,
        },
        'totals': {
            'in_qty': _f(tot_in_q), 'in_amount': _f(tot_in_a),
            'out_qty': _f(tot_out_q), 'out_amount': _f(tot_out_a),
        },
        'closing': {'qty': _f(run_qty), 'amount': _f(run_amt)},
        'rows': rows,
    }
    base['title'] = f"Sổ chi tiết VL/DC/SP/HH — {pd.get('name') or ''}"
    return base


def _sector_key(business_line: str | None, sector_code: str | None, product_type: str | None) -> str:
    raw = (sector_code or business_line or product_type or 'other').strip().lower()
    if raw in DEFAULT_SECTOR_TAX:
        return raw
    if any(x in raw for x in ('hang', 'goods', 'thuong', 'ban_le', 'retail', 'sp', 'tp')):
        return 'goods'
    if any(x in raw for x in ('dich', 'service', 'fb', 'dv')):
        return 'service'
    if any(x in raw for x in ('san_xuat', 'production', 'sx', 'che_bien')):
        return 'production'
    return 'other'


def revenue_sales_book(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    form_code: str = 'S1-DNSN',
    branch_code: str | None = None,
) -> dict[str, Any]:
    """S1 / S2a / S3a — sổ doanh thu theo hóa đơn bán hàng."""
    conn.row_factory = sqlite3.Row
    date_from = f'{fiscal_year:04d}-01-01'
    date_to = f'{fiscal_year:04d}-12-31'
    sector_tax = _load_sector_tax(conn, fiscal_year=fiscal_year)
    catalog = next((b for b in DNSN_BOOK_CATALOG if b['code'] == form_code), None)
    title = (catalog or {}).get('name') or 'Sổ doanh thu bán hàng hóa, dịch vụ'

    has_sale = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sale'"
    ).fetchone())
    if not has_sale:
        return {
            'code': form_code, 'title': title, 'fiscal_year': fiscal_year,
            'groups': [], 'totals': {}, 'entity': entity_header(conn),
            'note': 'Chưa có bảng sale',
        }

    sale_cols = {r[1] for r in conn.execute('PRAGMA table_info(sale)').fetchall()}
    item_cols = {r[1] for r in conn.execute('PRAGMA table_info(sale_items)').fetchall()} \
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sale_items'"
        ).fetchone() else set()

    status_sql = ''
    if 'status' in sale_cols:
        status_sql = "AND LOWER(COALESCE(s.status,'')) NOT IN ('void','cancelled','canceled','draft')"

    # Gom theo chứng từ bán; nhóm ngành từ dòng hàng / products
    sales = conn.execute(
        f"""
        SELECT s.id,
               COALESCE(s.invoice_number, CAST(s.id AS TEXT)) AS doc_no,
               substr(COALESCE(s.invoice_date, s.date), 1, 10) AS doc_date,
               COALESCE(s.customer_name, s.company_name, '') AS customer,
               COALESCE(s.total_amount, 0) AS total_amount,
               COALESCE(s.tax_amount, 0) AS tax_amount,
               COALESCE(s.tax_pct, 0) AS tax_pct,
               COALESCE(s.discount_amount, 0) AS discount_amount
        FROM sale s
        WHERE substr(COALESCE(s.invoice_date, s.date), 1, 10) >= ?
          AND substr(COALESCE(s.invoice_date, s.date), 1, 10) <= ?
          {status_sql}
        ORDER BY doc_date, s.id
        """,
        (date_from, date_to),
    ).fetchall()

    groups: dict[str, dict[str, Any]] = {}
    grand_rev = grand_vat = grand_cit = Decimal('0.00')

    for s in sales:
        sid = int(s['id'])
        # Phân nhóm theo ngành chiếm tỷ trọng lớn nhất trên dòng
        sector = 'other'
        line_rev = Decimal('0.00')
        line_vat = Decimal('0.00')
        if item_cols:
            sector_col = 'si.hkd_sector_code' if 'hkd_sector_code' in item_cols else 'NULL'
            bl_col = 'p.business_line' if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='products'"
            ).fetchone() else 'NULL'
            pt_col = 'p.product_type' if bl_col != 'NULL' else 'NULL'
            disc = 'COALESCE(si.discount_pct,0)' if 'discount_pct' in item_cols else '0'
            taxc = 'COALESCE(si.tax_pct,0)' if 'tax_pct' in item_cols else '0'
            items = conn.execute(
                f"""
                SELECT si.quantity, si.price, {disc} AS discount_pct, {taxc} AS tax_pct,
                       {sector_col} AS sector_code, {bl_col} AS business_line, {pt_col} AS product_type
                FROM sale_items si
                LEFT JOIN products p ON p.id = si.product_id
                WHERE si.sale_id = ?
                """,
                (sid,),
            ).fetchall()
            sector_amt: dict[str, Decimal] = {}
            for it in items:
                sub = _money(it['quantity']) * _money(it['price'])
                after = sub - (sub * _money(it['discount_pct']) / Decimal('100')).quantize(
                    MONEY_Q, rounding=ROUND_HALF_UP
                )
                vat = (after * _money(it['tax_pct']) / Decimal('100')).quantize(
                    MONEY_Q, rounding=ROUND_HALF_UP
                )
                sk = _sector_key(it['business_line'], it['sector_code'], it['product_type'])
                # S2a theo TT58 Điều 6: nhóm theo ngành có cùng tỷ lệ % GTGT (cấu hình)
                # — không nhóm theo thuế suất hóa đơn khấu trừ
                sector_amt[sk] = sector_amt.get(sk, Decimal('0')) + after
                line_rev += after
                line_vat += vat
            if sector_amt:
                sector = max(sector_amt.items(), key=lambda x: x[1])[0]
        else:
            line_rev = _money(s['total_amount']) - _money(s['tax_amount'])
            line_vat = _money(s['tax_amount'])

        if line_rev <= 0 and _money(s['total_amount']) > 0:
            # total_amount thường đã gồm VAT
            if line_vat > 0:
                line_rev = _money(s['total_amount']) - line_vat
            else:
                line_rev = _money(s['total_amount'])

        meta = sector_tax.get(
            sector if not sector.startswith('vat_') else 'goods',
        ) or sector_tax.get('other') or DEFAULT_SECTOR_TAX['other']
        if sector.startswith('vat_'):
            try:
                vat_rate = float(sector.split('_', 1)[1])
            except ValueError:
                vat_rate = 0.0
            label = f'Nhóm thuế suất GTGT {vat_rate:g}%'
            vat_amt = line_vat if line_vat else (
                line_rev * Decimal(str(vat_rate)) / Decimal('100')
            ).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
            cit_amt = Decimal('0.00')
        else:
            label = meta.get('label') or sector
            if form_code in ('S1-DNSN', 'S3a-DNSN'):
                # % trên doanh thu theo nhóm ngành (từ cấu hình)
                vat_amt = (
                    line_rev * Decimal(str(meta.get('vat_pct') or 0)) / Decimal('100')
                ).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
                cit_amt = (
                    line_rev * Decimal(str(meta.get('cit_pct') or 0)) / Decimal('100')
                ).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
            elif form_code == 'S2a-DNSN':
                # PP2: GTGT % trên DT theo ngành; TNDN tính trên thu nhập ở S2b/BCTC
                vat_amt = (
                    line_rev * Decimal(str(meta.get('vat_pct') or 0)) / Decimal('100')
                ).quantize(MONEY_Q, rounding=ROUND_HALF_UP)
                if line_vat > 0:
                    # nếu hóa đơn đã có VAT thì ưu tiên số trên hóa đơn? PP2 là % trên DT nên dùng cấu hình
                    pass
                cit_amt = Decimal('0.00')
            else:
                vat_amt = line_vat
                cit_amt = Decimal('0.00')

        g = groups.setdefault(sector, {
            'key': sector,
            'title': label,
            'vat_pct': meta.get('vat_pct') if not sector.startswith('vat_') else float(sector.split('_', 1)[1] or 0),
            'cit_pct': meta.get('cit_pct'),
            'rows': [],
            'revenue': Decimal('0.00'),
            'vat': Decimal('0.00'),
            'cit': Decimal('0.00'),
        })
        g['rows'].append({
            'document_no': s['doc_no'] or '',
            'document_date': s['doc_date'] or '',
            'description': (s['customer'] or 'Khách lẻ').strip() or 'Doanh thu bán hàng',
            'amount': _f(line_rev),
            'vat_amount': _f(vat_amt),
            'cit_amount': _f(cit_amt),
        })
        g['revenue'] += line_rev
        g['vat'] += vat_amt
        g['cit'] += cit_amt
        grand_rev += line_rev
        grand_vat += vat_amt
        grand_cit += cit_amt

    group_list = []
    for g in groups.values():
        group_list.append({
            'key': g['key'],
            'title': g['title'],
            'vat_pct': g['vat_pct'],
            'cit_pct': g['cit_pct'],
            'rows': g['rows'],
            'revenue': _f(g['revenue']),
            'vat': _f(g['vat']),
            'cit': _f(g['cit']),
        })
    group_list.sort(key=lambda x: x['title'])

    notes = {
        'S1-DNSN': 'PP khoán — thuế GTGT/TNDN ước tính % trên doanh thu theo nhóm ngành (có thể chỉnh tỷ lệ).',
        'S2a-DNSN': 'PP kê khai GTGT — doanh thu theo nhóm thuế suất; VAT lấy từ hóa đơn/dòng hàng.',
        'S3a-DNSN': 'PP GTGT trực tiếp — doanh thu theo nhóm ngành; thuế TNDN % trên doanh thu.',
    }

    return {
        'code': form_code,
        'title': title,
        'fiscal_year': fiscal_year,
        'groups': group_list,
        'totals': {
            'revenue': _f(grand_rev),
            'vat': _f(grand_vat),
            'cit': _f(grand_cit),
        },
        'note': notes.get(form_code, ''),
        'entity': entity_header(conn),
    }


def s3b_vat_book(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """S3b — sổ theo dõi nghĩa vụ thuế GTGT (đầu vào 133 / đầu ra 33311)."""
    from Services.sme.branches import branch_sql_filter
    from Services.sme.journal_engine import ensure_sme_journal_ready

    ensure_sme_journal_ready(conn, commit=False)
    date_from = f'{fiscal_year:04d}-01-01'
    date_to = f'{fiscal_year:04d}-12-31'
    bf, bp = branch_sql_filter(branch_code, alias='je')

    def _open_net(like: str, normal: str) -> Decimal:
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(jl.debit),0), COALESCE(SUM(jl.credit),0)
            FROM sme_journal_lines jl
            JOIN sme_journal_entries je ON je.id = jl.entry_id
            WHERE je.status IN ('posted','reversed')
              AND je.posting_date < ?
              AND jl.account_code LIKE ?
              {bf}
            """,
            (date_from, like, *bp),
        ).fetchone()
        d, c = _money(row[0]), _money(row[1])
        return (d - c) if normal == 'debit' else (c - d)

    open_input = _open_net('133%', 'debit')  # còn được khấu trừ
    open_output = _open_net('33311%', 'credit')  # còn phải nộp (gross before netting)

    # Số dư đầu kỳ net: input - output
    open_credit = open_input - open_output  # >0 được khấu trừ; <0 phải nộp
    open_deductible = open_credit if open_credit > 0 else Decimal('0')
    open_payable = -open_credit if open_credit < 0 else Decimal('0')

    lines = conn.execute(
        f"""
        SELECT je.posting_date, je.document_no, je.entry_no,
               COALESCE(jl.description, je.description, '') AS description,
               jl.account_code, jl.debit, jl.credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted','reversed')
          AND je.posting_date >= ? AND je.posting_date <= ?
          AND (jl.account_code LIKE '133%' OR jl.account_code LIKE '33311%')
          {bf}
        ORDER BY je.posting_date, je.id, jl.id
        """,
        (date_from, date_to, *bp),
    ).fetchall()

    rows = []
    tot_in = tot_out = Decimal('0.00')
    for ln in lines:
        code = ln['account_code'] or ''
        d, c = _money(ln['debit']), _money(ln['credit'])
        vin = vout = Decimal('0.00')
        if code.startswith('133'):
            vin = d - c  # tăng đầu vào
            if vin < 0:
                # hoàn/đảo
                vout = -vin
                vin = Decimal('0')
        else:
            vout = c - d
            if vout < 0:
                vin = -vout
                vout = Decimal('0')
        if abs(vin) < Decimal('0.005') and abs(vout) < Decimal('0.005'):
            continue
        tot_in += vin
        tot_out += vout
        rows.append({
            'document_no': ln['document_no'] or ln['entry_no'] or '',
            'document_date': (ln['posting_date'] or '')[:10],
            'description': f"[{code}] {ln['description'] or ''}".strip(),
            'vat_in': _f(vin),
            'vat_out': _f(vout),
        })

    period_payable = tot_out - tot_in
    # Cuối kỳ
    close_net = open_credit + tot_in - tot_out
    close_deductible = close_net if close_net > 0 else Decimal('0')
    close_payable = -close_net if close_net < 0 else Decimal('0')

    return {
        'code': 'S3b-DNSN',
        'title': 'Sổ theo dõi nghĩa vụ thuế GTGT',
        'fiscal_year': fiscal_year,
        'opening': {
            'deductible': _f(open_deductible),
            'payable': _f(open_payable),
        },
        'totals': {
            'vat_in': _f(tot_in),
            'vat_out': _f(tot_out),
            'payable_period': _f(period_payable if period_payable > 0 else 0),
        },
        'closing': {
            'deductible': _f(close_deductible),
            'payable': _f(close_payable),
        },
        'rows': rows,
        'entity': entity_header(conn),
    }


def s4c_other_tax_book(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """S4c — sổ theo dõi nghĩa vụ thuế khác (3332–3339, 33312, 3334…)."""
    from Services.sme.branches import branch_sql_filter
    from Services.sme.journal_engine import ensure_sme_journal_ready
    from Services.sme.tax_nsnn import TAX_GROUPS

    ensure_sme_journal_ready(conn, commit=False)
    date_from = f'{fiscal_year:04d}-01-01'
    date_to = f'{fiscal_year:04d}-12-31'
    bf, bp = branch_sql_filter(branch_code, alias='je')

    # Bỏ GTGT trong nước 133 / 33311 — thuộc S3b
    prefixes = [
        code for code, _label, _n, _k in TAX_GROUPS
        if code not in ('133', '33311')
    ]

    like_sql = ' OR '.join(["jl.account_code LIKE ?"] * len(prefixes))
    like_params = [f'{p}%' for p in prefixes]

    lines = conn.execute(
        f"""
        SELECT je.posting_date, je.document_no, je.entry_no,
               COALESCE(jl.description, je.description, '') AS description,
               jl.account_code, jl.debit, jl.credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted','reversed')
          AND je.posting_date >= ? AND je.posting_date <= ?
          AND ({like_sql})
          {bf}
        ORDER BY je.posting_date, je.id
        """,
        (date_from, date_to, *like_params, *bp),
    ).fetchall()

    col_map = {
        '33312': 'vat_import',
        '3332': 'special',
        '3333': 'import_export',
        '3334': 'cit',
        '3335': 'pit',
        '3336': 'resource',
        '3337': 'land',
        '3338': 'env',
        '3339': 'other',
    }
    rows = []
    totals = {v: Decimal('0.00') for v in col_map.values()}
    totals['payable'] = Decimal('0.00')

    for ln in lines:
        code = ln['account_code'] or ''
        # credit - debit = phát sinh phải nộp (liability)
        amt = _money(ln['credit']) - _money(ln['debit'])
        if abs(amt) < Decimal('0.005'):
            continue
        bucket = 'other'
        for pref, key in col_map.items():
            if code.startswith(pref):
                bucket = key
                break
        # 3333 covers XNK; 33312 is import VAT
        if code.startswith('33312'):
            bucket = 'vat_import'
        row = {
            'document_date': (ln['posting_date'] or '')[:10],
            'description': f"[{code}] {ln['description'] or ''}".strip(),
            'quantity': '',
            'tax_rate': '',
            'vat_import': 0.0,
            'special': 0.0,
            'import_export': 0.0,
            'cit': 0.0,
            'pit': 0.0,
            'resource': 0.0,
            'land': 0.0,
            'env': 0.0,
            'other': 0.0,
            'payable': _f(amt if amt > 0 else 0),
        }
        row[bucket] = _f(amt)
        totals[bucket] += amt
        if amt > 0:
            totals['payable'] += amt
        rows.append(row)

    return {
        'code': 'S4c-DNSN',
        'title': 'Sổ theo dõi nghĩa vụ thuế khác',
        'fiscal_year': fiscal_year,
        'rows': rows,
        'totals': {k: _f(v) for k, v in totals.items()},
        'note': 'Phát sinh từ TK 33312, 3332–3339 trên nhật ký. Cột lượng/thuế suất điền tay khi có hồ sơ riêng.',
        'entity': entity_header(conn),
    }


def _normalize_book_code(code: str) -> str:
    """Map input to catalog code (preserve catalog casing, e.g. S2d-DNSN)."""
    raw = (code or '').strip()
    if not raw:
        return ''
    upper = raw.upper().replace('_', '-')
    if not upper.endswith('-DNSN'):
        if upper.endswith('DNSN') and '-' not in upper:
            upper = f'{upper[:-4]}-DNSN'
        else:
            upper = f'{upper}-DNSN'
    for k in BOOK_HANDLERS:
        if k.upper() == upper:
            return k
    for b in DNSN_BOOK_CATALOG:
        if str(b.get('code', '')).upper() == upper:
            return b['code']
    return upper


def get_dnsn_book(
    conn: sqlite3.Connection,
    code: str,
    *,
    fiscal_year: int | None = None,
    partner_key: str | None = None,
    product_id: int | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    year = int(fiscal_year or datetime.now().year)
    code_u = _normalize_book_code(code)
    handler = BOOK_HANDLERS.get(code_u)
    if not handler:
        for k, v in BOOK_HANDLERS.items():
            if k.upper() == (code or '').strip().upper():
                handler = v
                code_u = k
                break
    if handler == 's2d':
        return s2d_cash_book(conn, fiscal_year=year, branch_code=branch_code)
    if handler == 's4a':
        return s4a_partner_book(
            conn, fiscal_year=year, partner_key=partner_key, branch_code=branch_code,
        )
    if handler == 's4b':
        return s4b_fixed_assets_book(conn, fiscal_year=year, branch_code=branch_code)
    if handler == 's4d':
        return s4d_equity_book(conn, fiscal_year=year, branch_code=branch_code)
    if handler == 's2b':
        return s2b_pnl_book(conn, fiscal_year=year, branch_code=branch_code)
    if handler == 'revenue':
        return revenue_sales_book(
            conn, fiscal_year=year, form_code=code_u, branch_code=branch_code,
        )
    if handler == 's2c':
        if product_id:
            return s2c_inventory_detail(
                conn, fiscal_year=year, product_id=int(product_id), branch_code=branch_code,
            )
        return s2c_inventory_summary(conn, fiscal_year=year, branch_code=branch_code)
    if handler == 's3b':
        return s3b_vat_book(conn, fiscal_year=year, branch_code=branch_code)
    if handler == 's4c':
        return s4c_other_tax_book(conn, fiscal_year=year, branch_code=branch_code)
    raise ValueError(f'Chưa hỗ trợ sổ {code}')
