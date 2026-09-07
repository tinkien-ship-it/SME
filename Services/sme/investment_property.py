"""Sổ chi tiết Bất động sản đầu tư (TT99) — journal-first.

Bảng register chỉ giữ hồ sơ/quan hệ nghiệp vụ. Debit/Credit vẫn thuộc SME journal.
Mã BĐSĐT sinh liên tục: BDSDT000001...
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from db_utils import sqlite_commit

SCHEMA_VERSION = 'investment_property_v1_2026-09'


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _money(v: Any) -> float:
    return float(Decimal(str(v or 0)).quantize(Decimal('0.01')))


def ensure_investment_property_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sme_investment_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            property_code TEXT NOT NULL UNIQUE,
            property_name TEXT NOT NULL,
            property_type TEXT,
            usage_purpose TEXT NOT NULL DEFAULT 'CAPITAL_APPRECIATION',
            address TEXT,
            legal_document_no TEXT,
            description TEXT,
            supplier_id INTEGER,
            acquisition_date TEXT,
            recognition_date TEXT,
            original_cost REAL NOT NULL DEFAULT 0,
            asset_account_role TEXT NOT NULL DEFAULT 'asset.investment_property',
            accum_depr_account_role TEXT NOT NULL DEFAULT 'accum_depr.investment_property',
            revenue_account_role TEXT NOT NULL DEFAULT 'revenue.investment_property',
            cogs_account_role TEXT NOT NULL DEFAULT 'cogs.investment_property',
            depreciation_method TEXT,
            useful_life_months INTEGER,
            depreciation_start_date TEXT,
            depreciable INTEGER NOT NULL DEFAULT 0,
            import_id INTEGER,
            import_detail_id INTEGER,
            source_journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            sale_id INTEGER,
            sale_journal_entry_id INTEGER,
            disposal_date TEXT,
            transferred_asset_id INTEGER,
            transferred_product_id INTEGER,
            note TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_bdsdt_status ON sme_investment_properties(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bdsdt_import ON sme_investment_properties(import_id, import_detail_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bdsdt_journal ON sme_investment_properties(source_journal_entry_id)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS sme_investment_property_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_date TEXT NOT NULL,
            source_type TEXT,
            source_id INTEGER,
            journal_entry_id INTEGER,
            amount REAL NOT NULL DEFAULT 0,
            note TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(property_id) REFERENCES sme_investment_properties(id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_bdsdt_event_property ON sme_investment_property_events(property_id, event_date)")
    if commit:
        sqlite_commit(conn, label='investment_property_schema')


def next_property_code(conn: sqlite3.Connection) -> str:
    ensure_investment_property_schema(conn, commit=False)
    row = conn.execute("""
        SELECT property_code
        FROM sme_investment_properties
        WHERE property_code LIKE 'BDSDT%'
        ORDER BY CAST(substr(property_code, 6) AS INTEGER) DESC
        LIMIT 1
    """).fetchone()
    seq = 1
    if row and row[0]:
        tail = str(row[0])[5:]
        if tail.isdigit():
            seq = int(tail) + 1
    return f'BDSDT{seq:06d}'


def create_property_from_import(
    conn: sqlite3.Connection, *, import_id: int, import_detail_id: int | None,
    property_name: str, original_cost: Any, supplier_id: int | None = None,
    acquisition_date: str | None = None, source_journal_entry_id: int | None = None,
    created_by: str | None = None, property_type: str | None = None,
    usage_purpose: str = 'CAPITAL_APPRECIATION', address: str | None = None,
    note: str | None = None, asset_unit: str | None = None,
    original_quantity: Any = 1,
) -> dict[str, Any]:
    """Idempotent theo import_detail_id; không tự commit để caller giữ atomic transaction."""
    ensure_investment_property_schema(conn, commit=False)
    if import_detail_id:
        existing = conn.execute(
            "SELECT id, property_code FROM sme_investment_properties WHERE import_detail_id = ? LIMIT 1",
            (import_detail_id,),
        ).fetchone()
        if existing:
            return {'id': int(existing[0]), 'property_code': existing[1], 'created': False}
    code = next_property_code(conn)
    c = conn.cursor()
    c.execute("""
        INSERT INTO sme_investment_properties (
            property_code, property_name, property_type, usage_purpose, address,
            supplier_id, acquisition_date, recognition_date, original_cost,
            import_id, import_detail_id, source_journal_entry_id, note, created_by,
            asset_unit, original_quantity, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        code, (property_name or code).strip(), property_type, usage_purpose or 'CAPITAL_APPRECIATION', address,
        supplier_id, acquisition_date, acquisition_date, _money(original_cost),
        import_id, import_detail_id, source_journal_entry_id, note, created_by,
        (str(asset_unit or 'BĐS').strip() or 'BĐS'), max(0.0, _money(original_quantity or 1)),
        _now(), _now(),
    ))
    pid = int(c.lastrowid)
    c.execute("""
        INSERT INTO sme_investment_property_events
        (property_id, event_type, event_date, source_type, source_id, journal_entry_id, amount, note, created_by)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        pid, 'ACQUISITION', acquisition_date or _now()[:10], 'import', import_id,
        source_journal_entry_id, _money(original_cost), note, created_by,
    ))
    return {'id': pid, 'property_code': code, 'created': True}

# ========================= PHASE 2 — END-TO-END =========================

def _table_columns(conn, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def ensure_investment_property_e2e_schema(conn, *, commit: bool = True) -> None:
    """Nâng schema v1 an toàn; giữ tương thích SQLite/PostgreSQL wrapper của KETO."""
    ensure_investment_property_schema(conn, commit=False)
    cols = _table_columns(conn, 'sme_investment_properties')
    additions = {
        'sale_product_id': 'INTEGER',
        'lease_product_id': 'INTEGER',
        'lease_deferred_product_id': 'INTEGER',
        'accumulated_depreciation': 'REAL NOT NULL DEFAULT 0',
        'impairment_amount': 'REAL NOT NULL DEFAULT 0',
        'carrying_amount': 'REAL NOT NULL DEFAULT 0',
        'last_depreciation_date': 'TEXT',
        # Đơn vị/số lượng gốc lấy từ dòng mua ban đầu. Đây là nguồn chuẩn cho modal bán.
        'asset_unit': "TEXT NOT NULL DEFAULT 'BĐS'",
        'original_quantity': 'REAL NOT NULL DEFAULT 1',
    }
    for col, decl in additions.items():
        if col not in cols:
            try:
                conn.execute(f'ALTER TABLE sme_investment_properties ADD COLUMN {col} {decl}')
            except Exception:
                pass

    # Kế hoạch cho thuê / khấu hao / phân bổ doanh thu nhận trước.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sme_investment_property_lease_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id INTEGER NOT NULL,
            usage_purpose TEXT NOT NULL DEFAULT 'RENTAL',
            revenue_mode TEXT NOT NULL DEFAULT 'DIRECT_MONTHLY',
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            months INTEGER NOT NULL DEFAULT 1,
            monthly_rent_amount REAL NOT NULL DEFAULT 0,
            prepaid_net_amount REAL NOT NULL DEFAULT 0,
            monthly_revenue_amount REAL NOT NULL DEFAULT 0,
            depreciation_total REAL NOT NULL DEFAULT 0,
            monthly_depreciation_amount REAL NOT NULL DEFAULT 0,
            auto_depreciation INTEGER NOT NULL DEFAULT 0,
            auto_revenue_recognition INTEGER NOT NULL DEFAULT 0,
            posting_timing TEXT NOT NULL DEFAULT 'MONTH_END',
            last_processed_period TEXT,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            note TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(property_id) REFERENCES sme_investment_properties(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_bdsdt_lease_plan_property
        ON sme_investment_property_lease_plans(property_id, status)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sme_investment_property_period_postings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            property_id INTEGER NOT NULL,
            period_key TEXT NOT NULL,
            period_no INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            posting_date TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            journal_entry_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(plan_id, period_key, event_type),
            FOREIGN KEY(plan_id) REFERENCES sme_investment_property_lease_plans(id),
            FOREIGN KEY(property_id) REFERENCES sme_investment_properties(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_bdsdt_period_posting_property
        ON sme_investment_property_period_postings(property_id, posting_date)
    """)

    # Lớp nghiệp vụ BĐSĐT: tách dữ liệu business/invoice khỏi sale_items chung.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sme_investment_property_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            property_id INTEGER NOT NULL,
            lease_plan_id INTEGER,
            transaction_type TEXT NOT NULL,
            transaction_date TEXT NOT NULL,
            period_from TEXT,
            period_to TEXT,
            contract_no TEXT,
            asset_unit TEXT,
            asset_quantity REAL NOT NULL DEFAULT 1,
            billing_unit TEXT,
            billing_quantity REAL NOT NULL DEFAULT 1,
            unit_price REAL NOT NULL DEFAULT 0,
            recognition_periods INTEGER,
            recognition_amount_per_period REAL NOT NULL DEFAULT 0,
            customer_name TEXT,
            company_name TEXT,
            tax_code TEXT,
            address TEXT,
            email TEXT,
            phone TEXT,
            payment_method TEXT,
            net_amount REAL NOT NULL DEFAULT 0,
            deductible_land_value REAL NOT NULL DEFAULT 0,
            vat_taxable_amount REAL NOT NULL DEFAULT 0,
            vat_rate REAL NOT NULL DEFAULT 0,
            vat_amount REAL NOT NULL DEFAULT 0,
            gross_amount REAL NOT NULL DEFAULT 0,
            sale_id INTEGER,
            invoice_id TEXT,
            journal_entry_id INTEGER,
            client_uuid TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            note TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(property_id) REFERENCES sme_investment_properties(id)
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_bdsdt_tx_sale ON sme_investment_property_transactions(sale_id) WHERE sale_id IS NOT NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_bdsdt_tx_uuid ON sme_investment_property_transactions(client_uuid) WHERE client_uuid IS NOT NULL AND client_uuid<>''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bdsdt_tx_property ON sme_investment_property_transactions(property_id, transaction_date)")

    if commit:
        sqlite_commit(conn, label='investment_property_e2e_schema')


