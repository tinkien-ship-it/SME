"""Nghiệp vụ kho SME P1 — kiểm kê (05-VT), chuyển kho, phân bổ NVL (07-VT), bảng kê mua (06-VT)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.inventory_cost import apply_cost_inbound, apply_cost_outbound
from Services.inventory_stock_helpers import (
    ledger_quantity,
    sync_inventory_quantity_from_moves,
)
from Services.sme.journal_engine import ensure_sme_journal_ready, post_journal_entry, reverse_journal_entry
from db_utils import sqlite_commit

MONEY_Q = Decimal('0.01')


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _f(val) -> float:
    return float(_money(val))


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_inventory_ops_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_stock_counts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_code TEXT NOT NULL DEFAULT '05-VT',
            doc_no TEXT NOT NULL UNIQUE,
            count_date TEXT NOT NULL,
            warehouse_code TEXT,
            notes TEXT,
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'posted',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_stock_count_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            count_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            book_qty REAL NOT NULL DEFAULT 0,
            counted_qty REAL NOT NULL DEFAULT 0,
            diff_qty REAL NOT NULL DEFAULT 0,
            unit_cost REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            inv_account TEXT,
            note TEXT,
            FOREIGN KEY(count_id) REFERENCES sme_stock_counts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_stock_transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_code TEXT NOT NULL DEFAULT '02-VT',
            doc_no TEXT NOT NULL UNIQUE,
            transfer_date TEXT NOT NULL,
            from_warehouse TEXT NOT NULL,
            to_warehouse TEXT NOT NULL,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'posted',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_stock_transfer_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity REAL NOT NULL DEFAULT 0,
            unit_cost REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(transfer_id) REFERENCES sme_stock_transfers(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_material_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            form_code TEXT NOT NULL DEFAULT '07-VT',
            doc_no TEXT NOT NULL UNIQUE,
            alloc_date TEXT NOT NULL,
            expense_account TEXT NOT NULL DEFAULT '621',
            notes TEXT,
            journal_entry_id INTEGER,
            status TEXT NOT NULL DEFAULT 'posted',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_material_allocation_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alloc_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity REAL NOT NULL DEFAULT 0,
            unit_cost REAL NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            inv_account TEXT,
            FOREIGN KEY(alloc_id) REFERENCES sme_material_allocations(id)
        )
        """
    )
    # Multi-branch analytic columns
    for table, cols in (
        ('sme_stock_transfers', (
            ('from_branch_code', 'TEXT'),
            ('to_branch_code', 'TEXT'),
            ('is_inter_branch', 'INTEGER NOT NULL DEFAULT 0'),
        )),
        ('sme_stock_counts', (('branch_code', 'TEXT'),)),
        ('sme_material_allocations', (('branch_code', 'TEXT'), ('warehouse_code', 'TEXT'),)),
    ):
        existing = {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
        for col, typ in cols:
            if col not in existing:
                try:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {typ}')
                except sqlite3.OperationalError:
                    pass
    if commit:
        sqlite_commit(conn, label='inventory_ops')


def _next_no(conn: sqlite3.Connection, table: str, col: str, prefix: str) -> str:
    row = conn.execute(
        f"SELECT {col} FROM {table} WHERE {col} LIKE ? ORDER BY id DESC LIMIT 1",
        (f'{prefix}%',),
    ).fetchone()
    if not row:
        return f'{prefix}000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row[col]
    digits = ''.join(ch for ch in str(raw) if ch.isdigit()) or '0'
    return f'{prefix}{int(digits) + 1:06d}'


def normalize_inventory_tk(account_code: str | None) -> str | None:
    """Chuẩn hóa mã TK → 152 / 155 / 156 (bỏ qua CCDC/TSCĐ)."""
    code = (account_code or '').strip()
    if not code:
        return None
    for prefix in ('152', '155', '156'):
        if code == prefix or code.startswith(prefix):
            return prefix
    return None


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        from db.schema_helpers import column_exists
        return bool(column_exists(conn, table, column))
    except Exception:
        try:
            rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
            names = {(r[1] if not isinstance(r, sqlite3.Row) else r['name']) for r in rows}
            return column in names
        except Exception:
            return False


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        from db.dialect import table_exists
        return bool(table_exists(conn, name))
    except Exception:
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
            return bool(row)
        except Exception:
            return False


def inventory_accounts_by_product(
    conn: sqlite3.Connection,
    product_ids: list[int] | None = None,
) -> dict[int, str]:
    """Map product_id → TK kho từ sổ cái (định khoản nhập/xuất 152/155/156).

    Ưu tiên TK có phát sinh Nợ lớn nhất (nhập kho); không suy từ product_type.
    """
    out: dict[int, str] = {}
    if not _table_exists(conn, 'sme_journal_lines'):
        return out
    if not _table_has_column(conn, 'sme_journal_lines', 'product_id'):
        return out
    if not _table_has_column(conn, 'sme_journal_lines', 'account_code'):
        return out

    where = [
        'product_id IS NOT NULL',
        "(account_code LIKE '152%' OR account_code LIKE '155%' OR account_code LIKE '156%')",
    ]
    params: list[Any] = []
    if product_ids is not None:
        ids = [int(x) for x in product_ids if x is not None]
        if not ids:
            return out
        placeholders = ','.join('?' for _ in ids)
        where.append(f'product_id IN ({placeholders})')
        params.extend(ids)

    try:
        rows = conn.execute(
            f"""
            SELECT product_id, account_code,
                   COALESCE(SUM(debit), 0) AS debit_sum,
                   COALESCE(SUM(credit), 0) AS credit_sum,
                   MAX(id) AS last_line_id
            FROM sme_journal_lines
            WHERE {' AND '.join(where)}
            GROUP BY product_id, account_code
            """,
            tuple(params),
        ).fetchall()
    except sqlite3.Error:
        return out

    # product_id -> best (debit, credit, last_line_id, tk)
    best: dict[int, tuple[float, float, int, str]] = {}
    for r in rows:
        d = dict(r) if isinstance(r, sqlite3.Row) else {
            'product_id': r[0], 'account_code': r[1],
            'debit_sum': r[2], 'credit_sum': r[3], 'last_line_id': r[4],
        }
        pid = int(d['product_id'] or 0)
        tk = normalize_inventory_tk(d.get('account_code'))
        if not pid or not tk:
            continue
        score = (
            float(d.get('debit_sum') or 0),
            float(d.get('credit_sum') or 0),
            int(d.get('last_line_id') or 0),
            tk,
        )
        prev = best.get(pid)
        if prev is None or score[:3] > prev[:3]:
            best[pid] = score

    for pid, score in best.items():
        out[pid] = score[3]
    return out


def _inventory_account_from_import_voucher(conn: sqlite3.Connection, product_id: int) -> str | None:
    """Fallback: TK theo dòng phiếu nhập gần nhất (line_type → TK định khoản), không theo products.product_type."""
    if not _table_exists(conn, 'import_details'):
        return None
    has_lt = _table_has_column(conn, 'import_details', 'line_type')
    has_aa = _table_has_column(conn, 'import_details', 'asset_account')
    if not has_lt and not has_aa:
        return None
    lt_expr = "COALESCE(d.line_type, '')" if has_lt else "''"
    aa_expr = "COALESCE(d.asset_account, '')" if has_aa else "''"
    try:
        row = conn.execute(
            f"""
            SELECT {lt_expr} AS line_type, {aa_expr} AS asset_account
            FROM import_details d
            WHERE d.product_id = ?
            ORDER BY d.id DESC
            LIMIT 1
            """,
            (int(product_id),),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    lt = row[0] if not isinstance(row, sqlite3.Row) else row['line_type']
    aa = row[1] if not isinstance(row, sqlite3.Row) else row['asset_account']
    try:
        from Services.sme.import_transit import final_inventory_account
        return normalize_inventory_tk(final_inventory_account(str(lt or ''), str(aa or '') or None))
    except Exception:
        return None


def _inventory_account_from_product_type_fallback(conn: sqlite3.Connection, product_id: int) -> str:
    """Chỉ dùng khi chưa có định khoản / phiếu nhập — giữ tương thích dữ liệu cũ."""
    try:
        row = conn.execute(
            "SELECT COALESCE(product_type, 'goods') FROM products WHERE id = ?",
            (product_id,),
        ).fetchone()
        pt = (row[0] if row else 'goods') or 'goods'
    except sqlite3.Error:
        pt = 'goods'
    pt = str(pt).lower()
    if pt in ('materials', 'material', 'raw_materials', 'nvl'):
        return '152'
    if pt in ('finished_goods', 'finished', 'thanh_pham', 'recipe'):
        return '155'
    if pt in ('tools', 'tool', 'ccdc'):
        return '153'
    if pt in ('fixed_asset', 'tscd'):
        return '211'
    return '156'


def inventory_account_for_product(conn: sqlite3.Connection, product_id: int) -> str:
    """TK kho theo số tài khoản đã định khoản (152/155/156), không ưu tiên product_type.

    1) Sổ cái sme_journal_lines (Nợ/Có 152|155|156 + product_id)
    2) Dòng phiếu nhập gần nhất (line_type → TK quy tắc định khoản)
    3) Fallback product_type (dữ liệu cũ / chưa phát sinh)
    """
    pid = int(product_id)
    mapped = inventory_accounts_by_product(conn, [pid])
    if pid in mapped:
        return mapped[pid]
    from_import = _inventory_account_from_import_voucher(conn, pid)
    if from_import:
        return from_import
    return _inventory_account_from_product_type_fallback(conn, pid)


def _avg_cost(conn: sqlite3.Connection, product_id: int) -> float:
    row = conn.execute(
        'SELECT COALESCE(avg_cost, 0) FROM inventory WHERE product_id = ?',
        (product_id,),
    ).fetchone()
    return float(row[0] if row else 0)


def post_stock_count(
    conn: sqlite3.Connection,
    *,
    count_date: str,
    items: list[dict],
    warehouse_code: str = '',
    notes: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Kiểm kê kho 05-VT: điều chỉnh stock_moves + bút toán thừa/thiếu."""
    from Services.sme.branches import get_warehouse_branch_code, resolve_posting_branch

    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_inventory_ops_schema(conn, commit=False)

    date_s = str(count_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày kiểm kê')
    if not items:
        raise ValueError('Không có dòng kiểm kê')

    wh = (warehouse_code or '').strip()
    branch = resolve_posting_branch(conn, get_warehouse_branch_code(conn, wh) if wh else None)

    doc_no = _next_no(conn, 'sme_stock_counts', 'doc_no', 'KK')
    when = _now()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sme_stock_counts
            (form_code, doc_no, count_date, warehouse_code, notes, status, created_by, created_at, branch_code)
        VALUES ('05-VT', ?, ?, ?, ?, 'posted', ?, ?, ?)
        """,
        (doc_no, date_s, wh or None, notes or '', created_by, when, branch),
    )
    count_id = cur.lastrowid

    journal_agg: dict[str, Decimal] = {}  # key f"{side}:{account}"
    line_count = 0

    for raw in items:
        pid = int(raw.get('product_id') or 0)
        if pid <= 0:
            continue
        counted = _money(raw.get('counted_qty') or raw.get('checked_qty') or 0)
        book = _money(ledger_quantity(cur, pid))
        diff = counted - book
        if diff == 0:
            continue
        cost = _money(raw.get('unit_cost') if raw.get('unit_cost') is not None else _avg_cost(conn, pid))
        amount = abs(diff) * cost
        inv_acc = inventory_account_for_product(conn, pid)
        note = raw.get('note') or ('Thừa kiểm kê' if diff > 0 else 'Thiếu kiểm kê')

        move_type = 'import' if diff > 0 else 'export'
        cur.execute(
            """
            INSERT INTO stock_moves
                (product_id, date, type, ref_id, ref_document, ref_type, quantity, note, type1, cost_price)
            VALUES (?, ?, ?, ?, ?, 'inventory_check', ?, ?, 'Kiểm Kê SME', ?)
            """,
            (pid, when, move_type, count_id, doc_no, float(diff), note, float(cost)),
        )
        row_inv = cur.execute(
            'SELECT product_id FROM inventory WHERE product_id = ?', (pid,)
        ).fetchone()
        if row_inv:
            cur.execute(
                'UPDATE inventory SET avg_cost = COALESCE(avg_cost, ?) WHERE product_id = ?',
                (float(cost), pid),
            )
        else:
            cur.execute(
                'INSERT INTO inventory (product_id, quantity, avg_cost) VALUES (?, 0, ?)',
                (pid, float(cost)),
            )
        try:
            sync_inventory_quantity_from_moves(cur, pid)
        except Exception:
            pass

        cur.execute(
            """
            INSERT INTO sme_stock_count_lines
                (count_id, product_id, book_qty, counted_qty, diff_qty, unit_cost, amount, inv_account, note)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (count_id, pid, float(book), float(counted), float(diff), float(cost), float(amount), inv_acc, note),
        )
        line_count += 1
        if amount <= 0:
            continue
        if diff > 0:
            # Thừa: Nợ kho / Có 711
            journal_agg[f'd:{inv_acc}'] = journal_agg.get(f'd:{inv_acc}', Decimal('0')) + amount
            journal_agg['c:711'] = journal_agg.get('c:711', Decimal('0')) + amount
        else:
            # Thiếu: Nợ role cogs.spoilage (mặc định 6328) / Có kho
            from Services.sme.cogs_accounts import cogs_spoilage_account
            spoil = cogs_spoilage_account()
            journal_agg[f'd:{spoil}'] = journal_agg.get(f'd:{spoil}', Decimal('0')) + amount
            journal_agg[f'c:{inv_acc}'] = journal_agg.get(f'c:{inv_acc}', Decimal('0')) + amount

    if line_count == 0:
        conn.execute('DELETE FROM sme_stock_counts WHERE id = ?', (count_id,))
        raise ValueError('Không có chênh lệch kiểm kê để ghi')

    entry = None
    if journal_agg:
        lines = []
        seq = 1
        for key, amt in journal_agg.items():
            side, acc = key.split(':', 1)
            if amt <= 0:
                continue
            lines.append({
                'sequence': seq,
                'account_code': acc,
                'debit': float(amt) if side == 'd' else 0,
                'credit': float(amt) if side == 'c' else 0,
                'description': f'Kiểm kê kho {doc_no}',
            })
            seq += 1
        if lines:
            entry = post_journal_entry(
                conn,
                posting_date=date_s,
                document_date=date_s,
                document_type='KKVT',
                document_no=doc_no,
                document_id=count_id,
                business_type='KIEM_KE_KHO',
                description=f'Kiểm kê kho {doc_no}',
                created_by=created_by,
                branch_code=branch,
                lines=lines,
            )
            conn.execute(
                'UPDATE sme_stock_counts SET journal_entry_id = ? WHERE id = ?',
                (entry['id'], count_id),
            )

    if commit:
        sqlite_commit(conn, label='inventory_ops')
    return get_stock_count(conn, count_id)


def get_stock_count(conn: sqlite3.Connection, count_id: int) -> dict[str, Any] | None:
    ensure_sme_inventory_ops_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_stock_counts WHERE id = ?', (count_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    lines = conn.execute(
        """
        SELECT l.*, p.name AS product_name, p.unit
        FROM sme_stock_count_lines l
        LEFT JOIN products p ON p.id = l.product_id
        WHERE l.count_id = ?
        ORDER BY l.id
        """,
        (count_id,),
    ).fetchall()
    d['lines'] = [dict(x) for x in lines]
    return d


def list_stock_counts(
    conn: sqlite3.Connection,
    *,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_inventory_ops_schema(conn, commit=False)
    from Services.sme.branches import DEFAULT_BRANCH_CODE

    sql = "SELECT * FROM sme_stock_counts WHERE status != 'void'"
    params: list[Any] = []
    code = (branch_code or '').strip().upper()
    if code and code != 'ALL':
        if code == DEFAULT_BRANCH_CODE:
            sql += " AND (branch_code IS NULL OR branch_code = '' OR branch_code = ?)"
        else:
            sql += ' AND branch_code = ?'
        params.append(code)
    sql += ' ORDER BY count_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def create_stock_transfer(
    conn: sqlite3.Connection,
    *,
    transfer_date: str,
    from_warehouse: str,
    to_warehouse: str,
    items: list[dict],
    notes: str = '',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Chuyển kho nội bộ / liên CN — stock_moves cặp xuất/nhập, không ghi GL (cùng pháp nhân)."""
    from Services.sme.branches import get_warehouse_branch_code

    ensure_sme_inventory_ops_schema(conn, commit=False)
    fw = (from_warehouse or '').strip()
    tw = (to_warehouse or '').strip()
    if not fw or not tw:
        raise ValueError('Thiếu kho đi / kho đến')
    if fw == tw:
        raise ValueError('Kho đi và kho đến phải khác nhau')
    date_s = str(transfer_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày chuyển kho')
    if not items:
        raise ValueError('Không có dòng chuyển kho')

    from_br = get_warehouse_branch_code(conn, fw)
    to_br = get_warehouse_branch_code(conn, tw)
    inter = 1 if from_br != to_br else 0
    note_extra = notes or ''
    if inter and 'liên CN' not in note_extra.lower():
        note_extra = (note_extra + f' · Liên CN {from_br}→{to_br}').strip(' ·')

    doc_no = _next_no(conn, 'sme_stock_transfers', 'doc_no', 'CK')
    when = _now()
    cur = conn.cursor()
    sm_cols = {r[1] for r in cur.execute('PRAGMA table_info(stock_moves)').fetchall()}
    has_wh = 'warehouse_code' in sm_cols
    cur.execute(
        """
        INSERT INTO sme_stock_transfers
            (form_code, doc_no, transfer_date, from_warehouse, to_warehouse, notes, status,
             created_by, created_at, from_branch_code, to_branch_code, is_inter_branch)
        VALUES ('02-VT', ?, ?, ?, ?, ?, 'posted', ?, ?, ?, ?, ?)
        """,
        (doc_no, date_s, fw, tw, note_extra, created_by, when, from_br, to_br, inter),
    )
    tid = cur.lastrowid
    n = 0
    for raw in items:
        pid = int(raw.get('product_id') or 0)
        qty = _money(raw.get('quantity'))
        if pid <= 0 or qty <= 0:
            continue
        cost = _money(raw.get('unit_cost') if raw.get('unit_cost') is not None else _avg_cost(conn, pid))
        note = f'CK {fw}→{tw}'
        if inter:
            note = f'CK liên CN {from_br}→{to_br} ({fw}→{tw})'
        if has_wh:
            cur.execute(
                """
                INSERT INTO stock_moves
                    (product_id, date, type, ref_id, ref_document, ref_type, quantity, note, type1, cost_price, warehouse_code)
                VALUES (?, ?, 'export', ?, ?, 'stock_transfer', ?, ?, 'Chuyển kho', ?, ?)
                """,
                (pid, when, tid, doc_no, float(-qty), note, float(cost), fw),
            )
            cur.execute(
                """
                INSERT INTO stock_moves
                    (product_id, date, type, ref_id, ref_document, ref_type, quantity, note, type1, cost_price, warehouse_code)
                VALUES (?, ?, 'import', ?, ?, 'stock_transfer', ?, ?, 'Chuyển kho', ?, ?)
                """,
                (pid, when, tid, doc_no, float(qty), note, float(cost), tw),
            )
        else:
            cur.execute(
                """
                INSERT INTO stock_moves
                    (product_id, date, type, ref_id, ref_document, ref_type, quantity, note, type1, cost_price)
                VALUES (?, ?, 'export', ?, ?, 'stock_transfer', ?, ?, 'Chuyển kho', ?)
                """,
                (pid, when, tid, doc_no, float(-qty), note, float(cost)),
            )
            cur.execute(
                """
                INSERT INTO stock_moves
                    (product_id, date, type, ref_id, ref_document, ref_type, quantity, note, type1, cost_price)
                VALUES (?, ?, 'import', ?, ?, 'stock_transfer', ?, ?, 'Chuyển kho', ?)
                """,
                (pid, when, tid, doc_no, float(qty), note, float(cost)),
            )
        try:
            sync_inventory_quantity_from_moves(cur, pid)
        except Exception:
            pass
        cur.execute(
            """
            INSERT INTO sme_stock_transfer_lines (transfer_id, product_id, quantity, unit_cost)
            VALUES (?,?,?,?)
            """,
            (tid, pid, float(qty), float(cost)),
        )
        n += 1
    if n == 0:
        conn.execute('DELETE FROM sme_stock_transfers WHERE id = ?', (tid,))
        raise ValueError('Không có dòng hợp lệ')
    if commit:
        sqlite_commit(conn, label='inventory_ops')
    row = conn.execute('SELECT * FROM sme_stock_transfers WHERE id = ?', (tid,)).fetchone()
    d = dict(row)
    d['lines'] = [dict(x) for x in conn.execute(
        'SELECT * FROM sme_stock_transfer_lines WHERE transfer_id = ?', (tid,)
    ).fetchall()]
    return d


def get_stock_transfer(conn: sqlite3.Connection, transfer_id: int) -> dict[str, Any] | None:
    ensure_sme_inventory_ops_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_stock_transfers WHERE id = ?', (transfer_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['lines'] = [dict(x) for x in conn.execute(
        """
        SELECT l.*, p.name AS product_name, p.unit
        FROM sme_stock_transfer_lines l
        LEFT JOIN products p ON p.id = l.product_id
        WHERE l.transfer_id = ? ORDER BY l.id
        """,
        (transfer_id,),
    ).fetchall()]
    return d


def list_stock_transfers(
    conn: sqlite3.Connection,
    *,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_inventory_ops_schema(conn, commit=False)
    from Services.sme.branches import DEFAULT_BRANCH_CODE

    sql = "SELECT * FROM sme_stock_transfers WHERE status != 'void'"
    params: list[Any] = []
    code = (branch_code or '').strip().upper()
    if code and code != 'ALL':
        if code == DEFAULT_BRANCH_CODE:
            sql += """
                AND (
                    from_branch_code IS NULL OR from_branch_code = '' OR from_branch_code = ?
                    OR to_branch_code IS NULL OR to_branch_code = '' OR to_branch_code = ?
                )
            """
            params.extend([DEFAULT_BRANCH_CODE, DEFAULT_BRANCH_CODE])
        else:
            sql += ' AND (from_branch_code = ? OR to_branch_code = ?)'
            params.extend([code, code])
    sql += ' ORDER BY transfer_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def void_stock_transfer(
    conn: sqlite3.Connection,
    transfer_id: int,
    *,
    reason: str = 'Hủy chuyển kho',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Hủy chuyển kho — đảo cặp stock_moves."""
    from Services.sme.branch_filter import assert_stock_transfer_in_branch
    assert_stock_transfer_in_branch(conn, transfer_id)
    doc = get_stock_transfer(conn, transfer_id)
    if not doc:
        raise ValueError('Không tìm thấy phiếu chuyển kho')
    if doc.get('status') == 'void':
        raise ValueError('Đã hủy')
    when = _now()
    moves = conn.execute(
        "SELECT * FROM stock_moves WHERE ref_type = 'stock_transfer' AND ref_id = ?",
        (transfer_id,),
    ).fetchall()
    sm_cols = {r[1] for r in conn.execute('PRAGMA table_info(stock_moves)').fetchall()}
    has_wh = 'warehouse_code' in sm_cols
    for m in moves:
        md = dict(m)
        qty = float(md.get('quantity') or 0)
        if qty == 0:
            continue
        rev_type = 'import' if qty < 0 else 'export'
        if has_wh:
            conn.execute(
                """
                INSERT INTO stock_moves
                    (product_id, date, type, ref_id, ref_document, ref_type, quantity, note, type1, cost_price, warehouse_code)
                VALUES (?, ?, ?, ?, ?, 'stock_transfer', ?, ?, 'Hủy chuyển kho', ?, ?)
                """,
                (
                    md['product_id'], when, rev_type, transfer_id, doc['doc_no'],
                    -qty, reason, md.get('cost_price') or 0, md.get('warehouse_code'),
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO stock_moves
                    (product_id, date, type, ref_id, ref_document, ref_type, quantity, note, type1, cost_price)
                VALUES (?, ?, ?, ?, ?, 'stock_transfer', ?, ?, 'Hủy chuyển kho', ?)
                """,
                (
                    md['product_id'], when, rev_type, transfer_id, doc['doc_no'],
                    -qty, reason, md.get('cost_price') or 0,
                ),
            )
        try:
            sync_inventory_quantity_from_moves(conn.cursor(), int(md['product_id']))
        except Exception:
            pass
    conn.execute(
        "UPDATE sme_stock_transfers SET status = 'void', notes = ? WHERE id = ?",
        ((doc.get('notes') or '') + f' | {reason}', transfer_id),
    )
    if commit:
        sqlite_commit(conn, label='inventory_ops')
    return get_stock_transfer(conn, transfer_id)


def allocate_materials(
    conn: sqlite3.Connection,
    *,
    alloc_date: str,
    items: list[dict],
    expense_account: str = '621',
    notes: str = '',
    warehouse_code: str = '',
    branch_code: str | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Phân bổ NVL/CCDC xuất dùng (07-VT): Nợ TK chi phí / Có 152|153."""
    from Services.sme.branches import get_warehouse_branch_code, resolve_posting_branch

    ensure_sme_journal_ready(conn, commit=False)
    ensure_sme_inventory_ops_schema(conn, commit=False)
    date_s = str(alloc_date or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày phân bổ')
    exp = (expense_account or '621').strip() or '621'
    if not items:
        raise ValueError('Không có dòng phân bổ')

    wh = (warehouse_code or '').strip()
    if branch_code:
        branch = resolve_posting_branch(conn, branch_code)
    elif wh:
        branch = resolve_posting_branch(conn, get_warehouse_branch_code(conn, wh))
    else:
        branch = resolve_posting_branch(conn, None)

    doc_no = _next_no(conn, 'sme_material_allocations', 'doc_no', 'PBN')
    when = _now()
    cur = conn.cursor()
    sm_cols = {r[1] for r in cur.execute('PRAGMA table_info(stock_moves)').fetchall()}
    has_wh = 'warehouse_code' in sm_cols
    cur.execute(
        """
        INSERT INTO sme_material_allocations
            (form_code, doc_no, alloc_date, expense_account, notes, status,
             created_by, created_at, branch_code, warehouse_code)
        VALUES ('07-VT', ?, ?, ?, ?, 'posted', ?, ?, ?, ?)
        """,
        (doc_no, date_s, exp, notes or '', created_by, when, branch, wh or None),
    )
    aid = cur.lastrowid
    by_inv: dict[str, Decimal] = {}
    total = Decimal('0.00')
    n = 0
    for raw in items:
        pid = int(raw.get('product_id') or 0)
        qty = _money(raw.get('quantity'))
        if pid <= 0 or qty <= 0:
            continue
        cost = _money(raw.get('unit_cost') if raw.get('unit_cost') is not None else _avg_cost(conn, pid))
        amt = qty * cost
        inv_acc = inventory_account_for_product(conn, pid)
        if inv_acc == '156':
            inv_acc = '152'  # xuất dùng ưu tiên NVL; hàng hóa → vẫn cho phép 156
            try:
                pt_row = conn.execute(
                    "SELECT COALESCE(product_type,'goods') FROM products WHERE id=?", (pid,)
                ).fetchone()
                pt = str(pt_row[0] if pt_row else 'goods').lower()
                if pt in ('goods', 'hang_hoa'):
                    inv_acc = '156'
            except sqlite3.Error:
                pass

        try:
            _, cost_used, _fifo = apply_cost_outbound(
                cur, pid, float(qty), float(cost),
                ref_type='material_alloc', ref_id=aid, conn=conn,
            )
            cost = _money(cost_used)
            amt = qty * cost
        except ValueError:
            raise
        except Exception:
            pass

        if has_wh and wh:
            cur.execute(
                """
                INSERT INTO stock_moves
                    (product_id, date, type, ref_id, ref_document, ref_type, quantity, note, type1, cost_price, warehouse_code)
                VALUES (?, ?, 'export', ?, ?, 'material_alloc', ?, ?, 'Phân bổ NVL', ?, ?)
                """,
                (pid, when, aid, doc_no, float(-qty), notes or f'PB {doc_no}', float(cost), wh),
            )
        else:
            cur.execute(
                """
                INSERT INTO stock_moves
                    (product_id, date, type, ref_id, ref_document, ref_type, quantity, note, type1, cost_price)
                VALUES (?, ?, 'export', ?, ?, 'material_alloc', ?, ?, 'Phân bổ NVL', ?)
                """,
                (pid, when, aid, doc_no, float(-qty), notes or f'PB {doc_no}', float(cost)),
            )
        try:
            sync_inventory_quantity_from_moves(cur, pid)
        except Exception:
            pass
        cur.execute(
            """
            INSERT INTO sme_material_allocation_lines
                (alloc_id, product_id, quantity, unit_cost, amount, inv_account)
            VALUES (?,?,?,?,?,?)
            """,
            (aid, pid, float(qty), float(cost), float(amt), inv_acc),
        )
        by_inv[inv_acc] = by_inv.get(inv_acc, Decimal('0')) + amt
        total += amt
        n += 1

    if n == 0 or total <= 0:
        conn.execute('DELETE FROM sme_material_allocations WHERE id = ?', (aid,))
        raise ValueError('Không có dòng phân bổ hợp lệ')

    lines = [{
        'sequence': 1, 'account_code': exp,
        'debit': float(total), 'credit': 0,
        'description': f'Phân bổ NVL {doc_no}',
    }]
    seq = 2
    for acc, amt in by_inv.items():
        lines.append({
            'sequence': seq, 'account_code': acc,
            'debit': 0, 'credit': float(amt),
            'description': f'Xuất {acc} {doc_no}',
        })
        seq += 1

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type='PBNVL',
        document_no=doc_no,
        document_id=aid,
        business_type='PHAN_BO_NVL',
        description=f'Phân bổ NVL/CCDC {doc_no}',
        created_by=created_by,
        branch_code=branch,
        lines=lines,
    )
    conn.execute(
        'UPDATE sme_material_allocations SET journal_entry_id = ? WHERE id = ?',
        (entry['id'], aid),
    )
    if commit:
        sqlite_commit(conn, label='inventory_ops')
    row = conn.execute('SELECT * FROM sme_material_allocations WHERE id = ?', (aid,)).fetchone()
    d = dict(row)
    d['lines'] = [dict(x) for x in conn.execute(
        'SELECT * FROM sme_material_allocation_lines WHERE alloc_id = ?', (aid,)
    ).fetchall()]
    return d


def get_material_allocation(conn: sqlite3.Connection, alloc_id: int) -> dict[str, Any] | None:
    ensure_sme_inventory_ops_schema(conn, commit=False)
    row = conn.execute('SELECT * FROM sme_material_allocations WHERE id = ?', (alloc_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['lines'] = [dict(x) for x in conn.execute(
        """
        SELECT l.*, p.name AS product_name, p.unit
        FROM sme_material_allocation_lines l
        LEFT JOIN products p ON p.id = l.product_id
        WHERE l.alloc_id = ? ORDER BY l.id
        """,
        (alloc_id,),
    ).fetchall()]
    return d


def list_material_allocations(
    conn: sqlite3.Connection,
    *,
    branch_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_sme_inventory_ops_schema(conn, commit=False)
    from Services.sme.branches import DEFAULT_BRANCH_CODE

    sql = "SELECT * FROM sme_material_allocations WHERE status != 'void'"
    params: list[Any] = []
    code = (branch_code or '').strip().upper()
    if code and code != 'ALL':
        if code == DEFAULT_BRANCH_CODE:
            sql += " AND (branch_code IS NULL OR branch_code = '' OR branch_code = ?)"
        else:
            sql += ' AND branch_code = ?'
        params.append(code)
    sql += ' ORDER BY alloc_date DESC, id DESC LIMIT ?'
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def void_material_allocation(
    conn: sqlite3.Connection,
    alloc_id: int,
    *,
    reason: str = 'Hủy phân bổ NVL',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_material_allocations', alloc_id, label='Bảng phân bổ NVL')
    doc = get_material_allocation(conn, alloc_id)
    if not doc:
        raise ValueError('Không tìm thấy bảng phân bổ')
    if doc.get('status') == 'void':
        raise ValueError('Đã hủy')
    if doc.get('journal_entry_id'):
        reverse_journal_entry(
            conn, int(doc['journal_entry_id']),
            created_by=created_by, reason=reason,
        )
    when = _now()
    moves = conn.execute(
        "SELECT * FROM stock_moves WHERE ref_type = 'material_alloc' AND ref_id = ?",
        (alloc_id,),
    ).fetchall()
    cur = conn.cursor()
    for m in moves:
        md = dict(m)
        qty = float(md.get('quantity') or 0)
        pid = int(md.get('product_id') or 0)
        cost = float(md.get('cost_price') or 0)
        if pid <= 0 or qty == 0:
            continue
        try:
            if qty < 0:
                apply_cost_inbound(
                    cur, pid, -qty, (-qty) * cost,
                    unit_cost=cost,
                    source_type='MATERIAL_ALLOC_VOID',
                    source_id=alloc_id,
                    received_at=when,
                    lot_no=f'H07-{doc["doc_no"]}-{pid}',
                    note=reason,
                    conn=conn,
                )
            else:
                apply_cost_outbound(
                    cur, pid, qty, cost,
                    ref_type='material_alloc_void', ref_id=alloc_id, conn=conn,
                )
        except Exception:
            pass
        conn.execute(
            """
            INSERT INTO stock_moves
                (product_id, date, type, ref_id, ref_document, ref_type, quantity, note, type1, cost_price)
            VALUES (?, ?, ?, ?, ?, 'material_alloc_void', ?, ?, 'Hủy PB NVL', ?)
            """,
            (
                pid, when,
                'import' if qty < 0 else 'export',
                alloc_id, doc['doc_no'],
                float(-qty), reason, cost,
            ),
        )
        try:
            sync_inventory_quantity_from_moves(cur, pid)
        except Exception:
            pass
    conn.execute(
        "UPDATE sme_material_allocations SET status = 'void', notes = COALESCE(notes,'') || ? WHERE id = ?",
        (f' | VOID: {reason}', alloc_id),
    )
    if commit:
        sqlite_commit(conn, label='inventory_ops')
    return get_material_allocation(conn, alloc_id)


def purchase_listing(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
    branch_code: str | None = None,
) -> dict[str, Any]:
    """Bảng kê mua hàng 06-VT — tổng hợp từ import + supplier_invoice SME."""
    ensure_sme_inventory_ops_schema(conn, commit=False)
    df, dt = date_from[:10], date_to[:10]
    lines = []
    total = Decimal('0.00')
    try:
        # Bảng import: supplier_id + total_value (không có supplier_name / total_amount)
        sql = """
            SELECT i.id, i.import_no, i.date AS import_date,
                   COALESCE(s.name, '') AS supplier_name,
                   COALESCE(i.total_value, 0) AS amount,
                   0 AS vat
            FROM import i
            LEFT JOIN suppliers s ON s.id = i.supplier_id
            WHERE date(COALESCE(i.date, '')) >= date(?)
              AND date(COALESCE(i.date, '')) <= date(?)
              AND COALESCE(i.doc_type,'') NOT IN ('landed_cost')
        """
        params: list[Any] = [df, dt]
        code = (branch_code or '').strip().upper()
        if code and code != 'ALL':
            from Services.sme.branches import DEFAULT_BRANCH_CODE
            imp_cols = {r[1] for r in conn.execute('PRAGMA table_info(import)').fetchall()}
            if 'warehouse_code' in imp_cols:
                if code == DEFAULT_BRANCH_CODE:
                    sql += """
                        AND (
                            i.warehouse_code IS NULL OR i.warehouse_code = ''
                            OR i.warehouse_code IN (
                                SELECT code FROM warehouses
                                WHERE branch_code IS NULL OR branch_code = '' OR branch_code = ?
                            )
                        )
                    """
                    params.append(DEFAULT_BRANCH_CODE)
                else:
                    sql += """
                        AND i.warehouse_code IN (
                            SELECT code FROM warehouses WHERE branch_code = ?
                        )
                    """
                    params.append(code)
        sql += ' ORDER BY i.date, i.id'
        rows = conn.execute(sql, params).fetchall()
        for r in rows:
            d = dict(r)
            amt = _money(d.get('amount'))
            total += amt
            lines.append({
                'source': 'import',
                'doc_no': d.get('import_no'),
                'doc_date': str(d.get('import_date') or '')[:10],
                'supplier': d.get('supplier_name'),
                'amount': _f(amt),
                'vat': _f(d.get('vat')),
            })
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

    return {
        'form_code': '06-VT',
        'date_from': df,
        'date_to': dt,
        'lines': lines,
        'total': _f(total),
        'count': len(lines),
    }


def void_stock_count(
    conn: sqlite3.Connection,
    count_id: int,
    *,
    reason: str = 'Hủy kiểm kê kho',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    from Services.sme.branch_filter import assert_row_in_branch
    assert_row_in_branch(conn, 'sme_stock_counts', count_id, label='Biên bản kiểm kê kho')
    doc = get_stock_count(conn, count_id)
    if not doc:
        raise ValueError('Không tìm thấy biên bản kiểm kê')
    if doc['status'] == 'void':
        raise ValueError('Đã hủy')
    if doc.get('journal_entry_id'):
        reverse_journal_entry(
            conn, int(doc['journal_entry_id']),
            created_by=created_by, reason=reason,
        )
    # Đảo stock_moves kiểm kê
    when = _now()
    moves = conn.execute(
        "SELECT * FROM stock_moves WHERE ref_type = 'inventory_check' AND ref_document = ?",
        (doc['doc_no'],),
    ).fetchall()
    for m in moves:
        md = dict(m)
        qty = float(md.get('quantity') or 0)
        if qty == 0:
            continue
        conn.execute(
            """
            INSERT INTO stock_moves
                (product_id, date, type, ref_id, ref_document, ref_type, quantity, note, type1, cost_price)
            VALUES (?, ?, ?, ?, ?, 'inventory_check', ?, ?, 'Hủy kiểm kê', ?)
            """,
            (
                md['product_id'], when,
                'export' if qty > 0 else 'import',
                count_id, doc['doc_no'], -qty, reason, md.get('cost_price') or 0,
            ),
        )
        try:
            sync_inventory_quantity_from_moves(conn.cursor(), int(md['product_id']))
        except Exception:
            pass
    conn.execute(
        "UPDATE sme_stock_counts SET status = 'void', notes = ? WHERE id = ?",
        ((doc.get('notes') or '') + f' | {reason}', count_id),
    )
    if commit:
        sqlite_commit(conn, label='inventory_ops')
    return get_stock_count(conn, count_id)
