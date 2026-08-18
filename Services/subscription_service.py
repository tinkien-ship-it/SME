"""Đăng ký dùng thử, gia hạn subscription và kích hoạt tenant sau thanh toán."""
import json
import logging
import os
import re
import secrets
import sqlite3
import string
from datetime import datetime
from dateutil.relativedelta import relativedelta

from flask_bcrypt import generate_password_hash

from db_utils import BASE_DIR, MAIN_DB_PATH, get_main_db_connection

logger = logging.getLogger(__name__)

TRIAL_MONTHS = 6  # mặc định khi chưa cấu hình trong Master Settings
TRIAL_MONTHS_SETTING_KEY = 'trial_months'
TRIAL_MONTHS_MIN = 1
TRIAL_MONTHS_MAX = 36
SUPPORT_NOTIFY_EMAIL = os.getenv('SUPPORT_NOTIFY_EMAIL', 'tinkien@gmail.com')


def get_trial_months(conn=None):
    """Số tháng dùng thử mặc định (Master Settings), fallback TRIAL_MONTHS."""
    own = conn is None
    if own:
        conn = get_main_db_connection()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (TRIAL_MONTHS_SETTING_KEY,),
        ).fetchone()
        if not row:
            return TRIAL_MONTHS
        val = row['value'] if isinstance(row, sqlite3.Row) else row[0]
        months = int(str(val).strip() or TRIAL_MONTHS)
        return max(TRIAL_MONTHS_MIN, min(TRIAL_MONTHS_MAX, months))
    except (TypeError, ValueError, sqlite3.Error):
        return TRIAL_MONTHS
    finally:
        if own:
            conn.close()


def set_trial_months(months, conn=None):
    """Lưu số tháng dùng thử mặc định vào main DB settings."""
    try:
        months = int(months)
    except (TypeError, ValueError):
        return {'success': False, 'error': 'Số tháng không hợp lệ'}
    if months < TRIAL_MONTHS_MIN or months > TRIAL_MONTHS_MAX:
        return {
            'success': False,
            'error': f'Số tháng phải từ {TRIAL_MONTHS_MIN} đến {TRIAL_MONTHS_MAX}',
        }

    own = conn is None
    if own:
        conn = get_main_db_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (TRIAL_MONTHS_SETTING_KEY, str(months)),
        )
        if own:
            conn.commit()
        return {'success': True, 'trial_months': months}
    except sqlite3.Error as exc:
        return {'success': False, 'error': str(exc)}
    finally:
        if own:
            conn.close()


def adjust_tenant_expiry_months(tenant_id, delta_months):
    """
    Tăng/giảm ngày hết hạn tenant theo số tháng (delta dương = thêm, âm = bớt).
    Nếu đã quá hạn và delta > 0: tính từ hôm nay.
    """
    try:
        delta = int(delta_months)
    except (TypeError, ValueError):
        return {'success': False, 'error': 'Số tháng điều chỉnh không hợp lệ'}
    if delta == 0:
        return {'success': False, 'error': 'Số tháng điều chỉnh phải khác 0'}
    if abs(delta) > TRIAL_MONTHS_MAX:
        return {'success': False, 'error': f'Chỉ điều chỉnh tối đa ±{TRIAL_MONTHS_MAX} tháng mỗi lần'}

    tenant_id = (tenant_id or '').strip()
    if not tenant_id:
        return {'success': False, 'error': 'Thiếu tenant_id'}

    conn = get_main_db_connection()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT tenant_id, business_name, expiry_date, settings, is_active FROM tenants WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        if not row:
            return {'success': False, 'error': 'Không tìm thấy tenant'}

        today = datetime.now().date()
        old_expiry = None
        if row['expiry_date']:
            try:
                old_expiry = datetime.strptime(str(row['expiry_date'])[:10], '%Y-%m-%d').date()
            except ValueError:
                old_expiry = None

        if delta > 0:
            base = old_expiry if (old_expiry and old_expiry >= today) else today
        else:
            base = old_expiry or today

        new_expiry = base + relativedelta(months=delta)
        new_expiry_str = new_expiry.strftime('%Y-%m-%d')

        settings = parse_tenant_settings(row['settings'])
        settings['trial_months'] = get_trial_months(conn)
        settings['last_trial_adjust_months'] = delta
        settings['last_trial_adjust_at'] = datetime.now().isoformat(timespec='seconds')

        conn.execute(
            """
            UPDATE tenants
            SET expiry_date = ?, settings = ?, is_active = CASE WHEN ? >= date('now') THEN 1 ELSE is_active END
            WHERE tenant_id = ?
            """,
            (
                new_expiry_str,
                json.dumps(settings, ensure_ascii=False),
                new_expiry_str,
                tenant_id,
            ),
        )
        conn.commit()
        return {
            'success': True,
            'tenant_id': tenant_id,
            'business_name': row['business_name'],
            'old_expiry_date': old_expiry.strftime('%Y-%m-%d') if old_expiry else None,
            'expiry_date': new_expiry_str,
            'delta_months': delta,
            'message': (
                f"Đã {'tăng' if delta > 0 else 'giảm'} {abs(delta)} tháng — "
                f"hết hạn mới: {new_expiry.strftime('%d/%m/%Y')}"
            ),
        }
    except Exception as exc:
        logger.exception('adjust_tenant_expiry_months: %s', exc)
        return {'success': False, 'error': str(exc)}
    finally:
        conn.close()

