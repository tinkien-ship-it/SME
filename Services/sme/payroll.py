"""Lương SME (TT99/TT58) — chốt bảng lương + hạch toán sổ kép, không dùng phieu_chi HKD."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.employee_payroll_helpers import ensure_payroll_schema
from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry
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
            UNIQUE(month, year)
        )
        """
    )
    if commit:
        conn.commit()


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


def list_payroll_runs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_sme_payroll_schema(conn, commit=False)
    rows = conn.execute(
        """
        SELECT * FROM sme_payroll_runs
        ORDER BY year DESC, month DESC
        LIMIT 60
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_payroll_run(conn: sqlite3.Connection, month: int, year: int) -> dict[str, Any] | None:
    ensure_sme_payroll_schema(conn, commit=False)
    row = conn.execute(
        'SELECT * FROM sme_payroll_runs WHERE month = ? AND year = ?',
        (int(month), int(year)),
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

    # Xoá run cũ + reverse không làm (kỳ ghi đè): xoá meta, journal cũ để lại (audit) — chỉ thay run
    existing = get_payroll_run(conn, month, year)
    desc = f'Trích lương + BH T{month}/{year}'
    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='LUONG',
        document_no=f'L{year}{month:02d}',
        business_type='TRICH_LUONG',
        description=desc,
        reference_document=f'SALARY|{month}|{year}',
        created_by=created_by,
        lines=lines,
    )

    cur.execute('DELETE FROM sme_payroll_runs WHERE month = ? AND year = ?', (month, year))
    cur.execute(
        """
        INSERT INTO sme_payroll_runs (
            month, year, posting_date, expense_account,
            total_income, total_deduct, total_net, employer_insurance,
            journal_entry_id, status, created_by, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,'accrued',?,?)
        """,
        (
            month, year, date_s, exp_acct,
            float(total_income), float(total_deduct), float(total_net), float(employer_total),
            entry['id'], created_by, _now(),
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
