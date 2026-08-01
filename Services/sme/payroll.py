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


def preview_payroll_grid(
    conn: sqlite3.Connection, month: int, year: int
) -> dict[str, Any]:
    """Lưới lương tháng — cùng logic HKD get_salary (NV + salary_detail + chấm công)."""
    ensure_payroll_schema(conn)
    standard_days = _working_days_exclude_sunday(month, year)

    from Services.attendance_helpers import get_monthly_work_days_map

    attendance_days = get_monthly_work_days_map(conn, month, year)

    query = """
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
    """
    rows = conn.execute(query, (month, year)).fetchall()

    data: list[dict[str, Any]] = []
    numeric_fields = [
        'actual_working_days', 'time_salary',
        'allowance_fund', 'allowance_other', 'bonus',
        'bhxh', 'bhyt', 'bhtn', 'tncn_tax',
        'total_income', 'total_deduct', 'final_amount',
    ]
    for row in rows:
        item = dict(row)
        if item.get('salary_id') is None:
            emp_id = item.get('employee_id')
            work_days = attendance_days.get(emp_id, 0) if emp_id else 0
            item['actual_working_days'] = work_days if work_days > 0 else standard_days
            item['attendance_work_days'] = work_days
            item['time_salary'] = item['base_salary']
            item['allowance_fund'] = float(item.get('emp_allowance_fund') or 0)
            item['allowance_other'] = float(item.get('emp_allowance_other') or 0)
            item['bonus'] = float(item.get('emp_default_bonus') or 0)
            item['bhxh'] = 0
            item['bhyt'] = 0
            item['bhtn'] = 0
            item['tncn_tax'] = 0
            item['total_income'] = (
                item['time_salary'] + item['allowance_fund']
                + item['allowance_other'] + item['bonus']
            )
            item['total_deduct'] = 0
            item['final_amount'] = item['total_income']

        for field in numeric_fields:
            if item.get(field) is None:
                item[field] = 0
            else:
                try:
                    item[field] = float(item[field])
                except (TypeError, ValueError):
                    item[field] = 0
        data.append(item)

    return {'data': data, 'standard_days': standard_days}


