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
    })
    if commit:
        conn.commit()


def ensure_salary_detail_allowance_columns(conn, commit=False):
    _ensure_columns(conn, 'salary_detail', {
        'allowance_fund': 'REAL DEFAULT 0',
        'allowance_other': 'REAL DEFAULT 0',
        'department': 'TEXT',
        'expense_account': 'TEXT',
    })
    if commit:
        conn.commit()


def ensure_payroll_schema(conn, commit=False):
    ensure_employee_allowance_columns(conn, commit=False)
    ensure_salary_detail_allowance_columns(conn, commit=False)
    if commit:
        conn.commit()


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
