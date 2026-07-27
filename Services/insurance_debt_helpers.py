"""Theo dõi công nợ BH — NLĐ (bảng lương) + Chủ hộ KD nộp (S5 tab 2)."""
from Services.chu_ho_helpers import employee_is_chu_ho, sync_chu_ho_from_business_info

INS_TYPES = ('BHXH', 'BHYT', 'BHTN')
EXPENSE_MAP = {
    'BHXH': 'CP_BHXH',
    'BHYT': 'CP_BHYT',
    'BHTN': 'CP_BHTN',
}
EXPENSE_MAP_CHU = {
    'BHXH': 'CP_BHXH_CHU',
    'BHYT': 'CP_BHYT_CHU',
    'BHTN': 'CP_BHTN_CHU',
}
INS_LABELS = {
    'BHXH': 'Bảo hiểm Xã hội',
    'BHYT': 'Bảo hiểm Y tế',
    'BHTN': 'Bảo hiểm Thất nghiệp',
}


def is_chu_ho_employee(row, conn=None):
    return employee_is_chu_ho(row, conn)


def insurance_reference_key(ins_type, month, year, payer='NLD'):
    return f"INSURANCE|{payer}|{ins_type}|{int(month)}|{int(year)}"


def expense_type_for(ins_type, payer='NLD'):
    return (EXPENSE_MAP_CHU if payer == 'CHU' else EXPENSE_MAP)[ins_type]


def _load_rates(conn):
    info = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
    info = dict(info) if info else {}
    base = float(info.get('base_salary_insurance') or 0)
    return {
        'base_insurance': base,
        'nld_bhxh': float(info.get('rate_bhxh') or 8) / 100,
        'nld_bhyt': float(info.get('rate_bhyt') or 1.5) / 100,
        'nld_bhtn': float(info.get('rate_bhtn') or 1) / 100,
        'chu_bhxh': float(info.get('rate_bhxh_chu') or 17.5) / 100,
        'chu_bhyt': float(info.get('rate_bhyt_chu') or 3) / 100,
        'chu_bhtn': float(info.get('rate_bhtn_chu') or 1) / 100,
    }


