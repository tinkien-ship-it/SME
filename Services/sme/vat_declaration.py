"""Tờ khai GTGT DN — bảng kê / chỉ tiêu từ sổ kép (tháng hoặc quý)."""
from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.bctc_report import _period_activity
from Services.sme.journal_engine import ensure_sme_journal_ready
from Services.sme.tax_nsnn import resolve_filing_window, tax_nsnn_summary

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _sum_side(activity: dict, prefixes: tuple[str, ...], *, side: str) -> Decimal:
    total = Decimal('0.00')
    for code, bal in activity.items():
        if not any(code == p or code.startswith(p) for p in prefixes):
            continue
        d, c = _money(bal.get('debit')), _money(bal.get('credit'))
        if side == 'credit':
            total += c - d
        else:
            total += d - c
    return _money(total)


def vat_declaration_worksheet(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int | None = None,
    quarter: int | None = None,
    filing_mode: str | None = None,
) -> dict[str, Any]:
    """
    Bảng chỉ tiêu kê khai GTGT (rút gọn, phục vụ đối chiếu sổ):
    - Doanh thu chịu thuế / GTGT đầu ra (PS Có 511* / 33311 trong kỳ)
    - GTGT đầu vào được khấu trừ (PS Nợ 133 trong kỳ)
    - Thuế phải nộp / còn được khấu trừ
    """
    ensure_sme_journal_ready(conn, commit=False)
    if not filing_mode and quarter is None:
        from Services.sme.regime_profile import default_vat_filing_mode
        filing_mode = default_vat_filing_mode(conn)
    window = resolve_filing_window(
        filing_mode=filing_mode, period=period, quarter=quarter,
    )
    p_from, p_to = int(window['period_from']), int(window['period_to'])
    # Loại QTGT: quyết toán bù trừ 133↔333 làm phát sinh kỳ về ~0
    activity = _period_activity(
        conn, fiscal_year, p_from, p_to,
        exclude_document_types=('QTGT', 'KCKQ'),
    )
    nsnn = tax_nsnn_summary(
        conn,
        fiscal_year=fiscal_year,
        period=p_to if window['filing_mode'] == 'monthly' else None,
        quarter=window['quarter'] if window['filing_mode'] == 'quarterly' else None,
        filing_mode=window['filing_mode'],
    )

    # DT chịu thuế ≈ PS Có 511; tách thuế suất từ journal_lines.tax_rate khi có
    revenue = _sum_side(activity, ('511', '515'), side='credit')
    vat_out = _sum_side(activity, ('33311',), side='credit')
    vat_in = _sum_side(activity, ('133', '1331', '1332'), side='debit')
    payable = max(Decimal('0.00'), vat_out - vat_in)
    credit = max(Decimal('0.00'), vat_in - vat_out)
    rate_breakdown = _vat_rate_breakdown(conn, fiscal_year, p_from, p_to)

    indicators = [
        {
            'code': '21',
            'label': 'Doanh thu hàng hóa, dịch vụ bán ra (PS Có 511/515)',
            'amount': _f(max(Decimal('0'), revenue)),
        },
        {
            'code': '22',
            'label': 'Thuế GTGT đầu ra (PS Có 33311 trong kỳ kê khai)',
            'amount': _f(max(Decimal('0'), vat_out)),
        },
        {
            'code': '23',
            'label': 'Thuế GTGT đầu vào được khấu trừ (PS Nợ 133 trong kỳ)',
            'amount': _f(max(Decimal('0'), vat_in)),
        },
        {
            'code': '24',
            'label': 'Thuế GTGT còn được khấu trừ chuyển kỳ sau',
            'amount': _f(credit),
        },
        {
            'code': '25',
            'label': 'Thuế GTGT phải nộp trong kỳ',
            'amount': _f(payable),
        },
        {
            'code': '26',
            'label': 'Số dư GTGT phải nộp cuối kỳ (sau QTGT — từ sổ 33311−133)',
            'amount': float((nsnn.get('summary') or {}).get('vat_payable') or 0),
        },
        {
            'code': '27',
            'label': 'Số dư GTGT còn được khấu trừ cuối kỳ',
            'amount': float((nsnn.get('summary') or {}).get('vat_credit_carry') or 0),
        },
    ]

    # Bảng kê theo tháng trong quý (nếu quý)
    monthly_break = []
    for m in range(p_from, p_to + 1):
        act = _period_activity(
            conn, fiscal_year, m, m,
            exclude_document_types=('QTGT', 'KCKQ'),
        )
        vo = _sum_side(act, ('33311',), side='credit')
        vi = _sum_side(act, ('133',), side='debit')
        monthly_break.append({
            'period': m,
            'label': f'Tháng {m}',
            'revenue': _f(max(Decimal('0'), _sum_side(act, ('511', '515'), side='credit'))),
            'vat_output': _f(max(Decimal('0'), vo)),
            'vat_input': _f(max(Decimal('0'), vi)),
            'vat_payable': _f(max(Decimal('0'), vo - vi)),
        })

    notes = [
        'Bảng chỉ tiêu đối chiếu sổ kép SME — XML khung HTKK (SME-1.0) tải tại nút XML.',
        'Chỉ tiêu 21–25 lấy phát sinh trong cửa sổ kê khai; 26–27 lấy số dư cuối kỳ.',
        'Nên chạy quyết toán GTGT (133↔33311) trước khi khóa sổ kỳ.',
        'File XML SME dùng lưu trữ/đối chiếu; kiểm tra schema HTKK hiện hành trước khi nộp eTax.',
    ]
    if rate_breakdown:
        notes.append('Đã tách GTGT đầu ra theo tax_rate trên dòng nhật ký (khi có).')
    else:
        notes.append('Chưa có tax_rate trên dòng nhật ký — chỉ tiêu 22 là tổng PS Có 33311.')

    return {
        'fiscal_year': fiscal_year,
        'filing_mode': window['filing_mode'],
        'filing_label': window['label'],
        'quarter': window['quarter'],
        'period_from': p_from,
        'period_to': p_to,
        'indicators': indicators,
        'monthly_break': monthly_break,
        'rate_breakdown': rate_breakdown,
        'summary': {
            'revenue': _f(max(Decimal('0'), revenue)),
            'vat_output': _f(max(Decimal('0'), vat_out)),
            'vat_input': _f(max(Decimal('0'), vat_in)),
            'vat_payable': _f(payable),
            'vat_credit': _f(credit),
        },
        'notes': notes,
        'nsnn': nsnn,
    }


