"""Công nợ / nộp BHXH · BHYT · BHTN · KPCĐ cho SME (07-LĐTL).

Logic tương tự HKD ``insurance_debt_helpers`` nhưng:
- Phải nộp NLĐ lấy từ ``salary_detail`` (đã chốt bảng lương)
- Phải nộp DN (người SDLĐ) = lương TG × tỷ lệ chủ (cùng công thức chốt lương)
- KPCĐ DN = 2% quỹ lương (TK 3382)
- Đã nộp theo dõi qua ``sme_vouchers`` (phiếu chi + nhật ký Nợ 338x · Có 1111/1121)
- Hạch toán tách từng khoản: 3383 BHXH · 3384 BHYT · 3385 BHTN · 3382 KPCĐ
"""
from __future__ import annotations

import sqlite3
from typing import Any

from Services.chu_ho_helpers import employee_is_chu_ho, sync_chu_ho_from_business_info
from Services.insurance_debt_helpers import INS_LABELS, INS_TYPES, _load_rates, _status

# TK hạch toán SME (TT99)
ACCOUNT_BY_TYPE = {
    'BHXH': '3383',
    'BHYT': '3384',
    'BHTN': '3385',
    'KPCD': '3382',
}

SME_INS_LABELS = {
    **INS_LABELS,
    'KPCD': 'Kinh phí công đoàn',
}

PAYER_NLD = 'NLD'
PAYER_DN = 'DN'  # Doanh nghiệp / người SDLĐ (HKD gọi Chủ hộ)


def insurance_reference_key(ins_type: str, month: int, year: int, payer: str = PAYER_NLD) -> str:
    return f'INSURANCE|{payer}|{ins_type}|{int(month)}|{int(year)}'


def period_reference_key(month: int, year: int) -> str:
    return f'INSURANCE|PERIOD|{int(month)}|{int(year)}'


def ensure_insurance_alloc_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    """Phân bổ số nộp theo loại BH / NLĐ|DN khi nộp cả kỳ bằng 1 phiếu chi."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_insurance_pay_alloc (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_id INTEGER NOT NULL,
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            ins_type TEXT NOT NULL,
            payer TEXT NOT NULL,
            account_code TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sme_ins_alloc_period
        ON sme_insurance_pay_alloc(year, month, ins_type, payer)
        """
    )
    if commit:
        conn.commit()


def _salary_rows(conn: sqlite3.Connection, month: int, year: int) -> list[dict]:
    try:
        sync_chu_ho_from_business_info(conn, commit=False)
    except Exception:
        pass
    cols = {r[1] for r in conn.execute('PRAGMA table_info(salary_detail)').fetchall()}
    income_expr = (
        'COALESCE(sd.total_income, sd.time_salary, 0)'
        if 'total_income' in cols else
        'COALESCE(sd.time_salary, 0)'
    )
    rows = conn.execute(
        f"""
        SELECT sd.employee_id, sd.fullname,
               COALESCE(sd.time_salary, 0) AS time_salary,
               {income_expr} AS total_income,
               COALESCE(sd.bhxh, 0) AS bhxh,
               COALESCE(sd.bhyt, 0) AS bhyt,
               COALESCE(sd.bhtn, 0) AS bhtn,
               e.position, e.is_chu_ho
        FROM salary_detail sd
        LEFT JOIN employees e ON e.id = sd.employee_id
        WHERE sd.month = ? AND sd.year = ?
        ORDER BY sd.fullname COLLATE NOCASE
        """,
        (int(month), int(year)),
    ).fetchall()
    return [dict(r) for r in rows]


