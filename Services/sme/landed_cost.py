"""Phân bổ chi phí mua hàng SME (landed cost) — vốn hóa vào 156/152/211/153.

Không sửa phiếu nhập hàng gốc (khớp HĐ điện tử). Tạo phiếu import doc_type=landed_cost
cho HĐ chi phí, điều chỉnh WAC/stock_moves (qty=0) + nguyên giá TSCĐ/CCDC + nhật ký.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from db_utils import sqlite_commit
from Services.inventory_cost import apply_cost_value_adjustment
from Services.inventory_stock_helpers import ledger_quantity
from Services.sme.journal_engine import (
    post_journal_entry,
    resolve_postable_account,
)

MONEY_Q = Decimal('0.01')

COST_CATEGORIES = {
    'FREIGHT': 'Chi phí vận chuyển',
    'HANDLING': 'Chi phí bốc xếp',
    'INSURANCE': 'Chi phí bảo hiểm',
    'INSTALL': 'Chi phí lắp đặt',
    'OTHER': 'Chi phí mua hàng khác',
}

SCOPE_ALL = 'all'
SCOPE_GOODS = 'goods'
SCOPE_MATERIALS = 'materials'
SCOPE_FA = 'fixed_asset'
SCOPE_TOOLS = 'tools'
SCOPE_LINES = 'lines'

VALID_SCOPES = frozenset({
    SCOPE_ALL, SCOPE_GOODS, SCOPE_MATERIALS, SCOPE_FA, SCOPE_TOOLS, SCOPE_LINES,
})

ALLOCATABLE_LINE_TYPES = frozenset({
    'goods', 'materials', 'fixed_asset', 'tools',
})

DEBIT_ACCOUNT_BY_LINE_TYPE = {
    'goods': '156',
    'materials': '152',
    'fixed_asset': '2112',
    'tools': '153',
}

DOCUMENT_TYPE = 'PNK'
BUSINESS_TYPE = 'PHAN_BO_CHI_PHI_MUA'


def _money(val: Any) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def ensure_sme_landed_cost_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    from db_utils import sqlite_is_ready, sqlite_mark_ready

    if sqlite_is_ready(conn, 'landed_cost_schema_v1'):
        return
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_landed_cost_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cost_invoice_id INTEGER NOT NULL,
            cost_import_id INTEGER NOT NULL,
            cost_category TEXT NOT NULL DEFAULT 'OTHER',
            scope TEXT NOT NULL DEFAULT 'all',
            amount_net REAL NOT NULL DEFAULT 0,
            amount_vat REAL NOT NULL DEFAULT 0,
            amount_total REAL NOT NULL DEFAULT 0,
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'posted',
            note TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(cost_invoice_id)
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_landed_cost_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            landed_cost_id INTEGER NOT NULL,
            target_import_id INTEGER NOT NULL,
            target_detail_id INTEGER,
            product_id INTEGER,
            line_type TEXT,
            warehouse_code TEXT,
            base_value REAL NOT NULL DEFAULT 0,
            allocated_net REAL NOT NULL DEFAULT 0,
            debit_account TEXT,
            wac_before REAL,
            wac_after REAL,
            stock_move_id INTEGER,
            stock_capitalized INTEGER DEFAULT 0,
            FOREIGN KEY (landed_cost_id) REFERENCES sme_landed_cost_docs(id)
        )
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sme_landed_cost_invoice
        ON sme_landed_cost_docs(cost_invoice_id)
        """
    )
    c.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sme_landed_cost_lines_doc
        ON sme_landed_cost_lines(landed_cost_id)
        """
    )
    # Sửa bản ghi cũ: chỉ cập nhật inventory, chưa bump vốn stock_moves import
    try:
        repair_landed_cost_stock_capital(conn, commit=False)
    except Exception:
        pass
    sqlite_mark_ready(conn, 'landed_cost_schema_v1')
    if commit:
        sqlite_commit(conn, label='landed_cost')


def repair_landed_cost_stock_capital(
    conn: sqlite3.Connection,
    *,
    commit: bool = True,
) -> dict:
    """
    Với các dòng phân bổ đã ghi (audit qty=0) nhưng chưa cộng vốn vào dòng import:
    bump cost_price/total_value trên stock_moves nhập gốc + import_details.cost_price.
    Idempotent nhờ meta flag trên từng line.
    """
    cols = _table_cols(conn, 'sme_landed_cost_lines')
    if not cols:
        return {'repaired': 0}
    if 'stock_capitalized' not in cols:
        try:
            conn.execute(
                'ALTER TABLE sme_landed_cost_lines ADD COLUMN stock_capitalized INTEGER DEFAULT 0'
            )
        except sqlite3.Error:
            pass
        cols = _table_cols(conn, 'sme_landed_cost_lines')

    rows = conn.execute(
        """
        SELECT id, target_import_id, target_detail_id, product_id, line_type,
               allocated_net, stock_move_id,
               COALESCE(stock_capitalized, 0) AS stock_capitalized
        FROM sme_landed_cost_lines
        WHERE LOWER(COALESCE(line_type, '')) IN ('goods', 'materials')
          AND COALESCE(allocated_net, 0) > 0
          AND product_id IS NOT NULL
        """
    ).fetchall()
    repaired = 0
    for r in rows:
        if int(r['stock_capitalized'] or 0) == 1:
            continue
        alloc = _money(r['allocated_net'])
        pid = int(r['product_id'])
        imp_id = int(r['target_import_id'])
        # Nếu stock_move_id đang trỏ dòng import (qty>0) và đã có total tăng — vẫn cần flag
        bumped = _bump_import_stock_move_cost(
            conn,
            product_id=pid,
            target_import_id=imp_id,
            allocated=alloc,
        )
        if not bumped:
            continue
        if r['target_detail_id']:
            _bump_import_detail_cost(
                conn,
                detail_id=int(r['target_detail_id']),
                allocated=alloc,
                qty=float(bumped['qty']),
            )
        try:
            if _table_exists(conn, 'chi_tiet_phieu_nhap_kho'):
                conn.execute(
                    """
                    UPDATE chi_tiet_phieu_nhap_kho
                    SET cost_price = COALESCE(cost_price, 0) + ?
                    WHERE import_id = ? AND product_id = ?
                    """,
                    (float(bumped['unit_bump']), imp_id, pid),
                )
        except sqlite3.Error:
            pass
        if 'stock_capitalized' in cols:
            conn.execute(
                """
                UPDATE sme_landed_cost_lines
                SET stock_move_id = ?, stock_capitalized = 1
                WHERE id = ?
                """,
                (bumped['stock_move_id'], int(r['id'])),
            )
        else:
            conn.execute(
                'UPDATE sme_landed_cost_lines SET stock_move_id = ? WHERE id = ?',
                (bumped['stock_move_id'], int(r['id'])),
            )
        repaired += 1
    if commit and repaired:
        sqlite_commit(conn, label='landed_cost')
    return {'repaired': repaired}


def default_scope_for_category(cost_category: str | None) -> str:
    cat = (cost_category or 'OTHER').strip().upper()
    if cat == 'INSTALL':
        return SCOPE_FA
    return SCOPE_ALL


def _normalize_scope(scope: str | None, cost_category: str | None) -> str:
    raw = (scope or '').strip().lower()
    if not raw:
        return default_scope_for_category(cost_category)
    if raw in ('hh', 'hang_hoa'):
        return SCOPE_GOODS
    if raw in ('nvl', 'vt', 'vat_tu'):
        return SCOPE_MATERIALS
    if raw in ('fa', 'tscd', 'tscđ'):
        return SCOPE_FA
    if raw in ('ccdc',):
        return SCOPE_TOOLS
    if raw in VALID_SCOPES:
        return raw
    raise ValueError(f'Phạm vi phân bổ không hợp lệ: {scope}')


def _normalize_category(raw: str | None) -> str:
    cat = (raw or 'OTHER').strip().upper()
    if cat not in COST_CATEGORIES:
        return 'OTHER'
    return cat


def _resolve_payment_method(payment_status: str | None, payment_method: str | None) -> str:
    status = (payment_status or '').strip()
    if status in ('Chưa thanh toán', 'Unpaid', ''):
        return 'CREDIT'
    raw = str(payment_method or 'cash').strip().upper()
    if raw in ('CASH', '111', 'TIỀN MẶT', 'TIEN MAT'):
        return 'CASH'
    if raw in ('CREDIT', '331', 'CONG NO', 'CÔNG NỢ'):
        return 'CREDIT'
    return 'BANK_TRANSFER'


def _credit_account_for_payment(pay_method: str) -> str:
    if pay_method == 'CASH':
        return '1111'
    if pay_method == 'BANK_TRANSFER':
        return '1121'
    return '331'


def _next_cost_import_no(conn: sqlite3.Connection) -> str:
    prefix = 'CP'
    row = conn.execute(
        """
        SELECT import_no FROM import
        WHERE import_no LIKE ?
        ORDER BY id DESC LIMIT 1
        """,
        (f'{prefix}%',),
    ).fetchone()
    max_num = 0
    if row:
        no = row[0] if not hasattr(row, 'keys') else row['import_no']
        suffix = str(no or '')[len(prefix):]
        if suffix.isdigit():
            max_num = int(suffix)
    return f'{prefix}{max_num + 1:06d}'


def _parse_invoice_payload(conn: sqlite3.Connection, invoice_id: int) -> tuple[dict, list[dict]]:
    row = conn.execute(
        'SELECT * FROM supplier_invoice WHERE id = ?',
        (invoice_id,),
    ).fetchone()
    if not row:
        raise ValueError(f'Không tìm thấy hóa đơn #{invoice_id}')
    inv = dict(row)
    raw = (inv.get('xml_data') or '').strip()
    lines: list[dict] = []
    payload: dict = {}
    if raw:
        from Services.inward_invoice_helpers import normalize_supplier_invoice_payload
        try:
            payload = normalize_supplier_invoice_payload(raw)
        except Exception:
            if raw.startswith('{') or raw.startswith('['):
                payload = json.loads(raw)
            else:
                payload = {}
        for item in payload.get('DSHHDVu') or []:
            name = (item.get('THHDVu') or item.get('name') or 'Chi phí').strip()
            qty = _money(item.get('SLuong') or item.get('qty') or 1)
            if qty <= 0:
                qty = Decimal('1.00')
            price = _money(item.get('DGia') or item.get('buyprice') or item.get('ThTien') or 0)
            if price <= 0 and item.get('ThTien') is not None:
                price = _money(item.get('ThTien')) / qty
            disc_pct = _money(str(item.get('TyLeCK') or 0).replace('%', ''))
            tax_raw = str(item.get('TSuat') or item.get('tax_pct') or 0)
            tax_pct = _money(tax_raw.replace('%', '').strip() or 0)
            subtotal = _money(qty * price)
            disc = _money(subtotal * disc_pct / Decimal('100'))
            net = subtotal - disc
            vat = _money(net * tax_pct / Decimal('100'))
            lines.append({
                'name': name,
                'unit': (item.get('DVTinh') or item.get('unit') or 'Lần').strip() or 'Lần',
                'qty': float(qty),
                'buyprice': float(price),
                'discount_pct': float(disc_pct),
                'tax_pct': float(tax_pct),
                'subtotal': float(subtotal),
                'discount': float(disc),
                'net': float(net),
                'tax': float(vat),
                'total': float(net + vat),
            })
    if not lines:
        total = _money(inv.get('total') or inv.get('total_amount') or 0)
        vat = _money(inv.get('vat_amount') or inv.get('tax') or 0)
        net = total - vat if total >= vat else total
        if net <= 0 and total > 0:
            net = total
            vat = Decimal('0.00')
        if net <= 0:
            raise ValueError('Hóa đơn chi phí không có số tiền hợp lệ')
        lines.append({
            'name': f"Chi phí HĐ {inv.get('invoice_no') or invoice_id}",
            'unit': 'Lần',
            'qty': 1.0,
            'buyprice': float(net),
            'discount_pct': 0.0,
            'tax_pct': float(_money(vat / net * 100) if net > 0 and vat > 0 else 0),
            'subtotal': float(net),
            'discount': 0.0,
            'net': float(net),
            'tax': float(vat),
            'total': float(net + vat),
        })
    return inv, lines


def get_cost_invoice_summary(conn: sqlite3.Connection, invoice_id: int) -> dict:
    ensure_sme_landed_cost_schema(conn, commit=False)
    inv, lines = _parse_invoice_payload(conn, invoice_id)
    amount_net = sum((_money(x['net']) for x in lines), Decimal('0.00'))
    amount_vat = sum((_money(x['tax']) for x in lines), Decimal('0.00'))
    existing = conn.execute(
        """
        SELECT id, cost_import_id, status, journal_entry_id
        FROM sme_landed_cost_docs
        WHERE cost_invoice_id = ?
          AND COALESCE(status, 'posted') = 'posted'
        """,
        (invoice_id,),
    ).fetchone()
    cost_import_no = None
    if existing and existing['cost_import_id']:
        row = conn.execute(
            'SELECT import_no FROM import WHERE id = ?',
            (int(existing['cost_import_id']),),
        ).fetchone()
        if row:
            cost_import_no = row['import_no'] if hasattr(row, 'keys') else row[0]
    cost_category = None
    is_manual = False
    raw = (inv.get('xml_data') or '').strip()
    if raw.startswith('{'):
        try:
            meta = json.loads(raw)
            cost_category = (meta.get('CostCategory') or None)
            if cost_category:
                cost_category = str(cost_category).strip().upper()
            is_manual = str(meta.get('SourceType') or '').lower() == 'manual'
        except Exception:
            pass
    return {
        'invoice_id': invoice_id,
        'invoice_no': inv.get('invoice_no'),
        'invoice_date': inv.get('invoice_date') or inv.get('date'),
        'seller_name': inv.get('seller_name'),
        'seller_tax_code': inv.get('seller_tax_code'),
        'seller_address': inv.get('address') or inv.get('seller_address'),
        'status': inv.get('status'),
        'lines': lines,
        'amount_net': float(amount_net),
        'amount_vat': float(amount_vat),
        'amount_total': float(amount_net + amount_vat),
        'cost_category': cost_category,
        'is_manual': is_manual,
        'already_allocated': bool(existing),
        'existing_landed_cost_id': int(existing['id']) if existing else None,
        'existing_cost_import_id': int(existing['cost_import_id']) if existing else None,
        'existing_cost_import_no': cost_import_no,
        'existing_journal_entry_id': (
            int(existing['journal_entry_id']) if existing and existing['journal_entry_id'] else None
        ),
        'categories': [
            {'value': k, 'label': v, 'default_scope': default_scope_for_category(k)}
            for k, v in COST_CATEGORIES.items()
        ],
    }


def list_eligible_target_imports(
    conn: sqlite3.Connection,
    *,
    scope: str = SCOPE_ALL,
    keyword: str | None = None,
    limit: int = 50,
    branch_code: str | None = None,
) -> list[dict]:
    """Phiếu nhập hàng (stock) có dòng HH/NVL/TSCĐ/CCDC để nhận phân bổ."""
    ensure_sme_landed_cost_schema(conn, commit=False)
    scope = _normalize_scope(scope, None)
    detail_cols = _table_cols(conn, 'import_details')
    has_line_type = 'line_type' in detail_cols
    lt_expr = "COALESCE(d.line_type, 'goods')" if has_line_type else "'goods'"

    type_filter = ''
    params: list[Any] = []
    if scope == SCOPE_GOODS:
        type_filter = f' AND {lt_expr} = ?'
        params.append('goods')
    elif scope == SCOPE_MATERIALS:
        type_filter = f' AND {lt_expr} = ?'
        params.append('materials')
    elif scope == SCOPE_FA:
        type_filter = f' AND {lt_expr} = ?'
        params.append('fixed_asset')
    elif scope == SCOPE_TOOLS:
        type_filter = f' AND {lt_expr} = ?'
        params.append('tools')
    else:
        type_filter = (
            f" AND {lt_expr} IN ('goods','materials','fixed_asset','tools')"
        )

    kw_filter = ''
    if keyword and keyword.strip():
        kw = f'%{keyword.strip()}%'
        kw_filter = ' AND (i.import_no LIKE ? OR i.bill_no LIKE ? OR COALESCE(s.name, \'\') LIKE ?)'
        params.extend([kw, kw, kw])

    from Services.sme.branches import import_branch_filter_sql
    branch_filter, branch_params = import_branch_filter_sql(conn, branch_code, alias='i')

    sql = f"""
        SELECT i.id, i.import_no, i.date, i.bill_no, i.bill_date,
               i.supplier_id,
               MAX(COALESCE(s.name, '')) AS supplier_name,
               COALESCE(i.doc_type, 'stock') AS doc_type,
               COUNT(d.id) AS line_count,
               SUM(
                   COALESCE(d.subtotal, d.qty * d.buyprice, 0) - COALESCE(d.discount, 0)
               ) AS base_value
        FROM import i
        JOIN import_details d ON d.import_id = i.id
        LEFT JOIN suppliers s ON s.id = i.supplier_id
        WHERE COALESCE(i.doc_type, 'stock') NOT IN ('service', 'landed_cost')
          {type_filter}
          {kw_filter}
          {branch_filter}
        GROUP BY i.id, i.import_no, i.date, i.bill_no, i.bill_date,
                 i.supplier_id, i.doc_type
        ORDER BY i.date DESC, i.id DESC
        LIMIT ?
    """
    params.extend(branch_params)
    params.append(int(limit or 50))
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            'import_id': int(r['id']),
            'import_no': r['import_no'],
            'date': r['date'],
            'bill_no': r['bill_no'],
            'bill_date': r['bill_date'],
            'supplier_id': r['supplier_id'],
            'supplier_name': r['supplier_name'],
            'line_count': int(r['line_count'] or 0),
            'base_value': float(r['base_value'] or 0),
        }
        for r in rows
    ]


def _fetch_target_lines(
    conn: sqlite3.Connection,
    *,
    target_import_ids: list[int],
    scope: str,
    target_detail_ids: list[int] | None = None,
) -> list[dict]:
    if not target_import_ids:
        raise ValueError('Chọn ít nhất một phiếu nhập hàng để phân bổ')
    detail_cols = _table_cols(conn, 'import_details')
    has_line_type = 'line_type' in detail_cols
    has_wh = 'warehouse_code' in detail_cols
    has_pname = 'product_name' in detail_cols

    select = [
        'd.id AS detail_id',
        'd.import_id',
        'd.product_id',
        'd.qty',
        'd.buyprice',
        'COALESCE(d.subtotal, d.qty * d.buyprice) AS subtotal',
        'COALESCE(d.discount, 0) AS discount',
        "COALESCE(i.import_no, '') AS import_no",
        "COALESCE(i.date, '') AS import_date",
    ]
    select.append(
        "COALESCE(d.line_type, 'goods') AS line_type" if has_line_type else "'goods' AS line_type"
    )
    select.append(
        "COALESCE(d.warehouse_code, 'KHO_001') AS warehouse_code"
        if has_wh else "'KHO_001' AS warehouse_code"
    )
    select.append(
        "COALESCE(d.product_name, p.name, '') AS product_name"
        if has_pname else "COALESCE(p.name, '') AS product_name"
    )

    ph = ','.join('?' * len(target_import_ids))
    params: list[Any] = list(target_import_ids)
    detail_filter = ''
    if scope == SCOPE_LINES:
        ids = [int(x) for x in (target_detail_ids or []) if x]
        if not ids:
            raise ValueError('Phạm vi theo dòng: chọn ít nhất một dòng phiếu nhập')
        ph_d = ','.join('?' * len(ids))
        detail_filter = f' AND d.id IN ({ph_d})'
        params.extend(ids)
    elif scope == SCOPE_GOODS:
        detail_filter = " AND COALESCE(d.line_type, 'goods') = 'goods'" if has_line_type else ''
    elif scope == SCOPE_MATERIALS:
        detail_filter = " AND COALESCE(d.line_type, 'goods') = 'materials'" if has_line_type else " AND 0"
    elif scope == SCOPE_FA:
        detail_filter = " AND COALESCE(d.line_type, 'goods') = 'fixed_asset'" if has_line_type else " AND 0"
    elif scope == SCOPE_TOOLS:
        detail_filter = " AND COALESCE(d.line_type, 'goods') = 'tools'" if has_line_type else " AND 0"
    else:
        if has_line_type:
            detail_filter = (
                " AND COALESCE(d.line_type, 'goods') IN "
                "('goods','materials','fixed_asset','tools')"
            )

    rows = conn.execute(
        f"""
        SELECT {', '.join(select)}
        FROM import_details d
        JOIN import i ON i.id = d.import_id
        LEFT JOIN products p ON p.id = d.product_id
        WHERE d.import_id IN ({ph})
          AND COALESCE(i.doc_type, 'stock') NOT IN ('service', 'landed_cost')
          {detail_filter}
        ORDER BY d.import_id, d.id
        """,
        params,
    ).fetchall()

    result = []
    for r in rows:
        lt = (r['line_type'] or 'goods').strip().lower()
        if lt not in ALLOCATABLE_LINE_TYPES:
            continue
        base = _money(r['subtotal']) - _money(r['discount'])
        if base <= 0:
            continue
        result.append({
            'detail_id': int(r['detail_id']),
            'import_id': int(r['import_id']),
            'import_no': r['import_no'],
            'import_date': str(r['import_date'] or '')[:10],
            'product_id': int(r['product_id']) if r['product_id'] else None,
            'product_name': (r['product_name'] or '').strip(),
            'line_type': lt,
            'warehouse_code': r['warehouse_code'] or 'KHO_001',
            'qty': float(r['qty'] or 0),
            'base_value': float(base),
            'debit_account': DEBIT_ACCOUNT_BY_LINE_TYPE.get(lt, '156'),
        })
    if not result:
        raise ValueError('Không có dòng hàng phù hợp để nhận phân bổ chi phí')
    return result


def _assert_target_imports_in_branch(
    conn: sqlite3.Connection,
    target_import_ids: list[int],
    *,
    branch_code: str | None = None,
) -> None:
    """Chặn phân bổ vào phiếu nhập ngoài chi nhánh đang chọn."""
    if not target_import_ids:
        return
    from Services.sme.branches import import_branch_filter_sql, request_branch_filter

    code = branch_code
    if code is None:
        try:
            code = request_branch_filter()
        except Exception:
            code = None
    bf, bp = import_branch_filter_sql(conn, code, alias='i')
    if not bf:
        return
    ids = [int(x) for x in target_import_ids]
    ph = ','.join('?' * len(ids))
    rows = conn.execute(
        f'SELECT i.id FROM import i WHERE i.id IN ({ph}) {bf}',
        ids + list(bp),
    ).fetchall()
    found = {int(r[0] if not hasattr(r, 'keys') else r['id']) for r in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ValueError(
            'Phiếu nhập không thuộc chi nhánh đang chọn: '
            + ', '.join(str(x) for x in missing)
        )


def preview_allocation(
    conn: sqlite3.Connection,
    *,
    invoice_id: int,
    target_import_ids: list[int],
    scope: str | None = None,
    cost_category: str | None = None,
    target_detail_ids: list[int] | None = None,
    branch_code: str | None = None,
) -> dict:
    _assert_target_imports_in_branch(
        conn,
        [int(x) for x in (target_import_ids or [])],
        branch_code=branch_code,
    )
    cat = _normalize_category(cost_category)
    sc = _normalize_scope(scope, cat)
    summary = get_cost_invoice_summary(conn, invoice_id)
    if summary['already_allocated']:
        raise ValueError('Hóa đơn này đã được phân bổ chi phí')
    targets = _fetch_target_lines(
        conn,
        target_import_ids=[int(x) for x in target_import_ids],
        scope=sc,
        target_detail_ids=target_detail_ids,
    )
    amount_net = _money(summary['amount_net'])
    base_total = sum((_money(t['base_value']) for t in targets), Decimal('0.00'))
    base_safe = base_total if base_total > 0 else Decimal('1.00')
    allocated_rows = []
    running = Decimal('0.00')
    for i, t in enumerate(targets):
        if i == len(targets) - 1:
            alloc = amount_net - running
        else:
            alloc = _money(amount_net * (_money(t['base_value']) / base_safe))
            running += alloc
        allocated_rows.append({**t, 'allocated_net': float(alloc)})
    return {
        'invoice': summary,
        'cost_category': cat,
        'cost_category_label': COST_CATEGORIES[cat],
        'scope': sc,
        'amount_net': float(amount_net),
        'amount_vat': float(summary['amount_vat']),
        'amount_total': float(summary['amount_total']),
        'base_total': float(base_total),
        'lines': allocated_rows,
    }


def _ensure_supplier(conn: sqlite3.Connection, inv: dict) -> int:
    tax = (inv.get('seller_tax_code') or '').strip()
    name = (inv.get('seller_name') or 'NCC chi phí').strip()
    address = (inv.get('address') or inv.get('seller_address') or '').strip()
    if tax:
        row = conn.execute(
            'SELECT id FROM suppliers WHERE TRIM(COALESCE(tax_code, "")) = ? LIMIT 1',
            (tax,),
        ).fetchone()
        if row:
            return int(row['id'] if hasattr(row, 'keys') else row[0])
    row = conn.execute(
        'SELECT id FROM suppliers WHERE TRIM(COALESCE(name, "")) = ? LIMIT 1',
        (name,),
    ).fetchone()
    if row:
        return int(row['id'] if hasattr(row, 'keys') else row[0])
    cols = _table_cols(conn, 'suppliers')
    fields = ['name']
    vals: list[Any] = [name]
    if 'tax_code' in cols:
        fields.append('tax_code')
        vals.append(tax or None)
    if 'address' in cols:
        fields.append('address')
        vals.append(address or None)
    ph = ','.join('?' * len(vals))
    conn.execute(
        f"INSERT INTO suppliers ({', '.join(fields)}) VALUES ({ph})",
        vals,
    )
    return int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])


def _bump_asset_cost(
    conn: sqlite3.Connection,
    *,
    line_type: str,
    import_detail_id: int,
    amount: Decimal,
) -> None:
    amt = float(amount)
    if amt <= 0:
        return
    if line_type == 'fixed_asset':
        table = 'fixed_assets'
        if not _table_exists(conn, table):
            return
        cols = _table_cols(conn, table)
        sets = []
        if 'nguyen_gia' in cols:
            sets.append('nguyen_gia = COALESCE(nguyen_gia, 0) + ?')
        if 'nguyen_gia_tinh_khau_hao' in cols:
            sets.append('nguyen_gia_tinh_khau_hao = COALESCE(nguyen_gia_tinh_khau_hao, 0) + ?')
        if 'gia_mua_chua_thue' in cols:
            sets.append('gia_mua_chua_thue = COALESCE(gia_mua_chua_thue, 0) + ?')
        if not sets:
            return
        params = [amt] * len(sets) + [import_detail_id]
        conn.execute(
            f"UPDATE {table} SET {', '.join(sets)} WHERE import_detail_id = ?",
            params,
        )
    elif line_type == 'tools':
        table = 'tools_supplies' if _table_exists(conn, 'tools_supplies') else (
            'tools' if _table_exists(conn, 'tools') else None
        )
        if not table:
            return
        cols = _table_cols(conn, table)
        sets = []
        if 'nguyen_gia' in cols:
            sets.append('nguyen_gia = COALESCE(nguyen_gia, 0) + ?')
        if 'gia_mua_chua_thue' in cols:
            sets.append('gia_mua_chua_thue = COALESCE(gia_mua_chua_thue, 0) + ?')
        if not sets:
            return
        params = [amt] * len(sets) + [import_detail_id]
        conn.execute(
            f"UPDATE {table} SET {', '.join(sets)} WHERE import_detail_id = ?",
            params,
        )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return bool(row)


def _bump_import_stock_move_cost(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    target_import_id: int,
    allocated: Decimal,
) -> dict | None:
    """Cộng CP phân bổ vào vốn dòng stock_moves nhập gốc (cost_price / total_value)."""
    sm_cols = _table_cols(conn, 'stock_moves')
    if not sm_cols or 'cost_price' not in sm_cols:
        return None

    row = conn.execute(
        """
        SELECT id, quantity, cost_price
               {tv}
        FROM stock_moves
        WHERE product_id = ?
          AND ref_id = ?
          AND LOWER(COALESCE(type, '')) IN ('import', 'nhập', 'nhap')
        ORDER BY id ASC
        LIMIT 1
        """.format(tv=', total_value' if 'total_value' in sm_cols else ', 0 AS total_value'),
        (int(product_id), int(target_import_id)),
    ).fetchone()
    if not row:
        # Fallback: không lọc type nếu schema cũ
        row = conn.execute(
            """
            SELECT id, quantity, cost_price
                   {tv}
            FROM stock_moves
            WHERE product_id = ? AND ref_id = ? AND quantity > 0
            ORDER BY id ASC
            LIMIT 1
            """.format(tv=', total_value' if 'total_value' in sm_cols else ', 0 AS total_value'),
            (int(product_id), int(target_import_id)),
        ).fetchone()
    if not row:
        return None

    move_id = int(row['id'])
    qty = float(row['quantity'] or 0)
    if qty <= 1e-9:
        return None
    old_cost = float(row['cost_price'] or 0)
    unit_bump = float(allocated) / qty
    new_cost = old_cost + unit_bump

    sets = ['cost_price = ?']
    vals: list[Any] = [new_cost]
    old_total = float(row['total_value'] or 0) if 'total_value' in sm_cols else (old_cost * qty)
    new_total = old_total + float(allocated)
    if 'total_value' in sm_cols:
        sets.append('total_value = ?')
        vals.append(new_total)
    if 'avg_cost' in sm_cols:
        sets.append('avg_cost = ?')
        vals.append(new_cost)
    vals.append(move_id)
    conn.execute(
        f"UPDATE stock_moves SET {', '.join(sets)} WHERE id = ?",
        vals,
    )
    return {
        'stock_move_id': move_id,
        'qty': qty,
        'cost_before': old_cost,
        'cost_after': new_cost,
        'total_before': old_total,
        'total_after': new_total,
        'unit_bump': unit_bump,
    }


def _bump_import_detail_cost(
    conn: sqlite3.Connection,
    *,
    detail_id: int,
    allocated: Decimal,
    qty: float,
) -> None:
    """Cập nhật cost_price nội bộ trên import_details (không đụng buyprice/subtotal HĐ)."""
    cols = _table_cols(conn, 'import_details')
    if 'cost_price' not in cols:
        return
    qty_base = float(qty or 0)
    if qty_base <= 1e-9:
        return
    unit_bump = float(allocated) / qty_base
    conn.execute(
        """
        UPDATE import_details
        SET cost_price = COALESCE(cost_price, 0) + ?
        WHERE id = ?
        """,
        (unit_bump, int(detail_id)),
    )


def _insert_value_adj_move(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    date: str,
    ref_id: int,
    ref_document: str,
    allocated: Decimal,
    wac_after: float,
    unit_bump: float,
    target_import_id: int,
    warehouse_code: str,
    note: str,
) -> int | None:
    """Dòng audit phân bổ: qty=0. Vốn thực đã cộng vào dòng import gốc."""
    sm_cols = _table_cols(conn, 'stock_moves')
    if not sm_cols:
        return None
    fields = {
        'product_id': product_id,
        'date': date,
        'type': 'landed_cost',
        'ref_id': ref_id,
        'quantity': 0,
        # Đơn giá điều chỉnh (không phải WAC) — rebuild bỏ qua type=landed_cost
        'cost_price': float(unit_bump),
        'note': note,
        'ref_document': ref_document,
        'ref_type': 'landed_cost',
        'type1': 'PBCP',
        'ref_no': str(target_import_id),
    }
    if 'warehouse_code' in sm_cols:
        fields['warehouse_code'] = warehouse_code
    if 'total_value' in sm_cols:
        fields['total_value'] = float(allocated)
    if 'avg_cost' in sm_cols:
        fields['avg_cost'] = float(wac_after)
    cols = [k for k in fields if k in sm_cols]
    vals = [fields[k] for k in cols]
    ph = ','.join('?' * len(cols))
    cur = conn.execute(
        f"INSERT INTO stock_moves ({', '.join(cols)}) VALUES ({ph})",
        vals,
    )
    return int(cur.lastrowid)


def allocate_landed_cost(
    conn: sqlite3.Connection,
    *,
    invoice_id: int,
    target_import_ids: list[int],
    scope: str | None = None,
    cost_category: str | None = None,
    target_detail_ids: list[int] | None = None,
    payment_status: str | None = 'Chưa thanh toán',
    payment_method: str | None = None,
    note: str | None = None,
    created_by: str | None = None,
    commit: bool = True,
    branch_code: str | None = None,
) -> dict:
    """Tạo PN chi phí + phân bổ + journal. Không commit nếu commit=False."""
    from Services.inward_invoice_helpers import ensure_import_service_schema
    from Services.import_line_helpers import insert_import_detail_row
    from Services.sme.bootstrap import ensure_sme_accounting_ready

    ensure_import_service_schema(conn)
    ensure_sme_accounting_ready(conn, commit=False)
    ensure_sme_landed_cost_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row

    cat = _normalize_category(cost_category)
    sc = _normalize_scope(scope, cat)
    preview = preview_allocation(
        conn,
        invoice_id=invoice_id,
        target_import_ids=target_import_ids,
        scope=sc,
        cost_category=cat,
        target_detail_ids=target_detail_ids,
        branch_code=branch_code,
    )
    inv, cost_lines = _parse_invoice_payload(conn, invoice_id)
    supplier_id = _ensure_supplier(conn, inv)
    pay_method = _resolve_payment_method(payment_status, payment_method)
    credit_acc = resolve_postable_account(conn, _credit_account_for_payment(pay_method))

    amount_net = _money(preview['amount_net'])
    amount_vat = _money(preview['amount_vat'])
    amount_total = amount_net + amount_vat
    bill_no = (inv.get('invoice_no') or '').strip()
    bill_date = str(inv.get('date') or '')[:10] or datetime.now().strftime('%Y-%m-%d')
    import_date = bill_date
    import_no = _next_cost_import_no(conn)
    cat_label = COST_CATEGORIES[cat]
    target_nos = sorted({t['import_no'] for t in preview['lines'] if t.get('import_no')})
    goods_ref = ', '.join(target_nos[:5]) + ('…' if len(target_nos) > 5 else '')
    header_note = (note or '').strip() or (
        f'{cat_label} HĐ {bill_no} phân bổ vào {goods_ref}'
    )

    import_cols = _table_cols(conn, 'import')
    pay_status = payment_status or 'Chưa thanh toán'
    pay_method_db = (
        'cash' if pay_method == 'CASH'
        else ('bank' if pay_method == 'BANK_TRANSFER' else None)
    )
    paid_amount = float(amount_total) if pay_status == 'Đã thanh toán' else 0.0

    fields = [
        'date', 'supplier_id', 'import_no', 'bill_no', 'bill_date',
        'note', 'payment_status', 'extra_cost', 'total_value', 'paid_amount',
    ]
    values: list[Any] = [
        import_date, supplier_id, import_no, bill_no, bill_date,
        header_note, pay_status, 0.0, float(amount_total), paid_amount,
    ]
    if 'doc_type' in import_cols:
        fields.append('doc_type')
        values.append('landed_cost')
    if 'from_invoice_id' in import_cols:
        fields.append('from_invoice_id')
        values.append(int(invoice_id))
    if 'payment_method' in import_cols:
        fields.append('payment_method')
        values.append(pay_method_db)
    if 'import_type' in import_cols:
        fields.append('import_type')
        values.append('DOMESTIC')
    if 'currency' in import_cols:
        fields.append('currency')
        values.append('VND')
    if 'exchange_rate' in import_cols:
        fields.append('exchange_rate')
        values.append(1.0)

    conn.execute(
        f'INSERT INTO import ({", ".join(fields)}) VALUES ({", ".join("?" * len(values))})',
        values,
    )
    cost_import_id = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])

    for line in cost_lines:
        qty = float(line['qty'] or 1) or 1.0
        insert_import_detail_row(conn.cursor(), cost_import_id, {
            'import_id': cost_import_id,
            'product_id': None,
            'qty': qty,
            'buyprice': float(line['buyprice']),
            'subtotal': float(line['subtotal']),
            'discount': float(line['discount']),
            'tax': float(line['tax']),
            'cost_price': float(line['net']) / qty if qty else 0,
            'tax_pct': float(line['tax_pct']),
            'discount_pct': float(line['discount_pct']),
            'payment_amt': float(line['total']),
            'product_name': line['name'],
            'unit': line['unit'],
            'line_type': 'service',
            'warehouse_code': 'KHO_001',
        })

    # Link doc
    cur = conn.execute(
        """
        INSERT INTO sme_landed_cost_docs (
            cost_invoice_id, cost_import_id, cost_category, scope,
            amount_net, amount_vat, amount_total, status, note, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'posted', ?, ?, ?)
        """,
        (
            int(invoice_id), cost_import_id, cat, sc,
            float(amount_net), float(amount_vat), float(amount_total),
            header_note, created_by, _now(),
        ),
    )
    landed_id = int(cur.lastrowid)

    journal_debit_lines: list[dict] = []
    saved_lines: list[dict] = []
    seq = 1
    for t in preview['lines']:
        alloc = _money(t['allocated_net'])
        if alloc <= 0:
            continue
        lt = t['line_type']
        debit_code = resolve_postable_account(
            conn, t.get('debit_account') or DEBIT_ACCOUNT_BY_LINE_TYPE.get(lt, '156')
        )
        wac_before = wac_after = None
        move_id = None
        pid = t.get('product_id')

        if lt in ('goods', 'materials'):
            if not pid:
                raise ValueError(f'Dòng {t["detail_id"]} thiếu product_id để vốn hóa kho')
            qty_on_hand = ledger_quantity(conn.cursor(), int(pid))
            if qty_on_hand <= 1e-9:
                raise ValueError(
                    f'SP #{pid} ({t.get("product_name") or ""}) hết tồn — '
                    f'không thể vốn hóa {float(alloc):,.0f} ₫'
                )
            wac_before, wac_after, _q = apply_cost_value_adjustment(
                conn.cursor(), int(pid), float(alloc),
                prefer_source_id=int(t['import_id']),
                prefer_source_type='IMPORT',
                conn=conn,
            )
            bumped = _bump_import_stock_move_cost(
                conn,
                product_id=int(pid),
                target_import_id=int(t['import_id']),
                allocated=alloc,
            )
            if not bumped:
                raise ValueError(
                    f'Không tìm thấy dòng stock_moves nhập của PN {t.get("import_no")} '
                    f'/ SP #{pid} để cập nhật vốn'
                )
            _bump_import_detail_cost(
                conn,
                detail_id=int(t['detail_id']),
                allocated=alloc,
                qty=float(bumped['qty'] or t.get('qty') or 0),
            )
            # Cập nhật luôn chi_tiet_phieu_nhap_kho nếu có
            try:
                if _table_exists(conn, 'chi_tiet_phieu_nhap_kho'):
                    conn.execute(
                        """
                        UPDATE chi_tiet_phieu_nhap_kho
                        SET cost_price = COALESCE(cost_price, 0) + ?
                        WHERE import_id = ? AND product_id = ?
                        """,
                        (float(bumped['unit_bump']), int(t['import_id']), int(pid)),
                    )
            except sqlite3.Error:
                pass
            move_id = _insert_value_adj_move(
                conn,
                product_id=int(pid),
                date=import_date,
                ref_id=cost_import_id,
                ref_document=import_no,
                allocated=alloc,
                wac_after=float(wac_after),
                unit_bump=float(bumped['unit_bump']),
                target_import_id=int(t['import_id']),
                warehouse_code=t.get('warehouse_code') or 'KHO_001',
                note=(
                    f'{cat_label} HĐ {bill_no} → PN {t.get("import_no")} '
                    f'/ dòng #{t["detail_id"]}: +{float(alloc):,.0f} ₫ '
                    f'(cost {bumped["cost_before"]:,.2f} → {bumped["cost_after"]:,.2f})'
                ),
            )
            # Ưu tiên lưu id dòng import đã bump (vốn thực trên sổ)
            move_id = bumped['stock_move_id'] or move_id
        elif lt in ('fixed_asset', 'tools'):
            _bump_asset_cost(
                conn,
                line_type=lt,
                import_detail_id=int(t['detail_id']),
                amount=alloc,
            )

        journal_debit_lines.append({
            'sequence': seq,
            'account_code': debit_code,
            'debit': alloc,
            'credit': 0,
            'partner_id': supplier_id,
            'partner_type': 'supplier',
            'product_id': pid,
            'warehouse_code': t.get('warehouse_code'),
            'vat_invoice_no': bill_no,
            'description': (
                f'{cat_label}: {t.get("product_name") or lt} '
                f'(PN {t.get("import_no")})'
            ),
        })
        seq += 1

        line_cols = _table_cols(conn, 'sme_landed_cost_lines')
        line_fields = {
            'landed_cost_id': landed_id,
            'target_import_id': int(t['import_id']),
            'target_detail_id': int(t['detail_id']),
            'product_id': pid,
            'line_type': lt,
            'warehouse_code': t.get('warehouse_code'),
            'base_value': float(t['base_value']),
            'allocated_net': float(alloc),
            'debit_account': debit_code,
            'wac_before': float(wac_before) if wac_before is not None else None,
            'wac_after': float(wac_after) if wac_after is not None else None,
            'stock_move_id': move_id,
            'stock_capitalized': 1 if lt in ('goods', 'materials') else 0,
        }
        use_cols = [k for k in line_fields if k in line_cols]
        conn.execute(
            f"INSERT INTO sme_landed_cost_lines ({', '.join(use_cols)}) "
            f"VALUES ({', '.join('?' * len(use_cols))})",
            [line_fields[k] for k in use_cols],
        )
        saved_lines.append({
            'target_import_id': t['import_id'],
            'target_detail_id': t['detail_id'],
            'product_id': pid,
            'line_type': lt,
            'allocated_net': float(alloc),
            'debit_account': debit_code,
            'wac_before': wac_before,
            'wac_after': wac_after,
            'stock_move_id': move_id,
        })

    if amount_vat > 0:
        vat_acc = resolve_postable_account(conn, '13311')
        journal_debit_lines.append({
            'sequence': seq,
            'account_code': vat_acc,
            'debit': amount_vat,
            'credit': 0,
            'partner_id': supplier_id,
            'partner_type': 'supplier',
            'vat_invoice_no': bill_no,
            'description': f'Thuế GTGT đầu vào — {cat_label} HĐ {bill_no}',
        })
        seq += 1

    journal_debit_lines.append({
        'sequence': seq,
        'account_code': credit_acc,
        'debit': 0,
        'credit': amount_total,
        'partner_id': supplier_id,
        'partner_type': 'supplier',
        'vat_invoice_no': bill_no,
        'description': f'Đối ứng {cat_label} HĐ {bill_no}',
    })

    entry = post_journal_entry(
        conn,
        posting_date=import_date,
        document_date=bill_date,
        document_type=DOCUMENT_TYPE,
        document_no=import_no,
        document_id=cost_import_id,
        business_type=BUSINESS_TYPE,
        description=header_note,
        reference_document=bill_no or import_no,
        created_by=created_by,
        lines=journal_debit_lines,
    )
    conn.execute(
        'UPDATE sme_landed_cost_docs SET journal_entry_id = ? WHERE id = ?',
        (int(entry['id']), landed_id),
    )

    # Đánh dấu HĐ đã hạch toán / phân bổ
    conn.execute(
        "UPDATE supplier_invoice SET status = 'accounted' WHERE id = ?",
        (int(invoice_id),),
    )

    if commit:
        sqlite_commit(conn, label='landed_cost')

    return {
        'success': True,
        'landed_cost_id': landed_id,
        'cost_import_id': cost_import_id,
        'import_no': import_no,
        'journal_entry_id': int(entry['id']),
        'amount_net': float(amount_net),
        'amount_vat': float(amount_vat),
        'amount_total': float(amount_total),
        'cost_category': cat,
        'scope': sc,
        'lines': saved_lines,
    }


def reverse_landed_cost(
    conn: sqlite3.Connection,
    *,
    invoice_id: int | None = None,
    landed_cost_id: int | None = None,
    created_by: str | None = None,
    reason: str = 'Hủy phân bổ chi phí để sửa lại',
    commit: bool = True,
) -> dict:
    """Đảo phân bổ: WAC/FIFO lots, vốn stock_moves, nguyên giá TSCĐ/CCDC, journal, mở lại HĐ."""
    from Services.inventory_cost import apply_cost_value_adjustment
    from Services.sme.bootstrap import ensure_sme_accounting_ready
    from Services.sme.journal_engine import reverse_journal_entry

    ensure_sme_accounting_ready(conn, commit=False)
    ensure_sme_landed_cost_schema(conn, commit=False)
    conn.row_factory = sqlite3.Row

    if landed_cost_id:
        doc = conn.execute(
            """
            SELECT * FROM sme_landed_cost_docs
            WHERE id = ? AND COALESCE(status, 'posted') = 'posted'
            """,
            (int(landed_cost_id),),
        ).fetchone()
    elif invoice_id:
        doc = conn.execute(
            """
            SELECT * FROM sme_landed_cost_docs
            WHERE cost_invoice_id = ? AND COALESCE(status, 'posted') = 'posted'
            """,
            (int(invoice_id),),
        ).fetchone()
    else:
        raise ValueError('Thiếu invoice_id hoặc landed_cost_id')

    if not doc:
        raise ValueError('Không tìm thấy phân bổ chi phí còn hiệu lực để hủy')

    doc_id = int(doc['id'])
    cost_import_id = int(doc['cost_import_id'])
    inv_id = int(doc['cost_invoice_id'])
    journal_id = int(doc['journal_entry_id']) if doc['journal_entry_id'] else None

    lines = conn.execute(
        'SELECT * FROM sme_landed_cost_lines WHERE landed_cost_id = ?',
        (doc_id,),
    ).fetchall()

    for line in lines:
        alloc = _money(line['allocated_net'])
        if alloc <= 0:
            continue
        lt = (line['line_type'] or '').strip().lower()
        pid = int(line['product_id']) if line['product_id'] else None
        detail_id = int(line['target_detail_id']) if line['target_detail_id'] else None
        target_import_id = int(line['target_import_id'])

        if lt in ('goods', 'materials') and pid:
            # Đảo giá vốn (WAC hoặc unit_cost lô)
            try:
                apply_cost_value_adjustment(
                    conn.cursor(), pid, float(-alloc),
                    prefer_source_id=target_import_id,
                    prefer_source_type='IMPORT',
                    conn=conn,
                )
            except ValueError:
                # Hết tồn — vẫn cố gắng đảo vốn dòng import gốc
                pass
            capitalized = 1
            if 'stock_capitalized' in line.keys():
                capitalized = int(line['stock_capitalized'] or 0)
            if capitalized:
                bumped = _bump_import_stock_move_cost(
                    conn,
                    product_id=pid,
                    target_import_id=target_import_id,
                    allocated=-alloc,
                )
                qty = float(bumped['qty']) if bumped else 0.0
                if detail_id and qty <= 0:
                    det = conn.execute(
                        'SELECT qty FROM import_details WHERE id = ?',
                        (detail_id,),
                    ).fetchone()
                    qty = float(det['qty'] if det else 0) or 0
                if detail_id and qty > 0:
                    _bump_import_detail_cost(
                        conn,
                        detail_id=detail_id,
                        allocated=-alloc,
                        qty=qty,
                    )
                try:
                    if bumped and _table_exists(conn, 'chi_tiet_phieu_nhap_kho'):
                        conn.execute(
                            """
                            UPDATE chi_tiet_phieu_nhap_kho
                            SET cost_price = COALESCE(cost_price, 0) + ?
                            WHERE import_id = ? AND product_id = ?
                            """,
                            (float(bumped['unit_bump']), target_import_id, pid),
                        )
                except sqlite3.Error:
                    pass
        elif lt in ('fixed_asset', 'tools') and detail_id:
            _bump_asset_cost(
                conn,
                line_type=lt,
                import_detail_id=detail_id,
                amount=-alloc,
            )

    # Xóa dòng audit landed_cost trên stock_moves
    try:
        conn.execute(
            """
            DELETE FROM stock_moves
            WHERE ref_id = ?
              AND LOWER(COALESCE(type, '')) IN ('landed_cost', 'pbcp', 'value_adj')
            """,
            (cost_import_id,),
        )
    except sqlite3.Error:
        pass

    # Đảo journal
    reversed_journal_id = None
    if journal_id:
        try:
            rev = reverse_journal_entry(
                conn,
                journal_id,
                created_by=created_by,
                reason=reason,
            )
            reversed_journal_id = int(rev['id'])
        except ValueError as exc:
            # Đã đảo rồi — tiếp tục dọn chứng từ
            if 'đảo' not in str(exc).lower() and 'reversed' not in str(exc).lower():
                raise

    # Xóa phiếu import chi phí + chi tiết
    try:
        conn.execute('DELETE FROM import_details WHERE import_id = ?', (cost_import_id,))
    except sqlite3.Error:
        pass
    try:
        conn.execute('DELETE FROM phieu_nhap_kho WHERE import_id = ?', (cost_import_id,))
    except sqlite3.Error:
        pass
    try:
        conn.execute('DELETE FROM import WHERE id = ?', (cost_import_id,))
    except sqlite3.Error:
        pass

    conn.execute('DELETE FROM sme_landed_cost_lines WHERE landed_cost_id = ?', (doc_id,))
    conn.execute('DELETE FROM sme_landed_cost_docs WHERE id = ?', (doc_id,))

    # Mở lại hóa đơn chờ xử lý
    conn.execute(
        "UPDATE supplier_invoice SET status = 'new' WHERE id = ?",
        (inv_id,),
    )

    if commit:
        sqlite_commit(conn, label='landed_cost')

    return {
        'success': True,
        'reversed_landed_cost_id': doc_id,
        'invoice_id': inv_id,
        'cost_import_id': cost_import_id,
        'reversed_journal_entry_id': reversed_journal_id,
        'reason': reason,
    }
