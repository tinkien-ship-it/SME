# -*- coding: utf-8 -*-
"""Mẫu HĐ CRM: mỗi tenant DB riêng — chỉnh tenant A không đổi tenant B."""
import sqlite3

from Services import crm_contract_template as tpl
from Services.crm_schema import ensure_crm_schema

MARK_A = '<!-- TENANT-A-CUSTOM -->'
MARK_B = '<!-- TENANT-B-CUSTOM -->'


def _tenant_db() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    ensure_crm_schema(conn)
    return conn


def main() -> None:
    conn_a = _tenant_db()
    conn_b = _tenant_db()

    tpl.set_template_html(conn_a, tpl.DEFAULT_TEMPLATE_HTML + MARK_A)
    tpl.set_template_html(conn_b, tpl.DEFAULT_TEMPLATE_HTML + MARK_B)

    html_a = tpl.get_template_html(conn_a)
    html_b = tpl.get_template_html(conn_b)

    assert MARK_A in html_a and MARK_A not in html_b, 'Tenant A leak'
    assert MARK_B in html_b and MARK_B not in html_a, 'Tenant B leak'

    tpl.reset_template(conn_a)
    assert MARK_A not in tpl.get_template_html(conn_a)
    assert MARK_B in tpl.get_template_html(conn_b), 'Reset A must not touch B'

    print('tenant isolation ok')


if __name__ == '__main__':
    main()
