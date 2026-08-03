"""Lương SME (TT99/TT58) — chốt bảng lương + hạch toán sổ kép, không dùng phieu_chi HKD."""
from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.employee_payroll_helpers import ensure_payroll_schema
from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    post_journal_entry,
    reverse_journal_entry,
)
from Services.sme.vouchers import create_payment

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_payroll_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    try:
        ensure_payroll_schema(conn, commit=False)
    except sqlite3.OperationalError:
        # DB test / bootstrap sớm chưa có employees — vẫn tạo sme_payroll_runs
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_payroll_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            posting_date TEXT NOT NULL,
            expense_account TEXT NOT NULL DEFAULT '642',
            total_income REAL NOT NULL DEFAULT 0,
            total_deduct REAL NOT NULL DEFAULT 0,
            total_net REAL NOT NULL DEFAULT 0,
            employer_insurance REAL NOT NULL DEFAULT 0,
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'accrued',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            branch_code TEXT
        )
        """
    )

    cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_payroll_runs)').fetchall()}
    if 'branch_code' not in cols:
        try:
            conn.execute('ALTER TABLE sme_payroll_runs ADD COLUMN branch_code TEXT')
        except Exception:
            pass
    if 'allocation_journal_id' not in cols:
        try:
            conn.execute('ALTER TABLE sme_payroll_runs ADD COLUMN allocation_journal_id INTEGER')
        except Exception:
            pass

    # Bỏ UNIQUE(month, year) cũ — mỗi CN một run/kỳ
    try:
        create_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sme_payroll_runs'"
        ).fetchone()
        raw_sql = (create_sql[0] if create_sql else '') or ''
        if 'UNIQUE(month, year)' in raw_sql.replace(' ', ''):
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sme_payroll_runs__mb (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    month INTEGER NOT NULL,
                    year INTEGER NOT NULL,
                    posting_date TEXT NOT NULL,
                    expense_account TEXT NOT NULL DEFAULT '642',
                    total_income REAL NOT NULL DEFAULT 0,
                    total_deduct REAL NOT NULL DEFAULT 0,
                    total_net REAL NOT NULL DEFAULT 0,
                    employer_insurance REAL NOT NULL DEFAULT 0,
                    journal_entry_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'accrued',
                    created_by TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    branch_code TEXT,
                    allocation_journal_id INTEGER
                )
                """
            )
            src_cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_payroll_runs)').fetchall()}
            common = [
                c for c in (
                    'id', 'month', 'year', 'posting_date', 'expense_account',
                    'total_income', 'total_deduct', 'total_net', 'employer_insurance',
                    'journal_entry_id', 'status', 'created_by', 'created_at',
                    'branch_code', 'allocation_journal_id',
                ) if c in src_cols
            ]
            if common:
                cols_sql = ', '.join(common)
                conn.execute(
                    f'INSERT OR IGNORE INTO sme_payroll_runs__mb ({cols_sql}) '
                    f'SELECT {cols_sql} FROM sme_payroll_runs'
                )
            conn.execute('DROP TABLE sme_payroll_runs')
            conn.execute('ALTER TABLE sme_payroll_runs__mb RENAME TO sme_payroll_runs')
    except Exception:
        pass

    # salary_detail theo chi nhánh (tránh chốt CN A xóa lưới CN B)
    try:
        sd_cols = {r[1] for r in conn.execute('PRAGMA table_info(salary_detail)').fetchall()}
        if sd_cols and 'branch_code' not in sd_cols:
            conn.execute('ALTER TABLE salary_detail ADD COLUMN branch_code TEXT')
    except Exception:
        pass

    if commit:
        conn.commit()


def _working_days_exclude_sunday(month: int, year: int) -> int:
    num_days = calendar.monthrange(year, month)[1]
    return sum(
        1 for d in range(1, num_days + 1) if date(year, month, d).weekday() != 6
    )


DEFAULT_SELF_DEDUCTION = 11_000_000
DEFAULT_DEPENDENT_DEDUCTION = 4_400_000


def get_salary_insurance_config(conn: sqlite3.Connection) -> dict[str, Any]:
    """Cấu hình vùng lương + tỷ lệ BH từ business_info (cùng nguồn HKD)."""
    from Services.insurance_debt_helpers import _load_rates

    info_row = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
    info = dict(info_row) if info_row else {}
    rates = _load_rates(conn)
    regions: list[dict[str, Any]] = []
    try:
        regions = [dict(r) for r in conn.execute('SELECT * FROM salary_regions').fetchall()]
    except sqlite3.Error:
        regions = []
    return {
        'salary_region': info.get('salary_region') or '',
        'base_salary_insurance': float(info.get('base_salary_insurance') or rates.get('base_insurance') or 0),
        'rate_bhxh': float(info.get('rate_bhxh') or 8),
        'rate_bhyt': float(info.get('rate_bhyt') or 1.5),
        'rate_bhtn': float(info.get('rate_bhtn') or 1),
        'rate_bhxh_chu': float(info.get('rate_bhxh_chu') or 17.5),
        'rate_bhyt_chu': float(info.get('rate_bhyt_chu') or 3),
        'rate_bhtn_chu': float(info.get('rate_bhtn_chu') or 1),
        'representative_name': info.get('representative_name') or '',
        'regions': regions,
        'rates_frac': {
            'nld_bhxh': rates['nld_bhxh'],
            'nld_bhyt': rates['nld_bhyt'],
            'nld_bhtn': rates['nld_bhtn'],
            'chu_bhxh': rates['chu_bhxh'],
            'chu_bhyt': rates['chu_bhyt'],
            'chu_bhtn': rates['chu_bhtn'],
        },
    }


def ensure_salary_insurance_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute('PRAGMA table_info(business_info)').fetchall()}
    for col, ddl in (
        ('salary_region', 'TEXT'),
        ('base_salary_insurance', 'REAL'),
        ('rate_bhxh', 'REAL DEFAULT 8'),
        ('rate_bhyt', 'REAL DEFAULT 1.5'),
        ('rate_bhtn', 'REAL DEFAULT 1'),
        ('rate_bhxh_chu', 'REAL DEFAULT 17.5'),
        ('rate_bhyt_chu', 'REAL DEFAULT 3'),
        ('rate_bhtn_chu', 'REAL DEFAULT 1'),
    ):
        if col not in cols:
            try:
                conn.execute(f'ALTER TABLE business_info ADD COLUMN {col} {ddl}')
            except sqlite3.Error:
                pass