def _insert_dynamic(conn, table: str, values: dict[str, Any]) -> int:
    cols = _table_columns(conn, table)
    data = {k: v for k, v in values.items() if k in cols}
    if not data:
        raise ValueError(f'Không có cột phù hợp để INSERT {table}')
    names = list(data)
    ph = ','.join('?' for _ in names)
    cur = conn.execute(
        f'INSERT INTO "{table}" ({", ".join(names)}) VALUES ({ph})',
        tuple(data[k] for k in names),
    )
    return int(cur.lastrowid)


def _ensure_pos_product(conn, *, code: str, name: str, deferred: bool = False, unit: str = 'BĐS') -> int:
    """Tạo sản phẩm POS phi tồn kho. Prefix BDSDT giúp sale_journal phân loại 5117."""
    row = conn.execute('SELECT id FROM products WHERE product_code = ? LIMIT 1', (code,)).fetchone()
    if row:
        return int(row[0])
    values = {
        'product_code': code,
        'name': name,
        'unit': str(unit or 'BĐS').strip() or 'BĐS',
        'unit_ratio': 1,
        'base_price': 0,
        'price': 0,
        'sale_price': 0,
        'avg_cost': 0,
        # Cố ý service: checkout không kiểm/trừ tồn; revenue vẫn BDSDT do prefix mã.
        'product_type': 'service',
        'revenue_mode': 'deferred' if deferred else 'immediate',
        'is_subscription_plan': 1 if deferred else 0,
    }
    return _insert_dynamic(conn, 'products', values)


def ensure_property_pos_products(conn, property_id: int) -> dict[str, int]:
    ensure_investment_property_e2e_schema(conn, commit=False)
    row = conn.execute(
        'SELECT id, property_code, property_name, sale_product_id, lease_product_id, lease_deferred_product_id, '
        "COALESCE(NULLIF(TRIM(asset_unit),''),'BĐS') AS asset_unit "
        'FROM sme_investment_properties WHERE id = ?', (property_id,)
    ).fetchone()
    if not row:
        raise ValueError(f'Không tìm thấy BĐSĐT #{property_id}')
    pid, code, name = int(row[0]), str(row[1]), str(row[2])
    asset_unit = str(row[6] or 'BĐS').strip() or 'BĐS'
    sale_pid = int(row[3]) if row[3] else _ensure_pos_product(
        conn, code=code, name=f'[BÁN BĐSĐT] {name}', unit=asset_unit)
    lease_pid = int(row[4]) if row[4] else _ensure_pos_product(
        conn, code=f'{code}-THUE', name=f'[CHO THUÊ BĐSĐT] {name}', unit='Tháng')
    deferred_pid = int(row[5]) if row[5] else _ensure_pos_product(
        conn, code=f'{code}-THUE-TRUOC', name=f'[THUÊ TRẢ TRƯỚC] {name}', deferred=True, unit='Tháng')
    # Đồng bộ DVT cầu nối; sale_items vẫn lưu snapshot DVT theo transaction.
    try:
        conn.execute('UPDATE products SET unit=? WHERE id=?', (asset_unit, sale_pid))
        conn.execute("UPDATE products SET unit='Tháng' WHERE id IN (?,?)", (lease_pid, deferred_pid))
    except Exception:
        pass
    conn.execute(
        'UPDATE sme_investment_properties SET sale_product_id=?, lease_product_id=?, '
        'lease_deferred_product_id=?, updated_at=? WHERE id=?',
        (sale_pid, lease_pid, deferred_pid, _now(), pid),
    )
    return {'sale_product_id': sale_pid, 'lease_product_id': lease_pid,
            'lease_deferred_product_id': deferred_pid}


def refresh_property_balances(conn, property_id: int) -> dict[str, float]:
    ensure_investment_property_e2e_schema(conn, commit=False)
    row = conn.execute(
        'SELECT original_cost FROM sme_investment_properties WHERE id=?', (property_id,)
    ).fetchone()
    if not row:
        raise ValueError(f'Không tìm thấy BĐSĐT #{property_id}')
    original = _money(row[0])
    dep = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM sme_investment_property_events "
        "WHERE property_id=? AND event_type='DEPRECIATION'", (property_id,)
    ).fetchone()[0]
    imp = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM sme_investment_property_events "
        "WHERE property_id=? AND event_type='IMPAIRMENT'", (property_id,)
    ).fetchone()[0]
    dep, imp = _money(dep), _money(imp)
    carrying = max(0.0, _money(original - dep - imp))
    conn.execute(
        'UPDATE sme_investment_properties SET accumulated_depreciation=?, impairment_amount=?, '
        'carrying_amount=?, updated_at=? WHERE id=?',
        (dep, imp, carrying, _now(), property_id),
    )
    return {'original_cost': original, 'accumulated_depreciation': dep,
            'impairment_amount': imp, 'carrying_amount': carrying}


