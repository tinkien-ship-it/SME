# -*- coding: utf-8 -*-
"""Ngữ cảnh in HĐLĐ — trường đồng bộ modal ký HĐ + điều khoản pháp lý."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Any

from Services.hrm.contracts import CONTRACT_TYPES, get_contract
from Services.hrm.legal_payroll import LUNCH_TAX_FREE_CAP, UNIFORM_TAX_FREE_MONTH

CONTRACT_PRINT_TEMPLATES: dict[str, str] = {
    'indefinite': 'hrm/contracts/indefinite.html',
    'definite': 'hrm/contracts/definite.html',
    'probation': 'hrm/contracts/probation.html',
    'apprentice': 'hrm/contracts/apprentice.html',
}

# Cùng nhãn modal; nature_print = cột Tính chất trên mẫu in (ảnh HĐLĐ)
CONTRACT_MODAL_SALARY_ROWS: tuple[dict[str, str], ...] = (
    {
        'field': 'base_salary',
        'label': 'Lương chính (HĐLĐ)',
        'nature': 'Chịu BHXH, BHYT, BHTN và thuế TNCN',
        'nature_print': 'Chịu BH & thuế',
        'group': 'bh',
    },
    {
        'field': 'allowance_position',
        'label': 'Phụ cấp chức vụ',
        'nature': 'Chịu BHXH, BHYT, BHTN và thuế TNCN',
        'nature_print': 'Chịu BH & thuế',
        'group': 'bh',
    },
    {
        'field': 'allowance_responsibility',
        'label': 'Phụ cấp trách nhiệm',
        'nature': 'Chịu BHXH, BHYT, BHTN và thuế TNCN',
        'nature_print': 'Chịu BH & thuế',
        'group': 'bh',
    },
    {
        'field': 'allowance_seniority',
        'label': 'Phụ cấp thâm niên',
        'nature': 'Chịu BHXH, BHYT, BHTN và thuế TNCN',
        'nature_print': 'Chịu BH & thuế',
        'group': 'bh',
    },
    {
        'field': 'allowance_lunch',
        'label': 'Phụ cấp ăn trưa',
        'nature': f'Miễn thuế TNCN tối đa {LUNCH_TAX_FREE_CAP:,.0f} đ/tháng'.replace(',', '.'),
        'nature_print': 'Miễn thuế ≤ 730.000đ/tháng',
        'group': 'benefit',
    },
    {
        'field': 'allowance_uniform',
        'label': 'Phụ cấp trang phục',
        'nature': 'Miễn thuế TNCN tối đa 5.000.000 đ/năm',
        'nature_print': 'Miễn thuế ≤ 5.000.000đ/năm',
        'group': 'benefit',
    },
    {
        'field': 'allowance_phone',
        'label': 'Phụ cấp điện thoại',
        'nature': 'Khoán chi theo quy chế công ty',
        'nature_print': 'Khoán chi theo quy chế',
        'group': 'benefit',
    },
)

SALARY_ALLOWANCE_ROWS = CONTRACT_MODAL_SALARY_ROWS


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v if v is not None and v != '' else default)
    except (TypeError, ValueError):
        return default


def _fmt_date_vi(iso: str | None) -> str:
    if not iso or not str(iso).strip():
        return '……/……/……'
    try:
        d = datetime.strptime(str(iso)[:10], '%Y-%m-%d')
        return d.strftime('%d/%m/%Y')
    except ValueError:
        return str(iso)[:10]


def _fmt_money(amount: float, *, blank_if_zero: bool = True) -> str:
    if blank_if_zero and not amount:
        return '…………'
    return f'{amount:,.0f}'.replace(',', '.')


def _honorific(name: str | None) -> str:
    n = (name or '').strip().lower()
    if not n:
        return 'Ông/Bà'
    if 'thị' in n or n.startswith('bà '):
        return 'Bà'
    return 'Ông'


def _parse_job_duties(notes: str | None) -> list[str]:
    if not notes or not str(notes).strip():
        return []
    lines: list[str] = []
    for raw in re.split(r'[\r\n;]+', str(notes)):
        line = raw.strip().lstrip('+-•*').strip()
        if line:
            lines.append(line)
    return lines


def _salary_rows(contract: dict) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in CONTRACT_MODAL_SALARY_ROWS:
        amt = _f(contract.get(spec['field']))
        rows.append({
            **spec,
            'nature_display': spec.get('nature_print') or spec['nature'],
            'amount': amt,
            'amount_display': _fmt_money(amt, blank_if_zero=False),
            'used': amt > 0,
        })
    return rows


def _contract_duration_months(start_date: str | None, end_date: str | None) -> int | None:
    if not start_date or not end_date:
        return None
    try:
        s = datetime.strptime(str(start_date)[:10], '%Y-%m-%d')
        e = datetime.strptime(str(end_date)[:10], '%Y-%m-%d')
        months = (e.year - s.year) * 12 + (e.month - s.month)
        if e.day >= s.day:
            months += 1
        return max(1, months)
    except ValueError:
        return None


def _contract_sign_date(start_date: str | None) -> tuple[int, int, int]:
    """Ngày ký HĐ = ngày bắt đầu làm việc (modal), fallback hôm nay."""
    if start_date and str(start_date).strip():
        try:
            d = datetime.strptime(str(start_date)[:10], '%Y-%m-%d')
            return d.day, d.month, d.year
        except ValueError:
            pass
    now = datetime.now()
    return now.day, now.month, now.year


def _pct(mult: float) -> int:
    return int(round(_f(mult) * 100))


def _row_to_dict(row, cur: sqlite3.Cursor | None = None) -> dict[str, Any]:
    if not row:
        return {}
    if hasattr(row, 'keys'):
        return dict(row)
    if cur and cur.description:
        return dict(zip([d[0] for d in cur.description], row))
    return {}


def _load_business_info(conn: sqlite3.Connection) -> dict[str, Any]:
    """Thông tin NSLĐ từ bảng business_info (cấu hình trang Settings)."""
    try:
        cur = conn.execute('SELECT * FROM business_info LIMIT 1')
        return _row_to_dict(cur.fetchone(), cur)
    except sqlite3.Error:
        return {}


def _format_company_bank(info: dict[str, Any]) -> str:
    acc = (info.get('bank_account') or '').strip()
    bank = (info.get('bank_name') or '').strip()
    holder = (info.get('account_holder') or '').strip()
    parts: list[str] = []
    if acc:
        parts.append(acc)
    if bank:
        parts.append(f'({bank})')
    if holder:
        parts.append(f'— {holder}')
    return ' '.join(parts)


def build_contract_print_context(conn: sqlite3.Connection, contract_id: int) -> dict[str, Any]:
    from Services.employee_payroll_helpers import department_label

    c = get_contract(conn, int(contract_id))
    if not c:
        raise ValueError('Không tìm thấy hợp đồng')

    info = _load_business_info(conn)

    si_base = (
        _f(c.get('base_salary'))
        + _f(c.get('allowance_position'))
        + _f(c.get('allowance_responsibility'))
        + _f(c.get('allowance_seniority'))
    )
    benefit_total = (
        _f(c.get('allowance_lunch'))
        + _f(c.get('allowance_uniform'))
        + _f(c.get('allowance_phone'))
    )
    insurance = _f(c.get('insurance_salary')) or si_base

    salary_rows = _salary_rows(c)
    bh_rows = [r for r in salary_rows if r['group'] == 'bh']
    benefit_rows = [r for r in salary_rows if r['group'] == 'benefit']

    rep_name = (
        info.get('representative_name')
        or info.get('director_name')
        or info.get('owner_name')
        or ''
    )
    emp_name = c.get('employee_name') or ''
    # business_info — cùng nguồn với trang Settings (Tên DN, MST, địa chỉ, SĐT, ngân hàng…)
    company_name = (info.get('business_name') or info.get('company_name') or info.get('name') or '').strip()
    company_address = (info.get('address') or info.get('company_address') or '').strip()
    company_phone = (info.get('phone') or '').strip()
    company_email = (info.get('email') or '').strip()
    company_tax_code = (info.get('tax_code') or info.get('mst') or '').strip()
    company_bank_display = _format_company_bank(info)

    from Services.hrm.work_calendar import WEEKDAY_LABELS, contract_work_defaults, _parse_int_list
    wdef = contract_work_defaults(conn, c.get('start_date'))
    ws = (wdef['work_start_time'] or '08:00').strip()
    ls = (wdef['work_lunch_start'] or '12:00').strip()
    le = (wdef['work_lunch_end'] or '13:00').strip()
    we = (wdef['work_end_time'] or '17:00').strip()
    wh = int(_f(c.get('work_hours_day') or wdef['work_hours_day'] or 8))
    wd_raw = c.get('work_weekdays_str') or wdef.get('work_weekdays_str') or '0,1,2,3,4'
    wd_ids = _parse_int_list(wd_raw, (0, 1, 2, 3, 4))
    wd_labels = [label for wid, label in WEEKDAY_LABELS if int(wid) in wd_ids]
    work_days = _f(c.get('work_days_month') or wdef['work_days_month'] or 0)
    shift_text = f'Sáng {ws}–{ls}, nghỉ trưa {ls}–{le}, Chiều {le}–{we}'
    sign_day, sign_month, sign_year = _contract_sign_date(c.get('start_date'))
    birth_raw = (c.get('employee_birth_date') or '').strip()
    birth_vi = _fmt_date_vi(birth_raw) if birth_raw and birth_raw != '……/……/……' else ''
    birth_year = birth_raw[:4] if len(birth_raw) >= 4 else ''
    from Services.hrm.contracts import normalize_contract_no
    contract_no_raw = (c.get('contract_no') or '').strip()
    contract_no_display = (
        normalize_contract_no(contract_no_raw)
        or (f'HĐLĐ-{int(c.get("id") or 0):06d}' if c.get('id') else 'HĐLĐ-000000')
    )
    mult_n = _f(wdef.get('mult_normal'), 1.5)
    mult_w = _f(wdef.get('mult_weekend'), 2.0)
    mult_h = _f(wdef.get('mult_holiday'), 3.0)
    ot_rates_text = (
        f'{_pct(mult_n)}% ngày thường; {_pct(mult_w)}% ngày nghỉ tuần; '
        f'{_pct(mult_h)}% ngày nghỉ lễ, Tết'
    )

    return {
        'c': c,
        'ctype': c.get('contract_type') or 'indefinite',
        'type_label': CONTRACT_TYPES.get(c.get('contract_type'), c.get('contract_type')),
        'template_name': CONTRACT_PRINT_TEMPLATES.get(
            c.get('contract_type') or 'indefinite',
            CONTRACT_PRINT_TEMPLATES['indefinite'],
        ),
        'company_name': company_name,
        'company_address': company_address,
        'company_tax_code': company_tax_code,
        'company_phone': company_phone,
        'company_email': company_email,
        'company_bank_account': company_bank_display,
        'company_bank_name': (info.get('bank_name') or '').strip(),
        'company_account_holder': (info.get('account_holder') or '').strip(),
        'employee_birth_year': birth_year,
        'employee_birth_date_vi': birth_vi,
        'employee_nationality': 'Việt Nam',
        'contract_duration_months': _contract_duration_months(
            c.get('start_date'), c.get('end_date'),
        ),
        'representative': rep_name,
        'representative_title': info.get('representative_title') or 'Giám đốc',
        'representative_honorific': _honorific(rep_name),
        'employee_honorific': _honorific(emp_name),
        'department_label': department_label(c.get('department')),
        'job_duties': _parse_job_duties(c.get('notes')),
        'salary_rows': salary_rows,
        'payroll_rows': salary_rows,
        'allowance_rows': salary_rows,
        'bh_rows': bh_rows,
        'benefit_rows': benefit_rows,
        'si_base': si_base,
        'benefit_total': benefit_total,
        'si_base_display': _fmt_money(si_base, blank_if_zero=False),
        'insurance_display': _fmt_money(insurance, blank_if_zero=False),
        'benefit_total_display': _fmt_money(benefit_total, blank_if_zero=False),
        'start_date_vi': _fmt_date_vi(c.get('start_date')),
        'end_date_vi': _fmt_date_vi(c.get('end_date')),
        'probation_end_vi': _fmt_date_vi(c.get('probation_end_date')),
        'contract_no_display': contract_no_display,
        'sign_day': sign_day,
        'sign_month': sign_month,
        'sign_year': sign_year,
        'sign_place': company_address or company_name or 'trụ sở công ty',
        'probation_salary_rate': 85,
        'work_days_month': work_days,
        'work_days_month_display': f'{work_days:.1f}'.rstrip('0').rstrip('.') if work_days else '0',
        'work_hours_day': wh,
        'work_hours_day_display': f'{wh:.1f}'.rstrip('0').rstrip('.') if wh else '8',
        'work_start_time': ws,
        'work_lunch_start': ls,
        'work_lunch_end': le,
        'work_end_time': we,
        'work_weekdays_display': ', '.join(wd_labels) if wd_labels else wdef.get('work_weekdays_display', ''),
        'work_shift_text': shift_text,
        'ot_rates_text': ot_rates_text,
        'mult_normal_pct': _pct(mult_n),
        'mult_weekend_pct': _pct(mult_w),
        'mult_holiday_pct': _pct(mult_h),
        'standard_month': wdef.get('standard_month'),
        'standard_year': wdef.get('standard_year'),
        'uniform_cap_month': _fmt_money(UNIFORM_TAX_FREE_MONTH, blank_if_zero=False),
        'lunch_cap': _fmt_money(LUNCH_TAX_FREE_CAP, blank_if_zero=False),
    }
