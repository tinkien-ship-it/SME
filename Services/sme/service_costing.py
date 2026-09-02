"""Giá vốn dịch vụ SME (TT99) — job → tập hợp CP → 154 → nghiệm thu 6323.

Luồng:
1. Lập lệnh dịch vụ (job) gắn mã dịch vụ (product_type=service).
2. Ghi chi phí: NVL / NC / mua ngoài / chung — trong mức → 621|622|627 → 154;
   vượt mức → Nợ 6323 thẳng (không giữ dở dang).
3. Nghiệm thu / bàn giao: Nợ 6323 / Có 154 = phần dở dang trong mức còn lại.
4. 641/642 bị chặn — không được đưa vào giá thành dịch vụ.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from db_utils import sqlite_commit

VOUCHER_PREFIX = 'DVGT'
STATUS_OPEN = 'open'
STATUS_IN_PROGRESS = 'in_progress'
STATUS_PARTIAL = 'partial_delivered'
STATUS_DELIVERED = 'delivered'
STATUS_CANCELLED = 'cancelled'

DEFAULT_DEPARTMENTS = (
    'Bộ phận bếp',
    'Bộ phận pha chế',
    'Bộ phận cung cấp dịch vụ khách hàng',
    'Bộ phận vận hành / điều phối',
    'Bộ phận kỹ thuật / triển khai',
    'Bộ phận dự án phần mềm',
    'Bộ phận vận tải / giao nhận',
)

COST_MATERIAL = 'material'
COST_LABOR = 'labor'
COST_OUTSOURCE = 'outsource'
COST_OVERHEAD = 'overhead'
COST_TYPES = (COST_MATERIAL, COST_LABOR, COST_OUTSOURCE, COST_OVERHEAD)

# TK tuyệt đối không được tính vào giá vốn dịch vụ
FORBIDDEN_COST_ACCOUNTS = ('641', '642')

_COLLECT_BY_TYPE = {
    COST_MATERIAL: '621',
    COST_LABOR: '622',
    COST_OUTSOURCE: '627',
    COST_OVERHEAD: '627',
}

_DEFAULT_CREDIT = {
    COST_MATERIAL: '152',
    COST_LABOR: '3341',
    COST_OUTSOURCE: '331',
    COST_OVERHEAD: '1111',
}


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _today() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def _money(v) -> float:
    return round(float(v or 0), 2)


def _account_root(code: str) -> str:
    digits = ''.join(ch for ch in str(code or '') if ch.isdigit())
    return digits[:3] if len(digits) >= 3 else digits


def assert_not_selling_expense(account_code: str) -> None:
    root = _account_root(account_code)
    if root in FORBIDDEN_COST_ACCOUNTS or str(account_code or '').startswith(FORBIDDEN_COST_ACCOUNTS):
        raise ValueError(
            f'Tài khoản {account_code} thuộc 641/642 — không được tính vào giá vốn dịch vụ (TT99)'
        )


def ensure_service_costing_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voucher_seq (
            type TEXT PRIMARY KEY,
            seq INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            voucher_no TEXT NOT NULL UNIQUE,
            job_date TEXT NOT NULL,
            service_product_id INTEGER NOT NULL,
            customer_id INTEGER,
            customer_name TEXT,
            qty REAL NOT NULL DEFAULT 1,
            unit TEXT,
            note TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            sale_id INTEGER,
            total_cost REAL NOT NULL DEFAULT 0,
            wip_balance REAL NOT NULL DEFAULT 0,
            cogs_posted REAL NOT NULL DEFAULT 0,
            overnorm_posted REAL NOT NULL DEFAULT 0,
            collect_journal_entry_id INTEGER,
            deliver_journal_entry_id INTEGER,
            created_by TEXT,
            created_at TEXT,
            updated_at TEXT,
            delivered_at TEXT,
            delivered_by TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_job_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            cost_date TEXT NOT NULL,
            cost_type TEXT NOT NULL,
            description TEXT,
            amount REAL NOT NULL DEFAULT 0,
            in_norm INTEGER NOT NULL DEFAULT 1,
            product_id INTEGER,
            qty REAL,
            unit_cost REAL,
            credit_account TEXT,
            debit_collect_account TEXT,
            source_type TEXT,
            source_id INTEGER,
            journal_entry_id INTEGER,
            posted_to_wip INTEGER NOT NULL DEFAULT 0,
            posted_to_cogs INTEGER NOT NULL DEFAULT 0,
            created_at TEXT,
            FOREIGN KEY (job_id) REFERENCES service_jobs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_job_journals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            step TEXT NOT NULL,
            journal_entry_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(job_id, step)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_cost_norms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_product_id INTEGER NOT NULL,
            cost_type TEXT NOT NULL,
            product_id INTEGER,
            qty_per_unit REAL DEFAULT 0,
            amount_per_unit REAL DEFAULT 0,
            note TEXT,
            UNIQUE(service_product_id, cost_type, product_id)
        )
        """
    )
    # Định mức theo mã dịch vụ (1 bộ / sản phẩm dịch vụ) — áp tự động khi lập lệnh
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_cost_standards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_product_id INTEGER NOT NULL UNIQUE,
            labor_std_per_unit REAL NOT NULL DEFAULT 0,
            oh_fixed_std_per_unit REAL NOT NULL DEFAULT 0,
            oh_variable_std_per_unit REAL NOT NULL DEFAULT 0,
            outsource_std_per_unit REAL NOT NULL DEFAULT 0,
            note TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_cost_standard_materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            standard_id INTEGER NOT NULL,
            material_product_id INTEGER NOT NULL,
            qty_per_unit REAL NOT NULL DEFAULT 0,
            unit_cost REAL,
            note TEXT,
            UNIQUE(standard_id, material_product_id),
            FOREIGN KEY (standard_id) REFERENCES service_cost_standards(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_svc_std_product ON service_cost_standards(service_product_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_svc_std_mats ON service_cost_standard_materials(standard_id)"
    )
    # Thuê ngoài: dự kiến trên lệnh → sau gán HĐ NCC
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_outsource_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            invoice_no TEXT,
            seller_name TEXT,
            assign_date TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            cost_id INTEGER,
            provisional_cost_id INTEGER,
            amount REAL NOT NULL DEFAULT 0,
            note TEXT,
            created_by TEXT,
            created_at TEXT,
            FOREIGN KEY (job_id) REFERENCES service_jobs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_svc_os_assign_inv "
        "ON service_outsource_assignments(invoice_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_svc_os_assign_job "
        "ON service_outsource_assignments(job_id)"
    )
    # Thu trước / ứng trước KH — gắn PT hoặc giao dịch NH với lệnh DV
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_job_advances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            voucher_id INTEGER,
            bank_txn_id INTEGER,
            assign_date TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            credit_account TEXT,
            note TEXT,
            created_by TEXT,
            created_at TEXT,
            FOREIGN KEY (job_id) REFERENCES service_jobs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_svc_job_adv_job "
        "ON service_job_advances(job_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_svc_job_adv_voucher "
        "ON service_job_advances(voucher_id)"
    )
    cost_cols = {r[1] for r in conn.execute('PRAGMA table_info(service_job_costs)').fetchall()}
    for col, decl in (
        ('match_status', "TEXT"),  # provisional | matched
        ('matched_invoice_id', 'INTEGER'),
        ('matched_amount', 'REAL'),
        ('vendor_name', 'TEXT'),
    ):
        if col not in cost_cols:
            try:
                conn.execute(f'ALTER TABLE service_job_costs ADD COLUMN {col} {decl}')
            except sqlite3.Error:
                pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_service_jobs_date ON service_jobs(job_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_service_jobs_status ON service_jobs(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_service_job_costs_job ON service_job_costs(job_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS service_job_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            deliver_date TEXT NOT NULL,
            percent REAL NOT NULL DEFAULT 100,
            amount REAL NOT NULL DEFAULT 0,
            note TEXT,
            journal_entry_id INTEGER,
            created_by TEXT,
            created_at TEXT,
            FOREIGN KEY (job_id) REFERENCES service_jobs(id) ON DELETE CASCADE
        )
        """
    )
    # Cột mở rộng lệnh
    cols = {r[1] for r in conn.execute('PRAGMA table_info(service_jobs)').fetchall()}
    alter_map = {
        'department': 'TEXT',
        'completion_pct': 'REAL DEFAULT 0',
        'contract_amount': 'REAL DEFAULT 0',
    }
    for col, decl in alter_map.items():
        if col not in cols:
            try:
                conn.execute(f'ALTER TABLE service_jobs ADD COLUMN {col} {decl}')
            except sqlite3.Error:
                pass
    # Optional link from sale_items
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(sale_items)').fetchall()}
        if cols and 'service_job_id' not in cols:
            conn.execute('ALTER TABLE sale_items ADD COLUMN service_job_id INTEGER')
    except sqlite3.Error:
        pass
    if commit:
        sqlite_commit(conn, label='service_costing')


def list_service_departments() -> list[str]:
    return list(DEFAULT_DEPARTMENTS)


def next_service_job_voucher(cursor) -> str:
    cursor.execute(
        "INSERT INTO voucher_seq (type, seq) VALUES (?, 1) "
        "ON CONFLICT(type) DO UPDATE SET seq = seq + 1",
        (VOUCHER_PREFIX,),
    )
    cursor.execute("SELECT seq FROM voucher_seq WHERE type = ?", (VOUCHER_PREFIX,))
    seq = int(cursor.fetchone()[0] or 1)
    return f"{VOUCHER_PREFIX}{seq:06d}"


def _assert_service_product(cursor, product_id: int) -> dict:
    cursor.execute(
        """
        SELECT id, name, product_code, unit,
               COALESCE(product_type, 'goods') AS product_type
        FROM products WHERE id = ?
        """,
        (product_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise ValueError(f'Không tìm thấy dịch vụ #{product_id}')
    if isinstance(row, sqlite3.Row):
        data = dict(row)
    else:
        data = {
            'id': row[0], 'name': row[1], 'product_code': row[2],
            'unit': row[3], 'product_type': row[4],
        }
    pt = str(data.get('product_type') or '').strip().lower()
    if pt not in ('service', 'services', 'dich_vu', 'dv'):
        raise ValueError('Chỉ sản phẩm loại dịch vụ (service) mới lập lệnh giá vốn DV')
    return data


def list_service_products(conn: sqlite3.Connection, q: str = '') -> list[dict]:
    ensure_service_costing_schema(conn)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT p.id, p.name, p.product_code, p.barcode, p.unit,
               COALESCE(p.product_type, 'goods') AS product_type,
               COALESCE(p.base_price, p.price, 0) AS price,
               CASE WHEN s.id IS NOT NULL THEN 1 ELSE 0 END AS has_standard,
               COALESCE(s.labor_std_per_unit, 0) AS labor_std_per_unit,
               COALESCE(s.oh_fixed_std_per_unit, 0) AS oh_fixed_std_per_unit,
               COALESCE(s.oh_variable_std_per_unit, 0) AS oh_variable_std_per_unit,
               COALESCE(s.outsource_std_per_unit, 0) AS outsource_std_per_unit,
               (SELECT COUNT(*) FROM service_cost_standard_materials m
                WHERE m.standard_id = s.id) AS material_line_count
        FROM products p
        LEFT JOIN service_cost_standards s ON s.service_product_id = p.id
        WHERE LOWER(COALESCE(p.product_type, '')) IN ('service', 'services', 'dich_vu', 'dv')
    """
    params: list = []
    if q:
        like = f"%{q.strip()}%"
        sql += " AND (p.name LIKE ? OR p.product_code LIKE ? OR p.barcode LIKE ?)"
        params.extend([like, like, like])
    sql += " ORDER BY p.name COLLATE NOCASE LIMIT 300"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def create_service_job(
    conn: sqlite3.Connection,
    *,
    service_product_id: int,
    job_date: str | None = None,
    qty: float = 1,
    customer_id: int | None = None,
    customer_name: str = '',
    department: str = '',
    labor_cost: float = 0,
    note: str = '',
    sale_id: int | None = None,
    apply_norms: bool = True,
    created_by: str = '',
    commit: bool = True,
) -> dict:
    ensure_service_costing_schema(conn)
    c = conn.cursor()
    prod = _assert_service_product(c, int(service_product_id))
    q = float(qty or 0)
    if q <= 0:
        raise ValueError('Số lượng dịch vụ phải > 0')
    date_s = (job_date or _today()).strip()[:10]
    voucher = next_service_job_voucher(c)
    now = _now()
    dept = (department or '').strip()
    c.execute(
        """
        INSERT INTO service_jobs (
            voucher_no, job_date, service_product_id, customer_id, customer_name,
            qty, unit, note, status, sale_id, department, completion_pct,
            created_by, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        """,
        (
            voucher, date_s, int(service_product_id),
            int(customer_id) if customer_id else None,
            (customer_name or '').strip(),
            q, prod.get('unit') or '',
            (note or '').strip(), STATUS_OPEN,
            int(sale_id) if sale_id else None,
            dept,
            (created_by or '').strip(), now, now,
        ),
    )
    job_id = int(c.lastrowid)

    applied = None
    std = get_service_cost_standard(conn, int(service_product_id)) if apply_norms else None
    if apply_norms and std and _standard_has_content(std):
        applied = apply_service_cost_standard(
            conn, job_id,
            standard=std,
            created_by=created_by,
            commit=False,
        )
    else:
        labor = _money(labor_cost)
        if labor > 0:
            add_service_job_cost(
                conn, job_id,
                cost_type=COST_LABOR,
                amount=labor,
                cost_date=date_s,
                description=f'Nhân công trực tiếp — {dept or "bộ phận thực hiện dịch vụ"}',
                in_norm=True,
                credit_account='3341',
                source_type='job_create',
                auto_post=True,
                created_by=created_by,
                commit=False,
            )

    if commit:
        sqlite_commit(conn, label='service_costing')
    out = get_service_job(conn, job_id)
    if out and applied:
        out['norms_applied'] = applied
    return out


def _recalc_job_totals(conn: sqlite3.Connection, job_id: int) -> None:
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN in_norm = 1 THEN amount ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN in_norm = 0 THEN amount ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN in_norm = 1 AND posted_to_wip = 1 THEN amount ELSE 0 END), 0)
        FROM service_job_costs WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    total_in = _money(row[0] if row else 0)
    total_over = _money(row[1] if row else 0)
    wip_posted = _money(row[2] if row else 0)
    delivered = _money(conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM service_job_deliveries WHERE job_id = ?",
        (job_id,),
    ).fetchone()[0])
    wip = max(0.0, _money(wip_posted - delivered))
    cogs = _money(total_over + delivered)
    denom = wip_posted if wip_posted > 0 else (total_in if total_in > 0 else 1.0)
    completion = min(100.0, round(delivered * 100.0 / denom, 2)) if delivered else 0.0
    conn.execute(
        """
        UPDATE service_jobs SET
            total_cost = ?, wip_balance = ?, cogs_posted = ?, overnorm_posted = ?,
            completion_pct = ?, updated_at = ?
        WHERE id = ?
        """,
        (round(total_in + total_over, 2), wip, cogs, total_over, completion, _now(), job_id),
    )


