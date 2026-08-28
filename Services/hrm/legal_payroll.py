# -*- coding: utf-8 -*-
"""Bảng lương chuẩn pháp lý VN — khớp mẫu Bang_Luong_Chi_Tiet_Chuan_Phap_Ly_VN.xlsx."""
from __future__ import annotations

import sqlite3
from typing import Any

# Miễn thuế theo thông lệ / mẫu chuẩn
LUNCH_TAX_FREE_CAP = 730_000.0          # PC ăn trưa ≤ 730k/tháng
UNIFORM_TAX_FREE_MONTH = 416_667.0      # PC trang phục ≤ 5 triệu/năm
DEFAULT_BHXH_CAP = 46_800_000.0         # 20 × 2.340.000
SELF_DEDUCTION = 11_000_000.0
DEPENDENT_DEDUCTION = 4_400_000.0

# Cột phụ cấp trên employees / HĐLĐ / salary_detail
LEGAL_ALLOWANCE_COLS = (
    ('allowance_position', 'REAL DEFAULT 0'),       # PC chức vụ — chịu BH & thuế
    ('allowance_responsibility', 'REAL DEFAULT 0'), # PC trách nhiệm — chịu BH & thuế
    ('allowance_seniority', 'REAL DEFAULT 0'),      # PC thâm niên — chịu BH & thuế
    ('allowance_lunch', 'REAL DEFAULT 0'),          # PC ăn trưa — miễn thuế ≤730k
    ('allowance_uniform', 'REAL DEFAULT 0'),        # PC trang phục — miễn ≤5tr/năm
    ('allowance_phone', 'REAL DEFAULT 0'),          # PC điện thoại — khoán chi
    ('employee_code', 'TEXT'),                      # Mã NV
)

# Mẫu in HĐLĐ — cùng nhãn/cột với bảng lương SME_salary.html
PAYROLL_PRINT_ROWS: tuple[dict[str, str], ...] = (
    {
        'field': 'base_salary',
        'code': 'E',
        'label': 'Lương HĐ',
        'full_label': 'Lương chính (HĐLĐ)',
        'group': 'bh',
        'nature': 'Căn cứ tính lương công · chịu BHXH/BHYT/BHTN & thuế TNCN',
    },
    {
        'field': 'allowance_position',
        'code': 'F',
        'label': 'PC CV',
        'full_label': 'Phụ cấp chức vụ',
        'group': 'bh',
        'nature': 'Chịu BHXH/BHYT/BHTN & thuế TNCN',
    },
    {
        'field': 'allowance_responsibility',
        'code': 'G',
        'label': 'PC TN',
        'full_label': 'Phụ cấp trách nhiệm',
        'group': 'bh',
        'nature': 'Chịu BHXH/BHYT/BHTN & thuế TNCN',
    },
    {
        'field': 'allowance_seniority',
        'code': 'H',
        'label': 'PC Thâm niên',
        'full_label': 'Phụ cấp thâm niên',
        'group': 'bh',
        'nature': 'Chịu BHXH/BHYT/BHTN & thuế TNCN',
    },
    {
        'field': 'allowance_lunch',
        'code': 'I',
        'label': 'PC Ăn',
        'full_label': 'Phụ cấp ăn trưa',
        'group': 'benefit',
        'nature': f'Miễn thuế TNCN ≤ {LUNCH_TAX_FREE_CAP:,.0f} đ/tháng'.replace(',', '.'),
    },
    {
        'field': 'allowance_uniform',
        'code': 'J',
        'label': 'PC Trang phục',
        'full_label': 'Phụ cấp trang phục',
        'group': 'benefit',
        'nature': f'Miễn thuế TNCN ≤ {UNIFORM_TAX_FREE_MONTH:,.0f} đ/tháng (5tr/năm)'.replace(',', '.'),
    },
    {
        'field': 'allowance_phone',
        'code': 'K',
        'label': 'PC ĐT',
        'full_label': 'Phụ cấp điện thoại',
        'group': 'benefit',
        'nature': 'Khoán chi theo quy chế · miễn thuế nếu đúng quy định',
    },
)

