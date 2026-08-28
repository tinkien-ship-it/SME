# -*- coding: utf-8 -*-
"""Smoke test in HĐLĐ — chạy: python scripts/test_contract_print.py [contract_id]"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jinja2 import Environment, FileSystemLoader, select_autoescape

from Services.hrm.contract_templates import build_contract_print_context
from Services.hrm.work_calendar import get_work_calendar_config


def main() -> int:
    cid = int(sys.argv[1] if len(sys.argv) > 1 else 1)
    db = ROOT / 'tenants' / 'sme_demo.db'
    conn = sqlite3.connect(f'file:{db.as_posix()}?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        cfg = get_work_calendar_config(conn)
        ctx = build_contract_print_context(conn, cid)
    finally:
        conn.close()

    env = Environment(
        loader=FileSystemLoader(str(ROOT / 'templates')),
        autoescape=select_autoescape(['html']),
    )
    html = env.get_template(ctx['template_name']).render(**ctx)
    out = ROOT / 'logs' / 'contract_print_test.html'
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding='utf-8')

    print('DB:', db.name)
    print('Contract:', cid, '| template:', ctx['template_name'])
    print('Work times:', cfg['work_start'], '-', cfg['work_end'])
    print('Shift:', ctx['work_shift_text'])
    print('Sign date:', ctx['sign_day'], ctx['sign_month'], ctx['sign_year'])
    print('Salary rows:')
    for row in ctx['salary_rows']:
        print(f"  - {row['label']}: {row['amount_display']}")
    print('HTML:', out, f'({len(html)} bytes)')

    checks = {
        'Điều 2 bảng lương': 'Điều 2. Tiền lương' in html,
        '7 dòng lương/PC': html.count('<td class="num">') >= 9,
        'Lương chính 5tr': '5.000.000' in html,
        'Ca 08:00': '08:00' in html,
        'Không còn 07:30': '07:30' not in html,
        'Ngày ký = start_date': f"ngày <strong>{ctx['sign_day']}</strong>" in html,
        'Điều 3 ca chuẩn': 'Buổi sáng: <strong>08:00–12:00</strong>' in html,
        'Điều 6 đơn phương (KXĐTH)': 'Đơn phương chấm dứt' in html or ctx.get('ctype') == 'definite',
        'Căn cứ BLĐ 2019': 'Bộ luật Lao động ngày 20 tháng 11 năm 2019' in html,
        'NSLĐ từ Settings': bool(ctx.get('company_name')),
    }
    print('\nChecks:')
    ok = True
    for label, passed in checks.items():
        mark = 'OK' if passed else 'FAIL'
        print(f'  [{mark}] {label}')
        ok = ok and passed
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