def get_service_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    ensure_service_costing_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT j.*,
               p.name AS service_name, p.product_code AS service_code,
               p.unit AS service_unit
        FROM service_jobs j
        JOIN products p ON p.id = j.service_product_id
        WHERE j.id = ?
        """,
        (job_id,),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    costs = conn.execute(
        """
        SELECT c.*,
               m.name AS material_name, m.product_code AS material_code
        FROM service_job_costs c
        LEFT JOIN products m ON m.id = c.product_id
        WHERE c.job_id = ?
        ORDER BY c.cost_date, c.id
        """,
        (job_id,),
    ).fetchall()
    data['costs'] = [dict(x) for x in costs]
    deliveries = conn.execute(
        """
        SELECT * FROM service_job_deliveries
        WHERE job_id = ? ORDER BY id
        """,
        (job_id,),
    ).fetchall()
    data['deliveries'] = [dict(x) for x in deliveries]
    data['cost_summary'] = {
        'material': _money(sum(c['amount'] for c in data['costs'] if c['cost_type'] == COST_MATERIAL)),
        'labor': _money(sum(c['amount'] for c in data['costs'] if c['cost_type'] == COST_LABOR)),
        'outsource': _money(sum(c['amount'] for c in data['costs'] if c['cost_type'] == COST_OUTSOURCE)),
        'overhead': _money(sum(c['amount'] for c in data['costs'] if c['cost_type'] == COST_OVERHEAD)),
        'in_norm': _money(sum(c['amount'] for c in data['costs'] if int(c.get('in_norm') or 0) == 1)),
        'over_norm': _money(sum(c['amount'] for c in data['costs'] if int(c.get('in_norm') or 0) == 0)),
    }
    advances = conn.execute(
        """
        SELECT a.*, v.voucher_no, v.voucher_date, v.party_name
        FROM service_job_advances a
        LEFT JOIN sme_vouchers v ON v.id = a.voucher_id
        WHERE a.job_id = ?
        ORDER BY a.assign_date DESC, a.id DESC
        """,
        (job_id,),
    ).fetchall()
    adv_rows = [dict(x) for x in advances]
    adv_total = _money(sum(x.get('amount') or 0 for x in adv_rows))
    contract = _money(data.get('contract_amount') or 0)
    data['advances'] = adv_rows
    data['advance_received'] = adv_total
    data['advance_remain_contract'] = _money(max(0.0, contract - adv_total)) if contract > 0 else None
    return data


def list_service_jobs(
    conn: sqlite3.Connection,
    *,
    date_from: str = '',
    date_to: str = '',
    status: str = '',
    q: str = '',
) -> list[dict]:
    ensure_service_costing_schema(conn)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT j.*,
               p.name AS service_name, p.product_code AS service_code
        FROM service_jobs j
        JOIN products p ON p.id = j.service_product_id
        WHERE 1=1
    """
    params: list = []
    if date_from:
        sql += " AND j.job_date >= ?"
        params.append(date_from[:10])
    if date_to:
        sql += " AND j.job_date <= ?"
        params.append(date_to[:10])
    if status:
        sql += " AND j.status = ?"
        params.append(status)
    if q:
        like = f"%{q.strip()}%"
        sql += " AND (j.voucher_no LIKE ? OR p.name LIKE ? OR p.product_code LIKE ? OR j.customer_name LIKE ?)"
        params.extend([like, like, like, like])
    sql += " ORDER BY j.job_date DESC, j.id DESC LIMIT 500"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def add_service_job_cost(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    cost_type: str,
    amount: float = 0,
    cost_date: str | None = None,
    description: str = '',
    in_norm: bool = True,
    product_id: int | None = None,
    qty: float | None = None,
    unit_cost: float | None = None,
    credit_account: str | None = None,
    debit_collect_account: str | None = None,
    source_type: str = 'manual',
    source_id: int | None = None,
    auto_post: bool = True,
    match_status: str | None = None,
    vendor_name: str = '',
    created_by: str = '',
    commit: bool = True,
) -> dict:
    ensure_service_costing_schema(conn)
    job = get_service_job(conn, job_id)
    if not job:
        raise ValueError('Không tìm thấy lệnh dịch vụ')
    if job['status'] in (STATUS_DELIVERED, STATUS_CANCELLED):
        raise ValueError('Lệnh đã nghiệm thu hoặc hủy — không thêm chi phí')

    ct = (cost_type or '').strip().lower()
    if ct not in COST_TYPES:
        raise ValueError(f'Loại chi phí không hợp lệ: {cost_type}')

    amt = _money(amount)
    if amt <= 0 and (qty is None or float(qty or 0) <= 0):
        raise ValueError('Số tiền chi phí phải > 0')

    # Material: có thể tính từ qty × WAC
    if ct == COST_MATERIAL and product_id and (amt <= 0 or qty is not None):
        from Services.inventory_stock_helpers import get_wac
        qv = float(qty or 0)
        if qv <= 0:
            raise ValueError('SL NVL phải > 0')
        wac = float(unit_cost if unit_cost is not None else get_wac(conn.cursor(), int(product_id)))
        amt = _money(qv * wac)
        unit_cost = wac
        qty = qv
        from Services.sme.inventory_ops import inventory_account_for_product
        credit_account = credit_account or inventory_account_for_product(conn, int(product_id))

    credit = (credit_account or _DEFAULT_CREDIT[ct]).strip()
    debit_collect = (debit_collect_account or _COLLECT_BY_TYPE[ct]).strip()
    assert_not_selling_expense(credit)
    assert_not_selling_expense(debit_collect)

    date_s = (cost_date or job.get('job_date') or _today())[:10]
    ms = (match_status or '').strip() or None
    if source_type == 'outsource_provisional' and not ms:
        ms = 'provisional'
    c = conn.cursor()
    # Cột mở rộng có thể chưa có trên DB rất cũ — insert theo cột hiện có
    cols = {r[1] for r in c.execute('PRAGMA table_info(service_job_costs)').fetchall()}
    fields = [
        'job_id', 'cost_date', 'cost_type', 'description', 'amount', 'in_norm',
        'product_id', 'qty', 'unit_cost', 'credit_account', 'debit_collect_account',
        'source_type', 'source_id', 'created_at',
    ]
    values: list[Any] = [
        job_id, date_s, ct, (description or '').strip(), amt,
        1 if in_norm else 0,
        int(product_id) if product_id else None,
        float(qty) if qty is not None else None,
        float(unit_cost) if unit_cost is not None else None,
        credit, debit_collect,
        (source_type or 'manual').strip(),
        int(source_id) if source_id else None,
        _now(),
    ]
    if 'match_status' in cols:
        fields.append('match_status')
        values.append(ms)
    if 'vendor_name' in cols:
        fields.append('vendor_name')
        values.append((vendor_name or '').strip() or None)
    placeholders = ', '.join('?' for _ in fields)
    c.execute(
        f"INSERT INTO service_job_costs ({', '.join(fields)}) VALUES ({placeholders})",
        values,
    )
    cost_id = int(c.lastrowid)

    # Xuất kho NVL nếu có product_id material
    if ct == COST_MATERIAL and product_id and float(qty or 0) > 0:
        _issue_material_stock(
            conn, job=job, product_id=int(product_id),
            qty=float(qty), unit_cost=float(unit_cost or 0),
            cost_date=date_s, cost_id=cost_id,
        )

    if job['status'] == STATUS_OPEN:
        conn.execute(
            "UPDATE service_jobs SET status = ?, updated_at = ? WHERE id = ?",
            (STATUS_IN_PROGRESS, _now(), job_id),
        )

    journal_info = None
    if auto_post:
        from Services.sme.service_costing_journal import post_service_cost_line
        journal_info = post_service_cost_line(
            conn, job_id, cost_id, created_by=created_by, commit=False,
        )

    _recalc_job_totals(conn, job_id)
    if commit:
        sqlite_commit(conn, label='service_costing')
    out = get_service_job(conn, job_id)
    if out:
        out['last_cost_id'] = cost_id
        out['last_journal'] = journal_info
    return out or {}


