"""Cửa sổ kê khai GTGT (tháng / quý) — module thuần, không phụ thuộc period_lock."""
from __future__ import annotations

from typing import Any

from Services.tenant_profile import normalize_vat_filing_period


def quarter_bounds(quarter: int) -> tuple[int, int]:
    q = int(quarter)
    if q < 1 or q > 4:
        raise ValueError('Quý phải từ 1 đến 4')
    start = (q - 1) * 3 + 1
    return start, start + 2


def resolve_filing_window(
    *,
    filing_mode: str | None = None,
    period: int | None = None,
    quarter: int | None = None,
) -> dict[str, Any]:
    """Xác định cửa sổ kê khai tháng hoặc quý."""
    if filing_mode:
        mode = normalize_vat_filing_period(filing_mode, default='monthly')
    elif quarter is not None:
        mode = 'quarterly'
    else:
        mode = 'monthly'

    if mode == 'quarterly':
        if quarter is not None:
            q = int(quarter)
        elif period is not None:
            p = int(period)
            if p < 1 or p > 12:
                raise ValueError('Kỳ phải từ 1 đến 12')
            q = (p - 1) // 3 + 1
        else:
            raise ValueError('Cần quý hoặc tháng khi kê khai theo quý')
        p_from, p_to = quarter_bounds(q)
        return {
            'filing_mode': 'quarterly',
            'quarter': q,
            'period_from': p_from,
            'period_to': p_to,
            'period': p_to,
            'label': f'Quý {q} (T{p_from}–T{p_to})',
        }

    p = int(period or 0)
    if p < 1 or p > 12:
        raise ValueError('Kỳ phải từ 1 đến 12')
    return {
        'filing_mode': 'monthly',
        'quarter': (p - 1) // 3 + 1,
        'period_from': p,
        'period_to': p,
        'period': p,
        'label': f'Tháng {p}',
    }