def _insurance_paid(conn, ins_type, month, year, payer='NLD'):
    m, y = int(month), int(year)
    exp = expense_type_for(ins_type, payer)
    ref = insurance_reference_key(ins_type, m, y, payer)
    key = f'{m}/{y}'
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS paid,
               MAX(voucher_no) AS voucher_no,
               MAX(id) AS phieu_chi_id
        FROM phieu_chi
        WHERE expense_type = ?
          AND (
            reference_document = ?
            OR (reason LIKE ? AND (reference_document IS NULL OR reference_document NOT LIKE 'INSURANCE|%'))
          )
        """,
        (exp, ref, f'%{key}%'),
    ).fetchone()
    if not row:
        return 0.0, None, None
    return float(row['paid'] or 0), row['voucher_no'], row['phieu_chi_id']


def _status(phai, da):
    phai = float(phai or 0)
    da = float(da or 0)
    con = max(0.0, phai - da)
    if phai <= 0:
        return 'Không phát sinh', 'bg-secondary-subtle text-secondary', con
    if da >= phai - 0.01:
        return 'Đã nộp đủ', 'bg-success-subtle text-success', con
    if da > 0:
        return 'Nộp một phần', 'bg-warning-subtle text-warning', con
    return 'Chưa nộp', 'bg-danger-subtle text-danger', con


def _salary_rows(conn, month, year):
    sync_chu_ho_from_business_info(conn, commit=False)
    return conn.execute(
        """
        SELECT sd.employee_id, sd.fullname, sd.bhxh, sd.bhyt, sd.bhtn,
               e.position, e.is_chu_ho
        FROM salary_detail sd
        LEFT JOIN employees e ON e.id = sd.employee_id
        WHERE sd.month = ? AND sd.year = ?
        ORDER BY sd.fullname COLLATE NOCASE
        """,
        (int(month), int(year)),
    ).fetchall()


def _type_row(conn, ins_type, phai, month, year, payer='NLD'):
    phai = round(float(phai or 0))
    da, voucher_no, pc_id = _insurance_paid(conn, ins_type, month, year, payer=payer)
    da = round(da)
    con = max(0.0, phai - da)
    st, st_cls, _ = _status(phai, da)
    return {
        'ins_type': ins_type,
        'expense_type': expense_type_for(ins_type, payer),
        'payer': payer,
        'label': INS_LABELS[ins_type],
        'phai_nop': phai,
        'da_nop': da,
        'con_lai': round(con),
        'is_paid': con <= 0.01,
        'status': st,
        'status_class': st_cls,
        'voucher_no': voucher_no,
        'phieu_chi_id': pc_id,
    }


def compute_period_insurance_nld(conn, month, year):
    """BH phải nộp — phần NLĐ (khấu trừ trên bảng lương)."""
    rows = _salary_rows(conn, month, year)
    if not rows:
        return None

    totals = {t: 0.0 for t in INS_TYPES}
    employees = []
    chu_ho_count = 0

    for row in rows:
        item = dict(row)
        chu_ho = is_chu_ho_employee(item, conn)
        if chu_ho:
            chu_ho_count += 1
        nld = {
            'BHXH': float(item.get('bhxh') or 0),
            'BHYT': float(item.get('bhyt') or 0),
            'BHTN': 0.0 if chu_ho else float(item.get('bhtn') or 0),
        }
        emp_types = {t: round(nld[t]) for t in INS_TYPES}
        for t in INS_TYPES:
            totals[t] += nld[t]
        employees.append({
            'employee_id': item['employee_id'],
            'fullname': item['fullname'],
            'position': item.get('position'),
            'is_chu_ho': chu_ho,
            'nld': emp_types,
            'types': emp_types,
            'total': sum(emp_types.values()),
        })

    type_rows = [_type_row(conn, t, totals[t], month, year, payer='NLD') for t in INS_TYPES]
    return _wrap_period(month, year, type_rows, employees, chu_ho_count, payer='NLD',
                       payer_label='NLĐ (khấu trừ lương)', base_insurance=0)


def compute_period_insurance_chu(conn, month, year):
    """BH phải nộp — phần Chủ hộ kinh doanh (S5 tab 2)."""
    rows = _salary_rows(conn, month, year)
    if not rows:
        return None

    rates = _load_rates(conn)
    base = rates['base_insurance']
    totals = {t: 0.0 for t in INS_TYPES}
    employees = []
    chu_ho_count = 0

    for row in rows:
        item = dict(row)
        chu_ho = is_chu_ho_employee(item, conn)
        if chu_ho:
            chu_ho_count += 1
        chu = {
            'BHXH': round(base * rates['chu_bhxh']),
            'BHYT': round(base * rates['chu_bhyt']),
            'BHTN': 0.0 if chu_ho else round(base * rates['chu_bhtn']),
        }
        emp_types = {t: round(chu[t]) for t in INS_TYPES}
        for t in INS_TYPES:
            totals[t] += chu[t]
        employees.append({
            'employee_id': item['employee_id'],
            'fullname': item['fullname'],
            'position': item.get('position'),
            'is_chu_ho': chu_ho,
            'chu': emp_types,
            'types': emp_types,
            'total': sum(emp_types.values()),
        })

    type_rows = [_type_row(conn, t, totals[t], month, year, payer='CHU') for t in INS_TYPES]
    return _wrap_period(month, year, type_rows, employees, chu_ho_count, payer='CHU',
                       payer_label='Chủ hộ KD nộp', base_insurance=base)


def _wrap_period(month, year, type_rows, employees, chu_ho_count, payer, payer_label, base_insurance):
    total_phai = sum(t['phai_nop'] for t in type_rows)
    total_da = sum(t['da_nop'] for t in type_rows)
    total_con = max(0.0, total_phai - total_da)
    period_status, period_status_class, _ = _status(total_phai, total_da)
    return {
        'month': int(month),
        'year': int(year),
        'period_label': f'Tháng {int(month)}/{int(year)}',
        'payer': payer,
        'payer_label': payer_label,
        'employee_count': len(employees),
        'chu_ho_count': chu_ho_count,
        'base_insurance': base_insurance,
        'types': type_rows,
        'employees': employees,
        'summary': {
            'total_phai_nop': round(total_phai),
            'total_da_nop': round(total_da),
            'total_con_lai': round(total_con),
            'is_paid': total_con <= 0.01,
            'status': period_status,
            'status_class': period_status_class,
        },
    }


def compute_period_insurance_combined(conn, month, year):
    """Gộp NLĐ + Chủ hộ cho trang Công nợ BH và đồng bộ S5."""
    nld = compute_period_insurance_nld(conn, month, year)
    chu = compute_period_insurance_chu(conn, month, year)
    if not nld and not chu:
        return None
    nld = nld or {'types': [], 'summary': {}, 'employees': [], 'employee_count': 0, 'chu_ho_count': 0}
    chu = chu or {'types': [], 'summary': {}, 'employees': [], 'employee_count': 0, 'chu_ho_count': 0, 'base_insurance': 0}

    nld_map = {t['ins_type']: t for t in nld['types']}
    chu_map = {t['ins_type']: t for t in chu['types']}
    combined_types = []
    for ins in INS_TYPES:
        nl = nld_map.get(ins, {})
        ch = chu_map.get(ins, {})
        phai = round(float(nl.get('phai_nop') or 0) + float(ch.get('phai_nop') or 0))
        da = round(float(nl.get('da_nop') or 0) + float(ch.get('da_nop') or 0))
        con = max(0.0, phai - da)
        st, st_cls, _ = _status(phai, da)
        combined_types.append({
            'ins_type': ins,
            'label': INS_LABELS[ins],
            'nld_phai_nop': round(float(nl.get('phai_nop') or 0)),
            'nld_da_nop': round(float(nl.get('da_nop') or 0)),
            'nld_con_lai': round(float(nl.get('con_lai') or 0)),
            'chu_phai_nop': round(float(ch.get('phai_nop') or 0)),
            'chu_da_nop': round(float(ch.get('da_nop') or 0)),
            'chu_con_lai': round(float(ch.get('con_lai') or 0)),
            'phai_nop': phai,
            'da_nop': da,
            'con_lai': round(con),
            'is_paid': con <= 0.01,
            'status': st,
            'status_class': st_cls,
            'nld': nl,
            'chu': ch,
        })

    emp_map = {}
    for e in nld.get('employees') or []:
        emp_map[e['employee_id']] = {
            'employee_id': e['employee_id'],
            'fullname': e['fullname'],
            'position': e.get('position'),
            'is_chu_ho': e.get('is_chu_ho'),
            'nld': e.get('nld') or e.get('types') or {},
            'chu': {'BHXH': 0, 'BHYT': 0, 'BHTN': 0},
        }
    for e in chu.get('employees') or []:
        if e['employee_id'] not in emp_map:
            emp_map[e['employee_id']] = {
                'employee_id': e['employee_id'],
                'fullname': e['fullname'],
                'position': e.get('position'),
                'is_chu_ho': e.get('is_chu_ho'),
                'nld': {'BHXH': 0, 'BHYT': 0, 'BHTN': 0},
                'chu': e.get('chu') or e.get('types') or {},
            }
        else:
            emp_map[e['employee_id']]['chu'] = e.get('chu') or e.get('types') or {}

    employees = []
    for e in emp_map.values():
        nld_t = e['nld']
        chu_t = e['chu']
        nld_sum = sum(nld_t.get(t, 0) for t in INS_TYPES)
        chu_sum = sum(chu_t.get(t, 0) for t in INS_TYPES)
        employees.append({
            **e,
            'nld_total': nld_sum,
            'chu_total': chu_sum,
            'total': nld_sum + chu_sum,
        })

    total_phai = sum(t['phai_nop'] for t in combined_types)
    total_da = sum(t['da_nop'] for t in combined_types)
    total_con = max(0.0, total_phai - total_da)
    period_status, period_status_class, _ = _status(total_phai, total_da)

    return {
        'month': int(month),
        'year': int(year),
        'period_label': f'Tháng {int(month)}/{int(year)}',
        'employee_count': max(nld.get('employee_count') or 0, chu.get('employee_count') or 0),
        'chu_ho_count': max(nld.get('chu_ho_count') or 0, chu.get('chu_ho_count') or 0),
        'base_insurance': chu.get('base_insurance') or 0,
        'nld': nld,
        'chu': chu,
        'types': combined_types,
        'employees': employees,
        'summary': {
            'nld_phai_nop': nld['summary'].get('total_phai_nop', 0),
            'nld_da_nop': nld['summary'].get('total_da_nop', 0),
            'nld_con_lai': nld['summary'].get('total_con_lai', 0),
            'chu_phai_nop': chu['summary'].get('total_phai_nop', 0),
            'chu_da_nop': chu['summary'].get('total_da_nop', 0),
            'chu_con_lai': chu['summary'].get('total_con_lai', 0),
            'total_phai_nop': round(total_phai),
            'total_da_nop': round(total_da),
            'total_con_lai': round(total_con),
            'is_paid': total_con <= 0.01,
            'status': period_status,
            'status_class': period_status_class,
        },
    }


def compute_period_insurance(conn, month, year):
    return compute_period_insurance_combined(conn, month, year)


def get_period_payer_detail(conn, month, year, payer='NLD'):
    if payer == 'CHU':
        return compute_period_insurance_chu(conn, month, year)
    return compute_period_insurance_nld(conn, month, year)


def get_insurance_debt_list(conn, year=None, include_paid=False):
    q = "SELECT DISTINCT month, year FROM salary_detail"
    params = []
    if year:
        q += ' WHERE year = ?'
        params.append(int(year))
    q += ' ORDER BY year DESC, month DESC'
    periods = conn.execute(q, params).fetchall()

    result = []
    total_unpaid = 0.0
    nld_unpaid = 0.0
    chu_unpaid = 0.0
    unpaid_periods = 0
    for p in periods:
        data = compute_period_insurance_combined(conn, p['month'], p['year'])
        if not data:
            continue
        s = data['summary']
        if not include_paid and s['is_paid']:
            continue
        if not s['is_paid']:
            unpaid_periods += 1
            total_unpaid += s['total_con_lai']
            nld_unpaid += s['nld_con_lai']
            chu_unpaid += s['chu_con_lai']
        result.append({
            'month': data['month'],
            'year': data['year'],
            'period_label': data['period_label'],
            'employee_count': data['employee_count'],
            'chu_ho_count': data['chu_ho_count'],
            'nld_con_lai': s['nld_con_lai'],
            'chu_con_lai': s['chu_con_lai'],
            'bhxh_con_lai': next(x['con_lai'] for x in data['types'] if x['ins_type'] == 'BHXH'),
            'bhyt_con_lai': next(x['con_lai'] for x in data['types'] if x['ins_type'] == 'BHYT'),
            'bhtn_con_lai': next(x['con_lai'] for x in data['types'] if x['ins_type'] == 'BHTN'),
            **s,
            'types': data['types'],
        })

    return {
        'periods': result,
        'summary': {
            'unpaid_periods': unpaid_periods,
            'total_unpaid': round(total_unpaid),
            'nld_unpaid': round(nld_unpaid),
            'chu_unpaid': round(chu_unpaid),
            'year': int(year) if year else None,
        },
    }


def get_total_insurance_debt(conn, year=None):
    return get_insurance_debt_list(conn, year=year, include_paid=False)['summary']['total_unpaid']