def _issue_material_stock(
    conn, *, job: dict, product_id: int, qty: float, unit_cost: float,
    cost_date: str, cost_id: int,
) -> None:
    from Services.inventory_cost import apply_cost_outbound
    from Services.inventory_stock_helpers import sync_inventory_quantity_from_moves
    from Services.stock_move_write import insert_stock_move

    c = conn.cursor()
    when = f"{cost_date} {datetime.now().strftime('%H:%M:%S')}"
    _wac, cost_used, _fifo = apply_cost_outbound(
        c, product_id, qty, unit_cost or None,
        ref_type='service_job', ref_id=job['id'], conn=conn,
    )
    insert_stock_move(c, {
        'product_id': product_id,
        'date': when,
        'type': 'export',
        'type1': 'Xuất NVL dịch vụ',
        'ref_type': 'SERVICE_JOB',
        'ref_id': int(job['id']),
        'ref_document': job.get('voucher_no'),
        'quantity': -qty,
        'cost_price': cost_used,
        'note': f"DV {job.get('voucher_no')}: NVL cost#{cost_id}",
    })
    sync_inventory_quantity_from_moves(c, product_id)
    if abs(float(cost_used or 0) - float(unit_cost or 0)) >= 0.01:
        conn.execute(
            "UPDATE service_job_costs SET unit_cost = ?, amount = ? WHERE id = ?",
            (cost_used, _money(qty * cost_used), cost_id),
        )


def allocate_overhead(
    conn: sqlite3.Connection,
    *,
    alloc_date: str | None = None,
    total_amount: float,
    credit_account: str = '1111',
    description: str = '',
    basis: str = 'qty',
    job_ids: list[int] | None = None,
    created_by: str = '',
    commit: bool = True,
) -> dict:
    """Phân bổ CPSX chung vào các job đang mở theo qty hoặc equal."""
    ensure_service_costing_schema(conn)
    assert_not_selling_expense(credit_account)
    total = _money(total_amount)
    if total <= 0:
        raise ValueError('Số tiền phân bổ phải > 0')

    conn.row_factory = sqlite3.Row
    if job_ids:
        placeholders = ','.join('?' * len(job_ids))
        jobs = [dict(r) for r in conn.execute(
            f"""
            SELECT * FROM service_jobs
            WHERE id IN ({placeholders})
              AND status IN ('open', 'in_progress')
            """,
            [int(x) for x in job_ids],
        ).fetchall()]
    else:
        jobs = [dict(r) for r in conn.execute(
            """
            SELECT * FROM service_jobs
            WHERE status IN ('open', 'in_progress')
            ORDER BY id
            """
        ).fetchall()]
    if not jobs:
        raise ValueError('Không có lệnh dịch vụ đang mở để phân bổ')

    if basis == 'qty':
        weights = [max(float(j.get('qty') or 0), 0.0001) for j in jobs]
    else:
        weights = [1.0] * len(jobs)
    wsum = sum(weights) or 1.0
    date_s = (alloc_date or _today())[:10]
    allocated = []
    remain = total
    for i, job in enumerate(jobs):
        if i == len(jobs) - 1:
            share = remain
        else:
            share = _money(total * (weights[i] / wsum))
            remain = _money(remain - share)
        if share <= 0:
            continue
        add_service_job_cost(
            conn, int(job['id']),
            cost_type=COST_OVERHEAD,
            amount=share,
            cost_date=date_s,
            description=description or f'Phân bổ CPSX chung ({basis})',
            in_norm=True,
            credit_account=credit_account,
            source_type='overhead_alloc',
            auto_post=True,
            created_by=created_by,
            commit=False,
        )
        allocated.append({'job_id': job['id'], 'voucher_no': job['voucher_no'], 'amount': share})

    if commit:
        sqlite_commit(conn, label='service_costing')
    return {
        'alloc_date': date_s,
        'total_amount': total,
        'basis': basis,
        'lines': allocated,
    }


def collect_job_to_wip(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    created_by: str = '',
    commit: bool = True,
) -> dict:
    """Tập hợp các dòng trong mức chưa vào 154."""
    from Services.sme.service_costing_journal import post_unposted_costs_to_wip
    ensure_service_costing_schema(conn)
    job = get_service_job(conn, job_id)
    if not job:
        raise ValueError('Không tìm thấy lệnh dịch vụ')
    if job['status'] == STATUS_CANCELLED:
        raise ValueError('Lệnh đã hủy')
    result = post_unposted_costs_to_wip(
        conn, job_id, created_by=created_by, commit=False,
    )
    _recalc_job_totals(conn, job_id)
    if job['status'] == STATUS_OPEN:
        conn.execute(
            "UPDATE service_jobs SET status = ?, updated_at = ? WHERE id = ?",
            (STATUS_IN_PROGRESS, _now(), job_id),
        )
    if commit:
        sqlite_commit(conn, label='service_costing')
    out = get_service_job(conn, job_id)
    if out:
        out['collect_result'] = result
    return out or {}


