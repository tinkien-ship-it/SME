"""Chi nhánh / đơn vị phụ thuộc trong một tenant SME (cùng pháp nhân)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

DEFAULT_BRANCH_CODE = 'HQ'

_BRANCH_SCHEMA_VERSION = '2026-08-03g'
_branches_schema_ready: dict[str, str] = {}


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _db_file_key(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute('PRAGMA database_list').fetchone()
        if row:
            path = row[2] if not isinstance(row, sqlite3.Row) else row['file']
            if path:
                return str(path)
    except sqlite3.Error:
        pass
    return f'conn:{id(conn)}'


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def ensure_sme_branches_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    """Idempotent — chỉ chạy DDL/seed một lần / process / DB (tránh khóa SQLite mỗi request)."""
    db_key = _db_file_key(conn)
    if _branches_schema_ready.get(db_key) == _BRANCH_SCHEMA_VERSION:
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            address TEXT,
            phone TEXT,
            is_default INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sme_branches_active ON sme_branches(is_active, code)"
    )
    # Liên kết kho → chi nhánh
    try:
        from Services.import_line_helpers import ensure_warehouse_schema
        ensure_warehouse_schema(conn)
        wh_cols = _table_cols(conn, 'warehouses')
        if 'branch_code' not in wh_cols:
            conn.execute("ALTER TABLE warehouses ADD COLUMN branch_code TEXT")
    except Exception:
        pass

    # Seed HQ nếu chưa có chi nhánh
    n = conn.execute('SELECT COUNT(*) FROM sme_branches').fetchone()[0]
    if not n:
        conn.execute(
            """
            INSERT INTO sme_branches (code, name, is_default, is_active, notes, created_at, updated_at)
            VALUES (?, ?, 1, 1, ?, ?, ?)
            """,
            (
                DEFAULT_BRANCH_CODE, 'Trụ sở chính',
                'Chi nhánh mặc định — dữ liệu cũ không gắn CN được xem như HQ',
                _now(), _now(),
            ),
        )
        try:
            conn.execute(
                "UPDATE warehouses SET branch_code = ? WHERE branch_code IS NULL OR branch_code = ''",
                (DEFAULT_BRANCH_CODE,),
            )
        except sqlite3.Error:
            pass

    # Luôn commit schema để nhả write-lock — không giữ transaction xuyên suốt render HTML
    try:
        conn.commit()
    except sqlite3.Error:
        pass
    _branches_schema_ready[db_key] = _BRANCH_SCHEMA_VERSION

def list_branches(
    conn: sqlite3.Connection,
    *,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    ensure_sme_branches_schema(conn, commit=False)
    sql = 'SELECT * FROM sme_branches'
    if active_only:
        sql += ' WHERE is_active = 1'
    sql += ' ORDER BY is_default DESC, code ASC'
    return [dict(r) for r in conn.execute(sql).fetchall()]


def get_branch(conn: sqlite3.Connection, code: str) -> dict[str, Any] | None:
    ensure_sme_branches_schema(conn, commit=False)
    row = conn.execute(
        'SELECT * FROM sme_branches WHERE code = ?', ((code or '').strip(),)
    ).fetchone()
    return dict(row) if row else None


def get_default_branch_code(conn: sqlite3.Connection) -> str:
    ensure_sme_branches_schema(conn, commit=False)
    row = conn.execute(
        'SELECT code FROM sme_branches WHERE is_active = 1 ORDER BY is_default DESC, id ASC LIMIT 1'
    ).fetchone()
    if not row:
        return DEFAULT_BRANCH_CODE
    return row[0] if not isinstance(row, sqlite3.Row) else row['code']


def resolve_posting_branch(
    conn: sqlite3.Connection,
    branch_code: str | None = None,
) -> str:
    """Mã CN ghi sổ: tham số > session posting > mặc định HQ.

    ``ALL`` / rỗng là bộ lọc báo cáo, không phải mã chi nhánh — map về CN mặc định.
    """
    ensure_sme_branches_schema(conn, commit=False)
    code = (branch_code or '').strip().upper() or None
    if code in ('ALL', '*', '-'):
        code = None
    if not code:
        try:
            from flask import has_request_context, session
            if has_request_context():
                code = (session.get('sme_branch_code') or '').strip().upper() or None
                if code in ('ALL', '*', '-'):
                    code = None
        except Exception:
            code = None
    if not code:
        return get_default_branch_code(conn)
    br = get_branch(conn, code)
    if not br or not br.get('is_active'):
        raise ValueError(f'Chi nhánh không hợp lệ: {code}')
    return code


def create_branch(
    conn: sqlite3.Connection,
    *,
    code: str,
    name: str,
    address: str = '',
    phone: str = '',
    is_default: bool = False,
    notes: str = '',
    commit: bool = False,
) -> dict[str, Any]:
    ensure_sme_branches_schema(conn, commit=False)
    code_s = (code or '').strip().upper()
    name_s = (name or '').strip()
    if not code_s or not name_s:
        raise ValueError('Thiếu mã / tên chi nhánh')
    if get_branch(conn, code_s):
        raise ValueError(f'Mã chi nhánh đã tồn tại: {code_s}')
    if is_default:
        conn.execute('UPDATE sme_branches SET is_default = 0')
    conn.execute(
        """
        INSERT INTO sme_branches
            (code, name, address, phone, is_default, is_active, notes, created_at, updated_at)
        VALUES (?,?,?,?,?,1,?,?,?)
        """,
        (code_s, name_s, address or '', phone or '', 1 if is_default else 0,
         notes or '', _now(), _now()),
    )
    if commit:
        conn.commit()
    return get_branch(conn, code_s)


def update_branch(
    conn: sqlite3.Connection,
    code: str,
    *,
    name: str | None = None,
    address: str | None = None,
    phone: str | None = None,
    is_default: bool | None = None,
    is_active: bool | None = None,
    notes: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    ensure_sme_branches_schema(conn, commit=False)
    br = get_branch(conn, code)
    if not br:
        raise ValueError('Không tìm thấy chi nhánh')
    code_s = br['code']
    if is_default:
        conn.execute('UPDATE sme_branches SET is_default = 0')
    fields = {
        'name': name if name is not None else br['name'],
        'address': address if address is not None else (br.get('address') or ''),
        'phone': phone if phone is not None else (br.get('phone') or ''),
        'is_default': 1 if (is_default if is_default is not None else br.get('is_default')) else 0,
        'is_active': 1 if (is_active if is_active is not None else br.get('is_active')) else 0,
        'notes': notes if notes is not None else (br.get('notes') or ''),
        'updated_at': _now(),
    }
    if fields['is_active'] == 0 and br.get('is_default'):
        raise ValueError('Không thể vô hiệu chi nhánh mặc định — đặt CN khác làm mặc định trước')
    conn.execute(
        """
        UPDATE sme_branches
        SET name=?, address=?, phone=?, is_default=?, is_active=?, notes=?, updated_at=?
        WHERE code=?
        """,
        (
            fields['name'], fields['address'], fields['phone'],
            fields['is_default'], fields['is_active'], fields['notes'],
            fields['updated_at'], code_s,
        ),
    )
    if commit:
        conn.commit()
    return get_branch(conn, code_s)


def get_warehouse_branch_code(
    conn: sqlite3.Connection,
    warehouse_code: str,
) -> str:
    """Mã CN của kho; thiếu / NULL → HQ."""
    ensure_sme_branches_schema(conn, commit=False)
    wh = (warehouse_code or '').strip()
    if not wh:
        return DEFAULT_BRANCH_CODE
    try:
        cols = _table_cols(conn, 'warehouses')
        if 'branch_code' not in cols:
            return DEFAULT_BRANCH_CODE
        row = conn.execute(
            'SELECT branch_code FROM warehouses WHERE code = ?', (wh,)
        ).fetchone()
        if not row:
            return DEFAULT_BRANCH_CODE
        code = (row[0] if not isinstance(row, sqlite3.Row) else row['branch_code']) or ''
        code = str(code).strip().upper()
        return code or DEFAULT_BRANCH_CODE
    except sqlite3.Error:
        return DEFAULT_BRANCH_CODE


def set_warehouse_branch(
    conn: sqlite3.Connection,
    warehouse_code: str,
    branch_code: str,
    *,
    commit: bool = False,
) -> None:
    ensure_sme_branches_schema(conn, commit=False)
    wh = (warehouse_code or '').strip()
    bc = resolve_posting_branch(conn, branch_code)
    if not wh:
        raise ValueError('Thiếu mã kho')
    conn.execute(
        'UPDATE warehouses SET branch_code = ? WHERE code = ?',
        (bc, wh),
    )
    if commit:
        conn.commit()


def request_branch_filter() -> str:
    """Lọc báo cáo vận hành từ query/session; mặc định ALL (hợp nhất)."""
    try:
        from flask import has_request_context, request, session
        if has_request_context():
            return (
                (request.args.get('branch') or '').strip().upper()
                or (session.get('sme_branch_filter') or '').strip().upper()
                or 'ALL'
            )
    except Exception:
        pass
    return 'ALL'


def backfill_asset_branches_from_warehouse(
    conn: sqlite3.Connection,
    *,
    commit: bool = False,
) -> int:
    """Gán branch_code cho TSCĐ/CCDC cũ theo kho (NULL → từ warehouses / HQ)."""
    ensure_sme_branches_schema(conn, commit=False)
    updated = 0
    for table in ('fixed_assets', 'tools_supplies'):
        try:
            cols = _table_cols(conn, table)
        except sqlite3.Error:
            continue
        if 'branch_code' not in cols:
            continue
        if 'warehouse_code' in cols:
            rows = conn.execute(
                f"""
                SELECT id, warehouse_code FROM {table}
                WHERE branch_code IS NULL OR branch_code = ''
                """
            ).fetchall()
            for r in rows:
                rid = r[0] if not isinstance(r, sqlite3.Row) else r['id']
                wh = r[1] if not isinstance(r, sqlite3.Row) else r['warehouse_code']
                bc = get_warehouse_branch_code(conn, wh or '')
                conn.execute(
                    f'UPDATE {table} SET branch_code = ? WHERE id = ?',
                    (bc, rid),
                )
                updated += 1
        else:
            conn.execute(
                f"""
                UPDATE {table} SET branch_code = ?
                WHERE branch_code IS NULL OR branch_code = ''
                """,
                (DEFAULT_BRANCH_CODE,),
            )
            updated += conn.total_changes
    if commit:
        conn.commit()
    return updated


def branch_sql_filter(
    branch_code: str | None,
    *,
    alias: str = 'je',
) -> tuple[str, list[Any]]:
    """
    Điều kiện lọc bút toán theo CN.
    - None / '' / 'ALL' → không lọc (hợp nhất pháp nhân)
    - HQ → CN = HQ hoặc NULL (dữ liệu cũ)
    - khác → đúng mã CN
    """
    code = (branch_code or '').strip().upper()
    if not code or code == 'ALL':
        return '', []
    col = f'{alias}.branch_code'
    if code == DEFAULT_BRANCH_CODE:
        return f' AND ({col} IS NULL OR {col} = ? OR {col} = \'\')', [DEFAULT_BRANCH_CODE]
    return f' AND {col} = ?', [code]


def sale_branch_filter_sql(
    conn: sqlite3.Connection,
    branch_code: str | None,
    *,
    alias: str = 's',
) -> tuple[str, list[Any]]:
    """Mệnh đề AND lọc dòng sale theo kho → chi nhánh."""
    code = (branch_code or '').strip().upper()
    if not code or code == 'ALL':
        return '', []
    sale_cols = _table_cols(conn, 'sale')
    a = alias
    if 'warehouse_code' not in sale_cols:
        bf, bp = branch_sql_filter(branch_code, alias='je')
        return f"""
            AND {a}.id IN (
                SELECT je.document_id FROM sme_journal_entries je
                WHERE je.document_type = 'SALE_REVENUE'
                  AND je.status IN ('posted', 'reversed')
                  {bf}
            )
        """, bp
    if code == DEFAULT_BRANCH_CODE:
        return f"""
            AND (
                {a}.warehouse_code IS NULL OR {a}.warehouse_code = ''
                OR {a}.warehouse_code IN (
                    SELECT code FROM warehouses
                    WHERE branch_code IS NULL OR branch_code = '' OR branch_code = ?
                )
            )
        """, [DEFAULT_BRANCH_CODE]
    return f"""
        AND {a}.warehouse_code IN (
            SELECT code FROM warehouses WHERE branch_code = ?
        )
    """, [code]


def warehouse_branch_filter_sql(
    conn: sqlite3.Connection,
    branch_code: str | None,
    *,
    table: str,
    alias: str,
) -> tuple[str, list[Any]]:
    """Mệnh đề AND lọc theo cột warehouse_code → chi nhánh."""
    code = (branch_code or '').strip().upper()
    if not code or code == 'ALL':
        return '', []
    if 'warehouse_code' not in _table_cols(conn, table):
        return '', []
    a = alias
    if code == DEFAULT_BRANCH_CODE:
        return f"""
            AND (
                {a}.warehouse_code IS NULL OR {a}.warehouse_code = ''
                OR {a}.warehouse_code IN (
                    SELECT code FROM warehouses
                    WHERE branch_code IS NULL OR branch_code = '' OR branch_code = ?
                )
            )
        """, [DEFAULT_BRANCH_CODE]
    return f"""
        AND {a}.warehouse_code IN (
            SELECT code FROM warehouses WHERE branch_code = ?
        )
    """, [code]


def import_branch_filter_sql(
    conn: sqlite3.Connection,
    branch_code: str | None,
    *,
    alias: str = 'i',
) -> tuple[str, list[Any]]:
    """Mệnh đề AND lọc phiếu nhập theo kho → chi nhánh."""
    return warehouse_branch_filter_sql(
        conn, branch_code, table='import', alias=alias,
    )


def active_report_branch_filter() -> str | None:
    """Branch filter khi tenant SME; None nếu không áp dụng (HKD/POS thuần)."""
    try:
        from Services.tenant_profile import get_current_tenant_profile
        regime = str(
            (get_current_tenant_profile() or {}).get('accounting_regime') or ''
        ).lower()
        if regime != 'sme':
            return None
        return request_branch_filter()
    except Exception:
        return None


def assert_sale_in_branch(
    conn: sqlite3.Connection,
    sale_id: int,
    branch_code: str | None = None,
) -> None:
    code = branch_code
    if code is None:
        try:
            code = request_branch_filter()
        except Exception:
            return
    bf, bp = sale_branch_filter_sql(conn, code, alias='s')
    if not bf:
        return
    row = conn.execute(
        f'SELECT s.id FROM sale s WHERE s.id = ? {bf}',
        [int(sale_id), *bp],
    ).fetchone()
    if not row:
        raise ValueError('Đơn bán không thuộc chi nhánh đang chọn')


def assert_import_in_branch(
    conn: sqlite3.Connection,
    import_id: int,
    branch_code: str | None = None,
) -> None:
    code = branch_code
    if code is None:
        try:
            code = request_branch_filter()
        except Exception:
            return
    bf, bp = import_branch_filter_sql(conn, code, alias='i')
    if not bf:
        return
    row = conn.execute(
        f'SELECT i.id FROM import i WHERE i.id = ? {bf}',
        [int(import_id), *bp],
    ).fetchone()
    if not row:
        raise ValueError('Phiếu nhập không thuộc chi nhánh đang chọn')


def branch_context(conn: sqlite3.Connection) -> dict[str, Any]:
    """Payload cho UI / context processor."""
    ensure_sme_branches_schema(conn, commit=False)
    # list_branches cũng gọi ensure — đã cache nên no-op
    branches = list_branches(conn, active_only=True)
    current = None
    try:
        from flask import has_request_context, session
        if has_request_context():
            current = (session.get('sme_branch_code') or '').strip().upper() or None
    except Exception:
        current = None
    if not current:
        current = get_default_branch_code(conn)
    multi = len(branches) > 1
    return {
        'branches': branches,
        'current_branch_code': current,
        'default_branch_code': get_default_branch_code(conn),
        'multi_branch': multi,
        'enabled': True,
    }
