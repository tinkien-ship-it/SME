"""Sổ tiền mặt/tiền gửi SME lấy trực tiếp từ nhật ký bút toán kép."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready

MONEY_Q = Decimal('0.01')


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def cash_account_balance_as_of(
    conn: sqlite3.Connection,
    *,
    account_code: str,
    as_of: str,
    branch_code: str | None = None,
) -> Decimal:
    """Số dư Nợ − Có của TK tiền (111*/112*) đến hết ngày ``as_of``."""
    from Services.sme.branches import branch_sql_filter

    ensure_sme_journal_ready(conn, commit=False)
    code = (account_code or '').strip()
    date_s = str(as_of or '')[:10]
    if not code or not date_s:
        return Decimal('0.00')
    if not (code.startswith('111') or code.startswith('112')):
        return Decimal('0.00')

    bf, bp = branch_sql_filter(branch_code, alias='je')
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(jl.debit), 0) AS debit,
               COALESCE(SUM(jl.credit), 0) AS credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND date(je.posting_date) <= date(?)
          AND (jl.account_code = ? OR jl.account_code LIKE ?)
          {bf}
        """,
        (date_s, code, f'{code}%', *bp),
    ).fetchone()
    debit = _money(row[0] if not isinstance(row, sqlite3.Row) else row['debit'])
    credit = _money(row[1] if not isinstance(row, sqlite3.Row) else row['credit'])
    return debit - credit


def assert_cash_credits_covered(
    conn: sqlite3.Connection,
    *,
    lines: list[dict[str, Any]],
    posting_date: str,
    branch_code: str | None = None,
) -> None:
    """Chặn bút toán làm số dư tiền mặt / TGNH âm.

    Với mỗi TK 111*/112*: nếu phát sinh ròng Có (chi / rút) > 0 thì
    số dư đến ngày hạch toán phải ≥ số chi.
    """
    date_s = str(posting_date or '')[:10]
    if not date_s:
        return

    net_out: dict[str, Decimal] = {}
    for ln in lines or []:
        code = str(ln.get('account_code') or '').strip()
        if not (code.startswith('111') or code.startswith('112')):
            continue
        out = _money(ln.get('credit')) - _money(ln.get('debit'))
        if out == 0:
            continue
        net_out[code] = net_out.get(code, Decimal('0.00')) + out

    details = cash_detail_balances_as_of(
        conn, as_of=date_s, branch_code=branch_code,
    )

    for code, amount in sorted(net_out.items()):
        if amount <= 0:
            continue  # thu / tăng quỹ (vd. Nợ 1122 khi mua ngoại tệ từ 1121)
        bal = cash_account_balance_as_of(
            conn, account_code=code, as_of=date_s, branch_code=branch_code,
        )
        if bal + Decimal('0.009') < amount:
            kind = 'tiền mặt' if code.startswith('111') else 'tiền gửi ngân hàng'
            if code.startswith('111'):
                detail = (
                    f'TK 1111 (VND): {details.get("1111", 0):,.0f} ₫; '
                    f'TK 1112 (NT): {details.get("1112", 0):,.0f} ₫'
                )
            else:
                detail = (
                    f'TK 1121 (VND): {details.get("1121", 0):,.0f} ₫; '
                    f'TK 1122 (NT): {details.get("1122", 0):,.0f} ₫'
                )
            raise ValueError(
                f'Số dư {kind} TK {code} không đủ để thanh toán. '
                f'Số dư TK {code}: {float(bal):,.0f} ₫, cần chi {float(amount):,.0f} ₫ '
                f'(ngày {date_s}). Chi tiết — {detail}. Không cho phép quỹ âm.'
            )


# TK tiền chi tiết dùng khi kiểm soát số dư / mua ngoại tệ
CASH_DETAIL_ACCOUNTS = ('1111', '1112', '1121', '1122')


def cash_detail_balances_as_of(
    conn: sqlite3.Connection,
    *,
    as_of: str,
    branch_code: str | None = None,
) -> dict[str, float]:
    """Số dư từng TK tiền chi tiết đến ngày ``as_of``."""
    date_s = str(as_of or '')[:10]
    out: dict[str, float] = {}
    for code in CASH_DETAIL_ACCOUNTS:
        if date_s:
            bal = cash_account_balance_as_of(
                conn, account_code=code, as_of=date_s, branch_code=branch_code,
            )
        else:
            bal = Decimal('0.00')
        out[code] = float(bal)
    return out


