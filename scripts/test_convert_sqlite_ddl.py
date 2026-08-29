"""Unit: convert_sqlite_ddl — datetime/AFTER/generated."""
from db.sql_compat import convert_sqlite_ddl


def test_datetime_now_not_timestamp_call():
    ddl = """
    CREATE TABLE crm_email_logs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at TEXT DEFAULT (datetime('now','localtime'))
    )
    """
    out = convert_sqlite_ddl(ddl)
    assert "TIMESTAMP('now')" not in out, out
    assert 'CURRENT_TIMESTAMP' in out, out
    assert 'SERIAL PRIMARY KEY' in out, out


def test_after_stripped():
    ddl = "CREATE TABLE t (import_id INTEGER, sale_id INTEGER AFTER import_id)"
    out = convert_sqlite_ddl(ddl)
    assert 'AFTER' not in out.upper(), out
    assert 'sale_id' in out


def test_generated_stored():
    ddl = """
    CREATE TABLE Operating_Cost (
      id INTEGER PRIMARY KEY,
      a REAL DEFAULT 0,
      b REAL DEFAULT 0,
      total_cost REAL GENERATED ALWAYS AS (a + b) VIRTUAL
    )
    """
    out = convert_sqlite_ddl(ddl)
    assert 'VIRTUAL' not in out.upper() or 'STORED' in out.upper()
    assert 'GENERATED ALWAYS AS' in out.upper()
    assert 'STORED' in out.upper()


def test_sqlite_as_shorthand():
    ddl = "CREATE TABLE cong_no (unpaid REAL, paid REAL, remaining_amount REAL AS (unpaid - paid) VIRTUAL)"
    out = convert_sqlite_ddl(ddl)
    assert "AS (unpaid - paid) VIRTUAL" not in out
    assert 'GENERATED ALWAYS AS' in out.upper()


if __name__ == '__main__':
    test_datetime_now_not_timestamp_call()
    test_after_stripped()
    test_generated_stored()
    test_sqlite_as_shorthand()
    print('OK convert_sqlite_ddl')
