"""Tính công nợ phải trả nhân viên từ bảng lương (hybrid: trả cả kỳ + trả lẻ)."""


def salary_reference_key(employee_id, month, year):
    return f"SALARY|{int(employee_id)}|{int(month)}|{int(year)}"


def period_reference_key(month, year):
    return f"SALARY|PERIOD|{int(month)}|{int(year)}"


def _period_payroll_total(conn, month, year):
    row = conn.execute(
        """
        SELECT COALESCE(SUM(final_amount), 0) AS total,
               COUNT(*) AS employee_count
        FROM salary_detail
        WHERE month = ? AND year = ?
        """,
        (int(month), int(year)),
    ).fetchone()
    if not row:
        return 0.0, 0
    return float(row['total'] or 0), int(row['employee_count'] or 0)


def _period_all_paid(conn, month, year):
    """Tổng đã trả cho kỳ lương (phiếu chi gộp + trả lẻ từng NV)."""
    m, y = int(month), int(year)
    period_ref = period_reference_key(m, y)
    key = f'{m}/{y}'
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS paid
        FROM phieu_chi
        WHERE (source_type = 'salary' OR expense_type = 'CP_LUONG')
          AND (
            reference_document = ?
            OR reference_document LIKE ?
            OR (
              reason LIKE ?
              AND (
                reference_document IS NULL
                OR reference_document = 'Bảng Lương 05-LĐTL'
              )
            )
          )
        """,
        (period_ref, f'SALARY|%{m}|{y}', f'%{key}%'),
    ).fetchone()
    return float(row['paid'] or 0)


def _period_payment_voucher(conn, month, year):
    """Phiếu chi gộp gần nhất của kỳ (nếu có)."""
    m, y = int(month), int(year)
    period_ref = period_reference_key(m, y)
    row = conn.execute(
        """
        SELECT voucher_no, id
        FROM phieu_chi
        WHERE (source_type = 'salary' OR expense_type = 'CP_LUONG')
          AND reference_document = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (period_ref,),
    ).fetchone()
    if row:
        return row['voucher_no'], row['id']
    key = f'{m}/{y}'
    row = conn.execute(
        """
        SELECT voucher_no, id
        FROM phieu_chi
        WHERE (source_type = 'salary' OR expense_type = 'CP_LUONG')
          AND reason LIKE ?
          AND (reference_document IS NULL OR reference_document = 'Bảng Lương 05-LĐTL')
        ORDER BY id DESC
        LIMIT 1
        """,
        (f'%{key}%',),
    ).fetchone()
    if row:
        return row['voucher_no'], row['id']
    return None, None


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