def deliver_service_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    deliver_date: str | None = None,
    sale_id: int | None = None,
    percent: float | None = None,
    amount: float | None = None,
    note: str = '',
    created_by: str = '',
    commit: bool = True,
) -> dict:
    """Nghiệm thu toàn bộ hoặc một phần (TT99 kịch bản 1 / 2).

    - Không truyền percent/amount → kết chuyển toàn bộ số dư 154 của lệnh (kịch bản 1).
    - percent (1–100) hoặc amount → chỉ KC phần tương ứng, giữ dở dang trên 154 (kịch bản 2).
    """
    from Services.sme.service_costing_journal import (
        post_unposted_costs_to_wip,
        post_service_delivery_cogs,
    )
    ensure_service_costing_schema(conn)
    job = get_service_job(conn, job_id)
    if not job:
        raise ValueError('Không tìm thấy lệnh dịch vụ')
    if job['status'] == STATUS_CANCELLED:
        raise ValueError('Lệnh đã hủy')
    if job['status'] == STATUS_DELIVERED:
        return job

    post_unposted_costs_to_wip(conn, job_id, created_by=created_by, commit=False)
    _recalc_job_totals(conn, job_id)
    job = get_service_job(conn, job_id)

    date_s = (deliver_date or _today())[:10]
    if sale_id:
        conn.execute(
            "UPDATE service_jobs SET sale_id = ? WHERE id = ?",
            (int(sale_id), job_id),
        )

    wip_available = _money(job.get('wip_balance') or 0)
    if wip_available <= 0:
        raise ValueError('Không còn chi phí dở dang trên tài khoản 154 để nghiệm thu')

    # Xác định số tiền kết chuyển
    cogs_amt = None
    pct = None
    if amount is not None and float(amount) > 0:
        cogs_amt = min(wip_available, _money(amount))
        pct = round(cogs_amt * 100.0 / wip_available, 4) if wip_available else 100.0
    elif percent is not None:
        pct = float(percent)
        if pct <= 0 or pct > 100:
            raise ValueError('Tỷ lệ nghiệm thu phải trong khoảng lớn hơn 0 và nhỏ hơn hoặc bằng 100')
        cogs_amt = _money(wip_available * pct / 100.0)
    else:
        pct = 100.0
        cogs_amt = wip_available

    if cogs_amt <= 0:
        raise ValueError('Số tiền nghiệm thu phải lớn hơn 0')

    journal = post_service_delivery_cogs(
        conn, job_id,
        deliver_date=date_s,
        amount=cogs_amt,
        percent=pct,
        note=note,
        created_by=created_by,
        commit=False,
    )

    c = conn.cursor()
    c.execute(
        """
        INSERT INTO service_job_deliveries (
            job_id, deliver_date, percent, amount, note, journal_entry_id, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id, date_s, pct, cogs_amt, (note or '').strip(),
            (journal or {}).get('journal_entry_id'),
            (created_by or '').strip(), _now(),
        ),
    )
    delivery_id = int(c.lastrowid)
    if journal and journal.get('journal_entry_id'):
        from Services.sme.service_costing_journal import _link_step
        _link_step(conn, job_id, f'deliver:{delivery_id}', int(journal['journal_entry_id']))

    _recalc_job_totals(conn, job_id)
    job_after = get_service_job(conn, job_id)
    remaining = _money((job_after or {}).get('wip_balance') or 0)
    if remaining <= 0.009:
        conn.execute(
            """
            UPDATE service_jobs SET
                status = ?, delivered_at = ?, delivered_by = ?, updated_at = ?,
                deliver_journal_entry_id = ?, completion_pct = 100
            WHERE id = ?
            """,
            (
                STATUS_DELIVERED, date_s, (created_by or '').strip(), _now(),
                (journal or {}).get('journal_entry_id'),
                job_id,
            ),
        )
        conn.execute(
            """
            UPDATE service_job_costs SET posted_to_cogs = 1
            WHERE job_id = ? AND in_norm = 1 AND posted_to_wip = 1
            """,
            (job_id,),
        )
    else:
        conn.execute(
            """
            UPDATE service_jobs SET
                status = ?, updated_at = ?,
                deliver_journal_entry_id = COALESCE(?, deliver_journal_entry_id)
            WHERE id = ?
            """,
            (
                STATUS_PARTIAL, _now(),
                (journal or {}).get('journal_entry_id'),
                job_id,
            ),
        )

    _recalc_job_totals(conn, job_id)
    if commit:
        sqlite_commit(conn, label='service_costing')
    out = get_service_job(conn, job_id)
    if out:
        out['deliver_journal'] = journal
        out['last_delivery_amount'] = cogs_amt
        out['last_delivery_percent'] = pct
    return out or {}


def cancel_service_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    reason: str = '',
    created_by: str = '',
    commit: bool = True,
) -> dict:
    from Services.sme.service_costing_journal import reverse_service_job_journals
    ensure_service_costing_schema(conn)
    job = get_service_job(conn, job_id)
    if not job:
        raise ValueError('Không tìm thấy lệnh dịch vụ')
    if job['status'] == STATUS_CANCELLED:
        return job
    reverse_service_job_journals(
        conn, job_id,
        reason=reason or 'Hủy lệnh giá vốn dịch vụ',
        created_by=created_by,
    )
    conn.execute('DELETE FROM service_job_deliveries WHERE job_id = ?', (job_id,))
    conn.execute(
        """
        UPDATE service_jobs SET
            status = ?, note = TRIM(COALESCE(note,'') || ?),
            wip_balance = 0, completion_pct = 0, updated_at = ?,
            collect_journal_entry_id = NULL, deliver_journal_entry_id = NULL
        WHERE id = ?
        """,
        (
            STATUS_CANCELLED,
            f" | Hủy: {(reason or '').strip()}" if reason else '',
            _now(), job_id,
        ),
    )
    if commit:
        sqlite_commit(conn, label='service_costing')
    return get_service_job(conn, job_id) or {}


def deliver_jobs_for_sale(
    conn: sqlite3.Connection,
    sale_id: int,
    *,
    created_by: str = '',
    commit: bool = False,
) -> list[dict]:
    """Khi bán hàng: nghiệm thu các job gắn sale_id hoặc sale_items.service_job_id."""
    ensure_service_costing_schema(conn)
    ids = set()
    for r in conn.execute(
        "SELECT id FROM service_jobs WHERE sale_id = ? AND status != ?",
        (int(sale_id), STATUS_CANCELLED),
    ).fetchall():
        ids.add(int(r[0]))
    try:
        cols = {x[1] for x in conn.execute('PRAGMA table_info(sale_items)').fetchall()}
        if 'service_job_id' in cols:
            for r in conn.execute(
                """
                SELECT DISTINCT service_job_id FROM sale_items
                WHERE sale_id = ? AND service_job_id IS NOT NULL AND service_job_id > 0
                """,
                (int(sale_id),),
            ).fetchall():
                ids.add(int(r[0]))
    except sqlite3.Error:
        pass

    results = []
    for jid in sorted(ids):
        job = get_service_job(conn, jid)
        if not job or job['status'] == STATUS_DELIVERED:
            continue
        conn.execute(
            "UPDATE service_jobs SET sale_id = COALESCE(sale_id, ?) WHERE id = ?",
            (int(sale_id), jid),
        )
        results.append(deliver_service_job(
            conn, jid, sale_id=sale_id, created_by=created_by, commit=False,
        ))
    if commit:
        sqlite_commit(conn, label='service_costing')
    return results


def service_costing_period_summary(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
) -> dict[str, Any]:
    ensure_service_costing_schema(conn)
    conn.row_factory = sqlite3.Row
    ym = f"{fiscal_year:04d}-{int(period):02d}"
    open_wip = _money(conn.execute(
        """
        SELECT COALESCE(SUM(wip_balance), 0) FROM service_jobs
        WHERE status IN ('open', 'in_progress')
        """
    ).fetchone()[0])
    delivered = conn.execute(
        """
        SELECT COALESCE(SUM(cogs_posted), 0), COUNT(*)
        FROM service_jobs
        WHERE status = 'delivered'
          AND strftime('%Y-%m', COALESCE(delivered_at, job_date)) = ?
        """,
        (ym,),
    ).fetchone()
    recent = [dict(r) for r in conn.execute(
        """
        SELECT j.id, j.voucher_no, j.job_date, j.status, j.total_cost,
               j.wip_balance, j.cogs_posted, p.name AS service_name
        FROM service_jobs j
        JOIN products p ON p.id = j.service_product_id
        ORDER BY j.job_date DESC, j.id DESC
        LIMIT 30
        """
    ).fetchall()]
    return {
        'service_wip_open': open_wip,
        'service_cogs_period': _money(delivered[0] if delivered else 0),
        'service_delivered_count': int(delivered[1] if delivered else 0),
        'recent_service_jobs': recent,
    }


def link_job_to_sale_item(
    conn: sqlite3.Connection,
    *,
    sale_item_id: int,
    job_id: int,
    commit: bool = True,
) -> None:
    ensure_service_costing_schema(conn)
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sale_items)').fetchall()}
    if 'service_job_id' not in cols:
        raise ValueError('Bảng sale_items chưa hỗ trợ service_job_id')
    job = get_service_job(conn, job_id)
    if not job:
        raise ValueError('Không tìm thấy lệnh dịch vụ')
    conn.execute(
        """
        UPDATE sale_items SET service_job_id = ?
        WHERE COALESCE(id, rowid) = ?
        """,
        (job_id, sale_item_id),
    )
    row = conn.execute(
        """
        SELECT sale_id FROM sale_items
        WHERE COALESCE(id, rowid) = ?
        """,
        (sale_item_id,),
    ).fetchone()
    if row and row[0]:
        conn.execute(
            "UPDATE service_jobs SET sale_id = COALESCE(sale_id, ?) WHERE id = ?",
            (int(row[0]), job_id),
        )
    if commit:
        sqlite_commit(conn, label='service_costing')


# ---------------------------------------------------------------------------
# Định mức giá vốn dịch vụ (theo mã dịch vụ)
# ---------------------------------------------------------------------------

def _standard_has_content(std: dict | None) -> bool:
    if not std:
        return False
    if (
        _money(std.get('labor_std_per_unit')) > 0
        or _money(std.get('oh_fixed_std_per_unit')) > 0
        or _money(std.get('oh_variable_std_per_unit')) > 0
        or _money(std.get('outsource_std_per_unit')) > 0
    ):
        return True
    mats = std.get('materials') or []
    return any(float(m.get('qty_per_unit') or 0) > 0 for m in mats)


def list_service_cost_standards(conn: sqlite3.Connection) -> list[dict]:
    ensure_service_costing_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT s.*, p.name AS service_name, p.product_code AS service_code,
               p.unit AS service_unit,
               (SELECT COUNT(*) FROM service_cost_standard_materials m
                WHERE m.standard_id = s.id) AS material_count
        FROM service_cost_standards s
        JOIN products p ON p.id = s.service_product_id
        ORDER BY p.name COLLATE NOCASE
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_service_cost_standard(
    conn: sqlite3.Connection,
    service_product_id: int,
) -> dict | None:
    ensure_service_costing_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT s.*, p.name AS service_name, p.product_code AS service_code,
               p.unit AS service_unit
        FROM service_cost_standards s
        JOIN products p ON p.id = s.service_product_id
        WHERE s.service_product_id = ?
        """,
        (int(service_product_id),),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    mats = conn.execute(
        """
        SELECT m.*, p.name AS material_name, p.product_code AS material_code,
               p.unit AS material_unit
        FROM service_cost_standard_materials m
        JOIN products p ON p.id = m.material_product_id
        WHERE m.standard_id = ?
        ORDER BY m.id
        """,
        (data['id'],),
    ).fetchall()
    data['materials'] = [dict(x) for x in mats]
    return data


def save_service_cost_standard(
    conn: sqlite3.Connection,
    *,
    service_product_id: int,
    labor_std_per_unit: float = 0,
    oh_fixed_std_per_unit: float = 0,
    oh_variable_std_per_unit: float = 0,
    outsource_std_per_unit: float = 0,
    note: str = '',
    materials: list[dict] | None = None,
    commit: bool = True,
) -> dict:
    """Lưu định mức / 1 đơn vị dịch vụ. materials: [{material_product_id, qty_per_unit, unit_cost?, note?}]"""
    ensure_service_costing_schema(conn)
    c = conn.cursor()
    prod = _assert_service_product(c, int(service_product_id))
    now = _now()
    existing = c.execute(
        "SELECT id FROM service_cost_standards WHERE service_product_id = ?",
        (int(service_product_id),),
    ).fetchone()
    vals = (
        _money(labor_std_per_unit),
        _money(oh_fixed_std_per_unit),
        _money(oh_variable_std_per_unit),
        _money(outsource_std_per_unit),
        (note or '').strip(),
        now,
    )
    if existing:
        sid = int(existing[0])
        c.execute(
            """
            UPDATE service_cost_standards SET
                labor_std_per_unit = ?, oh_fixed_std_per_unit = ?,
                oh_variable_std_per_unit = ?, outsource_std_per_unit = ?,
                note = ?, updated_at = ?
            WHERE id = ?
            """,
            (*vals, sid),
        )
        c.execute(
            "DELETE FROM service_cost_standard_materials WHERE standard_id = ?",
            (sid,),
        )
    else:
        c.execute(
            """
            INSERT INTO service_cost_standards (
                service_product_id, labor_std_per_unit, oh_fixed_std_per_unit,
                oh_variable_std_per_unit, outsource_std_per_unit, note, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (int(service_product_id), *vals),
        )
        sid = int(c.lastrowid)

    for raw in materials or []:
        if not isinstance(raw, dict):
            continue
        try:
            mid = int(raw.get('material_product_id') or raw.get('product_id') or 0)
            qpu = float(raw.get('qty_per_unit') or 0)
        except (TypeError, ValueError):
            continue
        if mid <= 0 or qpu <= 0:
            continue
        uc = raw.get('unit_cost')
        try:
            uc_f = float(uc) if uc not in (None, '') else None
        except (TypeError, ValueError):
            uc_f = None
        c.execute(
            """
            INSERT INTO service_cost_standard_materials (
                standard_id, material_product_id, qty_per_unit, unit_cost, note
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (sid, mid, qpu, uc_f, (raw.get('note') or '').strip()),
        )

    if commit:
        sqlite_commit(conn, label='service_costing')
    out = get_service_cost_standard(conn, int(service_product_id))
    if out:
        out['service_name'] = out.get('service_name') or prod.get('name')
    return out or {}


def delete_service_cost_standard(
    conn: sqlite3.Connection,
    service_product_id: int,
    *,
    commit: bool = True,
) -> None:
    ensure_service_costing_schema(conn)
    row = conn.execute(
        "SELECT id FROM service_cost_standards WHERE service_product_id = ?",
        (int(service_product_id),),
    ).fetchone()
    if not row:
        raise ValueError('Chưa có định mức cho dịch vụ này')
    sid = int(row[0])
    conn.execute("DELETE FROM service_cost_standard_materials WHERE standard_id = ?", (sid,))
    conn.execute("DELETE FROM service_cost_standards WHERE id = ?", (sid,))
    if commit:
        sqlite_commit(conn, label='service_costing')


def preview_service_cost_standard(
    conn: sqlite3.Connection,
    service_product_id: int,
    qty: float = 1,
) -> dict:
    """Ước tính chi phí định mức cho số lượng dịch vụ (không ghi sổ)."""
    std = get_service_cost_standard(conn, int(service_product_id))
    q = float(qty or 0)
    if q <= 0:
        raise ValueError('Số lượng phải > 0')
    if not std or not _standard_has_content(std):
        return {
            'service_product_id': int(service_product_id),
            'qty': q,
            'has_standard': False,
            'lines': [],
            'total': 0.0,
        }

    from Services.inventory_stock_helpers import get_wac

    lines: list[dict] = []
    labor = _money(std.get('labor_std_per_unit') * q)
    if labor > 0:
        lines.append({
            'cost_type': COST_LABOR, 'label': 'Nhân công trực tiếp (622)',
            'amount': labor, 'debit': '622', 'credit': '3341',
        })
    oh_f = _money(std.get('oh_fixed_std_per_unit') * q)
    if oh_f > 0:
        lines.append({
            'cost_type': COST_OVERHEAD, 'label': 'SXC định phí (6271)',
            'amount': oh_f, 'debit': '6271', 'credit': '1111',
        })
    oh_v = _money(std.get('oh_variable_std_per_unit') * q)
    if oh_v > 0:
        lines.append({
            'cost_type': COST_OVERHEAD, 'label': 'SXC biến phí (6272)',
            'amount': oh_v, 'debit': '6272', 'credit': '1111',
        })
    outs = _money(std.get('outsource_std_per_unit') * q)
    if outs > 0:
        lines.append({
            'cost_type': COST_OUTSOURCE, 'label': 'Dịch vụ mua ngoài (627)',
            'amount': outs, 'debit': '627', 'credit': '331',
        })
    for m in std.get('materials') or []:
        qpu = float(m.get('qty_per_unit') or 0)
        if qpu <= 0:
            continue
        mid = int(m['material_product_id'])
        mq = round(qpu * q, 6)
        uc = m.get('unit_cost')
        if uc in (None, ''):
            try:
                uc = float(get_wac(conn.cursor(), mid))
            except Exception:
                uc = 0.0
        else:
            uc = float(uc)
        amt = _money(mq * uc)
        lines.append({
            'cost_type': COST_MATERIAL,
            'label': f"NVL: {m.get('material_name') or mid}",
            'material_product_id': mid,
            'qty': mq,
            'unit_cost': uc,
            'amount': amt,
            'debit': '621',
            'credit': '152',
        })
    total = _money(sum(float(x.get('amount') or 0) for x in lines))
    return {
        'service_product_id': int(service_product_id),
        'service_name': std.get('service_name'),
        'qty': q,
        'has_standard': True,
        'lines': lines,
        'total': total,
        'standard': {
            'labor_std_per_unit': std.get('labor_std_per_unit'),
            'oh_fixed_std_per_unit': std.get('oh_fixed_std_per_unit'),
            'oh_variable_std_per_unit': std.get('oh_variable_std_per_unit'),
            'outsource_std_per_unit': std.get('outsource_std_per_unit'),
            'material_count': len(std.get('materials') or []),
        },
    }


def apply_service_cost_standard(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    standard: dict | None = None,
    created_by: str = '',
    commit: bool = True,
) -> dict:
    """Ghi chi phí định mức × qty lệnh vào job và hạch toán 621/622/627 → 154."""
    ensure_service_costing_schema(conn)
    job = get_service_job(conn, job_id)
    if not job:
        raise ValueError('Không tìm thấy lệnh dịch vụ')
    if job['status'] in (STATUS_DELIVERED, STATUS_CANCELLED):
        raise ValueError('Lệnh đã nghiệm thu hoặc hủy')

    std = standard or get_service_cost_standard(conn, int(job['service_product_id']))
    if not std or not _standard_has_content(std):
        raise ValueError('Dịch vụ chưa có định mức — thiết lập trước khi áp dụng')

    q = float(job.get('qty') or 0)
    if q <= 0:
        raise ValueError('Số lượng lệnh không hợp lệ')
    date_s = str(job.get('job_date') or _today())[:10]
    dept = job.get('department') or ''
    posted: list[dict] = []

    labor = _money(std.get('labor_std_per_unit') * q)
    if labor > 0:
        add_service_job_cost(
            conn, job_id, cost_type=COST_LABOR, amount=labor, cost_date=date_s,
            description=f'Định mức NCTT — {dept or "bộ phận thực hiện"}',
            in_norm=True, credit_account='3341',
            source_type='cost_standard', auto_post=True,
            created_by=created_by, commit=False,
        )
        posted.append({'cost_type': COST_LABOR, 'amount': labor})

    oh_f = _money(std.get('oh_fixed_std_per_unit') * q)
    if oh_f > 0:
        add_service_job_cost(
            conn, job_id, cost_type=COST_OVERHEAD, amount=oh_f, cost_date=date_s,
            description='Định mức SXC định phí (6271)',
            in_norm=True, credit_account='1111', debit_collect_account='6271',
            source_type='cost_standard', auto_post=True,
            created_by=created_by, commit=False,
        )
        posted.append({'cost_type': 'oh_fixed', 'amount': oh_f, 'debit': '6271'})

    oh_v = _money(std.get('oh_variable_std_per_unit') * q)
    if oh_v > 0:
        add_service_job_cost(
            conn, job_id, cost_type=COST_OVERHEAD, amount=oh_v, cost_date=date_s,
            description='Định mức SXC biến phí (6272)',
            in_norm=True, credit_account='1111', debit_collect_account='6272',
            source_type='cost_standard', auto_post=True,
            created_by=created_by, commit=False,
        )
        posted.append({'cost_type': 'oh_variable', 'amount': oh_v, 'debit': '6272'})

    outs = _money(std.get('outsource_std_per_unit') * q)
    if outs > 0:
        add_service_job_cost(
            conn, job_id, cost_type=COST_OUTSOURCE, amount=outs, cost_date=date_s,
            description='Định mức dịch vụ mua ngoài',
            in_norm=True, credit_account='331',
            source_type='cost_standard', auto_post=True,
            created_by=created_by, commit=False,
        )
        posted.append({'cost_type': COST_OUTSOURCE, 'amount': outs})

    for m in std.get('materials') or []:
        qpu = float(m.get('qty_per_unit') or 0)
        if qpu <= 0:
            continue
        mid = int(m['material_product_id'])
        mq = round(qpu * q, 6)
        uc = m.get('unit_cost')
        try:
            uc_f = float(uc) if uc not in (None, '') else None
        except (TypeError, ValueError):
            uc_f = None
        name = m.get('material_name') or f'#{mid}'
        add_service_job_cost(
            conn, job_id, cost_type=COST_MATERIAL, amount=0, cost_date=date_s,
            description=f'Định mức NVL — {name}',
            in_norm=True, product_id=mid, qty=mq, unit_cost=uc_f,
            source_type='cost_standard', auto_post=True,
            created_by=created_by, commit=False,
        )
        posted.append({
            'cost_type': COST_MATERIAL,
            'material_product_id': mid,
            'qty': mq,
        })

    if commit:
        sqlite_commit(conn, label='service_costing')
    return {
        'job_id': job_id,
        'voucher_no': job.get('voucher_no'),
        'qty': q,
        'lines': posted,
        'total': _money(sum(float(x.get('amount') or 0) for x in posted if 'amount' in x)),
    }


# ---------------------------------------------------------------------------
# Thuê ngoài: dự kiến trên lệnh → gán hóa đơn NCC
# ---------------------------------------------------------------------------

def add_outsource_provisional(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    amount: float,
    cost_date: str | None = None,
    vendor_name: str = '',
    description: str = '',
    credit_account: str = '331',
    created_by: str = '',
    commit: bool = True,
) -> dict:
    """Ghi thuê ngoài dự kiến (biết giá trước khi có HĐ): Nợ 627 / Có 331 → 154."""
    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số tiền thuê ngoài dự kiến phải > 0')
    vendor = (vendor_name or '').strip()
    desc = (description or '').strip() or (
        f'Thuê ngoài dự kiến' + (f' — {vendor}' if vendor else '')
    )
    return add_service_job_cost(
        conn, job_id,
        cost_type=COST_OUTSOURCE,
        amount=amt,
        cost_date=cost_date,
        description=desc,
        in_norm=True,
        credit_account=credit_account or '331',
        debit_collect_account='627',
        source_type='outsource_provisional',
        match_status='provisional',
        vendor_name=vendor,
        auto_post=True,
        created_by=created_by,
        commit=commit,
    )


def list_outsource_provisionals(
    conn: sqlite3.Connection,
    *,
    unmatched_only: bool = True,
) -> list[dict]:
    ensure_service_costing_schema(conn)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT c.*,
               j.voucher_no, j.job_date, j.customer_name, j.status AS job_status,
               j.service_product_id, j.qty AS job_qty,
               p.name AS service_name, p.product_code AS service_code
        FROM service_job_costs c
        JOIN service_jobs j ON j.id = c.job_id
        JOIN products p ON p.id = j.service_product_id
        WHERE c.cost_type = 'outsource'
          AND (
                c.source_type = 'outsource_provisional'
             OR COALESCE(c.match_status, '') = 'provisional'
          )
          AND j.status NOT IN ('cancelled', 'delivered')
    """
    if unmatched_only:
        sql += " AND COALESCE(c.match_status, 'provisional') = 'provisional'"
        sql += " AND COALESCE(c.matched_invoice_id, 0) = 0"
    sql += " ORDER BY j.job_date DESC, c.id DESC"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def _invoice_assigned_total(conn: sqlite3.Connection, invoice_id: int) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) FROM service_outsource_assignments
        WHERE invoice_id = ?
        """,
        (int(invoice_id),),
    ).fetchone()
    return _money(row[0] if row else 0)


def list_outsource_invoices(
    conn: sqlite3.Connection,
    *,
    date_from: str = '',
    date_to: str = '',
    q: str = '',
    unassigned_only: bool = True,
    limit: int = 200,
) -> list[dict]:
    """HĐ mua (supplier_invoice) để gán vào lệnh DV — ưu tiên còn số chưa gán."""
    ensure_service_costing_schema(conn)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT 1 FROM supplier_invoice LIMIT 1").fetchone()
    except sqlite3.Error:
        return []

    sql = """
        SELECT si.id, si.invoice_no, si.serial, si.invoice_date, si.date,
               si.seller_name, si.seller_tax_code,
               COALESCE(si.amount, 0) AS amount,
               COALESCE(si.tax_amount, 0) AS tax_amount,
               COALESCE(si.total, 0) AS total,
               si.status,
               COALESCE((
                   SELECT SUM(a.amount) FROM service_outsource_assignments a
                   WHERE a.invoice_id = si.id
               ), 0) AS assigned_amount
        FROM supplier_invoice si
        WHERE 1=1
    """
    params: list[Any] = []
    if date_from:
        sql += " AND date(COALESCE(si.invoice_date, si.date)) >= date(?)"
        params.append(date_from[:10])
    if date_to:
        sql += " AND date(COALESCE(si.invoice_date, si.date)) <= date(?)"
        params.append(date_to[:10])
    if q:
        like = f"%{q.strip()}%"
        sql += " AND (si.invoice_no LIKE ? OR si.seller_name LIKE ? OR si.seller_tax_code LIKE ?)"
        params.extend([like, like, like])
    sql += " ORDER BY date(COALESCE(si.invoice_date, si.date)) DESC, si.id DESC LIMIT ?"
    params.append(min(max(int(limit or 200), 1), 500))
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    out = []
    for r in rows:
        net = _money(r.get('amount'))
        assigned = _money(r.get('assigned_amount'))
        remain = _money(net - assigned)
        r['net_amount'] = net
        r['assigned_amount'] = assigned
        r['remain_amount'] = remain
        if unassigned_only and remain <= 0:
            continue
        out.append(r)
    return out


def list_outsource_assignments(
    conn: sqlite3.Connection,
    *,
    invoice_id: int | None = None,
    job_id: int | None = None,
    limit: int = 200,
) -> list[dict]:
    ensure_service_costing_schema(conn)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT a.*,
               j.voucher_no, j.customer_name,
               p.name AS service_name
        FROM service_outsource_assignments a
        JOIN service_jobs j ON j.id = a.job_id
        JOIN products p ON p.id = j.service_product_id
        WHERE 1=1
    """
    params: list[Any] = []
    if invoice_id:
        sql += " AND a.invoice_id = ?"
        params.append(int(invoice_id))
    if job_id:
        sql += " AND a.job_id = ?"
        params.append(int(job_id))
    sql += " ORDER BY a.id DESC LIMIT ?"
    params.append(min(max(int(limit or 200), 1), 500))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def assign_outsource_invoice(
    conn: sqlite3.Connection,
    *,
    invoice_id: int,
    allocations: list[dict],
    assign_date: str | None = None,
    note: str = '',
    created_by: str = '',
    commit: bool = True,
) -> dict:
    """Gán HĐ NCC vào các lệnh: khớp dòng dự kiến (điều chỉnh chênh) hoặc tạo dòng mới.

    allocations: [{job_id, amount, provisional_cost_id?}, ...]
    amount = giá trị trước VAT đưa vào giá thành.
    """
    ensure_service_costing_schema(conn)
    conn.row_factory = sqlite3.Row
    inv = conn.execute(
        """
        SELECT id, invoice_no, seller_name, amount, tax_amount, total,
               invoice_date, date
        FROM supplier_invoice WHERE id = ?
        """,
        (int(invoice_id),),
    ).fetchone()
    if not inv:
        raise ValueError('Không tìm thấy hóa đơn mua')
    inv = dict(inv)
    inv_net = _money(inv.get('amount'))
    if inv_net <= 0:
        raise ValueError('Hóa đơn không có giá trị trước thuế để gán')

    already = _invoice_assigned_total(conn, int(invoice_id))
    remain = _money(inv_net - already)
    if remain <= 0:
        raise ValueError('Hóa đơn đã gán hết vào lệnh dịch vụ')

    date_s = (assign_date or str(inv.get('invoice_date') or inv.get('date') or _today()))[:10]
    invoice_no = (inv.get('invoice_no') or '').strip()
    seller = (inv.get('seller_name') or '').strip()
    note_s = (note or '').strip()

    parsed: list[dict] = []
    total_alloc = 0.0
    for raw in allocations or []:
        if not isinstance(raw, dict):
            continue
        try:
            jid = int(raw.get('job_id') or 0)
            amt = _money(raw.get('amount'))
        except (TypeError, ValueError):
            continue
        if jid <= 0 or amt <= 0:
            continue
        pcid = raw.get('provisional_cost_id')
        try:
            pcid_i = int(pcid) if pcid not in (None, '') else None
        except (TypeError, ValueError):
            pcid_i = None
        parsed.append({'job_id': jid, 'amount': amt, 'provisional_cost_id': pcid_i})
        total_alloc = _money(total_alloc + amt)

    if not parsed:
        raise ValueError('Chọn ít nhất một lệnh và nhập số tiền gán > 0')
    if total_alloc > remain + 0.009:
        raise ValueError(
            f'Tổng gán {total_alloc:,.0f} vượt phần còn lại của HĐ ({remain:,.0f} ₫)'
        )

    results: list[dict] = []
    for alloc in parsed:
        jid = alloc['job_id']
        amt = alloc['amount']
        job = get_service_job(conn, jid)
        if not job:
            raise ValueError(f'Không tìm thấy lệnh #{jid}')
        if job['status'] in (STATUS_DELIVERED, STATUS_CANCELLED):
            raise ValueError(f"Lệnh {job.get('voucher_no')} đã nghiệm thu/hủy — không gán HĐ")

        provisional_cost_id = alloc.get('provisional_cost_id')
        cost_id = None
        delta_posted = 0.0

        if provisional_cost_id:
            prow = conn.execute(
                """
                SELECT * FROM service_job_costs
                WHERE id = ? AND job_id = ? AND cost_type = 'outsource'
                """,
                (int(provisional_cost_id), jid),
            ).fetchone()
            if not prow:
                raise ValueError(f'Không tìm thấy dòng dự kiến #{provisional_cost_id}')
            prow = dict(prow)
            if (prow.get('match_status') or '') == 'matched' or prow.get('matched_invoice_id'):
                raise ValueError(f"Dòng dự kiến #{provisional_cost_id} đã khớp HĐ")
            prev_amt = _money(prow.get('amount'))
            delta = _money(amt - prev_amt)
            # Đánh dấu khớp dự kiến
            conn.execute(
                """
                UPDATE service_job_costs SET
                    match_status = 'matched',
                    matched_invoice_id = ?,
                    matched_amount = ?,
                    source_id = COALESCE(source_id, ?),
                    vendor_name = COALESCE(NULLIF(vendor_name, ''), ?)
                WHERE id = ?
                """,
                (int(invoice_id), amt, int(invoice_id), seller or None, int(provisional_cost_id)),
            )
            cost_id = int(provisional_cost_id)
            if abs(delta) >= 0.01:
                # Chênh lệch: bổ sung (HĐ > dự kiến) vào giá thành
                if delta > 0:
                    adj = add_service_job_cost(
                        conn, jid,
                        cost_type=COST_OUTSOURCE,
                        amount=delta,
                        cost_date=date_s,
                        description=(
                            f'Điều chỉnh thuê ngoài theo HĐ {invoice_no}'
                            f' (dự kiến {prev_amt:,.0f} → {amt:,.0f})'
                        ),
                        in_norm=True,
                        credit_account='331',
                        debit_collect_account='627',
                        source_type='inward_invoice',
                        source_id=int(invoice_id),
                        match_status='matched',
                        vendor_name=seller,
                        auto_post=True,
                        created_by=created_by,
                        commit=False,
                    )
                    cost_id = int(adj.get('last_cost_id') or cost_id)
                    delta_posted = delta
                else:
                    # HĐ < dự kiến: ghi giảm (số dương trên dòng điều chỉnh kiểu đảo qua post riêng)
                    # Dùng bút toán điều chỉnh giảm WIP / giảm phải trả ước tính
                    from Services.sme.service_costing_journal import post_outsource_variance_credit
                    post_outsource_variance_credit(
                        conn, jid,
                        amount=abs(delta),
                        cost_date=date_s,
                        invoice_no=invoice_no,
                        provisional_cost_id=int(provisional_cost_id),
                        created_by=created_by,
                        commit=False,
                    )
                    # Giảm amount trên dòng dự kiến để báo cáo khớp
                    conn.execute(
                        """
                        UPDATE service_job_costs SET amount = ? WHERE id = ?
                        """,
                        (amt, int(provisional_cost_id)),
                    )
                    _recalc_job_totals(conn, jid)
                    delta_posted = delta
        else:
            created = add_service_job_cost(
                conn, jid,
                cost_type=COST_OUTSOURCE,
                amount=amt,
                cost_date=date_s,
                description=f'Thuê ngoài theo HĐ {invoice_no}' + (f' — {seller}' if seller else ''),
                in_norm=True,
                credit_account='331',
                debit_collect_account='627',
                source_type='inward_invoice',
                source_id=int(invoice_id),
                match_status='matched',
                vendor_name=seller,
                auto_post=True,
                created_by=created_by,
                commit=False,
            )
            cost_id = int(created.get('last_cost_id') or 0)

        if cost_id:
            conn.execute(
                """
                UPDATE service_job_costs SET
                    match_status = 'matched',
                    matched_invoice_id = ?,
                    matched_amount = ?,
                    vendor_name = COALESCE(NULLIF(vendor_name, ''), ?)
                WHERE id = ?
                """,
                (int(invoice_id), amt, seller or None, cost_id),
            )

        conn.execute(
            """
            INSERT INTO service_outsource_assignments (
                invoice_id, invoice_no, seller_name, assign_date,
                job_id, cost_id, provisional_cost_id, amount, note,
                created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(invoice_id), invoice_no, seller, date_s,
                jid, cost_id, provisional_cost_id, amt,
                note_s or None, (created_by or '').strip() or None, _now(),
            ),
        )
        results.append({
            'job_id': jid,
            'voucher_no': job.get('voucher_no'),
            'amount': amt,
            'cost_id': cost_id,
            'provisional_cost_id': provisional_cost_id,
            'delta': delta_posted,
        })

    if commit:
        sqlite_commit(conn, label='service_costing')
    return {
        'invoice_id': int(invoice_id),
        'invoice_no': invoice_no,
        'seller_name': seller,
        'invoice_net': inv_net,
        'assigned_before': already,
        'assigned_now': total_alloc,
        'remain_after': _money(remain - total_alloc),
        'lines': results,
    }