def _employer_insurance_from_records(
    conn: sqlite3.Connection, records: list[dict]
) -> tuple[Decimal, dict[str, Decimal]]:
    """BH chủ theo từng NV: căn = lương thời gian (time_salary) hoặc base_salary."""
    from Services.insurance_debt_helpers import _load_rates

    rates = _load_rates(conn)
    parts = {
        'bhxh': Decimal('0.00'),
        'bhyt': Decimal('0.00'),
        'bhtn': Decimal('0.00'),
    }
    for r in records:
        base = _money(r.get('time_salary') or r.get('base_salary') or r.get('salary_rate') or 0)
        if base <= 0:
            continue
        parts['bhxh'] += (base * _money(rates['chu_bhxh'])).quantize(MONEY_Q)
        parts['bhyt'] += (base * _money(rates['chu_bhyt'])).quantize(MONEY_Q)
        parts['bhtn'] += (base * _money(rates['chu_bhtn'])).quantize(MONEY_Q)
    total = parts['bhxh'] + parts['bhyt'] + parts['bhtn']
    return total, parts


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
    Chốt bảng lương kỳ + bút toán:
      Nợ expense (642/622/641) = gross + BH chủ
      Có 3341 = thực lĩnh
      Có 3383/3384/3385 = BH NLĐ + BH chủ (tách loại)
      Có 3335 = TNCN (nếu có)
    """
    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_payroll_schema(conn, commit=False)
    from Services.sme.branches import resolve_posting_branch
    branch = resolve_posting_branch(conn, branch_code)

    month, year = int(month), int(year)
    if not records:
        raise ValueError('Không có dòng lương để chốt')

    date_s = (posting_date or f'{year}-{month:02d}-28')[:10]
    exp_acct = (expense_account or '642').strip() or '642'

    total_income = Decimal('0.00')
    total_deduct = Decimal('0.00')
    total_net = Decimal('0.00')
    emp_bhxh = Decimal('0.00')
    emp_bhyt = Decimal('0.00')
    emp_bhtn = Decimal('0.00')
    emp_tncn = Decimal('0.00')

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

        if 'branch_code' in sd_cols:
            cur.execute(
                """
                INSERT INTO salary_detail (
                    employee_id, fullname, month, year,
                    salary_rate, actual_working_days, time_salary,
                    allowance_fund, allowance_other, bonus,
                    bhxh, bhyt, bhtn, tncn_tax,
                    total_income, total_deduct, final_amount, date, branch_code
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
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
                    branch,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO salary_detail (
                    employee_id, fullname, month, year,
                    salary_rate, actual_working_days, time_salary,
                    allowance_fund, allowance_other, bonus,
                    bhxh, bhyt, bhtn, tncn_tax,
                    total_income, total_deduct, final_amount, date
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
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
                ),
            )

    employer_total, emp_parts = _employer_insurance_from_records(conn, records)
    expense_amt = total_income + employer_total

    # Phân bổ có: 3341 = thực lĩnh; BH = NLĐ + chủ; TNCN
    credit_bhxh = emp_bhxh + emp_parts['bhxh']
    credit_bhyt = emp_bhyt + emp_parts['bhyt']
    credit_bhtn = emp_bhtn + emp_parts['bhtn']

    lines: list[dict] = [
        {
            'sequence': 1,
            'account_code': exp_acct,
            'debit': float(expense_amt),
            'credit': 0,
            'description': f'Chi phí lương T{month}/{year}',
        },
    ]
    seq = 2
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
    """Hủy chốt lương kỳ — đảo bút toán LUONG, đánh dấu run void, xoá salary_detail kỳ."""
    ensure_sme_payroll_schema(conn, commit=False)
    month, year = int(month), int(year)
    run = get_payroll_run(conn, month, year, branch_code=branch_code)
    if not run:
        raise ValueError('Không tìm thấy bảng lương kỳ này')
    if run.get('status') == 'void':
        raise ValueError('Bảng lương đã hủy')
    if run.get('journal_entry_id'):
        reverse_journal_entry(
            conn, int(run['journal_entry_id']),
            created_by=created_by, reason=reason,
        )
    run_id = int(run['id'])
    conn.execute(
        "UPDATE sme_payroll_runs SET status = 'void' WHERE id = ?",
        (run_id,),
    )
    # Chỉ xoá salary_detail của CN này khi không còn run CN khác cùng kỳ… thực ra chỉ xóa CN này
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
    if commit:
        conn.commit()
    return {
        'id': run_id,
        'month': month,
        'year': year,
        'status': 'void',
        'journal_entry_id': run.get('journal_entry_id'),
        'branch_code': run.get('branch_code'),
        'reason': reason,
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
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Trả lương kỳ — phiếu chi SME 02-TT (Nợ 3341 / Có 1111|1121), không ghi phieu_chi HKD."""
    from Services.sme.vouchers import ensure_sme_voucher_schema

    ensure_sme_payroll_schema(conn, commit=False)
    ensure_sme_voucher_schema(conn, commit=False)
    month, year = int(month), int(year)
    run = get_payroll_run(conn, month, year)
    if not run:
        # Cho phép trả nếu đã có salary_detail (chốt qua API cũ) nhưng chưa có run SME
        row = conn.execute(
            """
            SELECT COALESCE(SUM(final_amount), 0) AS net
            FROM salary_detail WHERE month = ? AND year = ?
            """,
            (month, year),
        ).fetchone()
        net = float(row[0] if not isinstance(row, sqlite3.Row) else row['net'])
        if net <= 0:
            raise ValueError('Chưa có bảng lương kỳ này — hãy chốt lương SME trước')
    else:
        net = float(run['total_net'] or 0)

    # Đã trả qua sme_vouchers source salary
    paid_row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS paid
        FROM sme_vouchers
        WHERE voucher_type = 'payment'
          AND source_type = 'salary'
          AND reference_document = ?
        """,
        (f'SALARY|{month}|{year}',),
    ).fetchone()
    paid = float(paid_row[0] if not isinstance(paid_row, sqlite3.Row) else paid_row['paid'])
    remain = max(0.0, net - paid)
    if remain <= 0.01:
        raise ValueError('Kỳ lương này đã thanh toán đủ')

    pay_amt = float(amount) if amount is not None else remain
    if pay_amt <= 0:
        raise ValueError('Số tiền không hợp lệ')
    if pay_amt > remain + 0.01:
        raise ValueError('Số tiền vượt quá còn phải trả')

    date_s = (pay_date or datetime.now().strftime('%Y-%m-%d'))[:10]
    result = create_payment(
        conn,
        voucher_date=date_s,
        party_name=receiver_name or 'Tập thể cán bộ nhân viên',
        amount=pay_amt,
        payment_method=payment_method,
        debit_account='3341',
        reason=f'Thanh toán lương tháng {month}/{year}',
        reference_document=f'SALARY|{month}|{year}',
        source_type='salary',
        source_id=run['id'] if run else None,
        created_by=created_by,
        commit=False,
    )
    if commit:
        conn.commit()
    return {
        **result,
        'month': month,
        'year': year,
        'paid_before': paid,
        'remain_after': round(remain - pay_amt, 2),
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
    created_by: str | None = None,
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
    result = create_payment(
        conn,
        voucher_date=date_s,
        party_name=receiver_name or 'Cơ quan BHXH',
        amount=amt,
        payment_method=payment_method or 'bank',
        debit_account=acc,
        reason=f'Nộp {acc}' + (f' — {reference}' if reference else ''),
        reference_document=reference or f'BH|{acc}|{date_s}',
        source_type='insurance',
        created_by=created_by,
        commit=False,
    )
    if commit:
        conn.commit()
    return {**result, 'form_code': '07-LĐTL', 'account_code': acc}


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


def _expense_account_for_position(position: str | None) -> str:
    p = (position or '').strip().lower()
    if any(k in p for k in ('sx', 'sản xuất', 'san xuat', 'công nhân', 'cong nhan', 'production')):
        return '622'
    if any(k in p for k in ('bán hàng', 'ban hang', 'sales', 'sale', 'kd')):
        return '641'
    if any(k in p for k in ('phân xưởng', 'phan xuong', 'chung', '627')):
        return '627'
    return '642'


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
            acc = _expense_account_for_position(ln.get('position'))
            w = _money(ln.get('total_income'))
            if w <= 0:
                continue
            weights[acc] = weights.get(acc, Decimal('0.00')) + w
        weight_sum = sum(weights.values()) or Decimal('1.00')
        for acc, w in weights.items():
            buckets[acc] = (total_cost * w / weight_sum).quantize(MONEY_Q)

    src = (source_account or run.get('expense_account') or '642').strip() or '642'
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