def _property_has_downstream_history(conn, property_id: int) -> bool:
    'Có nghiệp vụ sau ghi tăng thì không tự sửa ngược nguyên giá lịch sử.'
    row = conn.execute(
        '''
        SELECT 1
        FROM sme_investment_property_events
        WHERE property_id=?
          AND UPPER(COALESCE(event_type,'')) <> 'ACQUISITION'
        LIMIT 1
        ''',
        (int(property_id),),
    ).fetchone()
    if row:
        return True
    row = conn.execute(
        '''
        SELECT 1
        FROM sme_investment_property_lease_plans
        WHERE property_id=?
        LIMIT 1
        ''',
        (int(property_id),),
    ).fetchone()
    return bool(row)


def _normalize_match_text(value: Any) -> str:
    return ' '.join(str(value or '').strip().casefold().split())


def sync_properties_from_import(conn, import_id: int, *, source_journal_entry_id: int | None = None,
                                created_by: str | None = None) -> dict[str, Any]:
    'Đồng bộ register BĐSĐT với phiếu nhập, không tạo trùng khi sửa phiếu.'
    ensure_investment_property_e2e_schema(conn, commit=False)
    cols = _table_columns(conn, 'import_details')
    if 'line_type' not in cols:
        return {'created': 0, 'updated': 0, 'items': [], 'warnings': []}

    name_expr = (
        "COALESCE(d.product_name, p.name, 'Bất động sản đầu tư')"
        if 'product_name' in cols
        else "COALESCE(p.name, 'Bất động sản đầu tư')"
    )
    subtotal_expr = 'COALESCE(d.subtotal, d.qty*d.buyprice)' if 'subtotal' in cols else 'd.qty*d.buyprice'
    discount_expr = 'COALESCE(d.discount,0)' if 'discount' in cols else '0'

    unit_expr = "COALESCE(NULLIF(TRIM(d.unit),''), 'BĐS')" if 'unit' in cols else "'BĐS'"
    qty_expr = "COALESCE(d.qty,1)" if 'qty' in cols else "1"
    current_rows = conn.execute(f'''
        SELECT d.id, {name_expr} AS property_name,
               ({subtotal_expr} - {discount_expr}) AS original_cost,
               {unit_expr} AS asset_unit, {qty_expr} AS original_quantity
        FROM import_details d
        LEFT JOIN products p ON p.id=d.product_id
        WHERE d.import_id=? AND LOWER(COALESCE(d.line_type,''))='investment_property'
        ORDER BY d.id
    ''', (import_id,)).fetchall()

    imp = conn.execute('SELECT * FROM "import" WHERE id=?', (import_id,)).fetchone()
    if not imp:
        raise ValueError(f'Không tìm thấy phiếu nhập #{import_id}')
    d_imp = dict(imp) if hasattr(imp, 'keys') else {}
    supplier_id = d_imp.get('supplier_id') if d_imp else None
    acq_date = str(d_imp.get('date') or '')[:10] if d_imp else None

    existing_rows = conn.execute(
        '''
        SELECT id, property_code, property_name, original_cost, import_detail_id
        FROM sme_investment_properties
        WHERE import_id=?
        ORDER BY id
        ''',
        (import_id,),
    ).fetchall()

    existing = [{
        'id': int(r[0]),
        'property_code': r[1],
        'property_name': str(r[2] or ''),
        'original_cost': _money(r[3]),
        'import_detail_id': int(r[4]) if r[4] not in (None, '') else None,
    } for r in existing_rows]

    current = [{
        'detail_id': int(r[0]),
        'property_name': str(r[1] or 'Bất động sản đầu tư'),
        'original_cost': _money(r[2]),
        'asset_unit': str(r[3] or 'BĐS').strip() or 'BĐS',
        'original_quantity': max(0.0, _money(r[4] or 1)),
    } for r in current_rows]

    by_detail = {x['import_detail_id']: x for x in existing if x['import_detail_id']}
    unmatched_existing = {x['id']: x for x in existing}
    matches: dict[int, dict[str, Any]] = {}

    for cur in current:
        ex = by_detail.get(cur['detail_id'])
        if ex:
            matches[cur['detail_id']] = ex
            unmatched_existing.pop(ex['id'], None)

    for cur in current:
        if cur['detail_id'] in matches:
            continue
        candidates = [
            ex for ex in unmatched_existing.values()
            if _normalize_match_text(ex['property_name']) == _normalize_match_text(cur['property_name'])
            and abs(_money(ex['original_cost']) - _money(cur['original_cost'])) < 0.01
        ]
        if len(candidates) == 1:
            ex = candidates[0]
            matches[cur['detail_id']] = ex
            unmatched_existing.pop(ex['id'], None)

    # Khi sửa phiếu nhập, import_details có thể bị DELETE + INSERT lại nên detail_id đổi.
    # Sau khi match theo detail_id và tên+nguyên giá, ghép tuần tự các dòng còn lại
    # trong CÙNG import_id. Không yêu cầu số lượng hai phía phải bằng nhau:
    # - current <= existing: tái sử dụng register cũ, tuyệt đối không tạo thêm BĐSĐT;
    # - current > existing: chỉ tạo đúng phần tăng thêm thực sự.
    # Đây là điểm quan trọng để việc sửa cùng một phiếu không làm phình register.
    remaining_cur = [x for x in current if x['detail_id'] not in matches]
    remaining_ex = list(unmatched_existing.values())
    for cur, ex in zip(remaining_cur, remaining_ex):
        matches[cur['detail_id']] = ex
        unmatched_existing.pop(ex['id'], None)

    out, warnings = [], []
    created = updated = 0

    for cur in current:
        ex = matches.get(cur['detail_id'])
        if ex:
            pid = int(ex['id'])
            has_history = _property_has_downstream_history(conn, pid)
            fields = [
                'property_name=?',
                'import_detail_id=?',
                'supplier_id=?',
                'acquisition_date=?',
                'source_journal_entry_id=?',
                'asset_unit=?',
                'original_quantity=?',
                'updated_at=?',
            ]
            values = [
                cur['property_name'],
                cur['detail_id'],
                int(supplier_id) if supplier_id else None,
                acq_date,
                source_journal_entry_id,
                cur['asset_unit'],
                cur['original_quantity'],
                _now(),
            ]
            if not has_history:
                fields.insert(1, 'original_cost=?')
                values.insert(1, cur['original_cost'])
            values.append(pid)
            conn.execute(
                f"UPDATE sme_investment_properties SET {', '.join(fields)} WHERE id=?",
                tuple(values),
            )

            if not has_history:
                conn.execute(
                    '''
                    UPDATE sme_investment_property_events
                    SET event_date=?, journal_entry_id=?, amount=?
                    WHERE property_id=? AND event_type='ACQUISITION' AND source_type='import'
                    ''',
                    (acq_date or _now()[:10], source_journal_entry_id, cur['original_cost'], pid),
                )
            elif abs(_money(ex['original_cost']) - cur['original_cost']) >= 0.01:
                warnings.append(
                    f"{ex['property_code']}: nguyên giá trên phiếu nhập đã thay đổi nhưng BĐSĐT đã có lịch sử nghiệp vụ; "
                    "hệ thống giữ nguyên nguyên giá đã ghi sổ. Hãy lập nghiệp vụ điều chỉnh riêng nếu cần."
                )
            item = {
                'id': pid, 'property_code': ex['property_code'], 'created': False, 'updated': True,
                'relinked_detail_id': cur['detail_id'], 'history_locked_cost': bool(has_history),
            }
            updated += 1
        else:
            item = create_property_from_import(
                conn, import_id=import_id, import_detail_id=cur['detail_id'],
                property_name=cur['property_name'], original_cost=cur['original_cost'],
                supplier_id=int(supplier_id) if supplier_id else None,
                acquisition_date=acq_date, source_journal_entry_id=source_journal_entry_id,
                created_by=created_by, asset_unit=cur['asset_unit'],
                original_quantity=cur['original_quantity'],
            )
            if item.get('created'):
                created += 1

        ensure_property_pos_products(conn, int(item['id']))
        refresh_property_balances(conn, int(item['id']))
        out.append(item)

    removed = 0
    for ex in list(unmatched_existing.values()):
        pid = int(ex['id'])

        # Register BĐSĐT phải phản ánh đúng các dòng BĐSĐT còn tồn tại trên chính import_id.
        # Nếu người dùng xóa một dòng khỏi phiếu nhập và tài sản chưa phát sinh nghiệp vụ sau ghi tăng,
        # xóa register + sự kiện ACQUISITION tương ứng trong cùng transaction.
        #
        # Nếu đã có khấu hao/cho thuê/nghiệp vụ sau ghi tăng thì KHÔNG được âm thầm giữ dòng cũ
        # (sẽ làm register lệch phiếu), cũng KHÔNG được tự xóa lịch sử. Chặn Save để người dùng
        # xử lý nghiệp vụ điều chỉnh/thanh lý/chuyển đổi trước.
        if _property_has_downstream_history(conn, pid):
            raise ValueError(
                f"{ex['property_code']}: dòng BĐSĐT đã bị xóa khỏi phiếu nhập nhưng tài sản đã có "
                "nghiệp vụ phát sinh sau ghi tăng. Không thể sửa phiếu nhập theo cách xóa trực tiếp; "
                "hãy xử lý nghiệp vụ BĐSĐT liên quan trước."
            )

        conn.execute(
            '''
            DELETE FROM sme_investment_property_events
            WHERE property_id=? AND event_type='ACQUISITION' AND source_type='import'
            ''',
            (pid,),
        )
        conn.execute('DELETE FROM sme_investment_properties WHERE id=? AND import_id=?', (pid, import_id))
        removed += 1

    return {
        'created': created,
        'updated': updated,
        'removed': removed,
        'items': out,
        'warnings': warnings,
    }