# ---------------------------------------------------------------------------
# Thu trước / ứng trước KH — gắn phiếu thu hoặc giao dịch NH với lệnh DV
# ---------------------------------------------------------------------------

# Ứng trước DV thông thường: Có 131 (không dùng 3387 — 3387 chỉ cho DV dài hạn trả trước qua màn DT chưa TH)
_ADVANCE_CREDIT_ACCOUNT = '131'


def _advance_assigned_on_voucher(
    conn: sqlite3.Connection,
    voucher_id: int,
    *,
    exclude_job_id: int | None = None,
) -> float:
    sql = """
        SELECT COALESCE(SUM(amount), 0) FROM service_job_advances
        WHERE voucher_id = ?
    """
    params: list[Any] = [int(voucher_id)]
    if exclude_job_id:
        sql += " AND job_id != ?"
        params.append(int(exclude_job_id))
    row = conn.execute(sql, params).fetchone()
    return _money(row[0] if row else 0)


def get_advance_receipt_balance(
    conn: sqlite3.Connection,
    voucher_id: int,
    *,
    exclude_job_id: int | None = None,
) -> dict[str, Any]:
    """Số dư PT thu trước còn gán được vào lệnh dịch vụ."""
    from Services.sme.vouchers import ensure_sme_voucher_schema
    ensure_service_costing_schema(conn)
    ensure_sme_voucher_schema(conn, commit=False)
    row = conn.execute(
        """
        SELECT id, voucher_no, voucher_date, party_name, amount,
               credit_account, purpose, status, voucher_type
        FROM sme_vouchers WHERE id = ?
        """,
        (int(voucher_id),),
    ).fetchone()
    if not row:
        raise ValueError(f'Không tìm thấy phiếu thu #{voucher_id}')
    v = dict(row)
    if (v.get('voucher_type') or '') != 'receipt':
        raise ValueError('Chỉ gán phiếu thu (PT)')
    if (v.get('status') or '') != 'posted':
        raise ValueError('Phiếu thu chưa ghi sổ')
    credit = str(v.get('credit_account') or '')
    purpose = (v.get('purpose') or '').strip()
    if purpose == 'customer_advance':
        raise ValueError('PT tạm ứng XK — dùng màn hình xuất khẩu')
    if not credit.startswith(_ADVANCE_CREDIT_ACCOUNT):
        if purpose != 'service_advance':
            raise ValueError('PT phải Có 131 (ứng trước khách hàng — không dùng 3387)')
    face = _money(v.get('amount') or 0)
    assigned = _advance_assigned_on_voucher(
        conn, int(voucher_id), exclude_job_id=exclude_job_id,
    )
    # Trừ phần đã gắn HĐ bán XK (nếu có)
    sale_assigned = 0.0
    try:
        from Services.sme.export_payment import get_advance_voucher_balance
        bal = get_advance_voucher_balance(conn, int(voucher_id))
        sale_used = _money(bal.get('used_fc') or 0) or _money(bal.get('used_vnd') or 0)
        if sale_used > 0:
            sale_assigned = sale_used
    except Exception:
        pass
    remain = _money(max(0.0, face - assigned - sale_assigned))
    return {
        **v,
        'assigned_to_jobs': assigned,
        'assigned_to_sales': sale_assigned,
        'remaining_amount': remain,
        'can_link_job': remain > 0,
    }


