# -*- coding: utf-8 -*-
"""HĐLĐ: mẫu in theo tenant — tenant A không ảnh hưởng tenant B."""
import sqlite3

from Services.crm_schema import ensure_crm_schema
from Services.hrm import contract_template_store as ld_tpl

MARK = '<!-- TENANT-LD-CUSTOM -->'


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    ensure_crm_schema(conn)
    return conn


def main() -> None:
    a = _db()
    b = _db()
    ld_tpl.set_custom_template_html(a, 'indefinite', f'[[CONTRACT_NO]] [[SALARY_TABLE]] {MARK}-A')
    ld_tpl.set_custom_template_html(b, 'definite', f'[[CONTRACT_NO]] [[SALARY_TABLE]] {MARK}-B')

    assert MARK + '-A' in (ld_tpl.get_custom_template_html(a, 'indefinite') or '')
    assert MARK + '-B' in (ld_tpl.get_custom_template_html(b, 'definite') or '')
    assert ld_tpl.get_custom_template_html(a, 'definite') is None
    assert ld_tpl.get_custom_template_html(b, 'indefinite') is None

    ld_tpl.reset_custom_template(a, 'indefinite')
    assert ld_tpl.get_custom_template_html(a, 'indefinite') is None
    assert MARK + '-B' in (ld_tpl.get_custom_template_html(b, 'definite') or '')

    fill = ld_tpl.fill_template(
        '[[CONTRACT_NO]] [[SALARY_TABLE]]',
        {'CONTRACT_NO': 'HĐLĐ-000001', 'SALARY_TABLE': '<table></table>'},
    )
    assert 'HĐLĐ-000001' in fill and '<table>' in fill

    print('hrm ld template tenant isolation ok')


if __name__ == '__main__':
    main()