def _post_event_journal(conn, *, property_id: int, event_type: str, event_date: str,
                        amount: float, lines: list[dict[str, Any]], created_by: str | None,
                        source_type: str, source_id: int | None, note: str = '',
                        description_context: dict[str, Any] | None = None) -> dict[str, Any]:
    from Services.sme.journal_engine import post_journal_entry
    from Services.sme.description_templates import render_description

    prop = conn.execute(
        'SELECT property_code, property_name FROM sme_investment_properties WHERE id=?', (property_id,)
    ).fetchone()
    if not prop:
        raise ValueError(f'Không tìm thấy BĐSĐT #{property_id}')

    business_type = f'BDSDT_{event_type}'
    ctx = dict(description_context or {})
    ctx.setdefault('property_id', property_id)
    ctx.setdefault('property_code', str(prop[0]))
    ctx.setdefault('property_name', str(prop[1]))
    ctx.setdefault('posting_date', event_date)
    ctx.setdefault('period_key', str(event_date or '')[:7])
    ctx.setdefault('amount', _money(amount))
    ctx.setdefault('plan_id', source_id if source_type == 'lease_plan' else '')

    fallback = note or f'{event_type} {prop[1]}'
    description = render_description(
        conn, business_type=business_type, scope='header', context=ctx, fallback=fallback,
    )

    posted = post_journal_entry(
        conn, posting_date=event_date, document_date=event_date,
        document_type=business_type, document_no=str(prop[0]), document_id=property_id,
        business_type=business_type, description=description, description_context=ctx,
        reference_document=str(prop[0]), created_by=created_by, lines=lines,
    )
    conn.execute(
        '''INSERT INTO sme_investment_property_events
        (property_id,event_type,event_date,source_type,source_id,journal_entry_id,amount,note,created_by)
        VALUES (?,?,?,?,?,?,?,?,?)''',
        (property_id,event_type,event_date,source_type,source_id,posted['id'],_money(amount),description,created_by),
    )
    return posted


def post_depreciation(conn, property_id: int, *, amount: Any, posting_date: str,
                      created_by: str | None = None, note: str = '',
                      source_type: str = 'manual', source_id: int | None = None,
                      description_context: dict[str, Any] | None = None) -> dict[str, Any]:
    from Services.sme.account_roles import resolve_posting_account
    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Khấu hao BĐSĐT phải > 0')
    cogs = resolve_posting_account(conn, 'cogs.investment_property')
    accum = resolve_posting_account(conn, 'accum_depr.investment_property')
    posted = _post_event_journal(
        conn,
        property_id=property_id,
        event_type='DEPRECIATION',
        event_date=posting_date,
        amount=amt,
        created_by=created_by,
        source_type=source_type,
        source_id=source_id,
        note=note,
        description_context=description_context,
        lines=[
            {'sequence': 1, 'account_code': cogs, 'debit': amt, 'credit': 0,
             'description': 'Khấu hao BĐSĐT đang cho thuê/khai thác'},
            {'sequence': 2, 'account_code': accum, 'debit': 0, 'credit': amt,
             'description': 'Hao mòn lũy kế BĐSĐT'},
        ],
    )
    conn.execute(
        'UPDATE sme_investment_properties SET last_depreciation_date=?, updated_at=? WHERE id=?',
        (posting_date, _now(), property_id),
    )
    refresh_property_balances(conn, property_id)
    return posted


def post_impairment(conn, property_id: int, *, amount: Any, posting_date: str,
                    created_by: str | None = None, note: str = '') -> dict[str, Any]:
    from Services.sme.account_roles import resolve_posting_account
    amt = _money(amount)
    bal = refresh_property_balances(conn, property_id)
    if amt <= 0 or amt > bal['carrying_amount']:
        raise ValueError('Mức suy giảm phải > 0 và không vượt giá trị còn lại')
    cogs = resolve_posting_account(conn, 'cogs.investment_property')
    asset = resolve_posting_account(conn, 'asset.investment_property')
    posted = _post_event_journal(conn, property_id=property_id, event_type='IMPAIRMENT',
        event_date=posting_date, amount=amt, created_by=created_by, source_type='manual', source_id=None,
        note=note or 'Suy giảm giá trị BĐSĐT', lines=[
            {'sequence':1,'account_code':cogs,'debit':amt,'credit':0,'description':'Suy giảm giá trị BĐSĐT'},
            {'sequence':2,'account_code':asset,'debit':0,'credit':amt,'description':'Ghi giảm giá trị BĐSĐT do suy giảm'},
        ])
    refresh_property_balances(conn, property_id)
    return posted