# Giá trị mặc định lúc seed lần đầu — sau đó đọc/ghi qua bảng products (main DB).
DEFAULT_SUBSCRIPTION_PLANS = {
    'DV001': {
        'code': 'DV001',
        'name': 'Phần mềm Bán Hàng kiêm Kế Toán KETO POS - HKD có doanh thu dưới 1 tỷ — không xuất HĐĐT',
        'price': 1_200_000,
        'has_einvoice': False,
    },
    'DV002': {
        'code': 'DV002',
        'name': 'Phần mềm Bán Hàng kiêm Kế Toán KETO POS - HKD có doanh thu dưới 1 tỷ — có xuất HĐĐT',
        'price': 1_800_000,
        'has_einvoice': True,
    },
    'DV003': {
        'code': 'DV003',
        'name': 'Phần mềm Bán Hàng kiêm Kế Toán KETO POS - HKD có doanh thu trên 1 tỷ đến 3 tỷ',
        'price': 2_100_000,
        'has_einvoice': True,
    },
    'DV004': {
        'code': 'DV004',
        'name': 'Phần mềm Bán Hàng kiêm Kế Toán KETO POS - HKD có doanh thu trên 3 tỷ',
        'price': 3_200_000,
        'has_einvoice': True,
    },
}

# Giữ alias cũ để tương thích import (không dùng trực tiếp cho hiển thị).
SUBSCRIPTION_PLANS = DEFAULT_SUBSCRIPTION_PLANS

SERVICE_BUSINESS_LINES = frozenset({'fb_service', 'rental_service'})

BUSINESS_LINE_OPTIONS = {
    'pos': {
        'code': 'pos',
        'label': 'Bán Hàng - Dịch Vụ - Sản Xuất',
        'role': 'manager',
        'support_role': 'admin',
        'default_hkd_sector': None,
    },
    'fb_service': {
        'code': 'fb_service',
        'label': 'Dịch Vụ Ăn Uống',
        'role': 'managerFB',
        'support_role': 'adminFB',
        'default_hkd_sector': 'G2',
    },
    'rental_service': {
        'code': 'rental_service',
        'label': 'Dịch Vụ Lưu Trú',
        'role': 'manager*',
        'support_role': 'admin*',
        'default_hkd_sector': 'G2',
    },
}

HKD_SECTOR_CHOICES = ('NN1', 'NN2', 'NN3', 'NN4')


def resolve_provision_nn_profile(business_line, enabled_nn_sectors=None, primary_nn=None):
    """Trả (enabled_nn_sectors, primary_nn) khi tạo/sửa tenant."""
    from Services.hkd_sector import (
        default_nn_sectors_for_business_line,
        normalize_enabled_nn_sectors,
        normalize_nn_code,
    )

    bl = (business_line or 'pos').strip()
    if enabled_nn_sectors:
        sectors = normalize_enabled_nn_sectors(enabled_nn_sectors)
    else:
        sectors = default_nn_sectors_for_business_line(bl)
    primary = normalize_nn_code(primary_nn or sectors[0])
    if primary not in sectors:
        sectors = sorted(set(sectors + [primary]))
    return sectors, primary


def resolve_hkd_sector_for_business_line(business_line, hkd_sector=None):
    """Tương thích API cũ — trả primary NN."""
    _, primary = resolve_provision_nn_profile(business_line, None, hkd_sector)
    from Services.hkd_sector import nn_to_storage_code, normalize_nn_code
    return nn_to_storage_code(normalize_nn_code(primary))