def list_advance_receipts(
    conn: sqlite3.Connection,
    *,
    customer_name: str | None = None,
    unassigned_only: bool = True,
    include_job_id: int | None = None,
    limit: int = 200,
) -> list[dict]:
    """PT ứng trước KH (Có 131) còn số dư gán lệnh DV."""
    from Services.sme.vouchers import ensure_sme_voucher_schema
    ensure_service_costing_schema(conn)
    ensure_sme_voucher_schema(conn, commit=False)
    cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_vouchers)').fetchall()}
    purpose_col = 'purpose' in cols
    sql = f"""
        SELECT id, voucher_no, voucher_date, party_name, amount,
               credit_account, reason, status
               {', purpose' if purpose_col else ", '' AS purpose"}
        FROM sme_vouchers
        WHERE voucher_type = 'receipt'
          AND status = 'posted'
          AND (
                credit_account LIKE '131%'
             OR {'purpose = \'service_advance\'' if purpose_col else '0'}
          )
          AND credit_account NOT LIKE '3387%'
        ORDER BY date(voucher_date) DESC, id DESC
        LIMIT ?
    """
    rows = conn.execute(sql, (int(limit) * 3,)).fetchall()
    name = (customer_name or '').strip().lower()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        if (d.get('purpose') or '') == 'customer_advance':
            continue
        if name and name not in (d.get('party_name') or '').lower():
            continue
        try:
            bal = get_advance_receipt_balance(
                conn, int(d['id']),
                exclude_job_id=None if not include_job_id else None,
            )
        except ValueError:
            continue
        on_this = False
        if include_job_id:
            link = conn.execute(
                """
                SELECT amount FROM service_job_advances
                WHERE voucher_id = ? AND job_id = ?
                """,
                (int(d['id']), int(include_job_id)),
            ).fetchone()
            on_this = bool(link)
            if on_this:
                avail = _money(bal['remaining_amount']) + _money(link[0])
                bal['remaining_amount'] = avail
                bal['can_link_job'] = avail > 0
        if unassigned_only and not bal.get('can_link_job') and not on_this:
            continue
        out.append({**d, **bal})
        if len(out) >= int(limit):
            break
    return out


