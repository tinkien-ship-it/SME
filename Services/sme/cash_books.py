"""Sổ tiền mặt/tiền gửi SME lấy trực tiếp từ nhật ký bút toán kép."""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready

MONEY_Q = Decimal('0.01')


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


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
) -> dict[str, Any]:
    """Lập sổ chi tiết tiền theo dòng Nợ/Có trong nhật ký SME.

    TK 111*: Nợ là thu, Có là chi.
    TK 112*: Nợ là gửi vào, Có là rút/chuyển đi.
    Số dư đầu kỳ và lũy kế đều tính từ bút toán đã ghi sổ, không đọc phiếu HKD.
    """
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

    opening_row = conn.execute(
        """
        SELECT COALESCE(SUM(jl.debit), 0) AS debit,
               COALESCE(SUM(jl.credit), 0) AS credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND je.posting_date < ?
          AND (jl.account_code = ? OR jl.account_code LIKE ?)
        """,
        (date_from, *match_params),
    ).fetchone()
    opening_debit = _money(opening_row['debit'])
    opening_credit = _money(opening_row['credit'])
    opening_balance = opening_debit - opening_credit

    journal_rows = conn.execute(
        """
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
        ORDER BY je.posting_date, je.id, jl.sequence, jl.id
        """,
        (date_from, date_to, *match_params),
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
    }
