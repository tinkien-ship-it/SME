"""Nhập khẩu theo giai đoạn: hàng đi đường (151) / TSCĐ mua sắm (2411)
→ nộp thuế HQ → nhập kho thực tế / đưa vào sử dụng.

G1 IN_TRANSIT: Nợ 151 (HH/NVL/CCDC) hoặc Nợ 2411 (TSCĐ) + thuế vốn hóa /
               Có 331 / Có 333* / Nợ 13312 / Có 33312 — không tăng tồn.
G2 TAX_PAID:   Nợ 3333/3332/33312 / Có 112 — nút nộp thuế.
G3 RECEIVED:   Nợ 156|152|153 / Có 151; Nợ 2112 / Có 2411 — tăng tồn / ghi TSCĐ.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    post_journal_entry,
    resolve_postable_account,
)

MONEY_Q = Decimal('0.01')

STAGE_IN_TRANSIT = 'IN_TRANSIT'
STAGE_TAX_PAID = 'TAX_PAID'
STAGE_RECEIVED = 'RECEIVED'
STAGE_DOMESTIC = 'RECEIVED'  # mua trong nước = nhập kho ngay

DOC_TYPE_TRANSIT = 'HMDD'   # Hàng mua đang đi đường
DOC_TYPE_TAX = 'NTHQ'       # Nộp thuế hải quan
DOC_TYPE_RECEIVE = 'NKTT'   # Nhập kho thực tế


def _money(val) -> Decimal:
    if val is None:
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}


def ensure_import_transit_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    extras = [
        ('receipt_stage', "TEXT DEFAULT 'RECEIVED'"),
        ('customs_decl_no', 'TEXT'),
        ('customs_decl_date', 'TEXT'),
        ('customs_fx_rate', 'REAL'),
        ('tax_payment_voucher_id', 'INTEGER'),
        ('tax_payment_journal_id', 'INTEGER'),
        ('receive_journal_id', 'INTEGER'),
        ('transit_posted_at', 'TEXT'),
        ('received_at', 'TEXT'),
    ]
    names = _cols(conn, 'import')
    for col, decl in extras:
        if col not in names:
            try:
                conn.execute(f'ALTER TABLE "import" ADD COLUMN {col} {decl}')
            except sqlite3.OperationalError:
                pass
    if commit:
        conn.commit()


def default_receipt_stage(import_type: str) -> str:
    t = (import_type or 'DOMESTIC').strip().upper()
    return STAGE_IN_TRANSIT if t == 'IMPORT' else STAGE_RECEIVED


def is_in_transit_stage(stage: str | None, import_type: str | None = None) -> bool:
    s = (stage or '').strip().upper()
    if s in (STAGE_IN_TRANSIT, STAGE_TAX_PAID):
        return True
    if s == STAGE_RECEIVED:
        return False
    # Legacy: chưa có cột / trống → IMPORT coi như cần tách nếu đang migrate
    return False


def final_inventory_account(line_type: str) -> str:
    lt = (line_type or 'goods').strip().lower()
    if lt in ('materials', 'raw_materials', 'nvl'):
        return '152'
    if lt in ('fixed_asset', 'fa'):
        return '2112'
    if lt in ('tools', 'ccdc'):
        return '153'
    return '156'


def transit_clearing_account(line_type: str) -> str:
    """TK trung gian khi hàng/TSCĐ đang đi đường (G1)."""
    lt = (line_type or 'goods').strip().lower()
    if lt in ('fixed_asset', 'fa'):
        return '2411'
    return '151'


def pay_customs_taxes(
    conn: sqlite3.Connection,
    import_id: int,
    *,
    pay_date: str | None = None,
    payment_method: str = 'bank',
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """G2: Nộp thuế HQ — Nợ 3333/3332/33312 / Có 1121|1111."""
    from Services.sme.branches import resolve_posting_branch
    from Services.sme.vouchers import create_payment

    ensure_sme_journal_ready(conn, commit=False)
    ensure_import_transit_schema(conn, commit=False)

    imp = conn.execute('SELECT * FROM "import" WHERE id = ?', (import_id,)).fetchone()
    if not imp:
        raise ValueError('Không tìm thấy phiếu nhập')
    imp = dict(imp)
    itype = str(imp.get('import_type') or 'DOMESTIC').upper()
    if itype != 'IMPORT':
        raise ValueError('Chỉ áp dụng nộp thuế HQ cho hàng nhập khẩu')

    stage = str(imp.get('receipt_stage') or STAGE_IN_TRANSIT).upper()
    if stage == STAGE_RECEIVED and imp.get('tax_payment_journal_id'):
        raise ValueError('Đã nộp thuế và nhập kho — không nộp lại')
    if imp.get('tax_payment_journal_id'):
        raise ValueError('Đã nộp thuế HQ cho phiếu này')

    nk = _money(imp.get('import_tax_amount'))
    excise = _money(imp.get('excise_tax_amount'))
    # VAT nhập khẩu: tổng tax trên dòng chi tiết
    vat = Decimal('0.00')
    detail_cols = _cols(conn, 'import_details')
    if 'tax' in detail_cols:
        row = conn.execute(
            'SELECT COALESCE(SUM(tax), 0) FROM import_details WHERE import_id = ?',
            (import_id,),
        ).fetchone()
        vat = _money(row[0] if row else 0)

    if nk <= 0 and excise <= 0 and vat <= 0:
        raise ValueError('Không có thuế HQ phải nộp trên phiếu này')

    date_s = str(pay_date or imp.get('date') or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày nộp thuế')

    debit_lines = []
    if nk > 0:
        debit_lines.append({
            'account_code': '3333',
            'amount': float(nk),
            'description': f'Nộp thuế NK — PN {imp.get("import_no") or import_id}',
        })
    if excise > 0:
        debit_lines.append({
            'account_code': '3332',
            'amount': float(excise),
            'description': f'Nộp thuế TTĐB — PN {imp.get("import_no") or import_id}',
        })
    if vat > 0:
        debit_lines.append({
            'account_code': '33312',
            'amount': float(vat),
            'description': f'Nộp thuế GTGT hàng NK — PN {imp.get("import_no") or import_id}',
        })

    total = sum((_money(x['amount']) for x in debit_lines), Decimal('0.00'))
    party = 'Cơ quan Hải quan / Kho bạc Nhà nước'
    voucher = create_payment(
        conn,
        voucher_date=date_s,
        party_name=party,
        amount=float(total),
        payment_method=payment_method or 'bank',
        debit_account=debit_lines[0]['account_code'],
        debit_lines=debit_lines,
        reason=f'Nộp thuế HQ theo tờ khai — {imp.get("import_no") or ("#" + str(import_id))}',
        reference_document=imp.get('customs_decl_no') or imp.get('bill_no') or '',
        source_type='import_customs_tax',
        source_id=import_id,
        import_id=import_id,
        purpose='customs_tax',
        created_by=created_by,
        branch_code=resolve_posting_branch(conn, None),
        commit=False,
    )

    new_stage = STAGE_TAX_PAID if stage != STAGE_RECEIVED else stage
    cols = _cols(conn, 'import')
    sets = ["tax_payment_voucher_id = ?", "tax_payment_journal_id = ?"]
    vals: list[Any] = [voucher['id'], voucher.get('journal_entry_id')]
    if 'receipt_stage' in cols:
        sets.append('receipt_stage = ?')
        vals.append(new_stage)
    vals.append(import_id)
    conn.execute(f'UPDATE "import" SET {", ".join(sets)} WHERE id = ?', vals)

    if commit:
        conn.commit()
    return {
        'import_id': import_id,
        'receipt_stage': new_stage,
        'voucher': voucher,
        'amounts': {
            'import_tax': float(nk),
            'excise_tax': float(excise),
            'vat': float(vat),
            'total': float(total),
        },
    }


def receive_import_to_warehouse(
    conn: sqlite3.Connection,
    import_id: int,
    *,
    receive_date: str | None = None,
    created_by: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """G3: Chuyển 151 → 156/152… và tăng tồn kho vật lý."""
    from Services.import_line_helpers import tracks_retail_inventory
    from Services.inventory_stock_helpers import (
        apply_wac_inbound,
        sync_inventory_quantity_from_moves,
    )
    from Services.sme.branch_filter import warehouse_branch_or_session
    from Services.sme.branches import resolve_posting_branch

    ensure_sme_journal_ready(conn, commit=False)
    ensure_import_transit_schema(conn, commit=False)

    imp = conn.execute('SELECT * FROM "import" WHERE id = ?', (import_id,)).fetchone()
    if not imp:
        raise ValueError('Không tìm thấy phiếu nhập')
    imp = dict(imp)
    itype = str(imp.get('import_type') or 'DOMESTIC').upper()
    if itype != 'IMPORT':
        raise ValueError('Phiếu trong nước đã nhập kho khi lưu — không dùng bước này')

    stage = str(imp.get('receipt_stage') or '').upper()
    if stage == STAGE_RECEIVED or imp.get('receive_journal_id'):
        raise ValueError('Phiếu đã nhập kho thực tế')
    if stage not in (STAGE_IN_TRANSIT, STAGE_TAX_PAID, ''):
        raise ValueError(f'Trạng thái không hợp lệ để nhập kho: {stage or "(trống)"}')

    date_s = str(receive_date or imp.get('date') or '')[:10]
    if not date_s:
        raise ValueError('Thiếu ngày nhập kho')

    detail_cols = _cols(conn, 'import_details')
    select = [
        'd.id', 'd.product_id', 'd.qty', 'd.buyprice',
        'COALESCE(d.subtotal, d.qty * d.buyprice) AS subtotal',
        'COALESCE(d.discount, 0) AS discount',
        'COALESCE(d.tax, 0) AS tax',
    ]
    select.append(
        "COALESCE(d.line_type, 'goods') AS line_type"
        if 'line_type' in detail_cols else "'goods' AS line_type"
    )
    select.append(
        "COALESCE(d.warehouse_code, 'KHO_001') AS warehouse_code"
        if 'warehouse_code' in detail_cols else "'KHO_001' AS warehouse_code"
    )
    select.append(
        'COALESCE(d.import_tax_amount, 0) AS import_tax_amount'
        if 'import_tax_amount' in detail_cols else '0 AS import_tax_amount'
    )
    select.append(
        'COALESCE(d.excise_tax_amount, 0) AS excise_tax_amount'
        if 'excise_tax_amount' in detail_cols else '0 AS excise_tax_amount'
    )
    select.append(
        "COALESCE(d.product_name, '') AS product_name"
        if 'product_name' in detail_cols else "'' AS product_name"
    )
    select.append(
        "COALESCE(d.unit, '') AS unit" if 'unit' in detail_cols else "'' AS unit"
    )

    details = conn.execute(
        f"""
        SELECT {', '.join(select)}, COALESCE(p.name, '') AS pname
        FROM import_details d
        LEFT JOIN products p ON p.id = d.product_id
        WHERE d.import_id = ?
        """,
        (import_id,),
    ).fetchall()

    lines: list[dict[str, Any]] = []
    seq = 1
    clearing_totals: dict[str, Decimal] = {}  # 151 / 2411 → số tất toán
    stock_jobs: list[dict[str, Any]] = []
    fa_jobs: list[dict[str, Any]] = []

    for row in details:
        r = dict(row)
        lt = str(r.get('line_type') or 'goods').lower()
        if lt == 'service':
            continue
        net = _money(r.get('subtotal')) - _money(r.get('discount'))
        inv_amt = net + _money(r.get('import_tax_amount')) + _money(r.get('excise_tax_amount'))
        if inv_amt <= 0:
            continue
        final_acct = resolve_postable_account(conn, final_inventory_account(lt))
        clearing_code = transit_clearing_account(lt)
        name = (r.get('pname') or r.get('product_name') or f"SP#{r.get('product_id')}").strip()
        desc = (
            f'Đưa TSCĐ vào sử dụng: {name}' if lt in ('fixed_asset', 'fa')
            else f'Nhập kho thực tế: {name}'
        )
        lines.append({
            'sequence': seq,
            'account_code': final_acct,
            'debit': float(inv_amt),
            'credit': 0,
            'product_id': r.get('product_id'),
            'warehouse_code': r.get('warehouse_code'),
            'description': desc,
        })
        seq += 1
        clearing_totals[clearing_code] = clearing_totals.get(clearing_code, Decimal('0.00')) + inv_amt
        if tracks_retail_inventory(lt) and r.get('product_id'):
            qty = Decimal(str(r.get('qty') or 0))
            if qty > 0:
                stock_jobs.append({
                    'product_id': int(r['product_id']),
                    'qty': float(qty),
                    'amount': float(inv_amt),
                    'cost_per': float(_money(inv_amt / qty)),
                    'warehouse_code': r.get('warehouse_code') or 'KHO_001',
                    'name': name,
                    'unit': r.get('unit') or '',
                })
        if lt in ('fixed_asset', 'fa') and r.get('product_id'):
            fa_jobs.append({
                'detail_id': r.get('id'),
                'product_id': int(r['product_id']),
                'name': name,
                'qty': float(r.get('qty') or 1),
                'amount': float(inv_amt),
                'warehouse_code': r.get('warehouse_code') or 'KHO_001',
                'buyprice': float(r.get('buyprice') or 0),
                'tax': float(r.get('tax') or 0),
            })

    transit_total = sum(clearing_totals.values(), Decimal('0.00'))
    if transit_total <= 0 or not lines:
        raise ValueError('Không có giá trị hàng/TSCĐ đang đi đường để tất toán')

    for clearing_code, amt in clearing_totals.items():
        if amt <= 0:
            continue
        clearing_acct = resolve_postable_account(conn, clearing_code)
        label = '2411 mua sắm TSCĐ' if str(clearing_code).startswith('241') else '151 hàng đi đường'
        lines.append({
            'sequence': seq,
            'account_code': clearing_acct,
            'debit': 0,
            'credit': float(amt),
            'description': f'Tất toán {label} — {imp.get("import_no") or import_id}',
        })
        seq += 1

    wh0 = stock_jobs[0]['warehouse_code'] if stock_jobs else (imp.get('warehouse_code') or 'KHO_001')
    branch = warehouse_branch_or_session(conn, wh0) or resolve_posting_branch(conn, None)
    import_no = imp.get('import_no') or f'#{import_id}'

    entry = post_journal_entry(
        conn,
        posting_date=date_s,
        document_date=date_s,
        document_type=DOC_TYPE_RECEIVE,
        document_no=str(import_no),
        document_id=import_id,
        business_type='NHAP_KHO_THUC_TE',
        description=f'Nhập kho thực tế từ hàng đi đường — {import_no}',
        reference_document=imp.get('customs_decl_no') or imp.get('bill_no'),
        created_by=created_by,
        branch_code=branch,
        lines=lines,
    )

    # Tăng tồn kho vật lý
    sm_cols = _cols(conn, 'stock_moves') if conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='stock_moves'"
    ).fetchone() else set()

    for job in stock_jobs:
        pid = job['product_id']
        cur = conn.cursor()
        apply_wac_inbound(cur, pid, job['qty'], job['amount'])
        if sm_cols:
            _insert_stock_move_simple(
                conn, sm_cols,
                product_id=pid,
                import_date=date_s,
                import_id=import_id,
                qty=job['qty'],
                cost_per=job['cost_per'],
                note=f"Nhập kho thực tế — {import_no} ({job['warehouse_code']})",
                warehouse_code=job['warehouse_code'],
                unit=job.get('unit') or '',
            )
        try:
            conn.execute(
                """
                INSERT INTO inventory_transactions
                (product_id, type, type1, quantity, cost_price, reference_id, reference_type, note, created_at)
                VALUES (?, 'import', 'Nhập', ?, ?, ?, 'import_receive', ?, ?)
                """,
                (
                    pid, job['qty'], job['cost_per'], import_id,
                    f"Nhập kho thực tế - PN#{import_no}", date_s,
                ),
            )
        except sqlite3.Error:
            pass
        sync_inventory_quantity_from_moves(cur, pid)

    # Đăng ký TSCĐ khi tất toán 2411 → 2112
    if fa_jobs:
        try:
            from Services.fixed_assets_helpers import (
                ensure_fixed_assets_schema,
                register_fixed_asset_from_import,
            )
            ensure_fixed_assets_schema(conn)
            cur = conn.cursor()
            for job in fa_jobs:
                pcode = ''
                try:
                    prow = conn.execute(
                        'SELECT product_code FROM products WHERE id = ?',
                        (job['product_id'],),
                    ).fetchone()
                    if prow:
                        pcode = prow[0] if not hasattr(prow, 'keys') else (prow['product_code'] or '')
                except sqlite3.Error:
                    pass
                register_fixed_asset_from_import(
                    cur,
                    import_id=import_id,
                    import_detail_id=job.get('detail_id'),
                    product_id=job['product_id'],
                    product_code=pcode or '',
                    product_name=job['name'],
                    import_no=str(import_no),
                    import_date=date_s,
                    warehouse_code=job['warehouse_code'],
                    qty=job['qty'],
                    buyprice=job['buyprice'],
                    tax_amount=job.get('tax') or 0,
                    discount_amount=0,
                    line_total=job['amount'],
                    subtotal=job['amount'],
                    capitalized_cost=job['amount'],
                    ngay_bat_dau_su_dung=date_s,
                )
        except Exception:
            pass

    cols = _cols(conn, 'import')
    sets = ['receive_journal_id = ?', "receipt_stage = ?"]
    vals: list[Any] = [entry['id'], STAGE_RECEIVED]
    if 'received_at' in cols:
        sets.append('received_at = ?')
        vals.append(_now())
    vals.append(import_id)
    conn.execute(f'UPDATE "import" SET {", ".join(sets)} WHERE id = ?', vals)

    if commit:
        conn.commit()
    return {
        'import_id': import_id,
        'receipt_stage': STAGE_RECEIVED,
        'journal_entry_id': entry['id'],
        'transit_amount': float(transit_total),
        'clearing': {k: float(v) for k, v in clearing_totals.items()},
        'stock_lines': len(stock_jobs),
        'fixed_assets': len(fa_jobs),
    }


def _insert_stock_move_simple(
    conn: sqlite3.Connection,
    sm_cols: set[str],
    *,
    product_id: int,
    import_date: str,
    import_id: int,
    qty: float,
    cost_per: float,
    note: str,
    warehouse_code: str,
    unit: str = '',
) -> None:
    fields = ['product_id', 'type', 'quantity', 'unit_cost', 'note', 'created_at']
    values: list[Any] = [product_id, 'import', float(qty), float(cost_per), note, import_date]
    optional = {
        'reference_id': import_id,
        'reference_type': 'import',
        'warehouse_code': warehouse_code,
        'unit': unit or None,
        'date': import_date,
    }
    for col, val in optional.items():
        if col in sm_cols and val is not None:
            fields.append(col)
            values.append(val)
    # Common alternate column names
    if 'cost_price' in sm_cols and 'unit_cost' not in sm_cols:
        if 'unit_cost' in fields:
            idx = fields.index('unit_cost')
            fields[idx] = 'cost_price'
    placeholders = ','.join('?' * len(fields))
    try:
        conn.execute(
            f"INSERT INTO stock_moves ({', '.join(fields)}) VALUES ({placeholders})",
            values,
        )
    except sqlite3.Error:
        # Minimal fallback
        try:
            conn.execute(
                """
                INSERT INTO stock_moves (product_id, type, quantity, note, created_at)
                VALUES (?, 'import', ?, ?, ?)
                """,
                (product_id, float(qty), note, import_date),
            )
        except sqlite3.Error:
            pass
