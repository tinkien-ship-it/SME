"""Vai trò kế toán ổn định + resolve ra leaf postable.

Nghiệp vụ gọi role (vd cogs.goods.domestic), không hardcode mã leaf.
Khi DN mở/xóa TK con và gán mặc định, backend không cần sửa code.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from db_utils import sqlite_commit
from Services.sme.coa_service import ensure_sme_coa_ready, get_account

ROLES_SEED_VERSION = 'account_roles_v4_2026-09-bdsdt'

DEFAULT_ACCOUNT_ROLES: list[dict[str, str]] = [
    {
        'role_key': 'cogs.goods.domestic',
        'root_hint': '6321',
        'default_account': '6321',
        'label': 'Giá vốn hàng hóa (nội địa)',
        'description': 'GV HH bán trong nước — mặc định 6321; mở 63211… nếu cần tách',
        'category': 'cogs',
    },
    {
        'role_key': 'cogs.goods.export',
        'root_hint': '6321',
        'default_account': '6321',
        'label': 'Giá vốn hàng hóa (xuất khẩu)',
        'description': 'GV HH xuất khẩu — mặc định 6321; mở 63212… nếu cần tách',
        'category': 'cogs',
    },
    {
        'role_key': 'cogs.fg.domestic',
        'root_hint': '6322',
        'default_account': '6322',
        'label': 'Giá vốn thành phẩm (nội địa)',
        'description': 'GV TP bán trong nước — mặc định 6322',
        'category': 'cogs',
    },
    {
        'role_key': 'cogs.fg.export',
        'root_hint': '6322',
        'default_account': '6322',
        'label': 'Giá vốn thành phẩm (xuất khẩu)',
        'description': 'GV TP xuất khẩu — mặc định 6322',
        'category': 'cogs',
    },
    {
        'role_key': 'cogs.service.processing',
        'root_hint': '6323',
        'default_account': '6323',
        'label': 'Giá vốn dịch vụ',
        'description': 'GV dịch vụ — mặc định 6323',
        'category': 'cogs',
    },
    {
        'role_key': 'cogs.service.other',
        'root_hint': '6323',
        'default_account': '6323',
        'label': 'Giá vốn dịch vụ khác',
        'description': 'GV dịch vụ khác — mặc định 6323; mở TK con nếu cần tách',
        'category': 'cogs',
    },
    {
        'role_key': 'cogs.spoilage',
        'root_hint': '6328',
        'default_account': '6328',
        'label': 'Hao hụt / mất mát',
        'description': 'Hao hụt, mất mát, hàng hỏng quá hạn',
        'category': 'cogs',
    },
    {
        'role_key': 'revenue.goods',
        'root_hint': '5111',
        'default_account': '5111',
        'label': 'Doanh thu hàng hóa',
        'description': 'DT bán hàng hóa',
        'category': 'revenue',
    },
    {
        'role_key': 'revenue.fg',
        'root_hint': '5112',
        'default_account': '5112',
        'label': 'Doanh thu thành phẩm',
        'description': 'DT bán thành phẩm',
        'category': 'revenue',
    },
    {
        'role_key': 'revenue.service',
        'root_hint': '5113',
        'default_account': '5113',
        'label': 'Doanh thu dịch vụ',
        'description': 'DT cung cấp dịch vụ',
        'category': 'revenue',
    },
    {
        'role_key': 'revenue.investment_property',
        'root_hint': '5117',
        'default_account': '5117',
        'label': 'Doanh thu bất động sản đầu tư',
        'description': 'Doanh thu bán/cho thuê BĐSĐT; tự rơi về 511 nếu DN không mở 5117',
        'category': 'revenue',
    },
    {
        'role_key': 'cogs.investment_property',
        'root_hint': '6327',
        'default_account': '6327',
        'label': 'Giá vốn bất động sản đầu tư',
        'description': 'Giá vốn/khấu hao/chi phí trực tiếp BĐSĐT; tự rơi về 632 nếu cần',
        'category': 'cogs',
    },
    {
        'role_key': 'asset.investment_property',
        'root_hint': '217',
        'default_account': '217',
        'label': 'Bất động sản đầu tư',
        'description': 'Nguyên giá BĐSĐT; nếu DN mở TK con 217x thì tự chọn leaf postable/default',
        'category': 'asset',
    },
    {
        'role_key': 'accum_depr.investment_property',
        'root_hint': '2147',
        'default_account': '2147',
        'label': 'Hao mòn bất động sản đầu tư',
        'description': 'Hao mòn BĐSĐT; tự rơi về 214 nếu DN không mở 2147',
        'category': 'asset',
    },
    {
        'role_key': 'vat.input.investment_property',
        'root_hint': '1332',
        'default_account': '1332',
        'label': 'Thuế GTGT đầu vào BĐSĐT',
        'description': 'VAT đầu vào BĐSĐT/TSCĐ; tự rơi về 133 nếu DN không mở 1332',
        'category': 'tax',
    },
    {
        'role_key': 'cash.till.vnd',
        'root_hint': '1111',
        'default_account': '1111',
        'label': 'Tiền mặt VND',
        'description': 'Quỹ tiền mặt Việt Nam',
        'category': 'cash',
    },
    {
        'role_key': 'cash.till.fx',
        'root_hint': '1112',
        'default_account': '1112',
        'label': 'Tiền mặt ngoại tệ',
        'description': 'Quỹ tiền mặt ngoại tệ',
        'category': 'cash',
    },
    {
        'role_key': 'cash.bank.vnd',
        'root_hint': '1121',
        'default_account': '1121',
        'label': 'Tiền gửi NH VND',
        'description': 'TGNH VND (VietQR / mặc định)',
        'category': 'cash',
    },
    {
        'role_key': 'cash.bank.fx',
        'root_hint': '1122',
        'default_account': '1122',
        'label': 'Tiền gửi NH ngoại tệ',
        'description': 'TGNH ngoại tệ mặc định',
        'category': 'cash',
    },
    {
        'role_key': 'ar.customer',
        'root_hint': '131',
        'default_account': '131',
        'label': 'Phải thu khách hàng',
        'description': 'Phải thu của khách hàng',
        'category': 'ar',
    },
    {
        'role_key': 'ap.supplier',
        'root_hint': '331',
        'default_account': '331',
        'label': 'Phải trả nhà cung cấp',
        'description': 'Phải trả người bán',
        'category': 'ap',
    },
    {
        'role_key': 'inv.goods',
        'root_hint': '156',
        'default_account': '156',
        'label': 'Kho hàng hóa',
        'description': 'Hàng hóa',
        'category': 'inventory',
    },
    {
        'role_key': 'inv.materials',
        'root_hint': '152',
        'default_account': '152',
        'label': 'Kho NVL',
        'description': 'Nguyên liệu, vật liệu',
        'category': 'inventory',
    },
    {
        'role_key': 'inv.finished',
        'root_hint': '155',
        'default_account': '155',
        'label': 'Kho thành phẩm',
        'description': 'Sản phẩm',
        'category': 'inventory',
    },
    {
        'role_key': 'inv.consignment',
        'root_hint': '157',
        'default_account': '157',
        'label': 'Hàng gửi đi bán',
        'description': 'Gửi đại lý / ký gửi nội địa; xuất kho ra cảng chờ thông quan XK',
        'category': 'inventory',
    },
    {
        'role_key': 'tax.vat.out',
        'root_hint': '33311',
        'default_account': '33311',
        'label': 'Thuế GTGT đầu ra',
        'description': 'VAT đầu ra',
        'category': 'tax',
    },
    {
        'role_key': 'tax.vat.in.domestic',
        'root_hint': '13311',
        'default_account': '13311',
        'label': 'Thuế GTGT đầu vào trong nước',
        'description': 'VAT được khấu trừ HH/DV trong nước',
        'category': 'tax',
    },
]


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_account_roles_ready(conn: sqlite3.Connection, *, commit: bool = True) -> dict[str, Any]:
    from db_utils import sqlite_is_ready, sqlite_mark_ready, sqlite_table_exists

    flag = f'account_roles:{ROLES_SEED_VERSION}'
    if sqlite_is_ready(conn, flag):
        return {'seeded': False, 'cached': True, 'roles_version': ROLES_SEED_VERSION}

    # Fast path: đã seed trên disk — tránh mở connection phụ song song (gây database is locked)
    if sqlite_table_exists(conn, 'sme_account_roles_meta'):
        try:
            row = conn.execute(
                "SELECT value FROM sme_account_roles_meta WHERE key = 'roles_version'"
            ).fetchone()
            current = row[0] if row else None
            count = conn.execute('SELECT COUNT(*) FROM sme_account_roles').fetchone()[0]
            if current == ROLES_SEED_VERSION and count > 0:
                sqlite_mark_ready(conn, flag)
                return {
                    'seeded': False,
                    'cached': True,
                    'roles_version': ROLES_SEED_VERSION,
                    'count': count,
                }
        except sqlite3.Error:
            pass

    from Services.sme.schema import ensure_sme_coa_schema
    ensure_sme_coa_schema(conn, commit=False)
    return seed_account_roles(conn, commit=commit)


def seed_account_roles(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    commit: bool = True,
) -> dict[str, Any]:
    from db_utils import (
        _is_locked_error,
        sqlite_commit,
        sqlite_is_ready,
        sqlite_mark_ready,
        sqlite_table_exists,
        with_sqlite_write,
    )

    flag = f'account_roles:{ROLES_SEED_VERSION}'
    if not force and sqlite_is_ready(conn, flag):
        return {'seeded': False, 'cached': True, 'roles_version': ROLES_SEED_VERSION}

    if not force and sqlite_table_exists(conn, 'sme_account_roles_meta'):
        try:
            row = conn.execute(
                "SELECT value FROM sme_account_roles_meta WHERE key = 'roles_version'"
            ).fetchone()
            current = row[0] if row else None
            count = conn.execute('SELECT COUNT(*) FROM sme_account_roles').fetchone()[0]
            if current == ROLES_SEED_VERSION and count > 0:
                sqlite_mark_ready(conn, flag)
                return {'seeded': False, 'roles_version': current, 'count': count}
        except sqlite3.Error:
            pass

    result: dict[str, Any] = {}
    try:
        def _write(target):
            nonlocal result
            result = _apply_account_roles(target, force=force)

        with_sqlite_write(conn, _write, commit=commit, label='seed_account_roles')
        sqlite_mark_ready(conn, flag)
        return result or {'seeded': True, 'roles_version': ROLES_SEED_VERSION}
    except sqlite3.OperationalError as exc:
        if _is_locked_error(exc):
            if sqlite_is_ready(conn, flag):
                return {'seeded': False, 'cached': True, 'roles_version': ROLES_SEED_VERSION}
            return {'seeded': False, 'skipped': True, 'reason': 'seed_busy'}
        raise


def _raw_account_postable_active(c: sqlite3.Cursor, code: str) -> bool:
    row = c.execute(
        """
        SELECT is_active, is_postable FROM sme_chart_of_accounts
        WHERE code = ?
        """,
        (str(code or '').strip(),),
    ).fetchone()
    return bool(row and int(row[0] or 0) == 1 and int(row[1] or 0) == 1)


def _apply_account_roles(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, Any]:
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_account_roles_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    row = c.execute(
        "SELECT value FROM sme_account_roles_meta WHERE key = 'roles_version'"
    ).fetchone()
    current = row[0] if row else None
    count = c.execute('SELECT COUNT(*) FROM sme_account_roles').fetchone()[0]

    if not force and current == ROLES_SEED_VERSION and count > 0:
        return {'seeded': False, 'roles_version': current, 'count': count}

    inserted = 0
    updated = 0
    for role in DEFAULT_ACCOUNT_ROLES:
        exists = c.execute(
            'SELECT default_account FROM sme_account_roles WHERE role_key = ?',
            (role['role_key'],),
        ).fetchone()
        new_root = role['root_hint']
        new_default = role['default_account']
        if exists:
            old_default = str(exists[0] or '').strip()
            # Giữ default DN đã chọn nếu vẫn hợp lệ dưới root mới; không thì về seed (vd 6321)
            keep = (
                bool(old_default)
                and code_belongs_to_root(old_default, new_root)
                and _raw_account_postable_active(c, old_default)
            )
            final_default = old_default if keep else new_default
            c.execute(
                """
                UPDATE sme_account_roles SET
                    root_hint = ?,
                    default_account = ?,
                    label = ?,
                    description = ?,
                    category = ?,
                    updated_at = ?
                WHERE role_key = ?
                """,
                (
                    new_root,
                    final_default,
                    role['label'],
                    role.get('description') or '',
                    role.get('category') or '',
                    _now(),
                    role['role_key'],
                ),
            )
            updated += 1
        else:
            c.execute(
                """
                INSERT INTO sme_account_roles (
                    role_key, root_hint, default_account, label, description, category, updated_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    role['role_key'],
                    new_root,
                    new_default,
                    role['label'],
                    role.get('description') or '',
                    role.get('category') or '',
                    _now(),
                ),
            )
            inserted += 1

    c.execute(
        """
        INSERT INTO sme_account_roles_meta(key, value, updated_at) VALUES ('roles_version', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (ROLES_SEED_VERSION, _now()),
    )
    count = c.execute('SELECT COUNT(*) FROM sme_account_roles').fetchone()[0]
    return {
        'seeded': True,
        'roles_version': ROLES_SEED_VERSION,
        'inserted': inserted,
        'updated': updated,
        'count': count,
    }


def list_roles(conn: sqlite3.Connection, *, category: str | None = None) -> list[dict]:
    ensure_account_roles_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    sql = 'SELECT * FROM sme_account_roles WHERE 1=1'
    params: list[Any] = []
    if category:
        sql += ' AND category = ?'
        params.append(category)
    sql += ' ORDER BY category, role_key'
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_role(conn: sqlite3.Connection, role_key: str) -> dict | None:
    ensure_account_roles_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        'SELECT * FROM sme_account_roles WHERE role_key = ?',
        (role_key,),
    ).fetchone()
    return dict(row) if row else None


def code_belongs_to_root(code: str, root_hint: str) -> bool:
    code_s = str(code or '').strip()
    root = str(root_hint or '').strip()
    if not code_s or not root:
        return False
    return code_s == root or code_s.startswith(root)


def list_postable_under_root(
    conn: sqlite3.Connection,
    root_hint: str,
    *,
    active_only: bool = True,
) -> list[dict]:
    root = str(root_hint or '').strip()
    if not root:
        return []
    ensure_sme_coa_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    params: list[Any] = [root, f'{root}%']
    try:
        sql = """
            SELECT * FROM sme_chart_of_accounts
            WHERE is_postable = 1
              AND (code = ? OR code LIKE ?)
        """
        if active_only:
            sql += ' AND is_active = 1'
        sql += ' ORDER BY is_default_posting DESC, sort_order, code'
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        sql = """
            SELECT * FROM sme_chart_of_accounts
            WHERE is_postable = 1
              AND (code = ? OR code LIKE ?)
        """
        if active_only:
            sql += ' AND is_active = 1'
        sql += ' ORDER BY sort_order, code'
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _is_postable_active(conn: sqlite3.Connection, code: str) -> bool:
    acc = get_account(conn, code, commit=False)
    return bool(acc and acc.get('is_active') and acc.get('is_postable'))


def _resolve_leaf_under_root(conn: sqlite3.Connection, root_hint: str) -> str | None:
    root = str(root_hint or '').strip()
    if not root:
        return None
    if _is_postable_active(conn, root):
        return root
    leaves = list_postable_under_root(conn, root)
    if not leaves:
        return None
    for leaf in leaves:
        if int(leaf.get('is_default_posting') or 0) == 1:
            return leaf['code']
    return leaves[0]['code']


def set_role_default(
    conn: sqlite3.Connection,
    role_key: str,
    account_code: str,
    *,
    commit: bool = True,
) -> dict:
    ensure_account_roles_ready(conn, commit=False)
    role = get_role(conn, role_key)
    if not role:
        raise ValueError(f'Không tìm thấy vai trò {role_key}')
    code = str(account_code or '').strip()
    if not code_belongs_to_root(code, role['root_hint']):
        raise ValueError(
            f'Mã {code} không thuộc nhóm {role["root_hint"]} của vai trò {role_key}'
        )
    if not _is_postable_active(conn, code):
        raise ValueError(f'Mã {code} không phải TK ghi sổ đang hoạt động')

    try:
        conn.execute(
            """
            UPDATE sme_chart_of_accounts
            SET is_default_posting = 0, updated_at = ?
            WHERE code = ? OR code LIKE ?
            """,
            (_now(), role['root_hint'], f"{role['root_hint']}%"),
        )
        conn.execute(
            """
            UPDATE sme_chart_of_accounts
            SET is_default_posting = 1, updated_at = ?
            WHERE code = ?
            """,
            (_now(), code),
        )
    except sqlite3.OperationalError:
        pass

    conn.execute(
        """
        UPDATE sme_account_roles
        SET default_account = ?, updated_at = ?
        WHERE role_key = ?
        """,
        (code, _now(), role_key),
    )
    if commit:
        sqlite_commit(conn, label='account_roles')
    return get_role(conn, role_key) or role


def set_default_posting_flag(
    conn: sqlite3.Connection,
    code: str,
    *,
    is_default: bool = True,
    commit: bool = True,
) -> dict:
    """Đánh dấu leaf làm mặc định; cập nhật mọi role thuộc cùng root."""
    ensure_account_roles_ready(conn, commit=False)
    acc = get_account(conn, code, commit=False)
    if not acc or not acc.get('is_active'):
        raise ValueError(f'Không tìm thấy tài khoản {code}')
    if is_default and not acc.get('is_postable'):
        raise ValueError('Chỉ TK ghi sổ (leaf) mới đặt làm mặc định')

    code_s = str(code).strip()
    touched_roles = roles_for_account(conn, code_s)
    if is_default:
        for role in touched_roles:
            set_role_default(conn, role['role_key'], code_s, commit=False)
        if not touched_roles:
            # Không có role — chỉ đánh dấu flag trong nhóm cha
            parent = str(acc.get('parent_code') or code_s).strip()
            try:
                conn.execute(
                    """
                    UPDATE sme_chart_of_accounts
                    SET is_default_posting = 0, updated_at = ?
                    WHERE parent_code = ? OR code = ? OR code LIKE ?
                    """,
                    (_now(), parent, parent, f'{parent}%'),
                )
                conn.execute(
                    """
                    UPDATE sme_chart_of_accounts
                    SET is_default_posting = 1, updated_at = ?
                    WHERE code = ?
                    """,
                    (_now(), code_s),
                )
            except sqlite3.OperationalError:
                pass
    else:
        try:
            conn.execute(
                """
                UPDATE sme_chart_of_accounts
                SET is_default_posting = 0, updated_at = ?
                WHERE code = ?
                """,
                (_now(), code_s),
            )
        except sqlite3.OperationalError:
            pass
        conn.execute(
            """
            UPDATE sme_account_roles
            SET default_account = root_hint, updated_at = ?
            WHERE default_account = ?
            """,
            (_now(), code_s),
        )

    if commit:
        sqlite_commit(conn, label='account_roles')
    return get_account(conn, code_s, commit=False) or acc


def roles_for_account(conn: sqlite3.Connection, code: str) -> list[dict]:
    code_s = str(code or '').strip()
    return [r for r in list_roles(conn) if code_belongs_to_root(code_s, r['root_hint'])]


def on_child_account_created(
    conn: sqlite3.Connection,
    *,
    parent_code: str,
    child_code: str,
    set_as_default: bool = False,
    commit: bool = False,
) -> list[dict]:
    """Sau tạo TK con: nếu cha đang là default của role → chuyển default sang con."""
    ensure_account_roles_ready(conn, commit=False)
    parent = str(parent_code or '').strip()
    child = str(child_code or '').strip()
    touched: list[dict] = []

    for role in list_roles(conn):
        root = role['root_hint']
        default = role.get('default_account') or root
        relevant = (
            parent == root
            or parent == default
            or code_belongs_to_root(parent, root)
            or code_belongs_to_root(child, root)
        )
        if not relevant:
            continue

        should_set = bool(set_as_default)
        if default == parent and not _is_postable_active(conn, parent):
            should_set = True
        if default == root and parent == root and not _is_postable_active(conn, root):
            should_set = True

        if should_set and _is_postable_active(conn, child):
            touched.append(set_role_default(conn, role['role_key'], child, commit=False))

    # Đồng bộ setting NH nếu tách dưới 1121/1122 và đặt mặc định
    if set_as_default or any(t.get('role_key') == 'cash.bank.vnd' for t in touched):
        if parent == '1121' or child.startswith('1121'):
            try:
                from Services.sme.bank_accounts import set_default_bank_account
                if _is_postable_active(conn, child) and child.startswith('1121'):
                    set_default_bank_account(conn, child, commit=False)
            except ValueError:
                pass
    if set_as_default or any(t.get('role_key') == 'cash.bank.fx' for t in touched):
        if parent == '1122' or child.startswith('1122'):
            try:
                from Services.sme.bank_accounts import (
                    DEFAULT_FX_BANK_SETTING_KEY,
                    _setting_set,
                    is_postable_code,
                )
                if child.startswith('1122') and is_postable_code(conn, child):
                    _setting_set(conn, DEFAULT_FX_BANK_SETTING_KEY, child)
            except Exception:
                pass

    if commit:
        sqlite_commit(conn, label='account_roles')
    return touched


def on_account_deactivated(conn: sqlite3.Connection, code: str, *, commit: bool = False) -> None:
    """Role đang trỏ mã đã tắt → chuyển về leaf còn lại hoặc root_hint."""
    ensure_account_roles_ready(conn, commit=False)
    code_s = str(code or '').strip()
    try:
        conn.execute(
            """
            UPDATE sme_chart_of_accounts
            SET is_default_posting = 0, updated_at = ?
            WHERE code = ?
            """,
            (_now(), code_s),
        )
    except sqlite3.OperationalError:
        pass

    for role in list_roles(conn):
        if role.get('default_account') != code_s:
            continue
        root = role['root_hint']
        replacement = _resolve_leaf_under_root(conn, root) or root
        conn.execute(
            """
            UPDATE sme_account_roles
            SET default_account = ?, updated_at = ?
            WHERE role_key = ?
            """,
            (replacement, _now(), role['role_key']),
        )
        if replacement != code_s and _is_postable_active(conn, replacement):
            try:
                conn.execute(
                    """
                    UPDATE sme_chart_of_accounts
                    SET is_default_posting = 1, updated_at = ?
                    WHERE code = ?
                    """,
                    (_now(), replacement),
                )
            except sqlite3.OperationalError:
                pass
    if commit:
        sqlite_commit(conn, label='account_roles')


ROLE_PARENT_FALLBACKS: dict[str, tuple[str, ...]] = {
    'revenue.investment_property': ('511',),
    'cogs.investment_property': ('632',),
    'asset.investment_property': (),
    'accum_depr.investment_property': ('214',),
    'vat.input.investment_property': ('133',),
}


def resolve_posting_account(
    conn: sqlite3.Connection,
    role_or_code: str,
    override: str | None = None,
) -> str:
    """
    Resolve role hoặc mã TK → luôn ra leaf postable.

    1. override từ FE → validate postable + thuộc root
    2. role_key (có dấu chấm) → default_account của role
    3. mã TK → postable thì dùng; nếu cha → default/first leaf; hết con → root
    """
    from Services.sme.schema import ensure_sme_coa_schema

    ensure_sme_coa_schema(conn, commit=False)
    ensure_sme_coa_ready(conn, commit=False)
    ensure_account_roles_ready(conn, commit=False)

    raw = str(role_or_code or '').strip()
    if not raw:
        raise ValueError('Thiếu mã tài khoản hoặc vai trò kế toán')

    override_s = str(override or '').strip() or None
    role = None
    root_hint = raw
    candidate = raw

    if '.' in raw:
        role = get_role(conn, raw)
        if not role:
            raise ValueError(f'Không tìm thấy vai trò kế toán: {raw}')
        root_hint = role['root_hint']
        candidate = role.get('default_account') or root_hint

    if override_s:
        if not _is_postable_active(conn, override_s):
            raise ValueError(f'Tài khoản {override_s} không ghi sổ được hoặc đã ngừng')
        if not code_belongs_to_root(override_s, root_hint):
            raise ValueError(
                f'Tài khoản {override_s} không thuộc nhóm {root_hint}'
                + (f' (vai trò {raw})' if role else '')
            )
        return override_s

    if _is_postable_active(conn, candidate):
        return candidate

    for root in (candidate, root_hint):
        leaf = _resolve_leaf_under_root(conn, root)
        if leaf:
            if role and role.get('default_account') != leaf:
                try:
                    conn.execute(
                        """
                        UPDATE sme_account_roles
                        SET default_account = ?, updated_at = ?
                        WHERE role_key = ?
                        """,
                        (leaf, _now(), role['role_key']),
                    )
                except sqlite3.Error:
                    pass
            return leaf

    # Vai trò chuyên biệt có thể rơi về TK cha chuẩn khi DN không mở TK chi tiết.
    # Chỉ dùng TK cha nếu chính TK cha đang active + postable; không bypass quy tắc parent/child.
    if role:
        for parent_root in ROLE_PARENT_FALLBACKS.get(role['role_key'], ()):
            if _is_postable_active(conn, parent_root):
                return parent_root
            leaf = _resolve_leaf_under_root(conn, parent_root)
            if leaf:
                return leaf

    if _is_postable_active(conn, root_hint):
        return root_hint

    raise ValueError(
        f'Không tìm thấy tài khoản ghi sổ cho '
        f'{"vai trò " + raw if role else "mã " + raw} '
        f'(nhóm {root_hint}). Hãy tạo TK con hoặc gán TK mặc định.'
    )