SALARY_DETAIL_LEGAL_COLS = (
    ('contract_salary', 'REAL DEFAULT 0'),          # E lương chính
    ('allowance_position', 'REAL DEFAULT 0'),
    ('allowance_responsibility', 'REAL DEFAULT 0'),
    ('allowance_seniority', 'REAL DEFAULT 0'),
    ('allowance_lunch', 'REAL DEFAULT 0'),          # mức HĐ
    ('allowance_uniform', 'REAL DEFAULT 0'),
    ('allowance_phone', 'REAL DEFAULT 0'),
    ('standard_days', 'REAL DEFAULT 0'),
    ('ot_hours', 'REAL DEFAULT 0'),                 # TC ngày thường (150%)
    ('ot_hours_weekend_sat', 'REAL DEFAULT 0'),     # TC T7 khi nghỉ (150% mặc định)
    ('ot_hours_weekend', 'REAL DEFAULT 0'),         # TC CN/nghỉ tuần (200%)
    ('ot_hours_holiday', 'REAL DEFAULT 0'),         # TC ngày lễ (300%)
    ('lunch_amount', 'REAL DEFAULT 0'),             # tiền PC ăn thực tế
    ('uniform_amount', 'REAL DEFAULT 0'),
    ('phone_amount', 'REAL DEFAULT 0'),
    ('ot_amount', 'REAL DEFAULT 0'),                # tổng tiền TC
    ('ot_amount_normal', 'REAL DEFAULT 0'),
    ('ot_amount_weekend_sat', 'REAL DEFAULT 0'),
    ('ot_amount_weekend', 'REAL DEFAULT 0'),
    ('ot_amount_holiday', 'REAL DEFAULT 0'),
    ('insurance_salary_base', 'REAL DEFAULT 0'),    # V lương đóng BH
    ('taxable_income', 'REAL DEFAULT 0'),
    ('family_relief', 'REAL DEFAULT 0'),
    ('tax_exempt_lunch', 'REAL DEFAULT 0'),
    ('tax_exempt_uniform', 'REAL DEFAULT 0'),
    ('tax_exempt_phone', 'REAL DEFAULT 0'),
    ('tax_exempt_ot', 'REAL DEFAULT 0'),
    ('employer_bhxh', 'REAL DEFAULT 0'),
    ('employer_bhyt', 'REAL DEFAULT 0'),
    ('employer_bhtn', 'REAL DEFAULT 0'),
    ('employer_insurance', 'REAL DEFAULT 0'),
)


def _f(v, d=0.0) -> float:
    try:
        if v is None or v == '':
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def _r(v) -> float:
    return float(round(_f(v)))