def _vat_rate_breakdown(
    conn: sqlite3.Connection,
    fiscal_year: int,
    period_from: int,
    period_to: int,
) -> list[dict[str, Any]]:
    """Tách PS Có 33311 theo tax_rate trên dòng nhật ký (nếu cột tồn tại)."""
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_journal_lines)').fetchall()}
        if 'tax_rate' not in cols:
            return []
        rows = conn.execute(
            """
            SELECT COALESCE(jl.tax_rate, -1) AS tax_rate,
                   SUM(jl.credit - jl.debit) AS amount
            FROM sme_journal_lines jl
            JOIN sme_journal_entries je ON je.id = jl.entry_id
            WHERE je.status IN ('posted', 'reversed')
              AND je.fiscal_year = ?
              AND je.period BETWEEN ? AND ?
              AND je.document_type NOT IN ('QTGT', 'KCKQ')
              AND (jl.account_code = '33311' OR jl.account_code LIKE '33311%')
            GROUP BY COALESCE(jl.tax_rate, -1)
            HAVING ABS(SUM(jl.credit - jl.debit)) > 0.0001
            ORDER BY tax_rate
            """,
            (int(fiscal_year), int(period_from), int(period_to)),
        ).fetchall()
        out = []
        for r in rows:
            rate = r[0]
            amt = _money(r[1])
            if amt < 0:
                continue
            label = 'Không gắn thuế suất' if rate is None or float(rate) < 0 else f'{float(rate):g}%'
            out.append({
                'tax_rate': None if rate is None or float(rate) < 0 else float(rate),
                'label': label,
                'vat_output': _f(amt),
            })
        # Chỉ trả về nếu có ít nhất 1 dòng gắn rate thật
        if not any(x['tax_rate'] is not None for x in out):
            return []
        return out
    except sqlite3.Error:
        return []