def _paid_simple(
    conn: sqlite3.Connection,
    ins_type: str,
    month: int,
    year: int,
    payer: str,
) -> tuple[float, str | None, int | None]:
    """Đã nộp: phiếu đơn (reference) + phân bổ từ phiếu nộp cả kỳ."""
    ensure_insurance_alloc_schema(conn, commit=False)
    ref = insurance_reference_key(ins_type, month, year, payer)
    refs = [ref]
    if payer == PAYER_DN:
        refs.append(insurance_reference_key(ins_type, month, year, 'CHU'))

    paid = 0.0
    voucher_no = None
    voucher_id = None
    try:
        for rkey in refs:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS paid,
                       MAX(voucher_no) AS voucher_no,
                       MAX(id) AS id
                FROM sme_vouchers
                WHERE voucher_type = 'payment'
                  AND COALESCE(status, 'posted') != 'void'
                  AND reference_document = ?
                """,
                (rkey,),
            ).fetchone()
            if row:
                paid += float(row[0] or 0)
                if row[1]:
                    voucher_no = row[1]
                if row[2]:
                    voucher_id = int(row[2])
    except sqlite3.Error:
        pass

    # Phân bổ từ phiếu nộp cả kỳ (1 PC chung)
    payers = [payer]
    if payer == PAYER_DN:
        payers.append('CHU')
    try:
        placeholders = ','.join('?' for _ in payers)
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(a.amount), 0) AS paid,
                   MAX(v.voucher_no) AS voucher_no,
                   MAX(a.voucher_id) AS id
            FROM sme_insurance_pay_alloc a
            JOIN sme_vouchers v ON v.id = a.voucher_id
            WHERE a.month = ? AND a.year = ?
              AND a.ins_type = ?
              AND a.payer IN ({placeholders})
              AND COALESCE(v.status, 'posted') != 'void'
            """,
            (int(month), int(year), ins_type, *payers),
        ).fetchone()
        if row:
            paid += float(row[0] or 0)
            if row[1]:
                voucher_no = row[1]
            if row[2]:
                voucher_id = int(row[2])
    except sqlite3.Error:
        pass

    # Fallback: phiếu cũ không có reference chuẩn — chỉ gán vào NLD để tránh đếm kép
    if paid <= 0 and payer == PAYER_NLD:
        acct = ACCOUNT_BY_TYPE.get(ins_type, '338')
        period_tag = f'{int(month)}/{int(year)}'
        try:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS paid,
                       MAX(voucher_no) AS voucher_no,
                       MAX(id) AS id
                FROM sme_vouchers
                WHERE voucher_type = 'payment'
                  AND COALESCE(status, 'posted') != 'void'
                  AND source_type = 'insurance'
                  AND debit_account = ?
                  AND (reference_document IS NULL OR reference_document NOT LIKE 'INSURANCE|%')
                  AND (reason LIKE ? OR reason LIKE ?)
                """,
                (acct, f'%{period_tag}%', f'%T{int(month):02d}/{int(year)}%'),
            ).fetchone()
            if row and float(row[0] or 0) > 0:
                paid = float(row[0] or 0)
                voucher_no = row[1]
                voucher_id = int(row[2]) if row[2] else None
        except sqlite3.Error:
            pass

    return round(paid), voucher_no, voucher_id


def _type_row(
    conn: sqlite3.Connection,
    ins_type: str,
    phai: float,
    month: int,
    year: int,
    payer: str,
) -> dict[str, Any]:
    phai = round(float(phai or 0))
    da, voucher_no, vid = _paid_simple(conn, ins_type, month, year, payer)
    da = round(da)
    con = max(0.0, phai - da)
    st, st_cls, _ = _status(phai, da)
    return {
        'ins_type': ins_type,
        'account_code': ACCOUNT_BY_TYPE[ins_type],
        'payer': payer,
        'label': SME_INS_LABELS.get(ins_type, ins_type),
        'phai_nop': phai,
        'da_nop': da,
        'con_lai': round(con),
        'is_paid': con <= 0.01,
        'status': st,
        'status_class': st_cls,
        'voucher_no': voucher_no,
        'voucher_id': vid,
    }


def compute_period_insurance_nld(conn: sqlite3.Connection, month: int, year: int) -> dict | None:
    rows = _salary_rows(conn, month, year)
    if not rows:
        return None
    totals = {t: 0.0 for t in INS_TYPES}
    employees = []
    chu_ho_count = 0
    for item in rows:
        chu_ho = employee_is_chu_ho(item, conn)
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
    type_rows = [_type_row(conn, t, totals[t], month, year, PAYER_NLD) for t in INS_TYPES]
    total_phai = sum(t['phai_nop'] for t in type_rows)
    total_da = sum(t['da_nop'] for t in type_rows)
    total_con = max(0.0, total_phai - total_da)
    st, st_cls, _ = _status(total_phai, total_da)
    return {
        'month': int(month),
        'year': int(year),
        'payer': PAYER_NLD,
        'payer_label': 'NLĐ (khấu trừ lương)',
        'employee_count': len(employees),
        'chu_ho_count': chu_ho_count,
        'types': type_rows,
        'employees': employees,
        'summary': {
            'total_phai_nop': round(total_phai),
            'total_da_nop': round(total_da),
            'total_con_lai': round(total_con),
            'is_paid': total_con <= 0.01,
            'status': st,
            'status_class': st_cls,
        },
    }


def compute_period_insurance_dn(conn: sqlite3.Connection, month: int, year: int) -> dict | None:
    """BH phần DN — lương TG × tỷ lệ chủ (khớp bút toán chốt lương)."""
    rows = _salary_rows(conn, month, year)
    if not rows:
        return None
    rates = _load_rates(conn)
    totals = {t: 0.0 for t in INS_TYPES}
    employees = []
    chu_ho_count = 0
    for item in rows:
        chu_ho = employee_is_chu_ho(item, conn)
        if chu_ho:
            chu_ho_count += 1
        base = float(item.get('time_salary') or 0)
        dn = {
            'BHXH': round(base * rates['chu_bhxh']),
            'BHYT': round(base * rates['chu_bhyt']),
            'BHTN': 0.0 if chu_ho else round(base * rates['chu_bhtn']),
        }
        emp_types = {t: round(dn[t]) for t in INS_TYPES}
        for t in INS_TYPES:
            totals[t] += dn[t]
        employees.append({
            'employee_id': item['employee_id'],
            'fullname': item['fullname'],
            'position': item.get('position'),
            'is_chu_ho': chu_ho,
            'chu': emp_types,  # giữ key chu để UI HKD-compatible
            'dn': emp_types,
            'types': emp_types,
            'total': sum(emp_types.values()),
        })
    type_rows = [_type_row(conn, t, totals[t], month, year, PAYER_DN) for t in INS_TYPES]
    from Services.sme.ledger_ops import KPCD_EMPLOYER_RATE
    kpcd_base = sum(float(item.get('total_income') or item.get('time_salary') or 0) for item in rows)
    kpcd_amt = round(kpcd_base * float(KPCD_EMPLOYER_RATE))
    if kpcd_amt > 0:
        type_rows.append(_type_row(conn, 'KPCD', kpcd_amt, month, year, PAYER_DN))
    total_phai = sum(t['phai_nop'] for t in type_rows)
    total_da = sum(t['da_nop'] for t in type_rows)
    total_con = max(0.0, total_phai - total_da)
    st, st_cls, _ = _status(total_phai, total_da)
    return {
        'month': int(month),
        'year': int(year),
        'payer': PAYER_DN,
        'payer_label': 'DN (người SDLĐ)',
        'employee_count': len(employees),
        'chu_ho_count': chu_ho_count,
        'types': type_rows,
        'employees': employees,
        'summary': {
            'total_phai_nop': round(total_phai),
            'total_da_nop': round(total_da),
            'total_con_lai': round(total_con),
            'is_paid': total_con <= 0.01,
            'status': st,
            'status_class': st_cls,
        },
    }


def compute_period_insurance(conn: sqlite3.Connection, month: int, year: int) -> dict | None:
    nld = compute_period_insurance_nld(conn, month, year)
    dn = compute_period_insurance_dn(conn, month, year)
    if not nld and not dn:
        return None
    nld = nld or {'types': [], 'summary': {}, 'employees': [], 'employee_count': 0, 'chu_ho_count': 0}
    dn = dn or {'types': [], 'summary': {}, 'employees': [], 'employee_count': 0, 'chu_ho_count': 0}

    nld_map = {t['ins_type']: t for t in nld['types']}
    dn_map = {t['ins_type']: t for t in dn['types']}
    combined_types = []
    type_order = list(INS_TYPES) + (['KPCD'] if 'KPCD' in dn_map else [])
    for ins in type_order:
        nl = nld_map.get(ins, {})
        ch = dn_map.get(ins, {})
        phai = round(float(nl.get('phai_nop') or 0) + float(ch.get('phai_nop') or 0))
        da = round(float(nl.get('da_nop') or 0) + float(ch.get('da_nop') or 0))
        con = max(0.0, phai - da)
        st, st_cls, _ = _status(phai, da)
        combined_types.append({
            'ins_type': ins,
            'label': SME_INS_LABELS.get(ins, ins),
            'account_code': ACCOUNT_BY_TYPE.get(ins, '338'),
            'nld_phai_nop': round(float(nl.get('phai_nop') or 0)),
            'nld_da_nop': round(float(nl.get('da_nop') or 0)),
            'nld_con_lai': round(float(nl.get('con_lai') or 0)),
            'chu_phai_nop': round(float(ch.get('phai_nop') or 0)),  # alias UI
            'chu_da_nop': round(float(ch.get('da_nop') or 0)),
            'chu_con_lai': round(float(ch.get('con_lai') or 0)),
            'dn_phai_nop': round(float(ch.get('phai_nop') or 0)),
            'dn_da_nop': round(float(ch.get('da_nop') or 0)),
            'dn_con_lai': round(float(ch.get('con_lai') or 0)),
            'phai_nop': phai,
            'da_nop': da,
            'con_lai': round(con),
            'is_paid': con <= 0.01,
            'status': st,
            'status_class': st_cls,
            'nld': nl,
            'dn': ch,
            'chu': ch,
        })

    emp_map: dict[Any, dict] = {}
    for e in nld.get('employees') or []:
        emp_map[e['employee_id']] = {
            'employee_id': e['employee_id'],
            'fullname': e['fullname'],
            'position': e.get('position'),
            'is_chu_ho': e.get('is_chu_ho'),
            'nld': e.get('nld') or {},
            'chu': {'BHXH': 0, 'BHYT': 0, 'BHTN': 0},
            'dn': {'BHXH': 0, 'BHYT': 0, 'BHTN': 0},
        }
    for e in dn.get('employees') or []:
        dn_t = e.get('dn') or e.get('chu') or e.get('types') or {}
        if e['employee_id'] not in emp_map:
            emp_map[e['employee_id']] = {
                'employee_id': e['employee_id'],
                'fullname': e['fullname'],
                'position': e.get('position'),
                'is_chu_ho': e.get('is_chu_ho'),
                'nld': {'BHXH': 0, 'BHYT': 0, 'BHTN': 0},
                'chu': dn_t,
                'dn': dn_t,
            }
        else:
            emp_map[e['employee_id']]['chu'] = dn_t
            emp_map[e['employee_id']]['dn'] = dn_t

    employees = []
    for e in emp_map.values():
        nld_sum = sum(e['nld'].get(t, 0) for t in INS_TYPES)
        dn_sum = sum(e['dn'].get(t, 0) for t in INS_TYPES)
        employees.append({**e, 'nld_total': nld_sum, 'chu_total': dn_sum, 'dn_total': dn_sum, 'total': nld_sum + dn_sum})

    total_phai = sum(t['phai_nop'] for t in combined_types)
    total_da = sum(t['da_nop'] for t in combined_types)
    total_con = max(0.0, total_phai - total_da)
    st, st_cls, _ = _status(total_phai, total_da)

    return {
        'month': int(month),
        'year': int(year),
        'period_label': f'Tháng {int(month)}/{int(year)}',
        'employee_count': max(nld.get('employee_count') or 0, dn.get('employee_count') or 0),
        'chu_ho_count': max(nld.get('chu_ho_count') or 0, dn.get('chu_ho_count') or 0),
        'nld': nld,
        'dn': dn,
        'chu': dn,
        'types': combined_types,
        'employees': employees,
        'summary': {
            'nld_phai_nop': nld['summary'].get('total_phai_nop', 0),
            'nld_da_nop': nld['summary'].get('total_da_nop', 0),
            'nld_con_lai': nld['summary'].get('total_con_lai', 0),
            'chu_phai_nop': dn['summary'].get('total_phai_nop', 0),
            'chu_da_nop': dn['summary'].get('total_da_nop', 0),
            'chu_con_lai': dn['summary'].get('total_con_lai', 0),
            'dn_phai_nop': dn['summary'].get('total_phai_nop', 0),
            'dn_da_nop': dn['summary'].get('total_da_nop', 0),
            'dn_con_lai': dn['summary'].get('total_con_lai', 0),
            'total_phai_nop': round(total_phai),
            'total_da_nop': round(total_da),
            'total_con_lai': round(total_con),
            'is_paid': total_con <= 0.01,
            'status': st,
            'status_class': st_cls,
        },
    }


def get_period_payer_detail(conn: sqlite3.Connection, month: int, year: int, payer: str = PAYER_NLD):
    p = (payer or PAYER_NLD).strip().upper()
    if p in (PAYER_DN, 'CHU', 'EMPLOYER'):
        return compute_period_insurance_dn(conn, month, year)
    return compute_period_insurance_nld(conn, month, year)


def get_insurance_debt_list(
    conn: sqlite3.Connection,
    year: int | None = None,
    include_paid: bool = False,
) -> dict[str, Any]:
    q = 'SELECT DISTINCT month, year FROM salary_detail'
    params: list[Any] = []
    if year:
        q += ' WHERE year = ?'
        params.append(int(year))
    q += ' ORDER BY year DESC, month DESC'
    periods = conn.execute(q, params).fetchall()

    result = []
    total_unpaid = 0.0
    nld_unpaid = 0.0
    dn_unpaid = 0.0
    unpaid_periods = 0
    for p in periods:
        if hasattr(p, 'keys'):
            m, y = int(p['month']), int(p['year'])
        else:
            m, y = int(p[0]), int(p[1])
        data = compute_period_insurance(conn, m, y)
        if not data:
            continue
        s = data['summary']
        if not include_paid and s['is_paid']:
            continue
        if not s['is_paid']:
            unpaid_periods += 1
            total_unpaid += s['total_con_lai']
            nld_unpaid += s['nld_con_lai']
            dn_unpaid += s['dn_con_lai']
        result.append({
            'month': data['month'],
            'year': data['year'],
            'period_label': data['period_label'],
            'employee_count': data['employee_count'],
            'chu_ho_count': data['chu_ho_count'],
            'nld_con_lai': s['nld_con_lai'],
            'chu_con_lai': s['dn_con_lai'],
            'dn_con_lai': s['dn_con_lai'],
            'bhxh_con_lai': next((x['con_lai'] for x in data['types'] if x['ins_type'] == 'BHXH'), 0),
            'bhyt_con_lai': next((x['con_lai'] for x in data['types'] if x['ins_type'] == 'BHYT'), 0),
            'bhtn_con_lai': next((x['con_lai'] for x in data['types'] if x['ins_type'] == 'BHTN'), 0),
            'kpcd_con_lai': next((x['con_lai'] for x in data['types'] if x['ins_type'] == 'KPCD'), 0),
            **s,
            'types': data['types'],
        })

    return {
        'periods': result,
        'summary': {
            'unpaid_periods': unpaid_periods,
            'total_unpaid': round(total_unpaid),
            'nld_unpaid': round(nld_unpaid),
            'chu_unpaid': round(dn_unpaid),
            'dn_unpaid': round(dn_unpaid),
            'year': int(year) if year else None,
        },
    }


def pay_insurance_item(
    conn: sqlite3.Connection,
    *,
    month: int,
    year: int,
    ins_type: str,
    payer: str = PAYER_NLD,
    amount: float | None = None,
    pay_date: str | None = None,
    payment_method: str = 'bank',
    receiver_name: str = 'Cơ quan BHXH',
    reason: str = '',
    created_by: str | None = None,
    branch_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Nộp 1 khoản BH → phiếu chi SME + bút toán Nợ 338x · Có tiền."""
    from Services.sme.payroll import pay_insurance

    ins = (ins_type or '').strip().upper()
    if ins not in ACCOUNT_BY_TYPE:
        raise ValueError('Loại bảo hiểm không hợp lệ (BHXH/BHYT/BHTN/KPCĐ)')
    p = (payer or PAYER_NLD).strip().upper()
    if p in ('CHU', 'EMPLOYER'):
        p = PAYER_DN
    if ins == 'KPCD':
        p = PAYER_DN
    if p not in (PAYER_NLD, PAYER_DN):
        p = PAYER_NLD

    period = get_period_payer_detail(conn, month, year, payer=p)
    if not period:
        raise ValueError('Không có bảng lương kỳ này — hãy chốt lương trước')
    row = next((t for t in period['types'] if t['ins_type'] == ins), None)
    if not row:
        raise ValueError('Không tìm thấy khoản BH')
    if float(row['con_lai']) <= 0.01:
        raise ValueError(f'{ins} ({"DN" if p == PAYER_DN else "NLĐ"}) đã nộp đủ')

    pay_amt = float(amount) if amount is not None else float(row['con_lai'])
    if pay_amt <= 0:
        raise ValueError('Số tiền không hợp lệ')
    if pay_amt > float(row['con_lai']) + 0.01:
        raise ValueError('Số tiền vượt quá số còn phải nộp')

    payer_txt = 'DN' if p == PAYER_DN else 'NLĐ'
    if not reason:
        reason = (
            f'Nộp {ins} ({payer_txt}) tháng {int(month)}/{int(year)} '
            f'({period["employee_count"]} NV) — 07-LĐTL'
        )
    ref = insurance_reference_key(ins, month, year, p)
    acct = ACCOUNT_BY_TYPE[ins]

    result = pay_insurance(
        conn,
        amount=pay_amt,
        pay_date=pay_date,
        payment_method=payment_method,
        account_code=acct,
        receiver_name=receiver_name or 'Cơ quan BHXH',
        reference=ref,
        reason=reason,
        created_by=created_by,
        branch_code=branch_code,
        commit=False,
    )

    if commit:
        conn.commit()
    return {
        **result,
        'ins_type': ins,
        'payer': p,
        'account_code': acct,
        'month': int(month),
        'year': int(year),
        'amount': pay_amt,
        'form_code': '07-LĐTL',
        'message': f'Đã lập phiếu chi nộp {ins} ({payer_txt}) — Nợ {acct}',
    }