def list_unmatched_bank_inflows(
    conn: sqlite3.Connection,
    *,
    date_from: str = '',
    date_to: str = '',
    q: str = '',
    limit: int = 100,
) -> list[dict]:
    """Giao dịch NH tiền vào chưa gắn lệnh DV."""
    ensure_service_costing_schema(conn)
    try:
        conn.execute('SELECT 1 FROM bank_transactions LIMIT 1').fetchone()
    except sqlite3.Error:
        return []
    cols = {r[1] for r in conn.execute('PRAGMA table_info(bank_transactions)').fetchall()}
    sql = """
        SELECT bt.id, bt.transaction_date, bt.amount, bt.direction,
               bt.counterparty_name, bt.content, bt.match_status,
               bt.sale_id
        FROM bank_transactions bt
        WHERE COALESCE(bt.amount, 0) > 0
    """
    params: list[Any] = []
    if 'direction' in cols:
        sql += " AND LOWER(COALESCE(bt.direction, 'in')) IN ('in', 'credit', 'thu', '')"
    if date_from:
        sql += " AND date(bt.transaction_date) >= date(?)"
        params.append(date_from[:10])
    if date_to:
        sql += " AND date(bt.transaction_date) <= date(?)"
        params.append(date_to[:10])
    if q:
        like = f"%{q.strip()}%"
        sql += " AND (bt.counterparty_name LIKE ? OR bt.content LIKE ?)"
        params.extend([like, like])
    sql += " ORDER BY date(bt.transaction_date) DESC, bt.id DESC LIMIT ?"
    params.append(min(max(int(limit or 100), 1), 300))
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    out = []
    for r in rows:
        assigned = _money(conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM service_job_advances WHERE bank_txn_id = ?",
            (int(r['id']),),
        ).fetchone()[0])
        amt = abs(_money(r.get('amount') or 0))
        remain = _money(amt - assigned)
        if remain <= 0:
            continue
        if str(r.get('match_status') or '').lower() == 'matched' and assigned <= 0:
            # Đã khớp sale khác — vẫn cho gán nếu còn số (hiếm)
            pass
        r['assigned_amount'] = assigned
        r['remain_amount'] = remain
        out.append(r)
    return out