def _employee_salary_paid(conn, employee_id, month, year, final_amount=None):
    """Số tiền đã trả cho NV trong kỳ (trả lẻ + phân bổ khi kỳ đã trả đủ)."""
    ref = salary_reference_key(employee_id, month, year)
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS paid,
               MAX(voucher_no) AS voucher_no,
               MAX(id) AS phieu_chi_id
        FROM phieu_chi
        WHERE (source_type = 'salary' OR expense_type = 'CP_LUONG')
          AND reference_document = ?
        """,
        (ref,),
    ).fetchone()
    individual_paid = float(row['paid'] or 0) if row else 0.0
    voucher_no = (row['voucher_no'] if row else None) or None
    phieu_chi_id = (row['phieu_chi_id'] if row else None) or None

    if final_amount is None:
        emp_row = conn.execute(
            """
            SELECT final_amount FROM salary_detail
            WHERE employee_id = ? AND month = ? AND year = ?
            """,
            (employee_id, month, year),
        ).fetchone()
        final_amount = float(emp_row['final_amount'] or 0) if emp_row else 0.0

    period_total, _ = _period_payroll_total(conn, month, year)
    period_paid = _period_all_paid(conn, month, year)

    if period_total > 0 and period_paid >= period_total - 0.01:
        if not voucher_no:
            voucher_no, phieu_chi_id = _period_payment_voucher(conn, month, year)
        return float(final_amount), voucher_no, phieu_chi_id

    return individual_paid, voucher_no, phieu_chi_id


def get_period_debt_list(conn, include_paid=False):
    """Danh sách kỳ lương và công nợ (luồng chính — trả cả kỳ)."""
    periods = conn.execute(
        """
        SELECT month, year,
               COUNT(*) AS employee_count,
               SUM(final_amount) AS phai_nop
        FROM salary_detail
        GROUP BY year, month
        ORDER BY year DESC, month DESC
        """
    ).fetchall()

    result = []
    total_unpaid = 0.0
    unpaid_count = 0
    for p in periods:
        phai_nop = float(p['phai_nop'] or 0)
        da_nop = _period_all_paid(conn, p['month'], p['year'])
        con_lai = max(0.0, phai_nop - da_nop)
        status, status_class, _ = _salary_status(phai_nop, da_nop)
        voucher_no, phieu_chi_id = _period_payment_voucher(conn, p['month'], p['year'])
        item = {
            'month': int(p['month']),
            'year': int(p['year']),
            'period_label': f"Tháng {int(p['month'])}/{int(p['year'])}",
            'employee_count': int(p['employee_count'] or 0),
            'phai_nop': round(phai_nop),
            'da_nop': round(da_nop),
            'con_lai': round(con_lai),
            'is_paid': con_lai <= 0.01,
            'status': status,
            'status_class': status_class,
            'voucher_no': voucher_no,
            'phieu_chi_id': phieu_chi_id,
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


def get_period_debt_detail(conn, month, year):
    """Chi tiết kỳ lương + danh sách NV (trả lẻ từng người khi cần)."""
    phai_nop, employee_count = _period_payroll_total(conn, month, year)
    if employee_count <= 0:
        return None

    da_nop = _period_all_paid(conn, month, year)
    con_lai = max(0.0, phai_nop - da_nop)
    status, status_class, _ = _salary_status(phai_nop, da_nop)
    voucher_no, phieu_chi_id = _period_payment_voucher(conn, month, year)

    rows = conn.execute(
        """
        SELECT sd.employee_id, sd.fullname, sd.final_amount, e.position, e.phone
        FROM salary_detail sd
        LEFT JOIN employees e ON e.id = sd.employee_id
        WHERE sd.month = ? AND sd.year = ?
        ORDER BY sd.fullname COLLATE NOCASE
        """,
        (int(month), int(year)),
    ).fetchall()

    employees = []
    for row in rows:
        amt = float(row['final_amount'] or 0)
        emp_paid, emp_voucher, emp_pc_id = _employee_salary_paid(
            conn, row['employee_id'], month, year, amt,
        )
        emp_con = max(0.0, amt - emp_paid)
        emp_status, emp_status_class, _ = _salary_status(amt, emp_paid)
        employees.append({
            'employee_id': row['employee_id'],
            'fullname': row['fullname'],
            'position': row['position'],
            'phone': row['phone'],
            'phai_nop': amt,
            'da_nop': round(emp_paid),
            'con_lai': round(emp_con),
            'is_paid': emp_con <= 0.01,
            'status': emp_status,
            'status_class': emp_status_class,
            'voucher_no': emp_voucher,
            'phieu_chi_id': emp_pc_id,
        })

    return {
        'period': {
            'month': int(month),
            'year': int(year),
            'period_label': f"Tháng {int(month)}/{int(year)}",
            'employee_count': employee_count,
            'phai_nop': round(phai_nop),
            'da_nop': round(da_nop),
            'con_lai': round(con_lai),
            'is_paid': con_lai <= 0.01,
            'status': status,
            'status_class': status_class,
            'voucher_no': voucher_no,
            'phieu_chi_id': phieu_chi_id,
        },
        'employees': employees,
    }


def get_total_salary_debt(conn):
    data = get_period_debt_list(conn, include_paid=False)
    return data['summary']['total_unpaid']


def get_employee_debt_summary(conn):
    """NV còn kỳ lương chưa trả đủ (dùng cho lọc nhanh / báo cáo)."""
    employees = conn.execute(
        """
        SELECT DISTINCT e.id AS employee_id, e.fullname, e.position, e.phone
        FROM salary_detail sd
        JOIN employees e ON e.id = sd.employee_id
        ORDER BY e.fullname COLLATE NOCASE
        """
    ).fetchall()

    result = []
    for emp in employees:
        employee_id = emp['employee_id']
        periods = conn.execute(
            """
            SELECT month, year, final_amount
            FROM salary_detail
            WHERE employee_id = ?
            ORDER BY year DESC, month DESC
            """,
            (employee_id,),
        ).fetchall()
        unpaid = []
        total_unpaid = 0.0
        for p in periods:
            amt = float(p['final_amount'] or 0)
            paid, _, _ = _employee_salary_paid(conn, employee_id, p['month'], p['year'], amt)
            con_lai = max(0.0, amt - paid)
            if con_lai > 0.01:
                unpaid.append(dict(p))
                total_unpaid += con_lai
        if not unpaid:
            continue
        result.append({
            **dict(emp),
            'unpaid_periods': len(unpaid),
            'total_unpaid': round(total_unpaid),
        })
    return result


def get_employee_debt_detail(conn, employee_id):
    employee = conn.execute(
        'SELECT id, fullname, position, phone FROM employees WHERE id = ?',
        (employee_id,),
    ).fetchone()
    if not employee:
        return None

    rows = conn.execute(
        """
        SELECT id, month, year, final_amount, total_income, total_deduct, date
        FROM salary_detail
        WHERE employee_id = ?
        ORDER BY year DESC, month DESC, id DESC
        """,
        (employee_id,),
    ).fetchall()

    records = []
    total_unpaid = 0
    total_paid = 0
    for row in rows:
        item = dict(row)
        amt = float(item.get('final_amount') or 0)
        da_nop, voucher_no, phieu_chi_id = _employee_salary_paid(
            conn, employee_id, item['month'], item['year'], amt,
        )
        con_lai = max(0.0, amt - da_nop)
        status, status_class, _ = _salary_status(amt, da_nop)
        item['phai_nop'] = amt
        item['da_nop'] = round(da_nop)
        item['con_lai'] = round(con_lai)
        item['is_paid'] = con_lai <= 0.01
        item['period_label'] = f"Tháng {item['month']}/{item['year']}"
        item['status'] = status
        item['status_class'] = status_class
        item['voucher_no'] = voucher_no
        item['phieu_chi_id'] = phieu_chi_id
        if item['is_paid']:
            total_paid += amt
        else:
            total_unpaid += con_lai
        records.append(item)

    return {
        'employee': dict(employee),
        'records': records,
        'summary': {
            'total_unpaid': round(total_unpaid),
            'total_paid': round(total_paid),
            'unpaid_periods': sum(1 for r in records if not r['is_paid']),
        },
    }


def next_phieu_chi_no(cursor):
    cursor.execute(
        "SELECT voucher_no FROM phieu_chi WHERE voucher_no LIKE 'PC%' ORDER BY id DESC LIMIT 1"
    )
    last = cursor.fetchone()
    if last and last['voucher_no']:
        try:
            return f"PC{int(last['voucher_no'][2:]) + 1:06d}"
        except ValueError:
            pass
    return 'PC000001'