def update_salary_insurance_config(
    conn: sqlite3.Connection,
    *,
    region: str | None = None,
    base_salary: float | int | str | None = None,
    rate_bhxh: float | int | str | None = None,
    rate_bhyt: float | int | str | None = None,
    rate_bhtn: float | int | str | None = None,
    rate_bhxh_chu: float | int | str | None = None,
    rate_bhyt_chu: float | int | str | None = None,
    rate_bhtn_chu: float | int | str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Lưu cấu hình lương & tỷ lệ BH (business_info) — dùng chung SME/HKD."""
    ensure_salary_insurance_columns(conn)
    row = conn.execute('SELECT id FROM business_info LIMIT 1').fetchone()
    if not row:
        raise ValueError('Chưa có business_info — cấu hình doanh nghiệp trước.')

    def _num(v, default):
        if v is None or v == '':
            return default
        return float(v)

    current = get_salary_insurance_config(conn)
    conn.execute(
        """
        UPDATE business_info SET
            salary_region = ?,
            base_salary_insurance = ?,
            rate_bhxh = ?,
            rate_bhyt = ?,
            rate_bhtn = ?,
            rate_bhxh_chu = ?,
            rate_bhyt_chu = ?,
            rate_bhtn_chu = ?
        """,
        (
            region if region is not None else current['salary_region'],
            _num(base_salary, current['base_salary_insurance']),
            _num(rate_bhxh, current['rate_bhxh']),
            _num(rate_bhyt, current['rate_bhyt']),
            _num(rate_bhtn, current['rate_bhtn']),
            _num(rate_bhxh_chu, current['rate_bhxh_chu']),
            _num(rate_bhyt_chu, current['rate_bhyt_chu']),
            _num(rate_bhtn_chu, current['rate_bhtn_chu']),
        ),
    )
    if commit:
        conn.commit()
    return get_salary_insurance_config(conn)


def calculate_tncn_progressive(taxable_income: float) -> float:
    """Thuế TNCN lũy tiến từng phần (biểu tháng) — cùng HKD."""
    income = float(taxable_income or 0)
    if income <= 0:
        return 0.0
    if income <= 5_000_000:
        return income * 0.05
    if income <= 10_000_000:
        return income * 0.1 - 250_000
    if income <= 18_000_000:
        return income * 0.15 - 750_000
    if income <= 32_000_000:
        return income * 0.2 - 1_650_000
    if income <= 52_000_000:
        return income * 0.25 - 3_250_000
    if income <= 80_000_000:
        return income * 0.3 - 5_850_000
    return income * 0.35 - 9_850_000


def compute_payroll_line(
    *,
    base_salary: float,
    actual_working_days: float,
    standard_days: int,
    allowance_fund: float = 0,
    allowance_other: float = 0,
    bonus: float = 0,
    rates_frac: dict[str, float] | None = None,
    is_chu_ho: bool = False,
    self_deduction: float = DEFAULT_SELF_DEDUCTION,
    dependents: int = 0,
    dependent_deduction: float = DEFAULT_DEPENDENT_DEDUCTION,
    time_salary_override: float | None = None,
) -> dict[str, float]:
    """
    Công thức (khớp Hộ Kinh Doanh):
    - Lương TG = base_salary / công chuẩn × ngày công (hoặc override)
    - BHXH/BHYT/BHTN NLĐ = lương TG × tỷ lệ (Chủ hộ: BHTN = 0)
    - TNCN = biểu lũy tiến trên (thu nhập − BH NLĐ − GT bản thân − NPT×GT)
    - Thực lĩnh = thu nhập − BH − TNCN
    """
    std = max(int(standard_days or 0), 0)
    days = float(actual_working_days or 0)
    base = float(base_salary or 0)
    if time_salary_override is not None:
        time_salary = round(float(time_salary_override))
    else:
        time_salary = round((base / std) * days) if std > 0 else 0

    af = float(allowance_fund or 0)
    ao = float(allowance_other or 0)
    bn = float(bonus or 0)
    total_income = time_salary + af + ao + bn

    rf = rates_frac or {}
    r_bhxh = float(rf.get('nld_bhxh') or 0.08)
    r_bhyt = float(rf.get('nld_bhyt') or 0.015)
    r_bhtn = 0.0 if is_chu_ho else float(rf.get('nld_bhtn') or 0.01)

    bhxh = round(time_salary * r_bhxh)
    bhyt = round(time_salary * r_bhyt)
    bhtn = round(time_salary * r_bhtn)
    bh_total = bhxh + bhyt + bhtn

    gt = float(self_deduction or 0) + int(dependents or 0) * float(dependent_deduction or 0)
    taxable = total_income - bh_total - gt
    tncn_tax = max(0, round(calculate_tncn_progressive(taxable)))
    total_deduct = bh_total + tncn_tax
    final_amount = total_income - total_deduct

    # BH phía DN (Chủ / người SDLĐ)
    r_chu_bhxh = float(rf.get('chu_bhxh') or 0.175)
    r_chu_bhyt = float(rf.get('chu_bhyt') or 0.03)
    r_chu_bhtn = 0.0 if is_chu_ho else float(rf.get('chu_bhtn') or 0.01)
    employer_bhxh = round(time_salary * r_chu_bhxh)
    employer_bhyt = round(time_salary * r_chu_bhyt)
    employer_bhtn = round(time_salary * r_chu_bhtn)

    return {
        'time_salary': float(time_salary),
        'allowance_fund': af,
        'allowance_other': ao,
        'bonus': bn,
        'bhxh': float(bhxh),
        'bhyt': float(bhyt),
        'bhtn': float(bhtn),
        'tncn_tax': float(tncn_tax),
        'total_income': float(total_income),
        'total_deduct': float(total_deduct),
        'final_amount': float(final_amount),
        'employer_bhxh': float(employer_bhxh),
        'employer_bhyt': float(employer_bhyt),
        'employer_bhtn': float(employer_bhtn),
        'employer_insurance': float(employer_bhxh + employer_bhyt + employer_bhtn),
        'is_chu_ho': 1.0 if is_chu_ho else 0.0,
    }


def preview_payroll_grid(
    conn: sqlite3.Connection, month: int, year: int
) -> dict[str, Any]:
    """Lưới lương tháng — công thức theo tỷ lệ BH cấu hình (giống HKD)."""
    ensure_payroll_schema(conn)
    standard_days = _working_days_exclude_sunday(month, year)
    config = get_salary_insurance_config(conn)
    rates_frac = config['rates_frac']

    from Services.attendance_helpers import get_monthly_work_days_map
    from Services.chu_ho_helpers import (
        employee_is_chu_ho,
        ensure_is_chu_ho_column,
        sync_chu_ho_from_business_info,
    )

    try:
        ensure_is_chu_ho_column(conn)
        sync_chu_ho_from_business_info(conn, commit=False)
    except Exception:
        pass

    attendance_days = get_monthly_work_days_map(conn, month, year)

    from Services.employee_payroll_helpers import (
        department_label,
        ensure_payroll_schema,
        expense_account_for_department,
        normalize_department,
    )
    ensure_payroll_schema(conn, commit=False)

    query = """
        SELECT
            e.id as employee_id, e.fullname, e.salary_rate, e.base_salary,
            e.position, e.is_chu_ho, e.department,
            COALESCE(e.allowance_fund, 0) AS emp_allowance_fund,
            COALESCE(e.allowance_other, 0) AS emp_allowance_other,
            COALESCE(e.default_bonus, 0) AS emp_default_bonus,
            COALESCE(e.dependents, 0) AS dependents,
            COALESCE(e.self_deduction, 11000000) AS self_deduction,
            COALESCE(e.dependent_deduction, 4400000) AS dependent_deduction,
            s.id as salary_id, s.actual_working_days, s.time_salary,
            s.allowance_fund, s.allowance_other, s.bonus,
            s.bhxh, s.bhyt, s.bhtn, s.tncn_tax, s.total_income,
            s.total_deduct, s.final_amount, s.date as record_date
        FROM employees e
        LEFT JOIN salary_detail s ON e.id = s.employee_id AND s.month = ? AND s.year = ?
        WHERE e.status = 1
    """
    try:
        rows = conn.execute(query, (month, year)).fetchall()
    except sqlite3.OperationalError:
        # DB thiếu cột GT/TNCN — fallback tối thiểu
        rows = conn.execute(
            """
            SELECT
                e.id as employee_id, e.fullname, e.salary_rate, e.base_salary,
                COALESCE(e.allowance_fund, 0) AS emp_allowance_fund,
                COALESCE(e.allowance_other, 0) AS emp_allowance_other,
                COALESCE(e.default_bonus, 0) AS emp_default_bonus,
                s.id as salary_id, s.actual_working_days, s.time_salary,
                s.allowance_fund, s.allowance_other, s.bonus,
                s.bhxh, s.bhyt, s.bhtn, s.tncn_tax, s.total_income,
                s.total_deduct, s.final_amount, s.date as record_date
            FROM employees e
            LEFT JOIN salary_detail s ON e.id = s.employee_id AND s.month = ? AND s.year = ?
            WHERE e.status = 1
            """,
            (month, year),
        ).fetchall()

    data: list[dict[str, Any]] = []
    numeric_fields = [
        'actual_working_days', 'time_salary', 'base_salary',
        'allowance_fund', 'allowance_other', 'bonus',
        'bhxh', 'bhyt', 'bhtn', 'tncn_tax',
        'total_income', 'total_deduct', 'final_amount',
        'self_deduction', 'dependent_deduction', 'dependents',
        'employer_bhxh', 'employer_bhyt', 'employer_bhtn', 'employer_insurance',
    ]
    for row in rows:
        item = dict(row)
        emp_id = item.get('employee_id')
        work_days = attendance_days.get(emp_id, 0) if emp_id else 0
        item['attendance_work_days'] = work_days
        item['base_salary'] = float(item.get('base_salary') or item.get('salary_rate') or 0)
        is_chu = bool(employee_is_chu_ho(item, conn))
        item['is_chu_ho'] = 1 if is_chu else 0

        if item.get('salary_id') is None:
            item['actual_working_days'] = work_days if work_days > 0 else standard_days
            item['allowance_fund'] = float(item.get('emp_allowance_fund') or 0)
            item['allowance_other'] = float(item.get('emp_allowance_other') or 0)
            item['bonus'] = float(item.get('emp_default_bonus') or 0)
            calc = compute_payroll_line(
                base_salary=item['base_salary'],
                actual_working_days=float(item['actual_working_days'] or 0),
                standard_days=standard_days,
                allowance_fund=item['allowance_fund'],
                allowance_other=item['allowance_other'],
                bonus=item['bonus'],
                rates_frac=rates_frac,
                is_chu_ho=is_chu,
                self_deduction=float(item.get('self_deduction') or DEFAULT_SELF_DEDUCTION),
                dependents=int(item.get('dependents') or 0),
                dependent_deduction=float(
                    item.get('dependent_deduction') or DEFAULT_DEPENDENT_DEDUCTION
                ),
            )
            item.update(calc)
        else:
            # Đã chốt: giữ số liệu đã lưu; vẫn bổ sung BH chủ theo tỷ lệ hiện tại để tham chiếu
            for field in (
                'actual_working_days', 'time_salary', 'allowance_fund',
                'allowance_other', 'bonus', 'bhxh', 'bhyt', 'bhtn', 'tncn_tax',
                'total_income', 'total_deduct', 'final_amount',
            ):
                try:
                    item[field] = float(item[field] or 0)
                except (TypeError, ValueError):
                    item[field] = 0.0
            calc = compute_payroll_line(
                base_salary=item['base_salary'],
                actual_working_days=item['actual_working_days'],
                standard_days=standard_days,
                allowance_fund=item['allowance_fund'],
                allowance_other=item['allowance_other'],
                bonus=item['bonus'],
                rates_frac=rates_frac,
                is_chu_ho=is_chu,
                self_deduction=float(item.get('self_deduction') or DEFAULT_SELF_DEDUCTION),
                dependents=int(item.get('dependents') or 0),
                dependent_deduction=float(
                    item.get('dependent_deduction') or DEFAULT_DEPENDENT_DEDUCTION
                ),
                time_salary_override=item['time_salary'],
            )
            item['employer_bhxh'] = calc['employer_bhxh']
            item['employer_bhyt'] = calc['employer_bhyt']
            item['employer_bhtn'] = calc['employer_bhtn']
            item['employer_insurance'] = calc['employer_insurance']

        for field in numeric_fields:
            if item.get(field) is None:
                item[field] = 0
            else:
                try:
                    item[field] = float(item[field])
                except (TypeError, ValueError):
                    item[field] = 0
        dept = normalize_department(item.get('department'))
        item['department'] = dept
        item['department_label'] = department_label(dept)
        item['expense_account'] = expense_account_for_department(dept)
        data.append(item)

    return {
        'data': data,
        'standard_days': standard_days,
        'config': config,
    }


def _employer_parts_for_record(
    conn: sqlite3.Connection,
    record: dict,
    rates: dict | None = None,
) -> dict[str, Decimal]:
    """BH chủ 1 NV: căn = lương thời gian; Chủ hộ không đóng BHTN."""
    from Services.chu_ho_helpers import employee_is_chu_ho
    from Services.insurance_debt_helpers import _load_rates

    if rates is None:
        rates = _load_rates(conn)
    parts = {
        'bhxh': Decimal('0.00'),
        'bhyt': Decimal('0.00'),
        'bhtn': Decimal('0.00'),
    }
    base = _money(
        record.get('time_salary')
        or record.get('base_salary')
        or record.get('salary_rate')
        or 0
    )
    if base <= 0:
        return parts
    is_chu = bool(record.get('is_chu_ho')) or employee_is_chu_ho(record, conn)
    parts['bhxh'] = (base * _money(rates['chu_bhxh'])).quantize(MONEY_Q)
    parts['bhyt'] = (base * _money(rates['chu_bhyt'])).quantize(MONEY_Q)
    if not is_chu:
        parts['bhtn'] = (base * _money(rates['chu_bhtn'])).quantize(MONEY_Q)
    return parts


def _employer_insurance_from_records(
    conn: sqlite3.Connection, records: list[dict]
) -> tuple[Decimal, dict[str, Decimal]]:
    """BH chủ theo từng NV: căn = lương thời gian; Chủ hộ không đóng BHTN."""
    from Services.insurance_debt_helpers import _load_rates

    rates = _load_rates(conn)
    parts = {
        'bhxh': Decimal('0.00'),
        'bhyt': Decimal('0.00'),
        'bhtn': Decimal('0.00'),
    }
    for r in records:
        one = _employer_parts_for_record(conn, r, rates=rates)
        parts['bhxh'] += one['bhxh']
        parts['bhyt'] += one['bhyt']
        parts['bhtn'] += one['bhtn']
    total = parts['bhxh'] + parts['bhyt'] + parts['bhtn']
    return total, parts


def _resolve_record_department(conn: sqlite3.Connection, record: dict) -> str:
    """Lấy mã bộ phận từ dòng lương hoặc hồ sơ NV."""
    from Services.employee_payroll_helpers import normalize_department

    raw = record.get('department')
    if raw:
        return normalize_department(raw)
    emp_id = record.get('employee_id')
    if emp_id:
        try:
            row = conn.execute(
                'SELECT department FROM employees WHERE id = ?',
                (int(emp_id),),
            ).fetchone()
            if row:
                val = row['department'] if hasattr(row, 'keys') else row[0]
                return normalize_department(val)
        except sqlite3.Error:
            pass
    return normalize_department(None)


def list_payroll_runs(
    conn: sqlite3.Connection,
    *,
    branch_code: str | None = None,
) -> list[dict[str, Any]]:
    ensure_sme_payroll_schema(conn, commit=False)
    from Services.sme.branches import DEFAULT_BRANCH_CODE
    sql = """
        SELECT * FROM sme_payroll_runs
        WHERE COALESCE(status,'accrued') != 'void'
    """
    params: list[Any] = []
    code = (branch_code or '').strip().upper()
    if code and code != 'ALL':
        if code == DEFAULT_BRANCH_CODE:
            sql += " AND (branch_code IS NULL OR branch_code = '' OR branch_code = ?)"
        else:
            sql += ' AND branch_code = ?'
        params.append(code)
    sql += ' ORDER BY year DESC, month DESC LIMIT 60'
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_payroll_run(
    conn: sqlite3.Connection,
    month: int,
    year: int,
    branch_code: str | None = None,
) -> dict[str, Any] | None:
    ensure_sme_payroll_schema(conn, commit=False)
    from Services.sme.branch_filter import branch_where
    from Services.sme.branches import request_branch_filter

    code = branch_code
    if code is None:
        try:
            code = request_branch_filter()
        except Exception:
            code = None
    bf, bp = branch_where(code)
    row = conn.execute(
        f"""
        SELECT * FROM sme_payroll_runs
        WHERE month = ? AND year = ? AND COALESCE(status,'accrued') != 'void'
        {bf}
        ORDER BY id DESC LIMIT 1
        """,
        (int(month), int(year), *bp),
    ).fetchone()
    return dict(row) if row else None


def accrue_payroll(
    conn: sqlite3.Connection,
    *,
    month: int,
    year: int,
    records: list[dict],
    posting_date: str | None = None,
    expense_account: str = '642',
    branch_code: str | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """
    Chốt bảng lương kỳ + bút toán (PA A — theo bộ phận):
      Nợ 622/627/641/642 = gross + BH chủ (theo department từng NV)
      Có 3341 = thực lĩnh
      Có 3383/3384/3385 = BH NLĐ + BH chủ (tách loại)
      Có 3335 = TNCN (nếu có)
    """
    from Services.employee_payroll_helpers import (
        department_label,
        ensure_payroll_schema,
        expense_account_for_department,
    )
    from Services.insurance_debt_helpers import _load_rates

    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_payroll_schema(conn, commit=False)
    ensure_payroll_schema(conn, commit=False)
    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, branch_code)

    month, year = int(month), int(year)
    if not records:
        raise ValueError('Không có dòng lương để chốt')

    date_s = (posting_date or f'{year}-{month:02d}-28')[:10]
    # expense_account giữ tương thích API cũ — chỉ dùng khi NV không có bộ phận
    fallback_exp = (expense_account or '642').strip() or '642'

    total_income = Decimal('0.00')
    total_deduct = Decimal('0.00')
    total_net = Decimal('0.00')
    emp_bhxh = Decimal('0.00')
    emp_bhyt = Decimal('0.00')
    emp_bhtn = Decimal('0.00')
    emp_tncn = Decimal('0.00')
    expense_by_account: dict[str, Decimal] = {}
    rates = _load_rates(conn)

    cur = conn.cursor()
    # Chỉ xóa lưới lương của CN hiện tại (không đụng CN khác)
    sd_cols = {r[1] for r in conn.execute('PRAGMA table_info(salary_detail)').fetchall()}
    if 'branch_code' in sd_cols:
        cur.execute(
            """
            DELETE FROM salary_detail
            WHERE month = ? AND year = ?
              AND COALESCE(NULLIF(TRIM(branch_code), ''), ?) = ?
            """,
            (month, year, branch, branch),
        )
    else:
        # Legacy: chỉ xóa toàn kỳ nếu chưa có run CN khác
        other = conn.execute(
            """
            SELECT 1 FROM sme_payroll_runs
            WHERE month = ? AND year = ?
              AND COALESCE(status,'accrued') != 'void'
              AND COALESCE(NULLIF(TRIM(branch_code), ''), ?) != ?
            LIMIT 1
            """,
            (month, year, branch, branch),
        ).fetchone()
        if not other:
            cur.execute('DELETE FROM salary_detail WHERE month = ? AND year = ?', (month, year))

    for r in records:
        income = _money(r.get('total_income') or 0)
        deduct = _money(r.get('total_deduct') or 0)
        net = _money(r.get('final_amount') or 0)
        total_income += income
        total_deduct += deduct
        total_net += net
        emp_bhxh += _money(r.get('bhxh') or 0)
        emp_bhyt += _money(r.get('bhyt') or 0)
        emp_bhtn += _money(r.get('bhtn') or 0)
        emp_tncn += _money(r.get('tncn_tax') or 0)

        dept = _resolve_record_department(conn, r)
        exp_acct = expense_account_for_department(dept) or fallback_exp
        emp_parts_one = _employer_parts_for_record(conn, r, rates=rates)
        emp_employer = emp_parts_one['bhxh'] + emp_parts_one['bhyt'] + emp_parts_one['bhtn']
        expense_by_account[exp_acct] = (
            expense_by_account.get(exp_acct, Decimal('0.00')) + income + emp_employer
        )

        insert_cols = [
            'employee_id', 'fullname', 'month', 'year',
            'salary_rate', 'actual_working_days', 'time_salary',
            'allowance_fund', 'allowance_other', 'bonus',
            'bhxh', 'bhyt', 'bhtn', 'tncn_tax',
            'total_income', 'total_deduct', 'final_amount', 'date',
        ]
        insert_vals: list[Any] = [
            r.get('employee_id'),
            r.get('fullname') or r.get('fullname'),
            month, year,
            _f(r.get('base_salary') or r.get('salary_rate') or 0),
            _f(r.get('actual_working_days') or 0),
            _f(r.get('time_salary') or 0),
            _f(r.get('allowance_fund') or 0),
            _f(r.get('allowance_other') or 0),
            _f(r.get('bonus') or 0),
            _f(r.get('bhxh') or 0),
            _f(r.get('bhyt') or 0),
            _f(r.get('bhtn') or 0),
            _f(r.get('tncn_tax') or 0),
            _f(income),
            _f(deduct),
            _f(net),
            date_s,
        ]
        if 'branch_code' in sd_cols:
            insert_cols.append('branch_code')
            insert_vals.append(branch)
        if 'department' in sd_cols:
            insert_cols.append('department')
            insert_vals.append(dept)
        if 'expense_account' in sd_cols:
            insert_cols.append('expense_account')
            insert_vals.append(exp_acct)
        ph = ','.join('?' for _ in insert_cols)
        cur.execute(
            f"INSERT INTO salary_detail ({', '.join(insert_cols)}) VALUES ({ph})",
            insert_vals,
        )

    employer_total, emp_parts = _employer_insurance_from_records(conn, records)
    expense_amt = total_income + employer_total
    if not expense_by_account and expense_amt > 0:
        expense_by_account[fallback_exp] = expense_amt

    # Phân bổ có: 3341 = thực lĩnh; BH = NLĐ + chủ; TNCN
    credit_bhxh = emp_bhxh + emp_parts['bhxh']
    credit_bhyt = emp_bhyt + emp_parts['bhyt']
    credit_bhtn = emp_bhtn + emp_parts['bhtn']

    acct_order = ('622', '627', '641', '642')
    lines: list[dict] = []
    seq = 1
    for acct in acct_order:
        amt = expense_by_account.get(acct, Decimal('0.00'))
        if amt <= 0:
            continue
        lines.append({
            'sequence': seq,
            'account_code': acct,
            'debit': float(amt),
            'credit': 0,
            'description': (
                f'CP lương + BH DN T{month}/{year} — '
                f'{department_label(acct)}'
            ),
        })
        seq += 1
    # TK ngoài map chuẩn (nếu có)
    for acct, amt in sorted(expense_by_account.items()):
        if acct in acct_order or amt <= 0:
            continue
        lines.append({
            'sequence': seq,
            'account_code': acct,
            'debit': float(amt),
            'credit': 0,
            'description': f'CP lương + BH DN T{month}/{year}',
        })
        seq += 1
    exp_acct = '+'.join(a for a in acct_order if expense_by_account.get(a, 0) > 0) or fallback_exp
    if total_net > 0:
        lines.append({
            'sequence': seq,
            'account_code': '3341',
            'debit': 0,
            'credit': float(total_net),
            'description': f'Phải trả NLĐ T{month}/{year}',
        })
        seq += 1
    if credit_bhxh > 0:
        lines.append({
            'sequence': seq,
            'account_code': '3383',
            'debit': 0,
            'credit': float(credit_bhxh),
            'description': f'BHXH T{month}/{year}',
        })
        seq += 1
    if credit_bhyt > 0:
        lines.append({
            'sequence': seq,
            'account_code': '3384',
            'debit': 0,
            'credit': float(credit_bhyt),
            'description': f'BHYT T{month}/{year}',
        })
        seq += 1
    if credit_bhtn > 0:
        lines.append({
            'sequence': seq,
            'account_code': '3385',
            'debit': 0,
            'credit': float(credit_bhtn),
            'description': f'BHTN T{month}/{year}',
        })
        seq += 1
    if emp_tncn > 0:
        lines.append({
            'sequence': seq,
            'account_code': '3335',
            'debit': 0,
            'credit': float(emp_tncn),
            'description': f'TNCN T{month}/{year}',
        })
        seq += 1

    # Cân bằng: nếu còn lệch do làm tròn → điều chỉnh 3341
    deb = sum(_money(x['debit']) for x in lines)
    cred = sum(_money(x['credit']) for x in lines)
    diff = deb - cred
    if abs(diff) >= Decimal('0.01'):
        for ln in lines:
            if ln['account_code'] == '3341' and ln['credit'] > 0:
                ln['credit'] = float(_money(ln['credit']) + diff)
                break
        else:
            lines.append({
                'sequence': seq,
                'account_code': '3341',
                'debit': 0 if diff > 0 else float(-diff),
                'credit': float(diff) if diff > 0 else 0,
                'description': 'Điều chỉnh làm tròn',
            })

    # Ghi đè kỳ: đảo bút toán run cũ của CN này (nếu còn) rồi lập run mới
    existing = get_payroll_run(conn, month, year, branch_code=branch)
    if existing and existing.get('journal_entry_id') and existing.get('status') != 'void':
        try:
            reverse_journal_entry(
                conn, int(existing['journal_entry_id']),
                posting_date=date_s, created_by=created_by,
                reason=f'Thay thế bảng lương T{month}/{year}',
            )
        except Exception:
            pass
    desc = f'Trích lương + BH T{month}/{year}'
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='LUONG',
        document_no=f'L{year}{month:02d}-{branch}',
        business_type='TRICH_LUONG',
        description=desc,
        reference_document=f'SALARY|{month}|{year}|{branch}',
        created_by=created_by,
        branch_code=branch,
        lines=lines,
    )

    cur.execute(
        """
        DELETE FROM sme_payroll_runs
        WHERE month = ? AND year = ?
          AND COALESCE(NULLIF(TRIM(branch_code), ''), ?) = ?
        """,
        (month, year, branch, branch),
    )
    cur.execute(
        """
        INSERT INTO sme_payroll_runs (
            month, year, posting_date, expense_account,
            total_income, total_deduct, total_net, employer_insurance,
            journal_entry_id, status, created_by, created_at, branch_code
        ) VALUES (?,?,?,?,?,?,?,?,?,'accrued',?,?,?)
        """,
        (
            month, year, date_s, exp_acct,
            float(total_income), float(total_deduct), float(total_net), float(employer_total),
            entry['id'], created_by, _now(), branch,
        ),
    )
    run_id = cur.lastrowid
    if commit:
        conn.commit()

    return {
        'id': run_id,
        'month': month,
        'year': year,
        'posting_date': date_s,
        'journal_entry_id': entry['id'],
        'entry_no': entry.get('entry_no'),
        'total_income': float(total_income),
        'total_net': float(total_net),
        'employer_insurance': float(employer_total),
        'expense_amount': float(expense_amt),
        'replaced_previous': bool(existing),
        'employee_count': len(records),
        'branch_code': branch,
    }


def void_payroll_run(
    conn: sqlite3.Connection,
    *,
    month: int,
    year: int,
    reason: str = 'Hủy bảng lương',
    created_by: str | None = None,
    commit: bool = False,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Hủy chốt lương kỳ.

    Kỳ mở (chưa kê khai / chưa khóa sổ): xóa bút toán + run — không ghi đảo.
    Kỳ đã chốt/khóa: ghi đảo, đánh dấu run void.
    """
    from Services.sme.period_lock import is_period_sealed

    ensure_sme_payroll_schema(conn, commit=False)
    month, year = int(month), int(year)
    run = get_payroll_run(conn, month, year, branch_code=branch_code)
    if not run:
        raise ValueError('Không tìm thấy bảng lương kỳ này')
    if run.get('status') == 'void':
        raise ValueError('Bảng lương đã hủy')

    sealed = is_period_sealed(conn, year, month)
    rev = None
    if run.get('journal_entry_id'):
        rev = reverse_journal_entry(
            conn, int(run['journal_entry_id']),
            created_by=created_by, reason=reason,
        )
    if run.get('allocation_journal_id'):
        try:
            reverse_journal_entry(
                conn, int(run['allocation_journal_id']),
                created_by=created_by, reason=f'{reason} (phân bổ 08-LĐTL)',
            )
        except Exception:
            pass

    run_id = int(run['id'])
    mode = (rev or {}).get('mode') or ('reverse' if sealed else 'hard_delete')

    # Chỉ xoá salary_detail của CN này
    sd_cols = {r[1] for r in conn.execute('PRAGMA table_info(salary_detail)').fetchall()}
    if 'branch_code' in sd_cols:
        br = (run.get('branch_code') or '').strip() or 'HQ'
        conn.execute(
            """
            DELETE FROM salary_detail
            WHERE month = ? AND year = ?
              AND COALESCE(NULLIF(TRIM(branch_code), ''), ?) = ?
            """,
            (month, year, br, br),
        )
    else:
        other = conn.execute(
            """
            SELECT 1 FROM sme_payroll_runs
            WHERE month = ? AND year = ? AND id != ?
              AND COALESCE(status, 'accrued') != 'void'
            LIMIT 1
            """,
            (month, year, run_id),
        ).fetchone()
        if not other:
            conn.execute(
                'DELETE FROM salary_detail WHERE month = ? AND year = ?',
                (month, year),
            )

    if mode == 'hard_delete' or not sealed:
        conn.execute('DELETE FROM sme_payroll_runs WHERE id = ?', (run_id,))
        if commit:
            conn.commit()
        return {
            'id': run_id,
            'month': month,
            'year': year,
            'status': 'deleted',
            'deleted': True,
            'mode': 'hard_delete',
            'branch_code': run.get('branch_code'),
            'reason': reason,
            'message': (
                f'Đã xóa bút toán và bảng lương T{month}/{year} '
                f'(kỳ chưa kê khai / chưa khóa sổ) — có thể chốt lại.'
            ),
        }

    conn.execute(
        "UPDATE sme_payroll_runs SET status = 'void', journal_entry_id = NULL, "
        "allocation_journal_id = NULL WHERE id = ?",
        (run_id,),
    )
    if commit:
        conn.commit()
    return {
        'id': run_id,
        'month': month,
        'year': year,
        'status': 'void',
        'deleted': False,
        'mode': 'reverse',
        'journal_entry_id': run.get('journal_entry_id'),
        'branch_code': run.get('branch_code'),
        'reason': reason,
        'message': f'Đã hủy bảng lương T{month}/{year} và ghi bút toán đảo (kỳ đã khóa/kê khai).',
    }


def pay_payroll_period(
    conn: sqlite3.Connection,
    *,
    month: int,
    year: int,
    amount=None,
    pay_date: str | None = None,
    payment_method: str = 'bank',
    receiver_name: str = 'Tập thể cán bộ nhân viên',
    reason: str | None = None,
    created_by: str | None = None,
    branch_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Trả lương cả kỳ — 1 phiếu chi SME 02-TT (Nợ 3341 · Có tiền), giống HKD."""
    from Services.sme.employee_payable import (
        _period_all_paid,
        _period_payroll_total,
        period_reference_key,
    )
    from Services.sme.vouchers import create_payment, ensure_sme_voucher_schema

    ensure_sme_payroll_schema(conn, commit=False)
    ensure_sme_voucher_schema(conn, commit=False)
    month, year = int(month), int(year)
    net, emp_count = _period_payroll_total(conn, month, year)
    if emp_count <= 0 or net <= 0:
        raise ValueError('Chưa có bảng lương kỳ này — hãy chốt lương SME trước')

    run = get_payroll_run(conn, month, year, branch_code=branch_code)
    paid = _period_all_paid(conn, month, year)
    remain = max(0.0, net - paid)
    if remain <= 0.01:
        raise ValueError('Kỳ lương này đã thanh toán đủ')

    pay_amt = float(amount) if amount is not None else remain
    if pay_amt <= 0:
        raise ValueError('Số tiền không hợp lệ')
    if pay_amt > remain + 0.01:
        raise ValueError('Số tiền vượt quá còn phải trả của kỳ')

    date_s = (pay_date or datetime.now().strftime('%Y-%m-%d'))[:10]
    if not date_s:
        raise ValueError('Vui lòng nhập ngày chi trả')
    desc = (reason or '').strip() or (
        f'Thanh toán lương tháng {month}/{year} ({emp_count} nhân viên)'
    )
    ref = period_reference_key(month, year)
    method = payment_method or 'bank'
    if str(method) in ('111', '112'):
        method = 'cash' if str(method) == '111' else 'bank'

    result = create_payment(
        conn,
        voucher_date=date_s,
        party_name=receiver_name or 'Tập thể cán bộ nhân viên',
        amount=pay_amt,
        payment_method=method,
        debit_account='3341',
        reason=desc,
        reference_document=ref,
        source_type='salary',
        source_id=run['id'] if run else None,
        created_by=created_by,
        branch_code=branch_code,
        form_code='02-TT',
        commit=False,
    )
    if commit:
        conn.commit()
    return {
        **result,
        'month': month,
        'year': year,
        'voucher': result.get('voucher_no'),
        'employee_count': emp_count,
        'paid_before': paid,
        'remain_after': round(remain - pay_amt, 2),
        'message': f'Đã lập phiếu chi trả lương cả kỳ T{month}/{year}',
    }


def pay_payroll_employee(
    conn: sqlite3.Connection,
    *,
    employee_id: int,
    month: int,
    year: int,
    amount=None,
    pay_date: str | None = None,
    payment_method: str = 'bank',
    receiver_name: str | None = None,
    reason: str | None = None,
    created_by: str | None = None,
    branch_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Trả lương lẻ 1 NV — phiếu chi SME (trường hợp đặc biệt, giống HKD)."""
    from Services.sme.employee_payable import (
        _employee_salary_paid,
        salary_reference_key,
    )
    from Services.sme.vouchers import create_payment, ensure_sme_voucher_schema

    ensure_sme_payroll_schema(conn, commit=False)
    ensure_sme_voucher_schema(conn, commit=False)
    emp_id = int(employee_id)
    month, year = int(month), int(year)

    row = conn.execute(
        """
        SELECT sd.final_amount, sd.fullname, e.fullname AS emp_name
        FROM salary_detail sd
        LEFT JOIN employees e ON e.id = sd.employee_id
        WHERE sd.employee_id = ? AND sd.month = ? AND sd.year = ?
        """,
        (emp_id, month, year),
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy kỳ lương của nhân viên')
    final_amt = float(row['final_amount'] if hasattr(row, 'keys') else row[0] or 0)
    fullname = (
        (row['fullname'] if hasattr(row, 'keys') else row[1])
        or (row['emp_name'] if hasattr(row, 'keys') else None)
        or f'NV #{emp_id}'
    )
    paid, _, _ = _employee_salary_paid(conn, emp_id, month, year, final_amt)
    remain = max(0.0, final_amt - paid)
    if remain <= 0.01:
        raise ValueError('Kỳ lương này đã được thanh toán đủ')

    pay_amt = float(amount) if amount is not None else remain
    if pay_amt <= 0:
        raise ValueError('Số tiền không hợp lệ')
    if pay_amt > remain + 0.01:
        raise ValueError('Số tiền vượt quá số còn phải trả')

    date_s = (pay_date or datetime.now().strftime('%Y-%m-%d'))[:10]
    if not date_s:
        raise ValueError('Vui lòng nhập ngày chi trả')
    recv = (receiver_name or fullname or '').strip()
    if not recv:
        raise ValueError('Vui lòng nhập người nhận')
    desc = (reason or '').strip() or (
        f'Thanh toán lương lẻ tháng {month}/{year} — {fullname}'
    )
    method = payment_method or 'bank'
    if str(method) in ('111', '112'):
        method = 'cash' if str(method) == '111' else 'bank'

    result = create_payment(
        conn,
        voucher_date=date_s,
        party_name=recv,
        amount=pay_amt,
        payment_method=method,
        debit_account='3341',
        reason=desc,
        reference_document=salary_reference_key(emp_id, month, year),
        source_type='salary',
        source_id=emp_id,
        created_by=created_by,
        branch_code=branch_code,
        form_code='02-TT',
        commit=False,
    )
    if commit:
        conn.commit()
    return {
        **result,
        'employee_id': emp_id,
        'month': month,
        'year': year,
        'voucher': result.get('voucher_no'),
        'paid_before': paid,
        'remain_after': round(remain - pay_amt, 2),
        'message': f'Đã lập phiếu chi trả lương lẻ {fullname}',
    }


def pay_insurance(
    conn: sqlite3.Connection,
    *,
    amount,
    pay_date: str | None = None,
    payment_method: str = 'bank',
    account_code: str = '3383',
    receiver_name: str = 'Cơ quan BHXH',
    reference: str = '',
    reason: str | None = None,
    created_by: str | None = None,
    branch_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Nộp BHXH/BHYT/BHTN — phiếu chi Nợ 338x / Có tiền (mẫu theo dõi 07-LĐTL)."""
    from Services.sme.vouchers import create_payment, ensure_sme_voucher_schema

    ensure_sme_voucher_schema(conn, commit=False)
    amt = float(amount or 0)
    if amt <= 0:
        raise ValueError('Số tiền nộp BH phải > 0')
    acc = (account_code or '3383').strip() or '3383'
    if not acc.startswith('338'):
        raise ValueError('TK phải thuộc nhóm 338 (BHXH/BHYT/BHTN…)')
    date_s = (pay_date or datetime.now().strftime('%Y-%m-%d'))[:10]
    label = {'3383': 'BHXH', '3384': 'BHYT', '3385': 'BHTN'}.get(acc, acc)
    desc = (reason or '').strip() or (
        f'Nộp {label} ({acc})' + (f' — {reference}' if reference else '')
    )
    result = create_payment(
        conn,
        voucher_date=date_s,
        party_name=receiver_name or 'Cơ quan BHXH',
        amount=amt,
        payment_method=payment_method or 'bank',
        debit_account=acc,
        reason=desc,
        reference_document=reference or f'BH|{acc}|{date_s}',
        source_type='insurance',
        created_by=created_by,
        branch_code=branch_code,
        commit=False,
    )
    try:
        conn.execute(
            "UPDATE sme_vouchers SET form_code = '07-LĐTL' WHERE id = ?",
            (result.get('id'),),
        )
    except sqlite3.Error:
        pass
    if commit:
        conn.commit()
    return {**result, 'form_code': '07-LĐTL', 'account_code': acc, 'voucher_date': date_s}


def payroll_allocation_summary(
    conn: sqlite3.Connection,
    *,
    month: int,
    year: int,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Bảng phân bổ lương (08-LĐTL) — đọc từ salary_detail + run SME."""
    ensure_sme_payroll_schema(conn, commit=False)
    from Services.sme.branches import request_branch_filter
    br = branch_code
    if br is None:
        try:
            br = request_branch_filter()
        except Exception:
            br = None
    run = get_payroll_run(conn, int(month), int(year), branch_code=br)
    sd_cols = {r[1] for r in conn.execute('PRAGMA table_info(salary_detail)').fetchall()}
    br_sql = ''
    br_params: list[Any] = []
    code = (br or '').strip().upper()
    if 'branch_code' in sd_cols and code and code != 'ALL':
        br_sql = " AND COALESCE(NULLIF(TRIM(s.branch_code), ''), ?) = ?"
        br_params = [code, code]
    dept_select = ''
    if 'department' in sd_cols:
        dept_select = ", COALESCE(NULLIF(TRIM(s.department), ''), e.department, 'ADMIN') AS department"
    else:
        dept_select = ", COALESCE(e.department, 'ADMIN') AS department"
    exp_select = ''
    if 'expense_account' in sd_cols:
        exp_select = ', s.expense_account'
    rows = conn.execute(
        f"""
        SELECT COALESCE(e.fullname, s.fullname) AS fullname, e.position,
               COALESCE(s.salary_rate, 0) AS base_salary,
               COALESCE(s.allowance_fund, 0) + COALESCE(s.allowance_other, 0) AS allowance,
               COALESCE(s.bonus, 0) AS bonus,
               COALESCE(s.final_amount, 0) AS net_pay,
               COALESCE(s.total_income, 0) AS total_income,
               COALESCE(s.total_deduct, 0) AS total_deduct,
               COALESCE(s.bhxh, 0) AS bhxh,
               COALESCE(s.bhyt, 0) AS bhyt,
               COALESCE(s.bhtn, 0) AS bhtn,
               COALESCE(s.tncn_tax, 0) AS tncn_tax,
               COALESCE(s.actual_working_days, 0) AS actual_working_days
               {dept_select}
               {exp_select}
        FROM salary_detail s
        LEFT JOIN employees e ON e.id = s.employee_id
        WHERE s.month = ? AND s.year = ?
        {br_sql}
        ORDER BY COALESCE(e.fullname, s.fullname)
        """,
        (int(month), int(year), *br_params),
    ).fetchall()
    lines = [dict(r) for r in rows]
    total_gross = sum(float(x.get('total_income') or 0) for x in lines)
    total_net = sum(float(x.get('net_pay') or 0) for x in lines)
    return {
        'form_code': '08-LĐTL',
        'month': int(month),
        'year': int(year),
        'run': run,
        'lines': lines,
        'total_gross': total_gross,
        'total_net': total_net,
        'branch_code': code or 'ALL',
        'employer_insurance': float((run or {}).get('employer_insurance') or 0),
        'allocation_journal_id': (run or {}).get('allocation_journal_id'),
    }


def _expense_account_for_position(position: str | None, department: str | None = None) -> str:
    """Ưu tiên bộ phận (department); fallback đoán theo chức vụ (legacy)."""
    from Services.employee_payroll_helpers import expense_account_for_department, normalize_department

    if department:
        return expense_account_for_department(department)
    p = (position or '').strip().lower()
    if any(k in p for k in ('sx', 'sản xuất', 'san xuat', 'công nhân', 'cong nhan', 'production')):
        return expense_account_for_department('PRODUCTION')
    if any(k in p for k in ('bán hàng', 'ban hang', 'sales', 'sale', 'kd', 'shipper')):
        return expense_account_for_department('SALES')
    if any(k in p for k in ('phân xưởng', 'phan xuong', '627')):
        return expense_account_for_department('WORKSHOP')
    _ = normalize_department(None)
    return expense_account_for_department('ADMIN')


def post_payroll_allocation(
    conn: sqlite3.Connection,
    *,
    month: int,
    year: int,
    allocations: list[dict] | None = None,
    posting_date: str | None = None,
    source_account: str = '642',
    created_by: str | None = None,
    replace_existing: bool = True,
    commit: bool = False,
) -> dict[str, Any]:
    """
    Phân bổ lương 08-LĐTL: Nợ 622/627/641 / Có 642 (hoặc source_account).
    Nếu không truyền allocations → tự chia theo vị trí NV từ salary_detail.
    """
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_payroll_schema(conn, commit=False)
    month, year = int(month), int(year)
    run = get_payroll_run(conn, month, year)
    if not run:
        raise ValueError('Chưa chốt bảng lương kỳ này')

    cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_payroll_runs)').fetchall()}
    if 'allocation_journal_id' not in cols:
        try:
            conn.execute(
                'ALTER TABLE sme_payroll_runs ADD COLUMN allocation_journal_id INTEGER'
            )
        except sqlite3.OperationalError:
            pass

    if replace_existing and run.get('allocation_journal_id'):
        try:
            reverse_journal_entry(
                conn, int(run['allocation_journal_id']),
                created_by=created_by, reason='Thay phân bổ lương 08-LĐTL',
            )
        except Exception:
            pass
        conn.execute(
            'UPDATE sme_payroll_runs SET allocation_journal_id = NULL WHERE id = ?',
            (run['id'],),
        )

    buckets: dict[str, Decimal] = {}
    if allocations:
        for a in allocations:
            acc = str(a.get('account_code') or a.get('account') or '').strip()
            amt = _money(a.get('amount'))
            if not acc or amt <= 0:
                continue
            buckets[acc] = buckets.get(acc, Decimal('0.00')) + amt
    else:
        summary = payroll_allocation_summary(conn, month=month, year=year)
        # Phân bổ tổng chi phí lương (gross + BH chủ) theo vị trí
        total_cost = _money(summary.get('total_gross')) + _money(
            summary.get('employer_insurance')
        )
        if total_cost <= 0:
            raise ValueError('Không có số liệu lương để phân bổ')
        # Trọng số theo total_income từng NV
        weights: dict[str, Decimal] = {}
        for ln in summary.get('lines') or []:
            acc = (
                (ln.get('expense_account') or '').strip()
                or _expense_account_for_position(ln.get('position'), ln.get('department'))
            )
            w = _money(ln.get('total_income'))
            if w <= 0:
                continue
            weights[acc] = weights.get(acc, Decimal('0.00')) + w
        weight_sum = sum(weights.values()) or Decimal('1.00')
        for acc, w in weights.items():
            buckets[acc] = (total_cost * w / weight_sum).quantize(MONEY_Q)

    run_exp = str(run.get('expense_account') or '')
    if '+' in run_exp:
        raise ValueError(
            'Kỳ này đã hạch toán thẳng theo bộ phận (Nợ 622/627/641/642) — '
            'không cần phân bổ 08-LĐTL.'
        )
    src = (source_account or run_exp or '642').strip() or '642'
    # Chỉ bút toán phần chuyển khỏi TK nguồn
    move: dict[str, Decimal] = {
        acc: amt for acc, amt in buckets.items() if acc != src and amt > 0
    }
    if not move:
        raise ValueError(
            'Không có khoản cần phân bổ (toàn bộ đã nằm ở TK nguồn %s)' % src
        )

    total_move = sum(move.values())
    date_s = (posting_date or run.get('posting_date') or f'{year}-{month:02d}-28')[:10]
    desc = f'Phân bổ lương 08-LĐTL kỳ {month:02d}/{year}'
    jlines: list[dict] = []
    seq = 1
    for acc, amt in sorted(move.items()):
        jlines.append({
            'sequence': seq, 'account_code': acc,
            'debit': float(amt), 'credit': 0, 'description': desc,
        })
        seq += 1
    jlines.append({
        'sequence': seq, 'account_code': src,
        'debit': 0, 'credit': float(total_move), 'description': desc,
    })

    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, run.get('branch_code'))
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='08LDTL',
        document_no=f'PB{year}{month:02d}',
        document_id=int(run['id']),
        business_type='PHAN_BO_LUONG',
        description=desc,
        created_by=created_by,
        branch_code=branch,
        lines=jlines,
    )
    conn.execute(
        'UPDATE sme_payroll_runs SET allocation_journal_id = ? WHERE id = ?',
        (entry['id'], run['id']),
    )
    if commit:
        conn.commit()
    out = payroll_allocation_summary(conn, month=month, year=year)
    out['allocation_journal_id'] = entry['id']
    out['allocated'] = {k: float(v) for k, v in buckets.items()}
    out['moved_from'] = src
    out['moved_amount'] = float(total_move)
    return out


def salary_sheet_01(
    conn: sqlite3.Connection,
    *,
    month: int,
    year: int,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Dữ liệu in mẫu 01-LĐTL — bảng thanh toán tiền lương."""
    data = payroll_allocation_summary(
        conn, month=month, year=year, branch_code=branch_code,
    )
    run = data.get('run') or {}
    return {
        **data,
        'form_code': '01-LĐTL',
        'title': 'BẢNG THANH TOÁN TIỀN LƯƠNG',
        'total_income': float(run.get('total_income') or data['total_gross'] or 0),
        'total_deduct': float(run.get('total_deduct') or 0),
        'status': run.get('status') or ('posted' if data['lines'] else 'empty'),
    }
