from db_utils import sqlite_commit
"""Cột phụ cấp / thưởng mặc định — đồng bộ mẫu 05-LĐTL in."""

# Bộ phận → TK chi phí (PA A hạch toán lương + BH DN)
DEPARTMENT_OPTIONS = (
    {
        'code': 'ADMIN',
        'label': 'Văn phòng / Quản lý',
        'expense_account': '642',
        'hint': 'Khối văn phòng, giám đốc, kế toán',
    },
    {
        'code': 'SALES',
        'label': 'Bán hàng',
        'expense_account': '641',
        'hint': 'Nhân viên bán hàng, shipper',
    },
    {
        'code': 'WORKSHOP',
        'label': 'Quản lý phân xưởng',
        'expense_account': '627',
        'hint': 'Khối quản lý phân xưởng / SX chung',
    },
    {
        'code': 'PRODUCTION',
        'label': 'Sản xuất trực tiếp',
        'expense_account': '622',
        'hint': 'Công nhân trực tiếp sản xuất',
    },
)

_DEPARTMENT_BY_CODE = {d['code']: d for d in DEPARTMENT_OPTIONS}
_DEPARTMENT_BY_ACCOUNT = {d['expense_account']: d for d in DEPARTMENT_OPTIONS}

_SALARY_DETAIL_TABLE_NAMES = ('salary_detail', 'Salary_Detail')


def resolve_salary_detail_table(conn) -> str:
    """Tên bảng lương thực tế trong DB (legacy: Salary_Detail)."""
    from db.schema_helpers import table_exists

    for name in _SALARY_DETAIL_TABLE_NAMES:
        if table_exists(conn, name):
            return name
    return 'salary_detail'


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
        'department': "TEXT DEFAULT 'ADMIN'",
        'allowance_position': 'REAL DEFAULT 0',
        'allowance_responsibility': 'REAL DEFAULT 0',
        'allowance_seniority': 'REAL DEFAULT 0',
        'allowance_lunch': 'REAL DEFAULT 0',
        'allowance_uniform': 'REAL DEFAULT 0',
        'allowance_phone': 'REAL DEFAULT 0',
        'employee_code': 'TEXT',
        'birth_date': 'TEXT',
    })
    if commit:
        sqlite_commit(conn, label='employee_payroll_helpers')


def ensure_salary_detail_allowance_columns(conn, commit=False):
    sd_table = resolve_salary_detail_table(conn)
    _ensure_columns(conn, sd_table, {
        'allowance_fund': 'REAL DEFAULT 0',
        'allowance_other': 'REAL DEFAULT 0',
        'department': 'TEXT',
        'expense_account': 'TEXT',
        'contract_salary': 'REAL DEFAULT 0',
        'allowance_position': 'REAL DEFAULT 0',
        'allowance_responsibility': 'REAL DEFAULT 0',
        'allowance_seniority': 'REAL DEFAULT 0',
        'allowance_lunch': 'REAL DEFAULT 0',
        'allowance_uniform': 'REAL DEFAULT 0',
        'allowance_phone': 'REAL DEFAULT 0',
        'standard_days': 'REAL DEFAULT 0',
        'ot_hours': 'REAL DEFAULT 0',
        'lunch_amount': 'REAL DEFAULT 0',
        'uniform_amount': 'REAL DEFAULT 0',
        'phone_amount': 'REAL DEFAULT 0',
        'ot_amount': 'REAL DEFAULT 0',
        'insurance_salary_base': 'REAL DEFAULT 0',
        'taxable_income': 'REAL DEFAULT 0',
        'family_relief': 'REAL DEFAULT 0',
    })
    if commit:
        sqlite_commit(conn, label='employee_payroll_helpers')


def ensure_payroll_schema(conn, commit=False):
    ensure_employee_allowance_columns(conn, commit=False)
    ensure_salary_detail_allowance_columns(conn, commit=False)
    try:
        from Services.hrm.legal_payroll import ensure_legal_payroll_columns
        ensure_legal_payroll_columns(conn, commit=False)
    except Exception:
        pass
    if commit:
        sqlite_commit(conn, label='employee_payroll_helpers')


def normalize_department(raw) -> str:
    """Chuẩn hóa mã bộ phận; mặc định ADMIN (642)."""
    code = str(raw or '').strip().upper()
    if code in _DEPARTMENT_BY_CODE:
        return code
    # Cho phép truyền thẳng mã TK chi phí
    if code in _DEPARTMENT_BY_ACCOUNT:
        return _DEPARTMENT_BY_ACCOUNT[code]['code']
    # Alias tiếng Việt / cũ
    aliases = {
        'VAN_PHONG': 'ADMIN',
        'QLDN': 'ADMIN',
        'ADMINISTRATION': 'ADMIN',
        'BAN_HANG': 'SALES',
        'SALE': 'SALES',
        'KD': 'SALES',
        'PHAN_XUONG': 'WORKSHOP',
        'SX_CHUNG': 'WORKSHOP',
        'SAN_XUAT': 'PRODUCTION',
        'CONG_NHAN': 'PRODUCTION',
        'SX': 'PRODUCTION',
    }
    if code in aliases:
        return aliases[code]
    return 'ADMIN'


def expense_account_for_department(raw) -> str:
    dept = normalize_department(raw)
    return _DEPARTMENT_BY_CODE[dept]['expense_account']


def department_label(raw) -> str:
    dept = normalize_department(raw)
    return _DEPARTMENT_BY_CODE[dept]['label']


def list_department_options() -> list[dict]:
    return [dict(d) for d in DEPARTMENT_OPTIONS]
