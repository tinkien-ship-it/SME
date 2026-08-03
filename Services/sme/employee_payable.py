"""Công nợ phải trả nhân viên SME — logic giống HKD (trả cả kỳ + trả lẻ).

Theo dõi qua ``sme_vouchers`` (phiếu chi 02-TT + nhật ký Nợ 3341 · Có 1111/1121):
- Trả cả kỳ: ``SALARY|PERIOD|{month}|{year}``
- Trả lẻ NV: ``SALARY|{employee_id}|{month}|{year}``
- Legacy: ``SALARY|{month}|{year}``
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.bctc_report import _closing_balances
from Services.sme.journal_engine import ensure_sme_journal_ready

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def salary_reference_key(employee_id: int, month: int, year: int) -> str:
    return f'SALARY|{int(employee_id)}|{int(month)}|{int(year)}'


def period_reference_key(month: int, year: int) -> str:
    return f'SALARY|PERIOD|{int(month)}|{int(year)}'


def legacy_period_reference_key(month: int, year: int) -> str:
    return f'SALARY|{int(month)}|{int(year)}'


def _salary_status(phai_nop, da_nop):
    phai_nop = float(phai_nop or 0)
    da_nop = float(da_nop or 0)
    con_lai = max(0.0, phai_nop - da_nop)
    if phai_nop <= 0:
        return 'Không phát sinh', 'bg-secondary-subtle text-secondary', con_lai
    if da_nop >= phai_nop - 0.01:
        return 'Đã trả đủ', 'bg-success-subtle text-success', con_lai
    if da_nop > 0:
        return 'Trả một phần', 'bg-warning-subtle text-warning', con_lai
    return 'Chưa trả', 'bg-danger-subtle text-danger', con_lai


def _period_payroll_total(conn: sqlite3.Connection, month: int, year: int) -> tuple[float, int]:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(final_amount), 0) AS total, COUNT(*) AS employee_count
        FROM salary_detail WHERE month = ? AND year = ?
        """,
        (int(month), int(year)),
    ).fetchone()
    if not row:
        return 0.0, 0
    total = row['total'] if hasattr(row, 'keys') else row[0]
    count = row['employee_count'] if hasattr(row, 'keys') else row[1]
    return float(total or 0), int(count or 0)


def _period_all_paid(conn: sqlite3.Connection, month: int, year: int) -> float:
    """Tổng đã trả kỳ (phiếu gộp + trả lẻ + legacy)."""
    m, y = int(month), int(year)
    period_ref = period_reference_key(m, y)
    legacy_ref = legacy_period_reference_key(m, y)
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS paid
            FROM sme_vouchers
            WHERE voucher_type = 'payment'
              AND COALESCE(status, 'posted') != 'void'
              AND source_type = 'salary'
              AND (
                    reference_document = ?
                 OR reference_document = ?
                 OR reference_document LIKE ?
              )
            """,
            (period_ref, legacy_ref, f'SALARY|%|{m}|{y}'),
        ).fetchone()
        return float(row[0] if not hasattr(row, 'keys') else row['paid'] or 0)
    except sqlite3.Error:
        return 0.0


def _period_payment_voucher(
    conn: sqlite3.Connection, month: int, year: int,
) -> tuple[str | None, int | None]:
    m, y = int(month), int(year)
    for ref in (period_reference_key(m, y), legacy_period_reference_key(m, y)):
        try:
            row = conn.execute(
                """
                SELECT voucher_no, id FROM sme_vouchers
                WHERE voucher_type = 'payment'
                  AND COALESCE(status, 'posted') != 'void'
                  AND source_type = 'salary'
                  AND reference_document = ?
                ORDER BY id DESC LIMIT 1
                """,
                (ref,),
            ).fetchone()
            if row:
                return (
                    row['voucher_no'] if hasattr(row, 'keys') else row[0],
                    int(row['id'] if hasattr(row, 'keys') else row[1]),
                )
        except sqlite3.Error:
            pass
    return None, None


def _employee_salary_paid(
    conn: sqlite3.Connection,
    employee_id: int,
    month: int,
    year: int,
    final_amount: float | None = None,
) -> tuple[float, str | None, int | None]:
    ref = salary_reference_key(employee_id, month, year)
    individual_paid = 0.0
    voucher_no = None
    voucher_id = None
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS paid,
                   MAX(voucher_no) AS voucher_no,
                   MAX(id) AS id
            FROM sme_vouchers
            WHERE voucher_type = 'payment'
              AND COALESCE(status, 'posted') != 'void'
              AND source_type = 'salary'
              AND reference_document = ?
            """,
            (ref,),
        ).fetchone()
        if row:
            individual_paid = float(row['paid'] if hasattr(row, 'keys') else row[0] or 0)
            voucher_no = row['voucher_no'] if hasattr(row, 'keys') else row[1]
            voucher_id = int(row['id']) if (hasattr(row, 'keys') and row['id']) else (
                int(row[2]) if row and len(row) > 2 and row[2] else None
            )
    except sqlite3.Error:
        pass

    if final_amount is None:
        emp_row = conn.execute(
            """
            SELECT final_amount FROM salary_detail
            WHERE employee_id = ? AND month = ? AND year = ?
            """,
            (employee_id, month, year),
        ).fetchone()
        final_amount = float(
            (emp_row['final_amount'] if emp_row and hasattr(emp_row, 'keys') else (emp_row[0] if emp_row else 0)) or 0
        )

    period_total, _ = _period_payroll_total(conn, month, year)
    period_paid = _period_all_paid(conn, month, year)
    # Kỳ đã trả đủ bằng phiếu gộp → coi mỗi NV đã nhận đủ thực lĩnh
    if period_total > 0 and period_paid >= period_total - 0.01:
        if not voucher_no:
            voucher_no, voucher_id = _period_payment_voucher(conn, month, year)
        return float(final_amount), voucher_no, voucher_id

    return individual_paid, voucher_no, voucher_id