def normalize_tenant_phone(phone):
    """Chuẩn hóa SĐT VN → tenant_id dạng 0xxxxxxxxx."""
    digits = re.sub(r'\D', '', str(phone or ''))
    if digits.startswith('84') and len(digits) >= 11:
        digits = '0' + digits[2:]
    elif len(digits) == 9 and not digits.startswith('0'):
        digits = '0' + digits
    if len(digits) != 10 or not digits.startswith('0'):
        return None
    return digits


def generate_password(length=12):
    alphabet = string.ascii_letters + string.digits + '!@#$'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def parse_tenant_settings(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def role_for_business_line(business_line, accounting_regime=None):
    """Role chủ tenant. SME → managerSME58/99 theo chế độ; HKD theo ngành vụ."""
    from Services.tenant_profile import is_sme_regime
    if is_sme_regime(accounting_regime):
        from Services.sme_roles import owner_role_for_regime
        return owner_role_for_regime(accounting_regime)
    bl = (business_line or 'pos').strip()
    return BUSINESS_LINE_OPTIONS.get(bl, BUSINESS_LINE_OPTIONS['pos'])['role']


def support_role_for_business_line(business_line, accounting_regime=None):
    """Role tài khoản hỗ trợ KETO. SME → adminSME58/99 theo chế độ."""
    from Services.tenant_profile import is_sme_regime
    if is_sme_regime(accounting_regime):
        from Services.sme_roles import support_role_for_regime
        return support_role_for_regime(accounting_regime)
    bl = (business_line or 'pos').strip()
    return BUSINESS_LINE_OPTIONS.get(bl, BUSINESS_LINE_OPTIONS['pos'])['support_role']


def _ensure_subscription_columns(conn):
    """Thêm cột subscription nếu thiếu. Trả True nếu đã ALTER (cần commit)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
    changed = False
    if 'is_subscription_plan' not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN is_subscription_plan INTEGER DEFAULT 0")
        changed = True
    if 'has_einvoice' not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN has_einvoice INTEGER DEFAULT 0")
        changed = True
    return changed


def _subscription_plan_price(row):
    if not row:
        return 0
    for key in ('base_price', 'price'):
        val = row[key] if isinstance(row, dict) else row[key]
        if val not in (None, '', 0):
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return 0


def get_subscription_plans(conn=None):
    """Danh sách gói subscription từ bảng products (main DB).

    Seed chạy trên kết nối riêng + commit ngay, rồi mới SELECT — tránh giữ
    khóa ghi trên main DB khi render trang login (gây database is locked).
    """
    own_conn = conn is None
    if own_conn:
        try:
            ensure_subscription_products()
        except sqlite3.Error as exc:
            logger.warning('ensure_subscription_products skipped: %s', exc)
        conn = get_main_db_connection()
    try:
        if not own_conn:
            ensure_subscription_products(conn)
        rows = conn.execute(
            """
            SELECT id, product_code AS code, name,
                   COALESCE(base_price, price, 0) AS price,
                   COALESCE(has_einvoice, 0) AS has_einvoice
            FROM products
            WHERE COALESCE(is_subscription_plan, 0) = 1
            ORDER BY id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own_conn:
            conn.close()


def get_subscription_plan_by_code(plan_code, conn=None):
    plan_code = (plan_code or '').strip().upper()
    if not plan_code:
        return None
    own_conn = conn is None
    if own_conn:
        conn = get_main_db_connection()
    try:
        _ensure_subscription_columns(conn)
        row = conn.execute(
            """
            SELECT id, product_code AS code, name, unit,
                   COALESCE(base_price, price, 0) AS price,
                   COALESCE(has_einvoice, 0) AS has_einvoice
            FROM products
            WHERE product_code = ? AND COALESCE(is_subscription_plan, 0) = 1
            """,
            (plan_code,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own_conn:
            conn.close()


def ensure_subscription_products(conn=None):
    """Seed 4 gói subscription nếu thiếu — không ghi đè dữ liệu đã sửa trên products.

    Chỉ ghi DB khi thực sự thiếu cột/gói/cờ — tránh UPDATE mỗi lần mở trang login.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_main_db_connection()
    try:
        changed = _ensure_subscription_columns(conn)

        codes = tuple(DEFAULT_SUBSCRIPTION_PLANS.keys())
        placeholders = ','.join('?' for _ in codes)
        sub_count = int(conn.execute(
            "SELECT COUNT(*) FROM products WHERE COALESCE(is_subscription_plan, 0) = 1"
        ).fetchone()[0] or 0)
        legacy_unflagged = int(conn.execute(
            f"""
            SELECT COUNT(*) FROM products
            WHERE product_code IN ({placeholders})
              AND COALESCE(is_subscription_plan, 0) = 0
            """,
            codes,
        ).fetchone()[0] or 0)

        # Đã đủ gói và không còn mã mặc định chưa gắn cờ → không ghi gì thêm
        if sub_count >= len(DEFAULT_SUBSCRIPTION_PLANS) and legacy_unflagged == 0:
            if changed and own_conn:
                conn.commit()
            return

        if legacy_unflagged:
            for code, plan in DEFAULT_SUBSCRIPTION_PLANS.items():
                conn.execute(
                    """
                    UPDATE products
                    SET is_subscription_plan = 1,
                        has_einvoice = COALESCE(has_einvoice, ?),
                        product_type = COALESCE(product_type, 'service'),
                        hkd_sector_code = COALESCE(hkd_sector_code, 'G2')
                    WHERE product_code = ? AND COALESCE(is_subscription_plan, 0) = 0
                    """,
                    (1 if plan['has_einvoice'] else 0, code),
                )
            changed = True
            sub_count = int(conn.execute(
                "SELECT COUNT(*) FROM products WHERE COALESCE(is_subscription_plan, 0) = 1"
            ).fetchone()[0] or 0)

        for code, plan in DEFAULT_SUBSCRIPTION_PLANS.items():
            if sub_count >= len(DEFAULT_SUBSCRIPTION_PLANS):
                break
            row = conn.execute(
                "SELECT id FROM products WHERE product_code = ?",
                (code,),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE products
                    SET is_subscription_plan = 1,
                        has_einvoice = COALESCE(has_einvoice, ?),
                        product_type = COALESCE(product_type, 'service'),
                        hkd_sector_code = COALESCE(hkd_sector_code, 'G2')
                    WHERE id = ?
                    """,
                    (1 if plan['has_einvoice'] else 0, row['id']),
                )
                changed = True
                sub_count = int(conn.execute(
                    "SELECT COUNT(*) FROM products WHERE COALESCE(is_subscription_plan, 0) = 1"
                ).fetchone()[0] or 0)
                continue

            conn.execute(
                """
                INSERT INTO products (
                    product_code, barcode, name, unit, price, base_price,
                    product_type, hkd_sector_code, is_subscription_plan, has_einvoice
                ) VALUES (?, ?, ?, 'Gói/năm', ?, ?, 'service', 'G2', 1, ?)
                """,
                (
                    code, f'{code}01', plan['name'], plan['price'], plan['price'],
                    1 if plan['has_einvoice'] else 0,
                ),
            )
            changed = True
            sub_count += 1

        if own_conn and changed:
            conn.commit()
        elif own_conn:
            # Không có thay đổi — đảm bảo không để transaction treo
            conn.rollback()
    finally:
        if own_conn:
            conn.close()


def get_tenant_record(tenant_id, include_inactive=True):
    conn = get_main_db_connection()
    try:
        sql = """
            SELECT tenant_id, db_path, business_name, phone, address, email,
                   expiry_date, is_active, settings, created_at
            FROM tenants WHERE tenant_id = ?
        """
        if not include_inactive:
            sql += " AND is_active = 1"
        row = conn.execute(sql, (tenant_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_account_by_email(email, active_only=True):
    """Tìm tài khoản tenant theo email Google — có thể gồm tenant hết hạn."""
    email = (email or '').strip().lower()
    if not email:
        return None

    conn = get_main_db_connection()
    try:
        active_clause = " AND COALESCE(t.is_active, 1) = 1 AND COALESCE(m.is_active, 1) = 1" if active_only else ""
        rows = conn.execute(
            f"""
            SELECT m.username, m.tenant_id, t.db_path, t.is_2fa_enabled, m.email,
                   t.is_active AS tenant_active, t.expiry_date, t.business_name,
                   t.settings
            FROM user_tenant_mapping m
            JOIN tenants t ON t.tenant_id = m.tenant_id
            WHERE LOWER(COALESCE(m.email, '')) = ?
              AND COALESCE(m.is_active, 1) = 1
              {active_clause}
            ORDER BY t.is_active DESC, t.created_at DESC
            """,
            (email,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        db_path = row['db_path']
        if db_path and not os.path.isabs(db_path):
            db_path = os.path.join(BASE_DIR, db_path)
        if not os.path.exists(db_path):
            continue
        with sqlite3.connect(db_path) as conn_u:
            conn_u.row_factory = sqlite3.Row
            try:
                user = conn_u.execute(
                    """
                    SELECT * FROM users
                    WHERE username = ? AND COALESCE(is_support_account, 0) = 0
                    LIMIT 1
                    """,
                    (row['username'],),
                ).fetchone()
            except sqlite3.OperationalError:
                user = conn_u.execute(
                    "SELECT * FROM users WHERE username = ? LIMIT 1",
                    (row['username'],),
                ).fetchone()
            if not user:
                user = conn_u.execute(
                    "SELECT * FROM users WHERE username = ? LIMIT 1",
                    (row['username'],),
                ).fetchone()
        if user and str(user['username']).endswith('admin') and len(str(user['username'])) > 11:
            continue
        if user:
            return {
                'user': dict(user),
                'db_path': db_path,
                'tenant_id': row['tenant_id'],
                'tenant_2fa_enabled': bool(row['is_2fa_enabled']),
                'email': row['email'] or email,
                'tenant_active': bool(row['tenant_active']),
                'expiry_date': row['expiry_date'],
                'business_name': row['business_name'],
                'settings': parse_tenant_settings(row['settings']),
            }
    return None


def find_inactive_tenant_by_username(username):
    """Tìm tenant hết hạn theo username (SĐT) để chuyển sang trang gia hạn."""
    username = (username or '').strip()
    if not username:
        return None
    conn = get_main_db_connection()
    try:
        row = conn.execute(
            """
            SELECT t.tenant_id, t.business_name, t.expiry_date, t.is_active, t.settings,
                   m.email
            FROM user_tenant_mapping m
            JOIN tenants t ON t.tenant_id = m.tenant_id
            WHERE m.username = ? AND COALESCE(m.is_active, 1) = 1
            LIMIT 1
            """,
            (username,),
        ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec['settings'] = parse_tenant_settings(rec.get('settings'))
        return rec
    finally:
        conn.close()


def tenant_is_expired(tenant_record):
    if not tenant_record:
        return True
    if not tenant_record.get('is_active', 0):
        return True
    exp = (tenant_record.get('expiry_date') or '').strip()
    if not exp:
        return False
    try:
        exp_dt = datetime.strptime(exp[:10], '%Y-%m-%d').date()
        return exp_dt < datetime.now().date()
    except ValueError:
        return False


def get_tenant_business_info(tenant_id):
    rec = get_tenant_record(tenant_id, include_inactive=True)
    if not rec:
        return {}
    db_path = rec['db_path']
    if db_path and not os.path.isabs(db_path):
        db_path = os.path.join(BASE_DIR, db_path)
    if not os.path.exists(db_path):
        return {
            'business_name': rec.get('business_name') or '',
            'phone': rec.get('phone') or tenant_id,
            'email': rec.get('email') or '',
            'address': rec.get('address') or '',
            'tax_code': '',
        }
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        biz = conn.execute("SELECT * FROM business_info LIMIT 1").fetchone()
    if biz:
        d = dict(biz)
        return {
            'business_name': d.get('business_name') or rec.get('business_name') or '',
            'representative_name': d.get('representative_name') or '',
            'phone': d.get('phone') or rec.get('phone') or tenant_id,
            'email': d.get('email') or rec.get('email') or '',
            'address': d.get('address') or rec.get('address') or '',
            'tax_code': d.get('tax_code') or '',
        }
    return {
        'business_name': rec.get('business_name') or '',
        'phone': rec.get('phone') or tenant_id,
        'email': rec.get('email') or '',
        'address': rec.get('address') or '',
        'tax_code': '',
    }


def build_renewal_note(tenant_id, plan_code, years):
    return f"tenant_id:{tenant_id}|plan:{plan_code}|years:{int(years)}"


def parse_renewal_note(note):
    if not note:
        return {}
    out = {}
    for part in str(note).split('|'):
        if ':' in part:
            k, v = part.split(':', 1)
            out[k.strip()] = v.strip()
    return out


def create_renewal_checkout(tenant_id, plan_code, years=1, customer=None):
    """Tạo đơn pending trên main DB — thanh toán QR gia hạn."""
    plan_code = (plan_code or '').strip().upper()

    years = max(1, min(int(years or 1), 5))
    tenant = get_tenant_record(tenant_id, include_inactive=True)
    if not tenant:
        return {'success': False, 'error': 'Không tìm thấy tài khoản cửa hàng'}

    biz = get_tenant_business_info(tenant_id)
    customer = customer or {}
    customer_name = (customer.get('customer_name') or biz.get('business_name') or tenant.get('business_name') or '').strip()
    company_name = (customer.get('company_name') or '').strip()
    tax_code = (customer.get('tax_code') or biz.get('tax_code') or '').strip()
    phone = (customer.get('phone') or biz.get('phone') or tenant.get('phone') or tenant_id).strip()
    address = (customer.get('address') or biz.get('address') or tenant.get('address') or '').strip()
    email = (customer.get('email') or biz.get('email') or tenant.get('email') or '').strip()

    if not customer_name:
        return {'success': False, 'error': 'Vui lòng nhập tên hộ kinh doanh / khách hàng'}

    conn = get_main_db_connection()
    try:
        ensure_subscription_products(conn)
        product = conn.execute(
            """
            SELECT id, name, unit, product_code,
                   COALESCE(base_price, price, 0) AS price
            FROM products
            WHERE product_code = ? AND COALESCE(is_subscription_plan, 0) = 1
            """,
            (plan_code,),
        ).fetchone()
        if not product:
            return {'success': False, 'error': f'Gói dịch vụ không hợp lệ hoặc chưa cấu hình: {plan_code}'}

        unit_price = float(product['price'] or 0)
        if unit_price <= 0:
            return {'success': False, 'error': f'Gói {plan_code} chưa có đơn giá hợp lệ'}
        total_amount = unit_price * years
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        note = build_renewal_note(tenant_id, plan_code, years)

        cur = conn.cursor()
        last = cur.execute(
            "SELECT sale_no FROM sale WHERE sale_no LIKE 'ĐH%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        next_no = 1
        if last and last['sale_no']:
            m = re.search(r'(\d+)$', last['sale_no'])
            if m:
                next_no = int(m.group(1)) + 1
        sale_no = f"ĐH{next_no:06d}"

        cur.execute(
            """
            INSERT INTO sale (
                date, total_amount, payment_method, customer_name, company_name,
                tax_code, customer_phone, address, note, status, email, business_line, sale_no
            ) VALUES (?, ?, '112', ?, ?, ?, ?, ?, ?, 'pending', ?, 'subscription_renewal', ?)
            """,
            (
                now, total_amount, customer_name, company_name, tax_code,
                phone, address, note, email, sale_no,
            ),
        )
        sale_id = cur.lastrowid
        cur.execute(
            """
            INSERT INTO sale_items (sale_id, product_id, product_name, quantity, price, unit, tax_pct)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (
                sale_id, product['id'], product['name'], years, unit_price,
                product['unit'] or 'Gói/năm',
            ),
        )
        conn.commit()
        return {
            'success': True,
            'sale_id': sale_id,
            'sale_no': sale_no,
            'total_amount': total_amount,
            'plan': dict(product),
            'years': years,
            'payment_code': f"DH{sale_id:06d}",
            'qr_url': f"/qr_payment/{sale_id}",
        }
    except Exception as exc:
        conn.rollback()
        logger.exception("create_renewal_checkout: %s", exc)
        return {'success': False, 'error': str(exc)}
    finally:
        conn.close()


def activate_tenant_after_renewal(tenant_id, years, plan_code):
    tenant = get_tenant_record(tenant_id, include_inactive=True)
    if not tenant:
        return False, 'Không tìm thấy tenant'

    years = max(1, int(years or 1))
    today = datetime.now().date()
    base = today
    exp_raw = (tenant.get('expiry_date') or '')[:10]
    if exp_raw:
        try:
            exp_dt = datetime.strptime(exp_raw, '%Y-%m-%d').date()
            if exp_dt >= today:
                base = exp_dt
        except ValueError:
            pass
    new_expiry = base + relativedelta(years=years)

    settings = parse_tenant_settings(tenant.get('settings'))
    settings.update({
        'plan': plan_code,
        'plan_code': plan_code,
        'subscription_active': True,
        'onboarding_completed': settings.get('onboarding_completed', True),
    })

    conn = get_main_db_connection()
    try:
        conn.execute(
            """
            UPDATE tenants
            SET is_active = 1, expiry_date = ?, settings = ?
            WHERE tenant_id = ?
            """,
            (new_expiry.strftime('%Y-%m-%d'), json.dumps(settings, ensure_ascii=False), tenant_id),
        )
        conn.commit()
        return True, new_expiry.strftime('%Y-%m-%d')
    finally:
        conn.close()


def complete_subscription_renewal(sale_id):
    """Hoàn tất thanh toán gia hạn: sale + kích hoạt tenant + xuất HĐ (nếu có)."""
    conn = get_main_db_connection()
    conn.row_factory = sqlite3.Row
    try:
        sale = conn.execute("SELECT * FROM sale WHERE id = ?", (sale_id,)).fetchone()
        if not sale:
            return {'success': False, 'error': 'Không tìm thấy đơn hàng'}
        if sale['status'] == 'completed':
            meta = parse_renewal_note(sale['note'])
            return {
                'success': True,
                'already_completed': True,
                'tenant_id': meta.get('tenant_id'),
            }
        if (sale['business_line'] or '') != 'subscription_renewal':
            return {'success': False, 'error': 'Không phải đơn gia hạn subscription'}
    finally:
        conn.close()

    from routes.sale import complete_pos_bank_payment
    pay_result = complete_pos_bank_payment(sale_id)
    if not pay_result.get('success'):
        return pay_result

    conn = get_main_db_connection()
    try:
        sale = conn.execute("SELECT note FROM sale WHERE id = ?", (sale_id,)).fetchone()
    finally:
        conn.close()

    meta = parse_renewal_note(sale['note'] if sale else '')
    tenant_id = meta.get('tenant_id')
    plan_code = meta.get('plan', 'DV001')
    years = int(meta.get('years') or 1)

    if not tenant_id:
        return {'success': False, 'error': 'Thiếu tenant_id trong ghi chú đơn hàng'}

    ok, expiry = activate_tenant_after_renewal(tenant_id, years, plan_code)
    if not ok:
        return {'success': False, 'error': expiry}

    invoice_result = try_issue_renewal_invoice(sale_id)

    return {
        'success': True,
        'sale_id': sale_id,
        'tenant_id': tenant_id,
        'expiry_date': expiry,
        'plan_code': plan_code,
        'invoice': invoice_result,
    }


def try_issue_renewal_invoice(sale_id):
    """Thử xuất HĐĐT cho đơn gia hạn trên main DB."""
    try:
        from flask import current_app
        fn = current_app.config.get('issue_invoice_for_sale')
        if fn:
            return fn(sale_id)
        return {'success': False, 'skipped': True, 'reason': 'invoice_fn_not_configured'}
    except Exception as exc:
        logger.warning("try_issue_renewal_invoice sale_id=%s: %s", sale_id, exc)
        return {'success': False, 'error': str(exc)}


def send_trial_account_emails(
    tenant_id, phone, business_name, customer_email, customer_password,
    support_username, support_password, business_line,
    trial_months=None,
):
    from Services.email_service import send_email

    login_url = f"/{tenant_id}/login"
    bl_label = BUSINESS_LINE_OPTIONS.get(business_line, {}).get('label', business_line)
    months = trial_months if trial_months is not None else get_trial_months()

    if customer_email:
        subject = f"[KETO] Tài khoản dùng thử {months} tháng đã sẵn sàng"
        body = f"""Kính gửi {business_name},

Tài khoản dùng thử KETO ALL IN ONE của bạn đã được kích hoạt.

Đường dẫn: {login_url}
Tên đăng nhập: {phone}
Mật khẩu: {customer_password}
Ngành: {bl_label}
Thời hạn dùng thử: {months} tháng

Vui lòng đăng nhập và đổi mật khẩu ngay sau lần đăng nhập đầu tiên.

Trân trọng,
KETO ALL IN ONE"""
        send_email(customer_email, subject, body)

    support_subject = f"[KETO Trial] Tenant mới {tenant_id} — {business_name}"
    support_body = f"""Tenant trial mới:

Tenant ID: {tenant_id}
Tên HKD: {business_name}
SĐT: {phone}
Email khách: {customer_email}
Ngành: {bl_label}

URL: {login_url}
Tài khoản khách — user: {phone} / pass: {customer_password}
Tài khoản hỗ trợ — user: {support_username} / pass: {support_password}
"""
    send_email(SUPPORT_NOTIFY_EMAIL, support_subject, support_body)


def provision_tenant(
    tenant_id,
    business_name,
    phone,
    *,
    email='',
    address='',
    tax_code='',
    business_line='pos',
    hkd_sector='NN1',
    enabled_nn_sectors=None,
    revenue_tier='DT1',
    accounting_regime='HKD',
    expiry_date=None,
    representative_name='',
    customer_password=None,
    support_username=None,
    support_password=None,
    google_email='',
    subscription_plan='',
    extra_settings=None,
    send_emails=False,
    email_context=None,
    empty_business_data=False,
):
    """Luồng provisioning thống nhất — Master và Google trial."""
    from tenant_middleware import init_tenant_database
    from Services.tenant_profile import build_tenant_settings, is_sme_regime, normalize_revenue_tier

    if get_tenant_record(tenant_id, include_inactive=True):
        return {'success': False, 'error': f"Tenant '{tenant_id}' đã tồn tại"}

    if business_line not in BUSINESS_LINE_OPTIONS:
        return {'success': False, 'error': 'Ngành kinh doanh không hợp lệ'}

    sme = is_sme_regime(accounting_regime)
    if sme:
        sectors, primary = [], None
        revenue_tier = None
    else:
        sectors, primary = resolve_provision_nn_profile(
            business_line, enabled_nn_sectors, hkd_sector,
        )
        revenue_tier = normalize_revenue_tier(revenue_tier)

    customer_password = customer_password or generate_password()
    support_username = support_username or f"{tenant_id}admin"
    support_password = support_password or generate_password()

    trial_months = get_trial_months()
    if not expiry_date:
        expiry_date = (datetime.now() + relativedelta(months=trial_months)).strftime('%Y-%m-%d')

    settings = build_tenant_settings(
        business_line=business_line,
        hkd_sector=primary or 'NN1',
        enabled_nn_sectors=sectors,
        revenue_tier=revenue_tier or 'DT1',
        accounting_regime=accounting_regime,
        subscription_plan=subscription_plan or 'trial',
        onboarding_completed=False,
        extra={
            'trial_months': trial_months,
            **(extra_settings or {}),
        },
    )

    try:
        result = init_tenant_database(
            tenant_id,
            business_name,
            phone,
            email=google_email or email,
            address=address,
            expiry_date=expiry_date,
            tax_code=tax_code,
            business_line=business_line,
            hkd_sector=primary,
            revenue_tier=revenue_tier,
            accounting_regime=accounting_regime,
            representative_name=representative_name,
            contact_email=email,
            customer_password=customer_password,
            support_username=support_username,
            support_password=support_password,
            settings_json=settings,
            subscription_plan=subscription_plan,
            enabled_nn_sectors=sectors,
            empty_business_data=empty_business_data,
        )
    except Exception as exc:
        logger.exception('provision_tenant: %s', exc)
        return {'success': False, 'error': str(exc)}

    if send_emails:
        ctx = email_context or {}
        send_trial_account_emails(
            tenant_id,
            phone,
            business_name,
            google_email or email,
            customer_password,
            support_username,
            support_password,
            ctx.get('business_line', business_line),
            trial_months=trial_months,
        )

    return {
        'success': True,
        'tenant_id': tenant_id,
        'db_path': result,
        'username': phone,
        'password': customer_password,
        'expiry_date': expiry_date,
        'support_username': support_username,
        'revenue_tier': revenue_tier,
        'accounting_regime': accounting_regime,
    }


def provision_trial_tenant(
    tenant_id,
    business_name,
    phone,
    email,
    address,
    tax_code,
    business_line,
    hkd_sector,
    google_email,
    representative_name='',
    revenue_tier='DT1',
    accounting_regime='HKD',
    enabled_nn_sectors=None,
    extra_settings=None,
):
    """Tạo tenant dùng thử — bọc provision_tenant."""
    result = provision_tenant(
        tenant_id,
        business_name,
        phone,
        email=email,
        address=address,
        tax_code=tax_code,
        business_line=business_line,
        hkd_sector=hkd_sector,
        enabled_nn_sectors=enabled_nn_sectors,
        revenue_tier=revenue_tier,
        accounting_regime=accounting_regime,
        google_email=google_email,
        representative_name=representative_name,
        subscription_plan='trial',
        extra_settings=extra_settings,
        send_emails=True,
        email_context={'business_line': business_line},
        empty_business_data=True,
    )
    if not result.get('success'):
        return result

    return result