def sync_property_disposals_for_sale(conn, sale_id: int, *, created_by: str | None = None) -> dict[str, Any]:
    """Sau SALE_REVENUE: nếu POS bán đúng sale_product_id thì tự ghi giảm 217 và giá vốn 6327."""
    ensure_investment_property_e2e_schema(conn, commit=False)
    sale = conn.execute('SELECT date,status FROM sale WHERE id=?', (sale_id,)).fetchone()
    if not sale or str(sale[1] or '').lower() != 'completed':
        return {'posted': 0, 'items': []}
    rows = conn.execute('''
        SELECT DISTINCT p.id
        FROM sme_investment_properties p
        JOIN sale_items si ON si.product_id=p.sale_product_id
        WHERE si.sale_id=? AND p.status='ACTIVE'
    ''', (sale_id,)).fetchall()
    from Services.sme.account_roles import resolve_posting_account
    asset = resolve_posting_account(conn, 'asset.investment_property')
    accum = resolve_posting_account(conn, 'accum_depr.investment_property')
    cogs = resolve_posting_account(conn, 'cogs.investment_property')
    out=[]
    for r in rows:
        property_id=int(r[0])
        exists=conn.execute("SELECT 1 FROM sme_investment_property_events WHERE property_id=? AND event_type='SALE' AND source_id=? LIMIT 1", (property_id,sale_id)).fetchone()
        if exists:
            continue
        bal=refresh_property_balances(conn, property_id)
        lines=[]; seq=1
        accumulated_depreciation=_money(bal['accumulated_depreciation'])
        if accumulated_depreciation>0:
            lines.append({'sequence':seq,'account_code':accum,'debit':accumulated_depreciation,'credit':0,'description':'Xóa hao mòn lũy kế BĐSĐT khi bán'}); seq+=1
        if bal['carrying_amount']>0:
            lines.append({'sequence':seq,'account_code':cogs,'debit':bal['carrying_amount'],'credit':0,'description':'Giá vốn BĐSĐT bán'}); seq+=1
        asset_book_value=_money(bal['original_cost']-bal['impairment_amount'])
        lines.append({'sequence':seq,'account_code':asset,'debit':0,'credit':asset_book_value,'description':'Ghi giảm BĐSĐT khi bán'})
        posted=_post_event_journal(conn, property_id=property_id,event_type='SALE',event_date=str(sale[0])[:10],
            amount=bal['carrying_amount'],created_by=created_by,source_type='sale',source_id=sale_id,
            note=f'Ghi giảm BĐSĐT theo sale #{sale_id}',lines=lines)
        conn.execute("UPDATE sme_investment_properties SET status='SOLD', sale_id=?, sale_journal_entry_id=?, disposal_date=?, updated_at=? WHERE id=?",
                     (sale_id,posted['id'],str(sale[0])[:10],_now(),property_id))
        out.append({'property_id':property_id,'journal_entry_id':posted['id']})
    return {'posted':len(out),'items':out}


def transfer_to_fixed_asset(conn, property_id: int, *, posting_date: str, fixed_asset_account: str = '2111',
                            fixed_asset_accum_account: str = '2141', created_by: str | None = None) -> dict[str, Any]:
    from Services.sme.account_roles import resolve_posting_account
    bal=refresh_property_balances(conn, property_id)
    asset=resolve_posting_account(conn,'asset.investment_property')
    accum=resolve_posting_account(conn,'accum_depr.investment_property')
    fa=resolve_posting_account(conn,fixed_asset_account)
    fa_acc=resolve_posting_account(conn,fixed_asset_accum_account)
    reduction=_money(bal['accumulated_depreciation']+bal['impairment_amount'])
    lines=[
        {'sequence':1,'account_code':fa,'debit':bal['original_cost'],'credit':0,'description':'Chuyển BĐSĐT thành TSCĐ'},
        {'sequence':2,'account_code':asset,'debit':0,'credit':bal['original_cost'],'description':'Ghi giảm BĐSĐT'},
    ]
    if reduction>0:
        lines += [
            {'sequence':3,'account_code':accum,'debit':reduction,'credit':0,'description':'Kết chuyển hao mòn BĐSĐT'},
            {'sequence':4,'account_code':fa_acc,'debit':0,'credit':reduction,'description':'Kết chuyển hao mòn TSCĐ'},
        ]
    posted=_post_event_journal(conn,property_id=property_id,event_type='TRANSFER_TO_FA',event_date=posting_date,
        amount=bal['carrying_amount'],created_by=created_by,source_type='manual',source_id=None,note='Chuyển BĐSĐT thành TSCĐ',lines=lines)
    conn.execute("UPDATE sme_investment_properties SET status='TRANSFERRED_FA', updated_at=? WHERE id=?",(_now(),property_id))
    return posted


def transfer_to_inventory(conn, property_id: int, *, posting_date: str, inventory_account: str = '1567',
                          created_by: str | None = None) -> dict[str, Any]:
    from Services.sme.account_roles import resolve_posting_account
    bal=refresh_property_balances(conn, property_id)
    asset=resolve_posting_account(conn,'asset.investment_property')
    accum=resolve_posting_account(conn,'accum_depr.investment_property')
    inv=resolve_posting_account(conn,inventory_account)
    reduction=_money(bal['accumulated_depreciation']+bal['impairment_amount'])
    lines=[{'sequence':1,'account_code':inv,'debit':bal['carrying_amount'],'credit':0,'description':'BĐS chuyển thành hàng hóa BĐS'}]
    seq=2
    if reduction>0:
        lines.append({'sequence':seq,'account_code':accum,'debit':reduction,'credit':0,'description':'Xóa hao mòn BĐSĐT'}); seq+=1
    lines.append({'sequence':seq,'account_code':asset,'debit':0,'credit':bal['original_cost'],'description':'Ghi giảm nguyên giá BĐSĐT'})
    posted=_post_event_journal(conn,property_id=property_id,event_type='TRANSFER_TO_INVENTORY',event_date=posting_date,
        amount=bal['carrying_amount'],created_by=created_by,source_type='manual',source_id=None,note='Chuyển BĐSĐT thành hàng hóa',lines=lines)
    conn.execute("UPDATE sme_investment_properties SET status='TRANSFERRED_INVENTORY', updated_at=? WHERE id=?",(_now(),property_id))
    return posted


def reverse_property_disposals_for_sale(conn, sale_id: int, *, posting_date: str | None = None,
                                        created_by: str | None = None) -> dict[str, Any]:
    """Đảo bút toán ghi giảm BĐSĐT khi sale bị sửa/hủy; giữ lịch sử, đưa property về ACTIVE."""
    from Services.sme.journal_engine import reverse_journal_entry
    ensure_investment_property_e2e_schema(conn, commit=False)
    rows=conn.execute("SELECT id,property_id,journal_entry_id FROM sme_investment_property_events WHERE event_type='SALE' AND source_id=? ORDER BY id",(sale_id,)).fetchall()
    reversed_ids=[]
    for ev_id, property_id, journal_id in rows:
        if journal_id:
            # Chỉ đảo nếu entry gốc còn posted/chưa bị đảo.
            active=conn.execute("SELECT 1 FROM sme_journal_entries WHERE id=? AND status='posted' AND reverses_id IS NULL LIMIT 1",(journal_id,)).fetchone()
            if active:
                rev=reverse_journal_entry(conn,int(journal_id),posting_date=posting_date,created_by=created_by,reason=f'Đảo ghi giảm BĐSĐT do sửa/hủy sale #{sale_id}')
                reversed_ids.append(rev['id'] if isinstance(rev,dict) else rev)
        conn.execute("UPDATE sme_investment_property_events SET event_type='SALE_REVERSED', note=COALESCE(note,'') || ' | reversed' WHERE id=?",(ev_id,))
        conn.execute("UPDATE sme_investment_properties SET status='ACTIVE',sale_id=NULL,sale_journal_entry_id=NULL,disposal_date=NULL,updated_at=? WHERE id=?",(_now(),property_id))
    return {'reversed':len(reversed_ids),'entry_ids':reversed_ids}

# ========================= LEASE / PERIODIC ACCOUNTING =========================

def _row_dict(row) -> dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, 'keys'):
        return {k: row[k] for k in row.keys()}
    return dict(row)


def _date_value(value) -> datetime:
    text = str(value or '').strip()[:10]
    if not text:
        raise ValueError('Thiếu ngày bắt đầu')
    return datetime.strptime(text, '%Y-%m-%d')


def _month_shift(dt: datetime, months: int) -> datetime:
    import calendar
    idx = (dt.year * 12 + (dt.month - 1)) + months
    year, month0 = divmod(idx, 12)
    month = month0 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _month_end(dt: datetime) -> datetime:
    import calendar
    return dt.replace(day=calendar.monthrange(dt.year, dt.month)[1])