def resolve_cash_pay_account(payment_method: str | None) -> str | None:
    """Map hình thức chi → mã TK Có cần kiểm số dư (không gộp 1121+1122)."""
    raw = str(payment_method or '').strip().lower()
    if not raw:
        return None
    if raw in ('1112', 'cash_fx', 'fx_cash') or raw.startswith('1112'):
        return '1112'
    if raw in ('1122', 'bank_fx', 'fx_bank') or raw.startswith('1122'):
        return '1122'
    if raw in ('1111', 'cash', '111') or raw.startswith('1111'):
        return '1111'
    if raw in ('1121', 'bank', 'bank_transfer', 'ck', 'transfer', '112') or raw.startswith('1121'):
        return '1121'
    if raw.startswith('111'):
        return '1111'
    if raw.startswith('112'):
        return '1121'
    return None


def cash_fund_balances(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int | None = None,
    branch_code: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Số dư quỹ TM/NH từ sổ kép SME (thay /api/quy-so-du HKD).

    Luôn kèm số dư tách: 1111 / 1112 / 1121 / 1122 để UI kiểm đúng nguồn chi
    (vd. mua ngoại tệ: chi 1121 → vào 1122).
    """
    year = int(fiscal_year or datetime.now().year)
    date_s = str(as_of or '')[:10]
    if not date_s:
        date_s = f'{year:04d}-12-31'

    details = cash_detail_balances_as_of(
        conn, as_of=date_s, branch_code=branch_code,
    )
    cash_bal = _money(details.get('1111', 0)) + _money(details.get('1112', 0))
    bank_bal = _money(details.get('1121', 0)) + _money(details.get('1122', 0))
    cash_group = cash_account_balance_as_of(
        conn, account_code='111', as_of=date_s, branch_code=branch_code,
    )
    bank_group = cash_account_balance_as_of(
        conn, account_code='112', as_of=date_s, branch_code=branch_code,
    )
    return {
        'fiscal_year': year,
        'as_of': date_s,
        'so_du_tien_mat': float(cash_group),
        'so_du_ngan_hang': float(bank_group),
        'so_du_1111': float(details.get('1111', 0)),
        'so_du_1112': float(details.get('1112', 0)),
        'so_du_1121': float(details.get('1121', 0)),
        'so_du_1122': float(details.get('1122', 0)),
        'accounts': details,
        'cash_account': '111',
        'bank_account': '112',
        'source': 'sme_journal',
        'branch_code': branch_code or 'ALL',
        'so_du_tien_mat_chi_tiet': float(cash_bal),
        'so_du_ngan_hang_chi_tiet': float(bank_bal),
    }


def list_cash_accounts(
    conn: sqlite3.Connection,
    account_prefix: str,
) -> list[dict[str, Any]]:
    """Danh sách TK tiền đang hiệu lực để lọc sổ."""
    ensure_sme_journal_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT code, name, level, parent_code, is_postable
        FROM sme_chart_of_accounts
        WHERE is_active = 1 AND (code = ? OR code LIKE ?)
        ORDER BY code
        """,
        (account_prefix, f'{account_prefix}%'),
    ).fetchall()
    return [dict(row) for row in rows]


