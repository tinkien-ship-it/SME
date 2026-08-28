# -*- coding: utf-8 -*-
"""Trần đóng BHXH/BHYT/BHTN theo luật VN (× mức tham chiếu / LTT vùng)."""
from __future__ import annotations

import sqlite3
from typing import Any

# Mức lương cơ sở / tham chiếu mặc định (có thể đổi trên UI cấu hình)
DEFAULT_BHXH_REF = 2_340_000.0
DEFAULT_CAP_MULT = 20.0


def _f(v, default: float = 0.0) -> float:
    try:
        if v is None or v == '':
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def get_cap_config(conn: sqlite3.Connection) -> dict[str, Any]:
    from Services.hrm.schema import ensure_hrm_schema
    ensure_hrm_schema(conn)
    row = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
    info = dict(row) if row else {}
    region = (info.get('salary_region') or '').strip()
    region_min = 0.0
    if region:
        try:
            r = conn.execute(
                'SELECT min_salary FROM salary_regions WHERE region_name = ? LIMIT 1',
                (region,),
            ).fetchone()
            if r:
                region_min = _f(r['min_salary'] if hasattr(r, 'keys') else r[0])
        except sqlite3.Error:
            pass
    if region_min <= 0:
        region_min = _f(info.get('base_salary_insurance'), 0)
    ref = _f(info.get('bhxh_ref_salary'), 0)
    if ref <= 0:
        ref = DEFAULT_BHXH_REF
    bhxh_mult = _f(info.get('bhxh_cap_multiplier'), DEFAULT_CAP_MULT) or DEFAULT_CAP_MULT
    bhtn_mult = _f(info.get('bhtn_cap_multiplier'), DEFAULT_CAP_MULT) or DEFAULT_CAP_MULT
    return {
        'bhxh_ref_salary': ref,
        'bhxh_cap_multiplier': bhxh_mult,
        'bhtn_cap_multiplier': bhtn_mult,
        'region_min_salary': region_min,
        'salary_region': region,
        'bhxh_bhyt_cap': round(ref * bhxh_mult),
        'bhtn_cap': round(region_min * bhtn_mult) if region_min > 0 else round(ref * bhtn_mult),
        'base_salary_insurance': _f(info.get('base_salary_insurance')),
    }


def resolve_insurance_base(
    *,
    insurance_salary: float | None,
    base_salary: float | None,
    time_salary: float | None,
) -> float:
    """Mức lương tham gia BH trước khi áp trần."""
    for v in (insurance_salary, base_salary, time_salary):
        amt = _f(v)
        if amt > 0:
            return amt
    return 0.0


def apply_insurance_caps(
    conn: sqlite3.Connection,
    *,
    insurance_salary: float | None = None,
    base_salary: float | None = None,
    time_salary: float | None = None,
) -> dict[str, float]:
    """
    Trả mức đóng sau trần:
    - BHXH/BHYT: min(căn cứ, 20 × mức tham chiếu)
    - BHTN: min(căn cứ, 20 × LTT vùng)
    """
    cfg = get_cap_config(conn)
    raw = resolve_insurance_base(
        insurance_salary=insurance_salary,
        base_salary=base_salary,
        time_salary=time_salary,
    )
    bhxh_base = min(raw, cfg['bhxh_bhyt_cap']) if cfg['bhxh_bhyt_cap'] > 0 else raw
    bhtn_base = min(raw, cfg['bhtn_cap']) if cfg['bhtn_cap'] > 0 else raw
    return {
        'raw_base': round(raw),
        'bhxh_base': round(bhxh_base),
        'bhyt_base': round(bhxh_base),
        'bhtn_base': round(bhtn_base),
        'bhxh_bhyt_cap': float(cfg['bhxh_bhyt_cap']),
        'bhtn_cap': float(cfg['bhtn_cap']),
        'capped_bhxh': raw > cfg['bhxh_bhyt_cap'] > 0,
        'capped_bhtn': raw > cfg['bhtn_cap'] > 0,
    }