def _period_schedule(start_date: str, months: int) -> list[dict[str, Any]]:
    start = _date_value(start_date)
    out = []
    for idx in range(max(0, int(months or 0))):
        month_dt = _month_shift(start, idx)
        due = _month_end(month_dt)
        out.append({
            'period_no': idx + 1,
            'period_key': f'{due.year:04d}-{due.month:02d}',
            'posting_date': due.strftime('%Y-%m-%d'),
        })
    return out


def _split_period_amount(total: Any, months: int, period_no: int) -> float:
    total_d = Decimal(str(total or 0)).quantize(Decimal('0.01'))
    months_i = max(1, int(months or 1))
    if period_no < months_i:
        return float((total_d / Decimal(months_i)).quantize(Decimal('0.01')))
    regular = (total_d / Decimal(months_i)).quantize(Decimal('0.01'))
    last = total_d - regular * Decimal(months_i - 1)
    return float(last.quantize(Decimal('0.01')))


def get_active_lease_plan(conn, property_id: int) -> dict[str, Any] | None:
    ensure_investment_property_e2e_schema(conn, commit=False)
    row = conn.execute(
        """
        SELECT *
        FROM sme_investment_property_lease_plans
        WHERE property_id=? AND status='ACTIVE'
        ORDER BY id DESC
        LIMIT 1
        """,
        (property_id,),
    ).fetchone()
    if not row:
        return None
    data = _row_dict(row)
    postings = conn.execute(
        """
        SELECT period_key, period_no, event_type, posting_date, amount, journal_entry_id
        FROM sme_investment_property_period_postings
        WHERE plan_id=?
        ORDER BY period_no, event_type
        """,
        (data['id'],),
    ).fetchall()
    data['postings'] = [_row_dict(x) for x in postings]
    data['posted_depreciation_periods'] = sum(
        1 for x in data['postings'] if x.get('event_type') == 'DEPRECIATION'
    )
    data['posted_revenue_periods'] = sum(
        1 for x in data['postings'] if x.get('event_type') == 'REVENUE_RECOGNITION'
    )
    return data


def save_lease_plan(
    conn,
    property_id: int,
    payload: dict[str, Any],
    *,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Tạo/cập nhật kế hoạch trước khi có kỳ đã hạch toán.

    DIRECT_MONTHLY:
      - mỗi kỳ bán/thu qua POS -> Có revenue.investment_property (5117 chi tiết).
      - không tự ghi doanh thu định kỳ trong scheduler.

    PREPAID:
      - lúc thu trước dùng POS product -THUE-TRUOC -> Có 3387.
      - scheduler tự phân bổ mỗi kỳ N3387/Có revenue.investment_property.
    """
    ensure_investment_property_e2e_schema(conn, commit=False)

    prop_row = conn.execute(
        "SELECT id,status,property_name FROM sme_investment_properties WHERE id=?",
        (property_id,),
    ).fetchone()
    if not prop_row:
        raise ValueError(f'Không tìm thấy BĐSĐT #{property_id}')
    prop = _row_dict(prop_row)
    if str(prop.get('status') or '').upper() != 'ACTIVE':
        raise ValueError('Chỉ thiết lập cho thuê cho BĐSĐT đang ACTIVE.')

    usage_purpose = str(payload.get('usage_purpose') or 'RENTAL').strip().upper()
    if usage_purpose not in ('RENTAL', 'BOTH'):
        raise ValueError('BĐSĐT chỉ được tự khấu hao khi đang cho thuê/khai thác.')

    revenue_mode = str(payload.get('revenue_mode') or 'DIRECT_MONTHLY').strip().upper()
    if revenue_mode not in ('DIRECT_MONTHLY', 'PREPAID'):
        raise ValueError('Hình thức doanh thu không hợp lệ.')

    start_date = _date_value(payload.get('start_date')).strftime('%Y-%m-%d')
    try:
        months = int(payload.get('months') or 0)
    except (TypeError, ValueError):
        months = 0
    if months <= 0 or months > 600:
        raise ValueError('Số tháng phải từ 1 đến 600.')

    schedule = _period_schedule(start_date, months)
    end_date = schedule[-1]['posting_date']

    auto_dep = 1 if bool(payload.get('auto_depreciation')) else 0
    auto_rev = 1 if (revenue_mode == 'PREPAID' and bool(payload.get('auto_revenue_recognition', True))) else 0

    monthly_rent = _money(payload.get('monthly_rent_amount'))
    prepaid_net = _money(payload.get('prepaid_net_amount'))
    depreciation_total = _money(payload.get('depreciation_total'))

    if revenue_mode == 'DIRECT_MONTHLY':
        if monthly_rent < 0:
            raise ValueError('Tiền thuê tháng không hợp lệ.')
        monthly_revenue = monthly_rent
        prepaid_net = 0.0
        auto_rev = 0
    else:
        if prepaid_net <= 0:
            raise ValueError('Thu tiền trước nhiều kỳ phải nhập tổng doanh thu chưa VAT.')
        monthly_revenue = _split_period_amount(prepaid_net, months, 1)

    if auto_dep and depreciation_total <= 0:
        raise ValueError('Bật tự động khấu hao thì tổng khấu hao phải > 0.')
    monthly_dep = _split_period_amount(depreciation_total, months, 1) if depreciation_total > 0 else 0.0

    existing = get_active_lease_plan(conn, property_id)
    now = _now()
    if existing:
        posted_count = len(existing.get('postings') or [])
        if posted_count:
            raise ValueError(
                'Kế hoạch đã phát sinh hạch toán. Hãy dừng kế hoạch hiện tại rồi tạo kế hoạch mới.'
            )
        plan_id = int(existing['id'])
        conn.execute(
            """
            UPDATE sme_investment_property_lease_plans
            SET usage_purpose=?, revenue_mode=?, start_date=?, end_date=?, months=?,
                monthly_rent_amount=?, prepaid_net_amount=?, monthly_revenue_amount=?,
                depreciation_total=?, monthly_depreciation_amount=?,
                auto_depreciation=?, auto_revenue_recognition=?,
                posting_timing='MONTH_END', note=?, updated_at=?
            WHERE id=?
            """,
            (
                usage_purpose, revenue_mode, start_date, end_date, months,
                monthly_rent, prepaid_net, monthly_revenue,
                depreciation_total, monthly_dep,
                auto_dep, auto_rev,
                str(payload.get('note') or '').strip(), now, plan_id,
            ),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO sme_investment_property_lease_plans (
                property_id,usage_purpose,revenue_mode,start_date,end_date,months,
                monthly_rent_amount,prepaid_net_amount,monthly_revenue_amount,
                depreciation_total,monthly_depreciation_amount,
                auto_depreciation,auto_revenue_recognition,posting_timing,
                status,note,created_by,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'MONTH_END','ACTIVE',?,?,?,?)
            """,
            (
                property_id, usage_purpose, revenue_mode, start_date, end_date, months,
                monthly_rent, prepaid_net, monthly_revenue,
                depreciation_total, monthly_dep,
                auto_dep, auto_rev,
                str(payload.get('note') or '').strip(), created_by, now, now,
            ),
        )
        plan_id = int(cur.lastrowid)

    conn.execute(
        """
        UPDATE sme_investment_properties
        SET usage_purpose=?, depreciable=?, depreciation_method='STRAIGHT_LINE',
            useful_life_months=?, depreciation_start_date=?, updated_at=?
        WHERE id=?
        """,
        (usage_purpose, auto_dep, months, start_date, now, property_id),
    )

    plan = get_active_lease_plan(conn, property_id) or {}
    plan['schedule'] = schedule
    return plan


