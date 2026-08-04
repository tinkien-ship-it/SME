import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

from Services.sme.coa_service import ensure_sme_coa_ready, get_account, deactivate_account
from Services.sme.account_roles import get_role, seed_account_roles
from Services.sme.journal_engine import resolve_postable_account
from Services.sme.cogs_accounts import cogs_accounts_for_line

conn = sqlite3.connect(':memory:')
meta = ensure_sme_coa_ready(conn)
a6321 = get_account(conn, '6321')
print('seed', meta['seed_version'], '6321_postable', a6321['is_postable'], '63211', get_account(conn, '63211'))
print('role', get_role(conn, 'cogs.goods.export'))
deb, cred, _ = cogs_accounts_for_line(channel='export')
print('export resolve', deb, '->', resolve_postable_account(conn, deb), cred, '->', resolve_postable_account(conn, cred))

# Old DB path: L3 custom then deactivated; old role default 63211
conn2 = sqlite3.connect(':memory:')
ensure_sme_coa_ready(conn2)
conn2.execute(
    """
    INSERT INTO sme_chart_of_accounts (
        code, name, parent_code, level, account_class, normal_balance,
        is_postable, is_custom, is_active, legal_source
    ) VALUES ('63211', 'old', '6321', 3, 'expense', 'debit', 1, 1, 1, 'custom')
    """
)
conn2.execute("UPDATE sme_chart_of_accounts SET is_postable = 0 WHERE code = '6321'")
conn2.execute(
    "UPDATE sme_account_roles SET default_account = '63211', root_hint = '63211' "
    "WHERE role_key = 'cogs.goods.export'"
)
conn2.commit()
deactivate_account(conn2, '63211')
conn2.execute("UPDATE sme_account_roles_meta SET value = 'old' WHERE key = 'roles_version'")
conn2.commit()
seed_account_roles(conn2, force=True)
print('migrated role', get_role(conn2, 'cogs.goods.export'))
print('6321 postable', get_account(conn2, '6321')['is_postable'])
print('resolve after migrate', resolve_postable_account(conn2, 'cogs.goods.export'))

# Migrate real demo tenant roles/coa version
try:
    demo = sqlite3.connect(r'C:\SME\tenants\sme_demo.db')
    m = ensure_sme_coa_ready(demo)
    print('demo seed', m)
    print('demo role export', get_role(demo, 'cogs.goods.export'))
    print('demo resolve', resolve_postable_account(demo, 'cogs.goods.export'))
    a = get_account(demo, '6321')
    print('demo 6321', a['is_active'] if a else None, a['is_postable'] if a else None)
    demo.close()
except Exception as e:
    print('demo migrate skip', e)

print('OK')