def pay_insurance_period(
    conn: sqlite3.Connection,
    *,
    month: int,
    year: int,
    pay_date: str | None = None,
    payment_method: str = 'bank',
    receiver_name: str = 'Cơ quan BHXH',
    scope: str = 'ALL',
    created_by: str | None = None,
    branch_code: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Nộp cả kỳ — **1 phiếu chi**, bút toán nhiều dòng Nợ 3383/3384/3385/3382 · Có tiền."""
    from datetime import datetime
    from Services.sme.vouchers import create_payment

    ensure_insurance_alloc_schema(conn, commit=False)
    data = compute_period_insurance(conn, month, year)
    if not data:
        raise ValueError('Không có bảng lương kỳ này')

    scope_u = (scope or 'ALL').strip().upper()
    # Phân bổ theo loại BH + phía (để theo dõi công nợ); gộp theo TK khi hạch toán
    allocs: list[dict[str, Any]] = []
    by_acct: dict[str, float] = {}

    for t in data['types']:
        ins = t['ins_type']
        acct = ACCOUNT_BY_TYPE[ins]
        parts: list[tuple[str, float]] = []
        if scope_u in ('ALL', 'NLD') and float(t.get('nld_con_lai') or 0) > 0.01:
            parts.append((PAYER_NLD, float(t['nld_con_lai'])))
        if scope_u in ('ALL', 'DN', 'CHU') and float(t.get('dn_con_lai') or t.get('chu_con_lai') or 0) > 0.01:
            parts.append((PAYER_DN, float(t.get('dn_con_lai') or t.get('chu_con_lai') or 0)))
        for payer, amt in parts:
            allocs.append({
                'ins_type': ins,
                'payer': payer,
                'account_code': acct,
                'amount': round(amt),
            })
            by_acct[acct] = by_acct.get(acct, 0.0) + float(amt)

    if not allocs:
        raise ValueError('Kỳ này đã nộp đủ — không còn khoản phải nộp')

    acct_label = {'3383': 'BHXH', '3384': 'BHYT', '3385': 'BHTN', '3382': 'KPCĐ'}
    debit_lines = [
        {
            'account_code': acct,
            'amount': round(amt),
            'description': f'Nộp {acct_label.get(acct, acct)} T{int(month)}/{int(year)}',
        }
        for acct, amt in sorted(by_acct.items())
        if amt > 0.01
    ]
    total = round(sum(float(x['amount']) for x in debit_lines))
    date_s = (pay_date or datetime.now().strftime('%Y-%m-%d'))[:10]
    ref = period_reference_key(month, year)
    reason = (
        f'Nộp BHXH/BHYT/BHTN/KPCĐ tháng {int(month)}/{int(year)} '
        f'({data["employee_count"]} NV) — 07-LĐTL'
    )

    doc = create_payment(
        conn,
        voucher_date=date_s,
        party_name=receiver_name or 'Cơ quan BHXH',
        amount=total,
        payment_method=payment_method or 'bank',
        debit_account='338',
        reason=reason,
        reference_document=ref,
        source_type='insurance',
        created_by=created_by,
        branch_code=branch_code,
        debit_lines=debit_lines,
        form_code='07-LĐTL',
        commit=False,
    )

    for a in allocs:
        conn.execute(
            """
            INSERT INTO sme_insurance_pay_alloc(
                voucher_id, month, year, ins_type, payer, account_code, amount
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                doc['id'], int(month), int(year),
                a['ins_type'], a['payer'], a['account_code'], a['amount'],
            ),
        )

    if commit:
        conn.commit()

    return {
        'month': int(month),
        'year': int(year),
        'count': 1,
        'voucher': doc.get('voucher_no'),
        'vouchers': [doc.get('voucher_no')],
        'data': doc,
        'allocations': allocs,
        'debit_lines': debit_lines,
        'amount': total,
        'items': [doc],
        'message': (
            f"Đã lập 1 phiếu chi {doc.get('voucher_no')} nộp BH kỳ "
            f"T{int(month)}/{int(year)} — hạch toán "
            + ', '.join(f"Nợ {x['account_code']}={x['amount']:,.0f}" for x in debit_lines)
        ),
    }
