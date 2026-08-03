import sqlite3, tempfile, os
from Services.sme.coa_service import ensure_sme_coa_ready, create_child_account, get_account, suggest_next_child_code
from Services.sme.bank_accounts import get_default_bank_account, preview_bank_split

fd, path = tempfile.mkstemp(suffix='.db')
os.close(fd)
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
ensure_sme_coa_ready(conn, commit=True)
conn.execute('''CREATE TABLE IF NOT EXISTS business_info (
  id INTEGER PRIMARY KEY, bank_name TEXT, bank_account TEXT, account_holder TEXT, business_name TEXT)''')
conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
conn.execute('''CREATE TABLE IF NOT EXISTS sme_journal_lines (
  id INTEGER PRIMARY KEY, entry_id INTEGER, account_code TEXT, debit REAL, credit REAL)''')
conn.execute('''CREATE TABLE IF NOT EXISTS sme_vouchers (
  id INTEGER PRIMARY KEY, debit_account TEXT, credit_account TEXT)''')
conn.execute("INSERT INTO business_info(bank_name,bank_account,account_holder,business_name) VALUES ('VCB','001','A','DN')")
conn.execute("INSERT INTO sme_journal_lines(entry_id,account_code,debit,credit) VALUES (1,'1121',1000,0)")
conn.execute("INSERT INTO sme_vouchers(debit_account,credit_account) VALUES ('1121','131')")
conn.commit()

prev = preview_bank_split(conn, '1121')
assert prev and prev['will_auto_split'] and prev['default_code']=='112101'
assert suggest_next_child_code(conn,'1121')=='112102'

created = create_child_account(conn, parent_code='1121', name='TCB STK moi', code=None)
assert created['code']=='112102', created
assert get_account(conn,'112101',commit=False)
assert get_account(conn,'1121',commit=False)['is_postable']==0
jl = conn.execute("SELECT account_code FROM sme_journal_lines").fetchone()[0]
assert jl=='112101', jl
vv = conn.execute("SELECT debit_account FROM sme_vouchers").fetchone()[0]
assert vv=='112101', vv
assert get_default_bank_account(conn)=='112101'
assert created.get('automation_message')
print('PASS', created['code'], created.get('automation_message'))
conn.close()
os.remove(path)