def stop_lease_plan(conn, property_id: int, *, created_by: str | None = None) -> dict[str, Any]:
    ensure_investment_property_e2e_schema(conn, commit=False)
    plan = get_active_lease_plan(conn, property_id)
    if not plan:
        return {'stopped': False, 'reason': 'no_active_plan'}
    conn.execute(
        """
        UPDATE sme_investment_property_lease_plans
        SET status='STOPPED', updated_at=?
        WHERE id=?
        """,
        (_now(), int(plan['id'])),
    )
    # Dừng cho thuê không tự động đổi nguyên tắc phân loại BĐSĐT;
    # chỉ tắt scheduler khấu hao/phân bổ của plan.
    return {'stopped': True, 'plan_id': int(plan['id'])}


def post_deferred_revenue_recognition(
    conn,
    property_id: int,
    *,
    amount: Any,
    posting_date: str,
    plan_id: int,
    created_by: str | None = None,
    note: str = '',
) -> dict[str, Any]:
    """Phân bổ tiền thuê nhận trước: Nợ 3387 / Có doanh thu BĐSĐT (role -> 5117/511)."""
    from Services.sme.account_roles import resolve_posting_account

    amt = _money(amount)
    if amt <= 0:
        raise ValueError('Số tiền phân bổ doanh thu phải > 0.')

    deferred = resolve_posting_account(conn, '3387')
    revenue = resolve_posting_account(conn, 'revenue.investment_property')

    return _post_event_journal(
        conn,
        property_id=property_id,
        event_type='REVENUE_RECOGNITION',
        event_date=posting_date,
        amount=amt,
        created_by=created_by,
        source_type='lease_plan',
        source_id=plan_id,
        note=note,
        description_context={'period_key': str(posting_date)[:7], 'plan_id': plan_id},
        lines=[
            {
                'sequence': 1,
                'account_code': deferred,
                'debit': amt,
                'credit': 0,
                'description': 'Phân bổ doanh thu chưa thực hiện BĐSĐT',
            },
            {
                'sequence': 2,
                'account_code': revenue,
                'debit': 0,
                'credit': amt,
                'description': 'Ghi nhận doanh thu cho thuê BĐSĐT trong kỳ',
            },
        ],
    )


def _period_already_posted(conn, plan_id: int, period_key: str, event_type: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sme_investment_property_period_postings
        WHERE plan_id=? AND period_key=? AND event_type=?
        LIMIT 1
        """,
        (plan_id, period_key, event_type),
    ).fetchone()
    return bool(row)


def _mark_period_posted(
    conn,
    *,
    plan_id: int,
    property_id: int,
    period_key: str,
    period_no: int,
    event_type: str,
    posting_date: str,
    amount: float,
    journal_entry_id: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO sme_investment_property_period_postings (
            plan_id,property_id,period_key,period_no,event_type,
            posting_date,amount,journal_entry_id,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            plan_id, property_id, period_key, period_no, event_type,
            posting_date, _money(amount), journal_entry_id, _now(),
        ),
    )


def run_due_lease_plans(
    conn,
    *,
    as_of: str | datetime | None = None,
    property_id: int | None = None,
    created_by: str | None = 'scheduler',
) -> dict[str, Any]:
    """Catch-up an toàn/idempotent cho các kỳ đã kết thúc.

    Chỉ plan ACTIVE + usage_purpose RENTAL/BOTH mới chạy.
    DIRECT_MONTHLY: chỉ tự khấu hao; doanh thu ghi khi bán/thu từng kỳ qua POS.
    PREPAID: tự khấu hao + tự N3387/C5117 mỗi kỳ nếu bật.
    """
    ensure_investment_property_e2e_schema(conn, commit=False)

    if isinstance(as_of, datetime):
        as_of_date = as_of.strftime('%Y-%m-%d')
    elif as_of:
        as_of_date = str(as_of)[:10]
        _date_value(as_of_date)
    else:
        as_of_date = datetime.now().strftime('%Y-%m-%d')

    sql = """
        SELECT lp.*, p.status AS property_status, p.property_name
        FROM sme_investment_property_lease_plans lp
        JOIN sme_investment_properties p ON p.id=lp.property_id
        WHERE lp.status='ACTIVE'
          AND p.status='ACTIVE'
          AND UPPER(COALESCE(lp.usage_purpose,'')) IN ('RENTAL','BOTH')
    """
    params: list[Any] = []
    if property_id is not None:
        sql += " AND lp.property_id=?"
        params.append(int(property_id))
    sql += " ORDER BY lp.id"

    plans = conn.execute(sql, params).fetchall()

    posted_dep = 0
    posted_rev = 0
    skipped = 0
    items: list[dict[str, Any]] = []

    for raw in plans:
        plan = _row_dict(raw)
        plan_id = int(plan['id'])
        pid = int(plan['property_id'])
        months = int(plan.get('months') or 0)
        schedule = _period_schedule(str(plan.get('start_date') or ''), months)

        for period in schedule:
            posting_date = period['posting_date']
            if posting_date > as_of_date:
                continue

            pno = int(period['period_no'])
            pkey = period['period_key']

            if int(plan.get('auto_depreciation') or 0):
                event_type = 'DEPRECIATION'
                if not _period_already_posted(conn, plan_id, pkey, event_type):
                    amount = _split_period_amount(plan.get('depreciation_total'), months, pno)
                    if amount > 0:
                        journal = post_depreciation(
                            conn,
                            pid,
                            amount=amount,
                            posting_date=posting_date,
                            created_by=created_by,
                            source_type='lease_plan',
                            source_id=plan_id,
                            note='',
                            description_context={'period_key': pkey, 'plan_id': plan_id},
                        )
                        _mark_period_posted(
                            conn,
                            plan_id=plan_id,
                            property_id=pid,
                            period_key=pkey,
                            period_no=pno,
                            event_type=event_type,
                            posting_date=posting_date,
                            amount=amount,
                            journal_entry_id=int(journal['id']),
                        )
                        posted_dep += 1
                        items.append({
                            'property_id': pid, 'plan_id': plan_id, 'period': pkey,
                            'event_type': event_type, 'amount': amount,
                            'journal_entry_id': int(journal['id']),
                        })
                else:
                    skipped += 1

            if (
                str(plan.get('revenue_mode') or '').upper() == 'PREPAID'
                and int(plan.get('auto_revenue_recognition') or 0)
            ):
                event_type = 'REVENUE_RECOGNITION'
                if not _period_already_posted(conn, plan_id, pkey, event_type):
                    amount = _split_period_amount(plan.get('prepaid_net_amount'), months, pno)
                    if amount > 0:
                        journal = post_deferred_revenue_recognition(
                            conn,
                            pid,
                            amount=amount,
                            posting_date=posting_date,
                            plan_id=plan_id,
                            created_by=created_by,
                            note='',
                        )
                        _mark_period_posted(
                            conn,
                            plan_id=plan_id,
                            property_id=pid,
                            period_key=pkey,
                            period_no=pno,
                            event_type=event_type,
                            posting_date=posting_date,
                            amount=amount,
                            journal_entry_id=int(journal['id']),
                        )
                        posted_rev += 1
                        items.append({
                            'property_id': pid, 'plan_id': plan_id, 'period': pkey,
                            'event_type': event_type, 'amount': amount,
                            'journal_entry_id': int(journal['id']),
                        })
                else:
                    skipped += 1

            conn.execute(
                """
                UPDATE sme_investment_property_lease_plans
                SET last_processed_period=?, updated_at=?
                WHERE id=?
                """,
                (pkey, _now(), plan_id),
            )

        # Hoàn tất khi đã qua kỳ cuối và đủ số posting bắt buộc.
        if schedule and schedule[-1]['posting_date'] <= as_of_date:
            required_types = []
            if int(plan.get('auto_depreciation') or 0):
                required_types.append('DEPRECIATION')
            if (
                str(plan.get('revenue_mode') or '').upper() == 'PREPAID'
                and int(plan.get('auto_revenue_recognition') or 0)
            ):
                required_types.append('REVENUE_RECOGNITION')

            expected = months * len(required_types)
            if expected:
                marks = ','.join('?' for _ in required_types)
                count_row = conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM sme_investment_property_period_postings
                    WHERE plan_id=? AND event_type IN ({marks})
                    """,
                    (plan_id, *required_types),
                ).fetchone()
                done = int(count_row[0] if count_row else 0)
                if done >= expected:
                    conn.execute(
                        """
                        UPDATE sme_investment_property_lease_plans
                        SET status='COMPLETED', updated_at=?
                        WHERE id=?
                        """,
                        (_now(), plan_id),
                    )

    return {
        'processed_plans': len(plans),
        'depreciation_posted': posted_dep,
        'revenue_recognition_posted': posted_rev,
        'skipped_existing': skipped,
        'items': items,
        'as_of': as_of_date,
    }

