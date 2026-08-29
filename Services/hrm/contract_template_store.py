# -*- coding: utf-8 -*-
"""Mẫu in HĐLĐ tùy chỉnh theo tenant — placeholder [[FIELD]], lưu trong crm_settings.

Mỗi loại HĐ (indefinite/definite/probation/apprentice) có thể nạp HTML riêng.
Khôi phục mặc định = xóa bản tenant → in lại bằng mẫu Jinja chuẩn pháp lý hệ thống.
"""
from __future__ import annotations

import html
import re
import sqlite3
from typing import Any

from Services.crm_schema import ensure_crm_schema
from Services.hrm.contracts import CONTRACT_TYPES
from Services.hrm.contract_templates import CONTRACT_PRINT_TEMPLATES

PLACEHOLDER_RE = re.compile(r'\[\[([A-Z0-9_]+)\]\]')
SETTING_PREFIX = 'hrm_ld_template_'
REQUIRED_MARKERS = ('[[CONTRACT_NO]]', '[[SALARY_TABLE]]')

KNOWN_PLACEHOLDERS: tuple[tuple[str, str], ...] = (
    ('CONTRACT_NO', 'Số HĐLĐ'),
    ('TYPE_LABEL', 'Loại HĐ (Thử việc / XĐTH / KXĐTH / Học việc)'),
    ('COMPANY_NAME', 'Tên doanh nghiệp (Settings)'),
    ('COMPANY_ADDRESS', 'Địa chỉ DN'),
    ('COMPANY_TAX_CODE', 'MST DN'),
    ('COMPANY_PHONE', 'ĐT DN'),
    ('COMPANY_EMAIL', 'Email DN'),
    ('COMPANY_BANK_ACCOUNT', 'TK ngân hàng DN'),
    ('REPRESENTATIVE', 'Người đại diện DN'),
    ('REPRESENTATIVE_TITLE', 'Chức vụ người đại diện'),
    ('REPRESENTATIVE_HONORIFIC', 'Danh xưng người đại diện'),
    ('EMPLOYEE_NAME', 'Họ tên NLĐ (modal)'),
    ('EMPLOYEE_CODE', 'Mã NV'),
    ('EMPLOYEE_BIRTH_DATE', 'Ngày sinh NLĐ'),
    ('EMPLOYEE_NATIONALITY', 'Quốc tịch NLĐ'),
    ('EMPLOYEE_POSITION', 'Chức danh / vị trí'),
    ('EMPLOYEE_ADDRESS', 'Địa chỉ NLĐ'),
    ('EMPLOYEE_ID_CARD', 'CCCD/CMND'),
    ('EMPLOYEE_PHONE', 'ĐT NLĐ'),
    ('EMPLOYEE_HONORIFIC', 'Danh xưng NLĐ'),
    ('DEPARTMENT_LABEL', 'Phòng ban'),
    ('START_DATE', 'Ngày bắt đầu'),
    ('END_DATE', 'Ngày kết thúc'),
    ('PROBATION_END_DATE', 'Hết thử việc'),
    ('CONTRACT_DURATION_MONTHS', 'Thời hạn HĐ (tháng)'),
    ('SIGN_DAY', 'Ngày ký'),
    ('SIGN_MONTH', 'Tháng ký'),
    ('SIGN_YEAR', 'Năm ký'),
    ('SIGN_PLACE', 'Nơi ký'),
    ('SALARY_TABLE', 'Bảng lương & phụ cấp (HTML — từ modal)'),
    ('JOB_DUTIES_EXTRA', 'Nhiệm vụ bổ sung từ ghi chú HĐ (HTML)'),
    ('INSURANCE_DISPLAY', 'Mức lương đóng BH'),
    ('SI_BASE_DISPLAY', 'Cộng lương + PC chịu BH'),
    ('WORK_DAYS_MONTH', 'Ngày công chuẩn/tháng'),
    ('WORK_HOURS_DAY', 'Giờ công/ngày'),
    ('WORK_WEEKDAYS', 'Ngày làm việc trong tuần'),
    ('WORK_SHIFT_TEXT', 'Ca làm việc'),
    ('OT_RATES_TEXT', 'Hệ số làm thêm giờ'),
    ('STANDARD_MONTH', 'Tháng chuẩn'),
    ('STANDARD_YEAR', 'Năm chuẩn'),
    ('PROBATION_SALARY_RATE', '% lương thử việc'),
)


class _PlaceholderContract:
    """Object giả cho export mẫu — mọi thuộc tính → [[TÊN]]."""

    def __getattr__(self, name: str) -> str:
        return f'[[{name.upper()}]]'


def _setting_key(contract_type: str) -> str:
    ctype = (contract_type or 'indefinite').strip()
    if ctype not in CONTRACT_TYPES:
        ctype = 'indefinite'
    return f'{SETTING_PREFIX}{ctype}'


