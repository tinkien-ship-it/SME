"""Cột phụ cấp / thưởng mặc định — đồng bộ mẫu 05-LĐTL in."""


def _ensure_columns(conn, table, columns):
    existing = {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    for col, ddl in columns.items():
        if col not in existing:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {ddl}')


def ensure_employee_allowance_columns(conn, commit=False):
    _ensure_columns(conn, 'employees', {
        'allowance_fund': 'REAL DEFAULT 0',
        'allowance_other': 'REAL DEFAULT 0',
        'default_bonus': 'REAL DEFAULT 0',
    })
    if commit:
        conn.commit()


def ensure_salary_detail_allowance_columns(conn, commit=False):
    _ensure_columns(conn, 'salary_detail', {
        'allowance_fund': 'REAL DEFAULT 0',
        'allowance_other': 'REAL DEFAULT 0',
    })
    if commit:
        conn.commit()


def ensure_payroll_schema(conn, commit=False):
    ensure_employee_allowance_columns(conn, commit=False)
    ensure_salary_detail_allowance_columns(conn, commit=False)
    if commit:
        conn.commit()