def record_service_advance(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    amount: float,
    voucher_date: str | None = None,
    payment_method: str = 'bank',
    credit_account: str = '131',
    party_name: str = '',
    reason: str = '',
    note: str = '',
    created_by: str = '',
    commit: bool = True,
) -> dict:
    """Lập PT ứng trước và gắn lệnh: Nợ 112/111 / Có 131 (chưa ghi DT 5113)."""
    from Services.sme.vouchers import create_receipt
    ensure_service_costing_schema(conn)
    job = get_service_job(conn, job_id)
    if not job:
        raise ValueError('Không tìm thấy lệnh dịch vụ')
    if job['status'] == STATUS_CANCELLED:
        raise ValueError('Lệnh đã hủy')
    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số tiền thu trước phải > 0')
    credit = _ADVANCE_CREDIT_ACCOUNT
    name = (party_name or job.get('customer_name') or '').strip() or 'Khách hàng'
    date_s = (voucher_date or _today())[:10]
    desc = (reason or '').strip() or (
        f'Thu trước dịch vụ — lệnh {job.get("voucher_no") or job_id}'
    )
    voucher = create_receipt(
        conn,
        voucher_date=date_s,
        party_name=name,
        amount=amt,
        payment_method=payment_method or 'bank',
        credit_account=credit,
        reason=desc,
        reference_document=job.get('voucher_no') or f'DVGT-{job_id}',
        source_type='service_job',
        source_id=job_id,
        purpose='service_advance',
        created_by=created_by,
        commit=False,
    )
    conn.execute(
        """
        INSERT INTO service_job_advances (
            job_id, voucher_id, bank_txn_id, assign_date,
            amount, credit_account, note, created_by, created_at
        ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id, int(voucher['id']), date_s, amt, credit,
            (note or '').strip() or None,
            (created_by or '').strip() or None, _now(),
        ),
    )
    if commit:
        sqlite_commit(conn, label='service_costing')
    out = get_service_job(conn, job_id) or {}
    out['advance_voucher'] = voucher
    return out


def assign_advance_receipt(
    conn: sqlite3.Connection,
    *,
    voucher_id: int,
    allocations: list[dict],
    assign_date: str | None = None,
    note: str = '',
    created_by: str = '',
    commit: bool = True,
) -> dict:
    """Gắn PT thu trước đã lập vào một hoặc nhiều lệnh DV."""
    ensure_service_costing_schema(conn)
    if not allocations:
        raise ValueError('Chọn ít nhất một lệnh và số tiền gán')
    bal = get_advance_receipt_balance(conn, int(voucher_id))
    date_s = (assign_date or _today())[:10]
    note_s = (note or '').strip()
    credit = str(bal.get('credit_account') or _ADVANCE_CREDIT_ACCOUNT)
    total_alloc = 0.0
    results = []
    for raw in allocations:
        if not isinstance(raw, dict):
            continue
        try:
            jid = int(raw.get('job_id') or 0)
            amt = _money(raw.get('amount') or 0)
        except (TypeError, ValueError):
            continue
        if jid <= 0 or amt <= 0:
            continue
        job = get_service_job(conn, jid)
        if not job:
            raise ValueError(f'Không tìm thấy lệnh #{jid}')
        if job['status'] == STATUS_CANCELLED:
            raise ValueError(f'Lệnh {job.get("voucher_no")} đã hủy')
        total_alloc = _money(total_alloc + amt)
        conn.execute(
            """
            INSERT INTO service_job_advances (
                job_id, voucher_id, bank_txn_id, assign_date,
                amount, credit_account, note, created_by, created_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                jid, int(voucher_id), date_s, amt, credit,
                note_s or None, (created_by or '').strip() or None, _now(),
            ),
        )
        results.append({
            'job_id': jid,
            'voucher_no': job.get('voucher_no'),
            'amount': amt,
        })
    if not results:
        raise ValueError('Không có dòng phân bổ hợp lệ')
    remain = _money(bal.get('remaining_amount') or 0)
    if total_alloc > remain + 0.01:
        raise ValueError(
            f'Tổng gán ({total_alloc:,.0f}) vượt số dư PT ({remain:,.0f})'
        )
    if commit:
        sqlite_commit(conn, label='service_costing')
    return {
        'voucher_id': int(voucher_id),
        'voucher_no': bal.get('voucher_no'),
        'assigned_now': total_alloc,
        'remain_after': _money(remain - total_alloc),
        'lines': results,
    }


def assign_bank_txn_to_jobs(
    conn: sqlite3.Connection,
    *,
    bank_txn_id: int,
    allocations: list[dict],
    credit_account: str = '131',
    assign_date: str | None = None,
    note: str = '',
    created_by: str = '',
    commit: bool = True,
) -> dict:
    """Tạo PT từ giao dịch NH (Có 131) và gắn vào lệnh DV."""
    from Services.sme.bank_reconcile import create_receipt_from_bank_txn
    ensure_service_costing_schema(conn)
    if not allocations:
        raise ValueError('Chọn ít nhất một lệnh và số tiền gán')
    row = conn.execute(
        'SELECT * FROM bank_transactions WHERE id = ?', (int(bank_txn_id),),
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy giao dịch ngân hàng')
    txn = dict(row)
    txn_amt = abs(_money(txn.get('amount') or 0))
    if txn_amt <= 0:
        raise ValueError('Số tiền giao dịch không hợp lệ')
    already = _money(conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM service_job_advances WHERE bank_txn_id = ?",
        (int(bank_txn_id),),
    ).fetchone()[0])
    remain_txn = _money(txn_amt - already)
    date_s = (assign_date or str(txn.get('transaction_date') or '')[:10] or _today())[:10]
    credit = _ADVANCE_CREDIT_ACCOUNT
    if not credit.startswith(_ADVANCE_CREDIT_ACCOUNT):
        raise ValueError('Giao dịch NH gắn lệnh DV phải hạch toán Có 131')
    total_alloc = 0.0
    results = []
    voucher_id = None
    # Một PT cho toàn bộ lần gán (nếu chưa có)
    need_voucher = already <= 0
    voucher = None
    if need_voucher:
        first_job = None
        for raw in allocations:
            try:
                jid = int((raw or {}).get('job_id') or 0)
            except (TypeError, ValueError):
                continue
            if jid > 0:
                first_job = get_service_job(conn, jid)
                break
        name = (txn.get('counterparty_name') or '').strip()
        if not name and first_job:
            name = (first_job.get('customer_name') or '').strip()
        name = name or 'Thu từ ngân hàng'
        sum_alloc = _money(sum(_money((x or {}).get('amount') or 0) for x in allocations if isinstance(x, dict)))
        rcpt_amt = sum_alloc if sum_alloc > 0 else remain_txn
        voucher = create_receipt_from_bank_txn(
            conn,
            int(bank_txn_id),
            party_name=name,
            credit_account=credit,
            reason=(note or '').strip() or f'Gắn lệnh DV — GD NH #{bank_txn_id}',
            created_by=created_by,
            commit=False,
        )
        voucher_id = int((voucher.get('voucher') or {}).get('id') or 0)
        # Ghi đè purpose trên PT vừa tạo
        try:
            cols = {r[1] for r in conn.execute('PRAGMA table_info(sme_vouchers)').fetchall()}
            if 'purpose' in cols and voucher_id:
                conn.execute(
                    "UPDATE sme_vouchers SET purpose = 'service_advance', "
                    "source_type = 'service_job', updated_at = ? WHERE id = ?",
                    (_now(), voucher_id),
                )
        except sqlite3.Error:
            pass
    else:
        # PT đã tạo trước — lấy voucher_id từ assignment đầu
        prev = conn.execute(
            """
            SELECT voucher_id FROM service_job_advances
            WHERE bank_txn_id = ? AND voucher_id IS NOT NULL
            ORDER BY id LIMIT 1
            """,
            (int(bank_txn_id),),
        ).fetchone()
        voucher_id = int(prev[0]) if prev and prev[0] else None

    for raw in allocations:
        if not isinstance(raw, dict):
            continue
        try:
            jid = int(raw.get('job_id') or 0)
            amt = _money(raw.get('amount') or 0)
        except (TypeError, ValueError):
            continue
        if jid <= 0 or amt <= 0:
            continue
        job = get_service_job(conn, jid)
        if not job:
            raise ValueError(f'Không tìm thấy lệnh #{jid}')
        total_alloc = _money(total_alloc + amt)
        conn.execute(
            """
            INSERT INTO service_job_advances (
                job_id, voucher_id, bank_txn_id, assign_date,
                amount, credit_account, note, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                jid, voucher_id, int(bank_txn_id), date_s, amt, credit,
                (note or '').strip() or None,
                (created_by or '').strip() or None, _now(),
            ),
        )
        results.append({
            'job_id': jid,
            'voucher_no': job.get('voucher_no'),
            'amount': amt,
        })
    if not results:
        raise ValueError('Không có dòng phân bổ hợp lệ')
    if total_alloc > remain_txn + 0.01:
        raise ValueError(
            f'Tổng gán ({total_alloc:,.0f}) vượt số dư GD ({remain_txn:,.0f})'
        )
    if commit:
        sqlite_commit(conn, label='service_costing')
    return {
        'bank_txn_id': int(bank_txn_id),
        'voucher_id': voucher_id,
        'voucher': (voucher or {}).get('voucher'),
        'assigned_now': total_alloc,
        'remain_after': _money(remain_txn - total_alloc),
        'lines': results,
    }


def list_service_advance_assignments(
    conn: sqlite3.Connection,
    *,
    job_id: int | None = None,
    voucher_id: int | None = None,
    limit: int = 200,
) -> list[dict]:
    ensure_service_costing_schema(conn)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT a.*,
               j.voucher_no AS job_voucher_no, j.customer_name,
               p.name AS service_name,
               v.voucher_no AS receipt_no, v.voucher_date AS receipt_date
        FROM service_job_advances a
        JOIN service_jobs j ON j.id = a.job_id
        JOIN products p ON p.id = j.service_product_id
        LEFT JOIN sme_vouchers v ON v.id = a.voucher_id
        WHERE 1=1
    """
    params: list[Any] = []
    if job_id:
        sql += " AND a.job_id = ?"
        params.append(int(job_id))
    if voucher_id:
        sql += " AND a.voucher_id = ?"
        params.append(int(voucher_id))
    sql += " ORDER BY a.id DESC LIMIT ?"
    params.append(min(max(int(limit or 200), 1), 500))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