def _row(r) -> dict:
    if not r:
        return {}
    return dict(r) if hasattr(r, 'keys') else {}


def validate_template(html_body: str) -> list[str]:
    errs: list[str] = []
    body = html_body or ''
    for marker in REQUIRED_MARKERS:
        if marker not in body:
            errs.append(f'Thiếu mã bắt buộc {marker}')
    return errs


def extract_placeholders(html_body: str) -> list[str]:
    return sorted(set(PLACEHOLDER_RE.findall(html_body or '')))


def get_custom_template_html(conn: sqlite3.Connection, contract_type: str) -> str | None:
    ensure_crm_schema(conn, commit=False)
    key = _setting_key(contract_type)
    try:
        row = conn.execute(
            'SELECT value FROM crm_settings WHERE key = ?',
            (key,),
        ).fetchone()
        if row:
            val = (_row(row).get('value') or '').strip()
            if val:
                return val
    except sqlite3.Error:
        pass
    return None


def set_custom_template_html(
    conn: sqlite3.Connection,
    contract_type: str,
    html_body: str,
) -> None:
    ensure_crm_schema(conn, commit=False)
    ctype = (contract_type or 'indefinite').strip()
    if ctype not in CONTRACT_TYPES:
        raise ValueError('Loại HĐ không hợp lệ')
    body = (html_body or '').strip()
    if not body:
        raise ValueError('Nội dung mẫu trống')
    errs = validate_template(body)
    if errs:
        raise ValueError('; '.join(errs))
    conn.execute(
        """
        INSERT INTO crm_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (_setting_key(ctype), body),
    )


def reset_custom_template(conn: sqlite3.Connection, contract_type: str) -> None:
    ensure_crm_schema(conn, commit=False)
    conn.execute(
        'DELETE FROM crm_settings WHERE key = ?',
        (_setting_key(contract_type),),
    )


def build_export_placeholder_context(contract_type: str) -> dict[str, Any]:
    """Ngữ cảnh Jinja để xuất mẫu HTML có [[PLACEHOLDER]]."""
    ctype = contract_type if contract_type in CONTRACT_TYPES else 'indefinite'
    ph = lambda k: f'[[{k}]]'  # noqa: E731
    return {
        'export_template_mode': True,
        'c': _PlaceholderContract(),
        'ctype': ctype,
        'type_label': ph('TYPE_LABEL'),
        'contract_no_display': ph('CONTRACT_NO'),
        'company_name': ph('COMPANY_NAME'),
        'company_address': ph('COMPANY_ADDRESS'),
        'company_tax_code': ph('COMPANY_TAX_CODE'),
        'company_phone': ph('COMPANY_PHONE'),
        'company_email': ph('COMPANY_EMAIL'),
        'company_bank_account': ph('COMPANY_BANK_ACCOUNT'),
        'representative': ph('REPRESENTATIVE'),
        'representative_title': ph('REPRESENTATIVE_TITLE'),
        'representative_honorific': ph('REPRESENTATIVE_HONORIFIC'),
        'employee_honorific': ph('EMPLOYEE_HONORIFIC'),
        'employee_birth_date_vi': ph('EMPLOYEE_BIRTH_DATE'),
        'employee_birth_year': ph('EMPLOYEE_BIRTH_DATE'),
        'employee_nationality': ph('EMPLOYEE_NATIONALITY'),
        'department_label': ph('DEPARTMENT_LABEL'),
        'start_date_vi': ph('START_DATE'),
        'end_date_vi': ph('END_DATE'),
        'probation_end_vi': ph('PROBATION_END_DATE'),
        'contract_duration_months': ph('CONTRACT_DURATION_MONTHS'),
        'sign_day': ph('SIGN_DAY'),
        'sign_month': ph('SIGN_MONTH'),
        'sign_year': ph('SIGN_YEAR'),
        'sign_place': ph('SIGN_PLACE'),
        'salary_rows': [],
        'si_base_display': ph('SI_BASE_DISPLAY'),
        'insurance_display': ph('INSURANCE_DISPLAY'),
        'work_days_month_display': ph('WORK_DAYS_MONTH'),
        'work_hours_day_display': ph('WORK_HOURS_DAY'),
        'work_weekdays_display': ph('WORK_WEEKDAYS'),
        'work_shift_text': ph('WORK_SHIFT_TEXT'),
        'work_start_time': ph('WORK_START_TIME'),
        'work_lunch_start': ph('WORK_LUNCH_START'),
        'work_lunch_end': ph('WORK_LUNCH_END'),
        'work_end_time': ph('WORK_END_TIME'),
        'ot_rates_text': ph('OT_RATES_TEXT'),
        'standard_month': ph('STANDARD_MONTH'),
        'standard_year': ph('STANDARD_YEAR'),
        'probation_salary_rate': ph('PROBATION_SALARY_RATE'),
        'job_duties': [],
        'template_name': CONTRACT_PRINT_TEMPLATES.get(ctype, CONTRACT_PRINT_TEMPLATES['indefinite']),
    }


def render_system_default_template(app, contract_type: str) -> str:
    """Mẫu mặc định hệ thống (Jinja + placeholder) — dùng xuất / khôi phục tham chiếu."""
    ctype = contract_type if contract_type in CONTRACT_TYPES else 'indefinite'
    ctx = build_export_placeholder_context(ctype)
    tpl_name = ctx['template_name']
    with app.app_context():
        html_out = app.jinja_env.get_template(tpl_name).render(**ctx)
    hint = (
        '<div class="no-print hint-export" style="background:#fff8e6;border:1px dashed #c9a227;'
        'padding:.5rem .75rem;margin-bottom:12px;font-size:11pt">'
        '<b>Hướng dẫn:</b> Giữ các mã <code>[[…]]</code> để hệ thống điền từ form HĐLĐ. '
        'Có thể sửa câu chữ điều khoản pháp lý; mẫu chỉ lưu riêng doanh nghiệp (tenant) này.'
        '</div>'
    )
    return html_out.replace(
        '<div class="no-print" style="margin-bottom:12px">',
        hint + '<div class="no-print" style="margin-bottom:12px">',
        1,
    )


def build_salary_table_html(ctx: dict[str, Any]) -> str:
    rows = ctx.get('salary_rows') or []
    si = ctx.get('si_base_display') or ''
    ins = ctx.get('insurance_display') or ''
    wdm = ctx.get('work_days_month_display') or ''
    whd = ctx.get('work_hours_day_display') or ''
    ot = ctx.get('ot_rates_text') or ''
    body = []
    for row in rows:
        body.append(
            '<tr>'
            f'<td>{html.escape(str(row.get("label") or ""))}</td>'
            f'<td>{html.escape(str(row.get("nature_display") or row.get("nature") or ""))}</td>'
            f'<td class="num">{html.escape(str(row.get("amount_display") or ""))}</td>'
            '</tr>'
        )
    return (
        '<table class="pay"><thead><tr>'
        '<th>Khoản mục</th><th>Tính chất</th><th class="num">Số tiền (VNĐ/tháng)</th>'
        '</tr></thead><tbody>'
        + ''.join(body)
        + f'<tr class="summary"><td><strong>Cộng lương + PC chịu BH</strong></td>'
        f'<td>Căn cứ đóng BH (trước trần)</td><td class="num"><strong>{html.escape(str(si))}</strong></td></tr>'
        f'<tr class="summary"><td><strong>Mức lương đóng BHXH đăng ký</strong></td>'
        f'<td>Ghi nhận trên HĐ</td><td class="num"><strong>{html.escape(str(ins))}</strong></td></tr>'
        '</tbody></table>'
        f'<p class="note">Ngày công chuẩn: <strong>{html.escape(str(wdm))}</strong> ngày/tháng'
        f' · Giờ công/ngày: <strong>{html.escape(str(whd))}</strong> giờ'
        f' · Làm thêm giờ: {html.escape(str(ot))}.</p>'
    )


def build_job_duties_extra_html(duties: list[str] | None) -> str:
    if not duties:
        return ''
    return ''.join(
        f'<p class="bullet">+ {html.escape(str(d))}</p>' for d in duties if str(d).strip()
    )


def build_fill_map(ctx: dict[str, Any]) -> dict[str, str]:
    c = ctx.get('c') or {}
    if not isinstance(c, dict):
        c = dict(c) if hasattr(c, 'keys') else {}

    def _s(v: Any) -> str:
        if v is None:
            return ''
        return str(v)

    return {
        'CONTRACT_NO': _s(ctx.get('contract_no_display')),
        'TYPE_LABEL': _s(ctx.get('type_label')),
        'COMPANY_NAME': _s(ctx.get('company_name')),
        'COMPANY_ADDRESS': _s(ctx.get('company_address')),
        'COMPANY_TAX_CODE': _s(ctx.get('company_tax_code')),
        'COMPANY_PHONE': _s(ctx.get('company_phone')),
        'COMPANY_EMAIL': _s(ctx.get('company_email')),
        'COMPANY_BANK_ACCOUNT': _s(ctx.get('company_bank_account')),
        'REPRESENTATIVE': _s(ctx.get('representative')),
        'REPRESENTATIVE_TITLE': _s(ctx.get('representative_title')),
        'REPRESENTATIVE_HONORIFIC': _s(ctx.get('representative_honorific')),
        'EMPLOYEE_NAME': _s(c.get('employee_name')),
        'EMPLOYEE_CODE': _s(c.get('employee_code')),
        'EMPLOYEE_BIRTH_DATE': _s(ctx.get('employee_birth_date_vi') or ctx.get('employee_birth_year')),
        'EMPLOYEE_NATIONALITY': _s(ctx.get('employee_nationality')),
        'EMPLOYEE_POSITION': _s(c.get('position')),
        'EMPLOYEE_ADDRESS': _s(c.get('employee_address')),
        'EMPLOYEE_ID_CARD': _s(c.get('employee_id_card')),
        'EMPLOYEE_PHONE': _s(c.get('employee_phone')),
        'EMPLOYEE_HONORIFIC': _s(ctx.get('employee_honorific')),
        'DEPARTMENT_LABEL': _s(ctx.get('department_label') or c.get('department')),
        'START_DATE': _s(ctx.get('start_date_vi')),
        'END_DATE': _s(ctx.get('end_date_vi')),
        'PROBATION_END_DATE': _s(ctx.get('probation_end_vi')),
        'CONTRACT_DURATION_MONTHS': _s(ctx.get('contract_duration_months')),
        'SIGN_DAY': _s(ctx.get('sign_day')),
        'SIGN_MONTH': _s(ctx.get('sign_month')),
        'SIGN_YEAR': _s(ctx.get('sign_year')),
        'SIGN_PLACE': _s(ctx.get('sign_place')),
        'INSURANCE_DISPLAY': _s(ctx.get('insurance_display')),
        'SI_BASE_DISPLAY': _s(ctx.get('si_base_display')),
        'WORK_DAYS_MONTH': _s(ctx.get('work_days_month_display')),
        'WORK_HOURS_DAY': _s(ctx.get('work_hours_day_display')),
        'WORK_WEEKDAYS': _s(ctx.get('work_weekdays_display')),
        'WORK_SHIFT_TEXT': _s(ctx.get('work_shift_text')),
        'WORK_START_TIME': _s(ctx.get('work_start_time')),
        'WORK_LUNCH_START': _s(ctx.get('work_lunch_start')),
        'WORK_LUNCH_END': _s(ctx.get('work_lunch_end')),
        'WORK_END_TIME': _s(ctx.get('work_end_time')),
        'OT_RATES_TEXT': _s(ctx.get('ot_rates_text')),
        'STANDARD_MONTH': _s(ctx.get('standard_month')),
        'STANDARD_YEAR': _s(ctx.get('standard_year')),
        'PROBATION_SALARY_RATE': _s(ctx.get('probation_salary_rate')),
        'SALARY_TABLE': build_salary_table_html(ctx),
        'JOB_DUTIES_EXTRA': build_job_duties_extra_html(ctx.get('job_duties')),
    }


def fill_template(html_body: str, fill_map: dict[str, str]) -> str:
    out = html_body or ''
    out = out.replace('[[SALARY_TABLE]]', fill_map.get('SALARY_TABLE', ''))
    out = out.replace('[[JOB_DUTIES_EXTRA]]', fill_map.get('JOB_DUTIES_EXTRA', ''))

    def _repl(m: re.Match) -> str:
        key = m.group(1)
        if key in ('SALARY_TABLE', 'JOB_DUTIES_EXTRA'):
            return m.group(0)
        val = fill_map.get(key)
        if val is None:
            return m.group(0)
        return html.escape(val)

    return PLACEHOLDER_RE.sub(_repl, out)


def render_contract_html(
    conn: sqlite3.Connection,
    print_ctx: dict[str, Any],
    app,
) -> str:
    """In HĐ: mẫu tenant (nếu có) hoặc Jinja hệ thống."""
    ctype = print_ctx.get('ctype') or 'indefinite'
    custom = get_custom_template_html(conn, ctype)
    if custom:
        return fill_template(custom, build_fill_map(print_ctx))
    tpl_name = print_ctx.get('template_name') or CONTRACT_PRINT_TEMPLATES.get(ctype, 'indefinite')
    with app.app_context():
        return app.jinja_env.get_template(tpl_name).render(**print_ctx)


def placeholders_guide() -> list[dict[str, str]]:
    return [{'code': f'[[{k}]]', 'label': lab} for k, lab in KNOWN_PLACEHOLDERS]


def template_meta(conn: sqlite3.Connection, contract_type: str) -> dict[str, Any]:
    ctype = contract_type if contract_type in CONTRACT_TYPES else 'indefinite'
    custom = get_custom_template_html(conn, ctype)
    return {
        'contract_type': ctype,
        'type_label': CONTRACT_TYPES.get(ctype, ctype),
        'is_custom': bool(custom),
        'tenant_scoped': True,
        'storage': 'crm_settings',
        'setting_key': _setting_key(ctype),
    }
