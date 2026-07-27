"""Schema và xử lý dữ liệu chấm công từ máy / file."""
import calendar
import re
import sqlite3
from datetime import datetime


def ensure_attendance_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS attendance_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial_no TEXT UNIQUE,
            device_name TEXT,
            brand TEXT DEFAULT 'zkteco',
            last_seen_at TEXT,
            ip_address TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS attendance_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER,
            device_user_id TEXT,
            employee_name TEXT,
            punch_time TEXT NOT NULL,
            punch_date TEXT NOT NULL,
            punch_type TEXT DEFAULT 'auto',
            verify_mode TEXT,
            device_sn TEXT,
            source TEXT DEFAULT 'import',
            raw_line TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(device_user_id, punch_time, device_sn)
        );

        CREATE INDEX IF NOT EXISTS idx_attendance_logs_date
            ON attendance_logs(punch_date);
        CREATE INDEX IF NOT EXISTS idx_attendance_logs_employee
            ON attendance_logs(employee_id);
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(employees)").fetchall()}
    if 'attendance_code' not in cols:
        conn.execute("ALTER TABLE employees ADD COLUMN attendance_code TEXT")


def normalize_punch_type(value):
    if value is None:
        return 'auto'
    s = str(value).strip().lower()
    if s in ('0', 'in', 'checkin', 'check-in', 'vao', 'vào', 'check in'):
        return 'in'
    if s in ('1', 'out', 'checkout', 'check-out', 'ra', 'check out'):
        return 'out'
    return 'auto'


def parse_datetime_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    formats = (
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%Y/%m/%d %H:%M:%S',
        '%Y/%m/%d %H:%M',
        '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y %H:%M',
        '%d-%m-%Y %H:%M:%S',
        '%d-%m-%Y %H:%M',
        '%Y-%m-%dT%H:%M:%S',
    )
    for fmt in formats:
        try:
            return datetime.strptime(s[:19] if 'T' not in fmt else s.replace('Z', '')[:19], fmt)
        except ValueError:
            continue
    m = re.match(r'^(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?', s)
    if m:
        d, mo, y, h, mi, se = m.groups()
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(se or 0))
    return None


def parse_zkteco_attlog_line(line):
    """Dòng ATTLOG ZKTeco: PIN\\tDateTime\\tStatus\\tVerify..."""
    parts = (line or '').strip().split('\t')
    if len(parts) < 2:
        return None
    pin = parts[0].strip()
    dt = parse_datetime_value(parts[1].strip())
    if not pin or not dt:
        return None
    status = parts[2].strip() if len(parts) > 2 else ''
    verify = parts[3].strip() if len(parts) > 3 else ''
    return {
        'device_user_id': pin,
        'punch_time': dt.strftime('%Y-%m-%d %H:%M:%S'),
        'punch_date': dt.strftime('%Y-%m-%d'),
        'punch_type': normalize_punch_type(status),
        'verify_mode': verify,
        'raw_line': line.strip(),
    }


def resolve_employee_id(conn, device_user_id=None, employee_name=None):
    device_user_id = (device_user_id or '').strip()
    employee_name = (employee_name or '').strip()
    if device_user_id:
        row = conn.execute(
            """
            SELECT id FROM employees
            WHERE COALESCE(attendance_code, '') = ?
               OR CAST(id AS TEXT) = ?
            LIMIT 1
            """,
            (device_user_id, device_user_id),
        ).fetchone()
        if row:
            return row['id']
    if employee_name:
        row = conn.execute(
            "SELECT id FROM employees WHERE fullname = ? COLLATE NOCASE LIMIT 1",
            (employee_name,),
        ).fetchone()
        if row:
            return row['id']
    return None


def upsert_attendance_log(conn, item, source='import', device_sn=None):
    ensure_attendance_schema(conn)
    device_sn = (device_sn or 'UNKNOWN').strip()
    punch_time = item.get('punch_time')
    punch_date = item.get('punch_date')
    if not punch_time:
        dt = parse_datetime_value(item.get('datetime') or item.get('time'))
        if not dt:
            return False, 'invalid_time'
        punch_time = dt.strftime('%Y-%m-%d %H:%M:%S')
        punch_date = dt.strftime('%Y-%m-%d')
    device_user_id = (item.get('device_user_id') or item.get('pin') or item.get('code') or '').strip()
    employee_name = (item.get('employee_name') or item.get('name') or item.get('fullname') or '').strip()
    employee_id = item.get('employee_id') or resolve_employee_id(conn, device_user_id, employee_name)
    try:
        conn.execute(
            """
            INSERT INTO attendance_logs (
                employee_id, device_user_id, employee_name, punch_time, punch_date,
                punch_type, verify_mode, device_sn, source, raw_line
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_user_id, punch_time, device_sn) DO NOTHING
            """,
            (
                employee_id,
                device_user_id or None,
                employee_name or None,
                punch_time,
                punch_date,
                normalize_punch_type(item.get('punch_type') or item.get('status')),
                (item.get('verify_mode') or item.get('verify') or '') or None,
                device_sn,
                source,
                item.get('raw_line'),
            ),
        )
        return True, None
    except sqlite3.Error as e:
        return False, str(e)


