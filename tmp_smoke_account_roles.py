import sqlite3
from Services.sme.coa_service import ensure_sme_coa_ready, create_child_account, deactivate_account
from Services.sme.account_roles import (
    resolve_posting_account, list_roles, get_role, set_default_posting_flag,
)
from Services.sme.cogs_accounts import cogs_accounts_for_line, cogs_spoilage_account
from Services.sme.journal_engine import resolve_postable_account

conn = sqlite3.connect(':memory:')
meta = ensure_sme_coa_ready(conn)
print('coa', meta)
roles = list_roles(conn)
print('roles', len(roles))

print('cogs domestic', resolve_postable_account(conn, 'cogs.goods.domestic'))
print('spoilage', resolve_postable_account(conn, cogs_spoilage_account()))
print('parent 131', resolve_postable_account(conn, '131'))

deb, cred, lab = cogs_accounts_for_line(channel='domestic')
print('map', deb, cred, '->', resolve_postable_account(conn, deb), resolve_postable_account(conn, cred))

child = create_child_account(conn, parent_code='63211', name='GV HH ND - CH1', set_as_default=True)
print('child', child['code'], 'flag', child.get('is_default_posting'))
print('role after', get_role(conn, 'cogs.goods.domestic')['default_account'])
print('resolve role', resolve_postable_account(conn, 'cogs.goods.domestic'))
print('resolve parent 63211', resolve_postable_account(conn, '63211'))

c2 = create_child_account(conn, parent_code='63211', name='GV HH ND - CH2')
print('c2', c2['code'], 'still', get_role(conn, 'cogs.goods.domestic')['default_account'])

set_default_posting_flag(conn, c2['code'], is_default=True)
print('switched', get_role(conn, 'cogs.goods.domestic')['default_account'])

deactivate_account(conn, c2['code'])
print('after deact', get_role(conn, 'cogs.goods.domestic')['default_account'], resolve_postable_account(conn, 'cogs.goods.domestic'))
print('OK')