# ========================= PHASE 3 — TRANSACTION METADATA =========================

def validate_bdsdt_checkout_context(conn, context: dict[str, Any]) -> dict[str, Any]:
    """Validate/normalize BĐSĐT transaction context. No commit."""
    ensure_investment_property_e2e_schema(conn, commit=False)
    property_id = int(context.get('property_id') or 0)
    row = conn.execute(
        """SELECT id,property_code,property_name,status,
                  COALESCE(NULLIF(TRIM(asset_unit),''),'BĐS') AS asset_unit,
                  COALESCE(original_quantity,1) AS original_quantity,
                  sale_product_id,lease_product_id,lease_deferred_product_id
           FROM sme_investment_properties WHERE id=?""", (property_id,)
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy BĐSĐT.')
    d = dict(row) if hasattr(row, 'keys') else {
        'id':row[0],'property_code':row[1],'property_name':row[2],'status':row[3],
        'asset_unit':row[4],'original_quantity':row[5],
        'sale_product_id':row[6],'lease_product_id':row[7],'lease_deferred_product_id':row[8],
    }
    if str(d.get('status') or '').upper() != 'ACTIVE':
        raise ValueError('BĐSĐT không còn ở trạng thái ACTIVE.')
    tx_type = str(context.get('transaction_type') or '').upper()
    if tx_type not in ('SALE','LEASE_DIRECT','LEASE_PREPAID'):
        raise ValueError('Loại giao dịch BĐSĐT không hợp lệ.')
    billing_unit = str(context.get('billing_unit') or '').strip()
    billing_qty = _money(context.get('billing_quantity') or 0)
    unit_price = _money(context.get('unit_price') or 0)
    if not billing_unit:
        raise ValueError('Thiếu đơn vị tính giao dịch.')
    if billing_qty <= 0 or unit_price < 0:
        raise ValueError('Số lượng kỳ/đơn giá không hợp lệ.')
    if tx_type == 'SALE':
        # Một register = một tài sản. Không cho bán quantity khác 1.
        billing_unit = str(d.get('asset_unit') or 'BĐS')
        billing_qty = 1.0
    if tx_type == 'LEASE_PREPAID' and str(context.get('payment_method') or '') == '131':
        raise ValueError('Thu trước nhiều kỳ chỉ ghi nhận 3387 khi đã thực thu; chọn 111 hoặc 112.')
    out = dict(context)
    out.update({
        'property_id': property_id,
        'transaction_type': tx_type,
        'asset_unit': str(d.get('asset_unit') or 'BĐS'),
        'asset_quantity': _money(d.get('original_quantity') or 1),
        'billing_unit': billing_unit,
        'billing_quantity': billing_qty,
        'unit_price': unit_price,
    })
    return out


def save_bdsdt_sale_transaction(conn, *, sale_id: int, context: dict[str, Any],
                                client_uuid: str | None = None, created_by: str | None = None) -> int:
    """Snapshot metadata nghiệp vụ theo sale_id; idempotent. No commit."""
    ctx = validate_bdsdt_checkout_context(conn, context)
    net = _money(ctx.get('net_amount') or (ctx['billing_quantity'] * ctx['unit_price']))
    land = _money(ctx.get('deductible_land_value') or 0)
    taxable = _money(ctx.get('vat_taxable_amount') if ctx.get('vat_taxable_amount') is not None else max(0, net-land))
    rate = _money(ctx.get('vat_rate') or 0)
    vat = _money(ctx.get('vat_amount') if ctx.get('vat_amount') is not None else (taxable*rate/100 if rate>0 else 0))
    gross = _money(ctx.get('gross_amount') if ctx.get('gross_amount') is not None else net+vat)
    existing = conn.execute('SELECT id FROM sme_investment_property_transactions WHERE sale_id=? LIMIT 1',(int(sale_id),)).fetchone()
    vals = (
        ctx['property_id'], ctx.get('lease_plan_id'), ctx['transaction_type'],
        str(ctx.get('transaction_date') or _now())[:19], ctx.get('period_from'), ctx.get('period_to'),
        ctx.get('contract_no'), ctx['asset_unit'], ctx['asset_quantity'],
        ctx['billing_unit'], ctx['billing_quantity'], ctx['unit_price'],
        int(ctx.get('recognition_periods') or 0) or None, _money(ctx.get('recognition_amount_per_period') or 0),
        ctx.get('customer_name'), ctx.get('company_name'), ctx.get('tax_code'), ctx.get('address'),
        ctx.get('email'), ctx.get('phone'), ctx.get('payment_method'),
        net, land, taxable, rate, vat, gross, int(sale_id), client_uuid or None,
        'COMPLETED', ctx.get('note'), created_by, _now(),
    )
    if existing:
        conn.execute("""UPDATE sme_investment_property_transactions SET
          property_id=?,lease_plan_id=?,transaction_type=?,transaction_date=?,period_from=?,period_to=?,
          contract_no=?,asset_unit=?,asset_quantity=?,billing_unit=?,billing_quantity=?,unit_price=?,
          recognition_periods=?,recognition_amount_per_period=?,customer_name=?,company_name=?,tax_code=?,
          address=?,email=?,phone=?,payment_method=?,net_amount=?,deductible_land_value=?,vat_taxable_amount=?,
          vat_rate=?,vat_amount=?,gross_amount=?,sale_id=?,client_uuid=?,status=?,note=?,created_by=?,updated_at=?
          WHERE id=?""", vals + (int(existing[0]),))
        return int(existing[0])
    cur=conn.execute("""INSERT INTO sme_investment_property_transactions(
      property_id,lease_plan_id,transaction_type,transaction_date,period_from,period_to,contract_no,
      asset_unit,asset_quantity,billing_unit,billing_quantity,unit_price,recognition_periods,
      recognition_amount_per_period,customer_name,company_name,tax_code,address,email,phone,payment_method,
      net_amount,deductible_land_value,vat_taxable_amount,vat_rate,vat_amount,gross_amount,sale_id,
      client_uuid,status,note,created_by,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals)
    return int(cur.lastrowid)


def get_bdsdt_transaction_by_sale(conn, sale_id: int) -> dict[str, Any] | None:
    ensure_investment_property_e2e_schema(conn, commit=False)
    row=conn.execute('SELECT * FROM sme_investment_property_transactions WHERE sale_id=? LIMIT 1',(int(sale_id),)).fetchone()
    return dict(row) if row and hasattr(row,'keys') else None
