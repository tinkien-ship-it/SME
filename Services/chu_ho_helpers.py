"""Ánh xạ Chủ hộ kinh doanh từ business_info → nhân viên trên bảng lương."""
import re
import unicodedata
from db_utils import sqlite_commit


def ensure_is_chu_ho_column(conn):
    cols = {row[1] for row in conn.execute('PRAGMA table_info(employees)').fetchall()}
    if 'is_chu_ho' not in cols:
        conn.execute('ALTER TABLE employees ADD COLUMN is_chu_ho INTEGER DEFAULT 0')
        sqlite_commit(conn, label='chu_ho_helpers')


def normalize_person_name(name):
    if not name:
        return ''
    s = str(name).strip().lower()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = s.replace('đ', 'd').replace('Đ', 'd')
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def normalize_id_card(value):
    if not value:
        return ''
    return re.sub(r'\D', '', str(value))


def get_owner_profile(conn):
    """Chủ hộ từ Cài đặt (business_info.representative_name)."""
    row = conn.execute(
        """
        SELECT representative_name, business_name, tax_code
        FROM business_info LIMIT 1
        """
    ).fetchone()
    if not row:
        return {'representative_name': '', 'business_name': '', 'tax_code': ''}
    data = dict(row)
    rep = (data.get('representative_name') or '').strip()
    if not rep:
        rep = (data.get('business_name') or '').strip()
    return {
        'representative_name': rep,
        'business_name': (data.get('business_name') or '').strip(),
        'tax_code': (data.get('tax_code') or '').strip(),
    }


def employee_matches_owner(employee_row, owner_profile):
    """Khớp NV với chủ hộ theo họ tên (chuẩn hóa tiếng Việt)."""
    if not owner_profile or not employee_row:
        return False
    owner_name = normalize_person_name(owner_profile.get('representative_name'))
    if not owner_name:
        return False
    emp = dict(employee_row) if hasattr(employee_row, 'keys') else employee_row
    emp_name = normalize_person_name(emp.get('fullname') or emp.get('name'))
    if emp_name and emp_name == owner_name:
        return True
    return False


def employee_is_chu_ho(row, conn=None):
    """NV là Chủ hộ — ưu tiên cột is_chu_ho, fallback so khớp business_info."""
    if row is None:
        return False
    emp = dict(row) if hasattr(row, 'keys') and not isinstance(row, dict) else (row or {})
    if int(emp.get('is_chu_ho') or 0) == 1:
        return True
    if conn is not None:
        return employee_matches_owner(emp, get_owner_profile(conn))
    return False


def sync_chu_ho_from_business_info(conn, commit=True):
    """
    Đồng bộ is_chu_ho cho toàn bộ nhân viên theo representative_name trong Cài đặt.
    Trả về (matched_ids, representative_name).
    """
    ensure_is_chu_ho_column(conn)
    owner = get_owner_profile(conn)
    owner_name = owner.get('representative_name') or ''
    owner_norm = normalize_person_name(owner_name)

    rows = conn.execute(
        'SELECT id, fullname, id_card, is_chu_ho FROM employees'
    ).fetchall()

    matched_ids = []
    for row in rows:
        is_match = employee_matches_owner(row, owner) if owner_norm else False
        flag = 1 if is_match else 0
        if int(row['is_chu_ho'] or 0) != flag:
            conn.execute(
                'UPDATE employees SET is_chu_ho = ? WHERE id = ?',
                (flag, row['id']),
            )
        if is_match:
            matched_ids.append(int(row['id']))

    if commit:
        sqlite_commit(conn, label='chu_ho_helpers')
    return matched_ids, owner_name