def cash_account_book(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    account_prefix: str,
    account_code: str | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Lập sổ chi tiết tiền theo dòng Nợ/Có trong nhật ký SME.

    TK 111*: Nợ là thu, Có là chi.
    TK 112*: Nợ là gửi vào, Có là rút/chuyển đi.
    Số dư đầu kỳ và lũy kế đều tính từ bút toán đã ghi sổ, không đọc phiếu HKD.
    """
    from Services.sme.branches import branch_sql_filter

    if account_prefix not in ('111', '112'):
        raise ValueError('Sổ tiền chỉ hỗ trợ nhóm tài khoản 111 hoặc 112')
    if fiscal_year < 2000 or fiscal_year > 2100:
        raise ValueError('Năm tài chính không hợp lệ')

    ensure_sme_journal_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    accounts = list_cash_accounts(conn, account_prefix)
    valid_codes = {row['code'] for row in accounts}

    selected = (account_code or account_prefix).strip()
    if selected not in valid_codes:
        raise ValueError(f'Tài khoản {selected} không thuộc nhóm {account_prefix}')

    # TK tổng hợp bao gồm toàn bộ hậu duệ; TK chi tiết vẫn cho phép tiểu khoản tùy chỉnh.
    match_params = (selected, f'{selected}%')
    date_from = f'{fiscal_year:04d}-01-01'
    date_to = f'{fiscal_year:04d}-12-31'
    bf, bp = branch_sql_filter(branch_code, alias='je')

    opening_row = conn.execute(
        f"""
        SELECT COALESCE(SUM(jl.debit), 0) AS debit,
               COALESCE(SUM(jl.credit), 0) AS credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND je.posting_date < ?
          AND (jl.account_code = ? OR jl.account_code LIKE ?)
          {bf}
        """,
        (date_from, *match_params, *bp),
    ).fetchone()
    opening_debit = _money(opening_row['debit'])
    opening_credit = _money(opening_row['credit'])
    opening_balance = opening_debit - opening_credit

    journal_rows = conn.execute(
        f"""
        SELECT
            jl.id AS line_id,
            jl.entry_id,
            jl.sequence,
            jl.account_code,
            jl.debit,
            jl.credit,
            COALESCE(jl.description, je.description, '') AS description,
            je.entry_no,
            je.posting_date,
            COALESCE(je.document_date, je.posting_date) AS document_date,
            je.document_type,
            je.document_no,
            je.reference_document,
            je.business_type,
            (
                SELECT GROUP_CONCAT(x.account_code, ', ')
                FROM (
                    SELECT DISTINCT other.account_code
                    FROM sme_journal_lines other
                    WHERE other.entry_id = jl.entry_id
                      AND other.id <> jl.id
                    ORDER BY other.sequence, other.id
                ) x
            ) AS counterpart_accounts
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND je.posting_date >= ? AND je.posting_date <= ?
          AND (jl.account_code = ? OR jl.account_code LIKE ?)
          {bf}
        ORDER BY je.posting_date, je.id, jl.sequence, jl.id
        """,
        (date_from, date_to, *match_params, *bp),
    ).fetchall()

    running = opening_balance
    total_receipt = Decimal('0.00')
    total_payment = Decimal('0.00')
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(journal_rows, start=1):
        receipt = _money(row['debit'])
        payment = _money(row['credit'])
        running += receipt - payment
        total_receipt += receipt
        total_payment += payment
        rows.append({
            'sequence': index,
            'line_id': row['line_id'],
            'entry_id': row['entry_id'],
            'posting_date': (row['posting_date'] or '')[:10],
            'document_date': (row['document_date'] or '')[:10],
            'entry_no': row['entry_no'] or f"#{row['entry_id']}",
            'document_no': row['document_no'] or row['entry_no'] or '',
            'document_type': row['document_type'],
            'reference_document': row['reference_document'] or '',
            'business_type': row['business_type'] or '',
            'description': row['description'],
            'account_code': row['account_code'],
            'counterpart_accounts': row['counterpart_accounts'] or '',
            'receipt': float(receipt),
            'payment': float(payment),
            'balance': float(running),
        })

    selected_meta = next(row for row in accounts if row['code'] == selected)
    return {
        'fiscal_year': fiscal_year,
        'date_from': date_from,
        'date_to': date_to,
        'account_prefix': account_prefix,
        'account_code': selected,
        'account_name': selected_meta['name'],
        'accounts': accounts,
        'opening_debit': float(opening_debit),
        'opening_credit': float(opening_credit),
        'opening_balance': float(opening_balance),
        'total_receipt': float(total_receipt),
        'total_payment': float(total_payment),
        'closing_balance': float(running),
        'rows': rows,
        'row_count': len(rows),
        'source': 'sme_journal',
        'branch_code': branch_code or 'ALL',
    }