def get_period_debt_list(
    conn: sqlite3.Connection, *, include_paid: bool = False,
) -> dict[str, Any]:
    periods = conn.execute(
        """
        SELECT month, year, COUNT(*) AS employee_count, SUM(final_amount) AS phai_nop
        FROM salary_detail
        GROUP BY year, month
        ORDER BY year DESC, month DESC
        """
    ).fetchall()

    result = []
    total_unpaid = 0.0
    unpaid_count = 0
    for p in periods:
        month = int(p['month'] if hasattr(p, 'keys') else p[0])
        year = int(p['year'] if hasattr(p, 'keys') else p[1])
        emp_count = int(p['employee_count'] if hasattr(p, 'keys') else p[2] or 0)
        phai_nop = float(p['phai_nop'] if hasattr(p, 'keys') else p[3] or 0)
        da_nop = _period_all_paid(conn, month, year)
        con_lai = max(0.0, phai_nop - da_nop)
        status, status_class, _ = _salary_status(phai_nop, da_nop)
        voucher_no, voucher_id = _period_payment_voucher(conn, month, year)
        item = {
            'month': month,
            'year': year,
            'period_label': f'Tháng {month}/{year}',
            'employee_count': emp_count,
            'phai_nop': round(phai_nop),
            'da_nop': round(da_nop),
            'con_lai': round(con_lai),
            'is_paid': con_lai <= 0.01,
            'status': status,
            'status_class': status_class,
            'voucher_no': voucher_no,
            'voucher_id': voucher_id,
            'phieu_chi_id': voucher_id,
        }
        if not include_paid and item['is_paid']:
            continue
        if not item['is_paid']:
            total_unpaid += con_lai
            unpaid_count += 1
        result.append(item)

    return {
        'periods': result,
        'summary': {
            'unpaid_periods': unpaid_count,
            'total_unpaid': round(total_unpaid),
        },
    }


def get_period_debt_detail(
    conn: sqlite3.Connection, month: int, year: int,
) -> dict[str, Any] | None:
    phai_nop, employee_count = _period_payroll_total(conn, month, year)
    if employee_count <= 0:
        return None
    da_nop = _period_all_paid(conn, month, year)
    con_lai = max(0.0, phai_nop - da_nop)
    status, status_class, _ = _salary_status(phai_nop, da_nop)
    voucher_no, voucher_id = _period_payment_voucher(conn, month, year)

    rows = conn.execute(
        """
        SELECT sd.employee_id, sd.fullname, sd.final_amount, e.position
        FROM salary_detail sd
        LEFT JOIN employees e ON e.id = sd.employee_id
        WHERE sd.month = ? AND sd.year = ?
        ORDER BY sd.fullname COLLATE NOCASE
        """,
        (int(month), int(year)),
    ).fetchall()

    employees = []
    for r in rows:
        item = dict(r)
        emp_id = int(item['employee_id'])
        final_amt = float(item.get('final_amount') or 0)
        paid, vno, vid = _employee_salary_paid(conn, emp_id, month, year, final_amt)
        emp_con = max(0.0, final_amt - paid)
        st, st_cls, _ = _salary_status(final_amt, paid)
        employees.append({
            'employee_id': emp_id,
            'fullname': item.get('fullname'),
            'position': item.get('position'),
            'phai_nop': round(final_amt),
            'da_nop': round(paid),
            'con_lai': round(emp_con),
            'is_paid': emp_con <= 0.01,
            'status': st,
            'status_class': st_cls,
            'voucher_no': vno,
            'voucher_id': vid,
        })

    return {
        'period': {
            'month': int(month),
            'year': int(year),
            'period_label': f'Tháng {int(month)}/{int(year)}',
            'employee_count': employee_count,
            'phai_nop': round(phai_nop),
            'da_nop': round(da_nop),
            'con_lai': round(con_lai),
            'status': status,
            'status_class': status_class,
            'voucher_no': voucher_no,
            'voucher_id': voucher_id,
        },
        'employees': employees,
    }


def employee_payable_summary(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Số dư Có TK 334 / 334x (lương và phải trả nhân viên) — bổ sung sổ kép."""
    ensure_sme_journal_ready(conn, commit=False)
    bals = _closing_balances(conn, fiscal_year, period, branch_code=branch_code)
    coa = {}
    try:
        rows = conn.execute(
            "SELECT code, name FROM sme_chart_of_accounts WHERE is_active = 1"
        ).fetchall()
        coa = {r[0]: r[1] for r in rows}
    except sqlite3.Error:
        pass

    lines = []
    total = Decimal('0.00')
    for code in sorted(bals.keys()):
        if not (code == '334' or code.startswith('334')):
            continue
        bal = bals[code]
        net = _money(bal.get('credit')) - _money(bal.get('debit'))
        if net == 0:
            continue
        lines.append({
            'account_code': code,
            'name': coa.get(code) or code,
            'debit': _f(bal.get('debit')),
            'credit': _f(bal.get('credit')),
            'balance': _f(net),
        })
        total += net

    return {
        'fiscal_year': fiscal_year,
        'period': period,
        'account_prefix': '334',
        'lines': lines,
        'total': _f(total),
        'hint': 'Số dư Có TK 334* = lương / phải trả nhân viên trên sổ kép SME.',
        'branch_code': branch_code or 'ALL',
    }