def touch_device(conn, serial_no, ip_address=None):
    ensure_attendance_schema(conn)
    conn.execute(
        """
        INSERT INTO attendance_devices (serial_no, device_name, last_seen_at, ip_address)
        VALUES (?, ?, CURRENT_TIMESTAMP, ?)
        ON CONFLICT(serial_no) DO UPDATE SET
            last_seen_at = CURRENT_TIMESTAMP,
            ip_address = COALESCE(excluded.ip_address, attendance_devices.ip_address)
        """,
        (serial_no, serial_no, ip_address),
    )


def build_daily_summary(conn, start_date, end_date, employee_id=None):
    ensure_attendance_schema(conn)
    sql = """
        SELECT
            COALESCE(e.id, 0) AS employee_id,
            COALESCE(e.fullname, l.employee_name, l.device_user_id, '—') AS fullname,
            l.device_user_id,
            l.punch_date,
            MIN(l.punch_time) AS first_punch,
            MAX(l.punch_time) AS last_punch,
            COUNT(*) AS punch_count
        FROM attendance_logs l
        LEFT JOIN employees e ON e.id = l.employee_id
        WHERE l.punch_date BETWEEN ? AND ?
    """
    params = [start_date, end_date]
    if employee_id:
        sql += ' AND (l.employee_id = ? OR CAST(l.device_user_id AS TEXT) = ?)'
        params.extend([employee_id, str(employee_id)])
    sql += """
        GROUP BY COALESCE(e.id, 0), l.device_user_id, l.punch_date,
                 COALESCE(e.fullname, l.employee_name, l.device_user_id)
        ORDER BY l.punch_date DESC, fullname COLLATE NOCASE
    """
    rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            t1 = datetime.strptime(item['first_punch'], '%Y-%m-%d %H:%M:%S')
            t2 = datetime.strptime(item['last_punch'], '%Y-%m-%d %H:%M:%S')
            item['work_hours'] = round(max(0, (t2 - t1).total_seconds()) / 3600, 2)
        except Exception:
            item['work_hours'] = 0
        result.append(item)
    return result


def get_monthly_work_days_map(conn, month, year):
    """Đếm số ngày công thực tế (distinct punch_date) theo nhân viên trong tháng."""
    ensure_attendance_schema(conn)
    last_day = calendar.monthrange(year, month)[1]
    start = f'{year:04d}-{month:02d}-01'
    end = f'{year:04d}-{month:02d}-{last_day:02d}'

    rows = conn.execute(
        """
        SELECT emp_id, COUNT(DISTINCT punch_date) AS work_days
        FROM (
            SELECT
                COALESCE(
                    NULLIF(l.employee_id, 0),
                    e_code.id,
                    e_id.id,
                    e_name.id
                ) AS emp_id,
                l.punch_date
            FROM attendance_logs l
            LEFT JOIN employees e_code
                ON l.device_user_id IS NOT NULL
               AND TRIM(l.device_user_id) != ''
               AND COALESCE(e_code.attendance_code, '') = TRIM(l.device_user_id)
            LEFT JOIN employees e_id
                ON l.device_user_id IS NOT NULL
               AND CAST(e_id.id AS TEXT) = TRIM(l.device_user_id)
            LEFT JOIN employees e_name
                ON l.employee_name IS NOT NULL
               AND TRIM(l.employee_name) != ''
               AND e_name.fullname = TRIM(l.employee_name) COLLATE NOCASE
            WHERE l.punch_date BETWEEN ? AND ?
        ) AS mapped
        WHERE emp_id IS NOT NULL
        GROUP BY emp_id
        """,
        (start, end),
    ).fetchall()

    return {int(row['emp_id']): int(row['work_days']) for row in rows}