def ensure_legal_payroll_columns(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    from db.schema_helpers import add_column_if_missing
    from db_utils import sqlite_commit

    for col, ddl in LEGAL_ALLOWANCE_COLS:
        add_column_if_missing(conn, 'employees', col, ddl)
    from Services.employee_payroll_helpers import resolve_salary_detail_table, _ensure_columns
    sd_table = resolve_salary_detail_table(conn)
    _ensure_columns(conn, sd_table, dict(SALARY_DETAIL_LEGAL_COLS))
    # HĐLĐ
    try:
        from Services.hrm.schema import ensure_hrm_schema
        ensure_hrm_schema(conn)
        for col, ddl in LEGAL_ALLOWANCE_COLS:
            if col == 'employee_code':
                continue
            add_column_if_missing(conn, 'hrm_employment_contracts', col, ddl)
        add_column_if_missing(conn, 'hrm_employment_contracts', 'work_days_month', 'REAL')
        add_column_if_missing(conn, 'hrm_employment_contracts', 'work_hours_day', 'REAL DEFAULT 8')
        add_column_if_missing(conn, 'hrm_employment_contracts', 'work_start_time', "TEXT DEFAULT '08:00'")
        add_column_if_missing(conn, 'hrm_employment_contracts', 'work_lunch_start', "TEXT DEFAULT '12:00'")
        add_column_if_missing(conn, 'hrm_employment_contracts', 'work_lunch_end', "TEXT DEFAULT '13:00'")
        add_column_if_missing(conn, 'hrm_employment_contracts', 'work_end_time', "TEXT DEFAULT '17:00'")
        add_column_if_missing(conn, 'hrm_employment_contracts', 'work_weekdays_str', 'TEXT')
    except Exception:
        pass
    if commit:
        try:
            sqlite_commit(conn, label='legal_payroll_cols')
        except Exception:
            pass


def bhxh_cap_amount(conn: sqlite3.Connection | None = None) -> float:
    if conn is None:
        return DEFAULT_BHXH_CAP
    try:
        from Services.hrm.insurance_cap import get_cap_config
        return float(get_cap_config(conn).get('bhxh_bhyt_cap') or DEFAULT_BHXH_CAP)
    except Exception:
        return DEFAULT_BHXH_CAP


def calculate_tncn_progressive(taxable_income: float) -> float:
    """Biểu thuế rút gọn 7 bậc (tháng) — đúng công thức mẫu Excel."""
    x = _f(taxable_income)
    if x <= 0:
        return 0.0
    if x <= 5_000_000:
        return x * 0.05
    if x <= 10_000_000:
        return x * 0.1 - 250_000
    if x <= 18_000_000:
        return x * 0.15 - 750_000
    if x <= 32_000_000:
        return x * 0.2 - 1_650_000
    if x <= 52_000_000:
        return x * 0.25 - 3_250_000
    if x <= 80_000_000:
        return x * 0.3 - 5_850_000
    return x * 0.35 - 9_850_000


def calculate_tncn_progressive(taxable_income: float) -> float:
    """Biểu thuế rút gọn 7 bậc (tháng) — đúng công thức mẫu Excel."""
    x = _f(taxable_income)
    if x <= 0:
        return 0.0
    if x <= 5_000_000:
        return x * 0.05
    if x <= 10_000_000:
        return x * 0.1 - 250_000
    if x <= 18_000_000:
        return x * 0.15 - 750_000
    if x <= 32_000_000:
        return x * 0.2 - 1_650_000
    if x <= 52_000_000:
        return x * 0.25 - 3_250_000
    if x <= 80_000_000:
        return x * 0.3 - 5_850_000
    return x * 0.35 - 9_850_000


def _ot_tax_exempt(amount: float, multiplier: float) -> float:
    """Phần miễn thuế TNCN = tiền TC × (hệ số − 1) / hệ số."""
    mult = max(_f(multiplier), 1.0)
    amt = max(_f(amount), 0.0)
    if mult <= 1.0 or amt <= 0:
        return 0.0
    return _r(amt * (mult - 1.0) / mult)


def compute_ot_breakdown(
    contract_salary: float,
    standard_days: float,
    *,
    ot_hours: float = 0,
    ot_hours_weekend_sat: float = 0,
    ot_hours_weekend: float = 0,
    ot_hours_holiday: float = 0,
    mult_normal: float = 1.5,
    mult_sat: float = 1.5,
    mult_weekend: float = 2.0,
    mult_holiday: float = 3.0,
    hours_per_day: float = 8.0,
) -> dict[str, float]:
    """
    Tính tiền & miễn thuế tăng ca theo từng loại ngày.
    Đơn giá giờ = Lương chính ÷ công chuẩn ÷ giờ/ngày.
    """
    e = _f(contract_salary)
    L = max(_f(standard_days), 1.0)
    hpd = max(_f(hours_per_day), 1.0)
    rate = e / L / hpd if e > 0 else 0.0

    h_n = max(_f(ot_hours), 0.0)
    h_sat = max(_f(ot_hours_weekend_sat), 0.0)
    h_we = max(_f(ot_hours_weekend), 0.0)
    h_hol = max(_f(ot_hours_holiday), 0.0)

    m_n = max(_f(mult_normal), 1.0)
    m_sat = max(_f(mult_sat), 1.0)
    m_we = max(_f(mult_weekend), 1.0)
    m_hol = max(_f(mult_holiday), 1.0)

    amt_n = _r(rate * h_n * m_n)
    amt_sat = _r(rate * h_sat * m_sat)
    amt_we = _r(rate * h_we * m_we)
    amt_hol = _r(rate * h_hol * m_hol)
    total = amt_n + amt_sat + amt_we + amt_hol

    tax_exempt = (
        _ot_tax_exempt(amt_n, m_n)
        + _ot_tax_exempt(amt_sat, m_sat)
        + _ot_tax_exempt(amt_we, m_we)
        + _ot_tax_exempt(amt_hol, m_hol)
    )

    return {
        'ot_hours': h_n,
        'ot_hours_weekend_sat': h_sat,
        'ot_hours_weekend': h_we,
        'ot_hours_holiday': h_hol,
        'ot_amount_normal': amt_n,
        'ot_amount_weekend_sat': amt_sat,
        'ot_amount_weekend': amt_we,
        'ot_amount_holiday': amt_hol,
        'ot_amount': total,
        'tax_exempt_ot': _r(tax_exempt),
        'hourly_rate': _r(rate),
    }


def compute_legal_payroll_line(
    *,
    contract_salary: float,
    allowance_position: float = 0,
    allowance_responsibility: float = 0,
    allowance_seniority: float = 0,
    allowance_lunch: float = 0,
    allowance_uniform: float = 0,
    allowance_phone: float = 0,
    standard_days: float = 22,
    actual_days: float = 22,
    ot_hours: float = 0,
    ot_hours_weekend_sat: float = 0,
    ot_hours_weekend: float = 0,
    ot_hours_holiday: float = 0,
    mult_normal: float = 1.5,
    mult_sat: float = 1.5,
    mult_weekend: float = 2.0,
    mult_holiday: float = 3.0,
    hours_per_day: float = 8.0,
    bonus_kpi: float = 0,
    dependents: int = 0,
    self_deduction: float = SELF_DEDUCTION,
    dependent_deduction: float = DEPENDENT_DEDUCTION,
    rates_frac: dict[str, float] | None = None,
    is_chu_ho: bool = False,
    bhxh_cap: float | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, float]:
    """
    Công thức Excel + tách tăng ca:
      O = (E+F+G+H)/L*M
      P,Q,R = I,J,K /L*M
      S = TC thường + TC T7 + TC CN + TC lễ (hệ số 150/150/200/300%)
      U = O+P+Q+R+S+T
      V = MIN(E+F+G+H, trần BHXH)
      TNTT = U − PC miễn thuế − miễn thuế TC − BH − GTGC
    """
    e = _f(contract_salary)
    f_pos = _f(allowance_position)
    g_resp = _f(allowance_responsibility)
    h_sen = _f(allowance_seniority)
    i_lunch = _f(allowance_lunch)
    j_uni = _f(allowance_uniform)
    k_phone = _f(allowance_phone)
    L = max(_f(standard_days), 1.0)
    M = max(_f(actual_days), 0.0)
    T = _f(bonus_kpi)

    si_components = e + f_pos + g_resp + h_sen  # chịu BH & thuế (mức HĐ)
    time_salary = _r(si_components / L * M)     # O
    lunch_amt = _r(i_lunch / L * M)             # P
    uniform_amt = _r(j_uni / L * M)             # Q
    phone_amt = _r(k_phone / L * M)             # R

    ot = compute_ot_breakdown(
        e, L,
        ot_hours=ot_hours,
        ot_hours_weekend_sat=ot_hours_weekend_sat,
        ot_hours_weekend=ot_hours_weekend,
        ot_hours_holiday=ot_hours_holiday,
        mult_normal=mult_normal,
        mult_sat=mult_sat,
        mult_weekend=mult_weekend,
        mult_holiday=mult_holiday,
        hours_per_day=hours_per_day,
    )
    ot_amount = ot['ot_amount']

    total_income = time_salary + lunch_amt + uniform_amt + phone_amt + ot_amount + T  # U

    cap = _f(bhxh_cap)
    if cap <= 0:
        cap = bhxh_cap_amount(conn)
    insurance_base = min(si_components, cap)  # V

    rf = rates_frac or {}
    r_bhxh = _f(rf.get('nld_bhxh'), 0.08)
    r_bhyt = _f(rf.get('nld_bhyt'), 0.015)
    r_bhtn = 0.0 if is_chu_ho else _f(rf.get('nld_bhtn'), 0.01)

    # BHTN trần riêng theo vùng nếu có conn
    bhtn_base = insurance_base
    if conn is not None:
        try:
            from Services.hrm.insurance_cap import apply_insurance_caps
            caps = apply_insurance_caps(
                conn, insurance_salary=si_components, base_salary=si_components,
            )
            insurance_base = min(si_components, caps['bhxh_base']) if caps['bhxh_bhyt_cap'] else insurance_base
            # apply_insurance_caps already mins — use returned bases
            insurance_base = caps['bhxh_base']
            bhtn_base = caps['bhtn_base']
        except Exception:
            pass

    bhxh = _r(insurance_base * r_bhxh)
    bhyt = _r(insurance_base * r_bhyt)
    bhtn = _r(bhtn_base * r_bhtn)
    bh_total = bhxh + bhyt + bhtn

    family = _f(self_deduction) + int(dependents or 0) * _f(dependent_deduction)
    tax_free_lunch = min(lunch_amt, LUNCH_TAX_FREE_CAP)
    tax_free_uniform = min(uniform_amt, UNIFORM_TAX_FREE_MONTH)
    tax_free_phone = phone_amt  # khoán chi — miễn toàn bộ trong mẫu
    tax_free_ot = ot['tax_exempt_ot']

    taxable = max(
        0.0,
        total_income - tax_free_lunch - tax_free_uniform - tax_free_phone - tax_free_ot - bh_total - family,
    )
    tncn = _r(calculate_tncn_progressive(taxable))
    final_amount = total_income - bh_total - tncn

    r_chu_bhxh = _f(rf.get('chu_bhxh'), 0.175)
    r_chu_bhyt = _f(rf.get('chu_bhyt'), 0.03)
    r_chu_bhtn = 0.0 if is_chu_ho else _f(rf.get('chu_bhtn'), 0.01)
    employer_bhxh = _r(insurance_base * r_chu_bhxh)
    employer_bhyt = _r(insurance_base * r_chu_bhyt)
    employer_bhtn = _r(bhtn_base * r_chu_bhtn)

    # Tương thích field cũ
    allowance_fund = f_pos + g_resp + h_sen  # PC chịu BH
    allowance_other = lunch_amt + uniform_amt + phone_amt

    return {
        'contract_salary': e,
        'allowance_position': f_pos,
        'allowance_responsibility': g_resp,
        'allowance_seniority': h_sen,
        'allowance_lunch': i_lunch,
        'allowance_uniform': j_uni,
        'allowance_phone': k_phone,
        'standard_days': L,
        'actual_working_days': M,
        'ot_hours': ot['ot_hours'],
        'ot_hours_weekend_sat': ot['ot_hours_weekend_sat'],
        'ot_hours_weekend': ot['ot_hours_weekend'],
        'ot_hours_holiday': ot['ot_hours_holiday'],
        'time_salary': time_salary,
        'lunch_amount': lunch_amt,
        'uniform_amount': uniform_amt,
        'phone_amount': phone_amt,
        'ot_amount': ot_amount,
        'ot_amount_normal': ot['ot_amount_normal'],
        'ot_amount_weekend_sat': ot['ot_amount_weekend_sat'],
        'ot_amount_weekend': ot['ot_amount_weekend'],
        'ot_amount_holiday': ot['ot_amount_holiday'],
        'bonus': T,
        'total_income': total_income,
        'insurance_salary_base': insurance_base,
        'bhxh': bhxh,
        'bhyt': bhyt,
        'bhtn': bhtn,
        'bh_total': bh_total,
        'dependents': float(int(dependents or 0)),
        'family_relief': family,
        'taxable_income': taxable,
        'tncn_tax': tncn,
        'total_deduct': bh_total + tncn,
        'final_amount': final_amount,
        'tax_exempt_lunch': tax_free_lunch,
        'tax_exempt_uniform': tax_free_uniform,
        'tax_exempt_phone': tax_free_phone,
        'tax_exempt_ot': tax_free_ot,
        'employer_bhxh': employer_bhxh,
        'employer_bhyt': employer_bhyt,
        'employer_bhtn': employer_bhtn,
        'employer_insurance': employer_bhxh + employer_bhyt + employer_bhtn,
        'allowance_fund': allowance_fund,
        'allowance_other': allowance_other,
        'base_salary': e,
        'is_chu_ho': 1.0 if is_chu_ho else 0.0,
    }


def verify_against_sample() -> list[str]:
    """Đối chiếu 1 dòng mẫu NV001 trong Excel."""
    line = compute_legal_payroll_line(
        contract_salary=25_000_000,
        allowance_position=3_000_000,
        allowance_responsibility=2_000_000,
        allowance_seniority=1_000_000,
        allowance_lunch=730_000,
        allowance_uniform=416_667,
        allowance_phone=1_000_000,
        standard_days=22,
        actual_days=22,
        ot_hours=5,
        bonus_kpi=5_000_000,
        dependents=2,
        bhxh_cap=46_800_000,
    )
    expect = {
        'time_salary': 31_000_000,
        'lunch_amount': 730_000,
        'uniform_amount': 416_667,
        'phone_amount': 1_000_000,
        'ot_amount': 1_065_341,
        'total_income': 39_212_008,
        'insurance_salary_base': 31_000_000,
        'bhxh': 2_480_000,
        'final_amount': 34_658_724,
    }
    errs = []
    for k, v in expect.items():
        if abs(line.get(k, 0) - v) > 2:
            errs.append(f'{k}: got {line.get(k)} expect {v}')
    return errs
