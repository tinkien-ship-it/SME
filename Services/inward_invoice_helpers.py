"""Helper chuẩn hóa hóa đơn NCC và hỗ trợ hạch toán dịch vụ HKD."""
import json
import logging
import sqlite3
import xml.etree.ElementTree as ET


def _safe_float(val, default=0.0):
    if val is None or val == '':
        return default
    try:
        return float(str(val).replace(',', '').replace('%', '').strip() or 0)
    except (TypeError, ValueError):
        return default


def _parse_vat_rate(raw):
    s = str(raw or '0').replace('%', '').strip()
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _line_from_xml_hh(hh):
    vat_rate = _parse_vat_rate(hh.findtext('TSuat'))
    tl_ck = _parse_vat_rate(hh.findtext('TLCKhau'))
    st_ck = _safe_float(hh.findtext('STCKhau') or hh.findtext('TienCKhau'))
    return {
        'THHDVu': (hh.findtext('THHDVu') or '').strip(),
        'DVTinh': (hh.findtext('DVTinh') or '').strip(),
        'SLuong': _safe_float(hh.findtext('SLuong'), 0),
        'DGia': _safe_float(hh.findtext('DGia'), 0),
        'TSuat': str(int(vat_rate)) if vat_rate == int(vat_rate) else str(vat_rate),
        'TyLeCK': tl_ck,
        'STCKhau': st_ck,
    }


def _line_from_dict(row):
    if not isinstance(row, dict):
        return None
    name = (
        row.get('THHDVu') or row.get('Ten') or row.get('ten')
        or row.get('TenHang') or row.get('tenHang') or row.get('TenHHDVu') or ''
    ).strip()
    if not name:
        return None
    vat = row.get('TSuat') or row.get('ThueSuat') or row.get('tsuat') or row.get('VAT') or 0
    disc_pct = row.get('TyLeCK') or row.get('TLCKhau') or row.get('tlCKhau') or 0
    disc_amt = row.get('STCKhau') or row.get('TienCKhau') or row.get('stCKhau') or 0
    return {
        'THHDVu': name,
        'DVTinh': (row.get('DVTinh') or row.get('DVT') or row.get('donViTinh') or '').strip(),
        'SLuong': _safe_float(row.get('SLuong') or row.get('SoLuong') or row.get('sluong') or 1, 1),
        'DGia': _safe_float(row.get('DGia') or row.get('DonGia') or row.get('dgia') or 0),
        'TSuat': str(int(_parse_vat_rate(vat))) if _parse_vat_rate(vat) == int(_parse_vat_rate(vat)) else str(_parse_vat_rate(vat)),
        'TyLeCK': _safe_float(disc_pct),
        'STCKhau': _safe_float(disc_amt),
    }


def _extract_lines_from_payload(data):
    lines = []
    if not isinstance(data, dict):
        return lines

    candidates = []
    for key in ('DSHHDVu', 'HHDVu', 'HangHoa', 'ChiTiet', 'chiTiet', 'Details', 'details'):
        val = data.get(key)
        if val:
            candidates.append(val)

    for cand in candidates:
        if isinstance(cand, list):
            for row in cand:
                mapped = _line_from_dict(row)
                if mapped:
                    lines.append(mapped)
        elif isinstance(cand, dict):
            mapped = _line_from_dict(cand)
            if mapped:
                lines.append(mapped)

    return lines


def normalize_supplier_invoice_payload(raw_data):
    """
    Chuẩn hóa xml_data (JSON Mắt Bão hoặc XML) → schema thống nhất cho UI import.
    """
    if not raw_data:
        raise ValueError('Dữ liệu hóa đơn rỗng')

    text = raw_data.strip() if isinstance(raw_data, str) else raw_data

    if isinstance(text, str) and text.startswith('<'):
        from Services.invoice_xml_parse import (
            decode_xml_bytes,
            find_invoice_payload_node,
            strip_xml_namespaces,
        )
        root = ET.fromstring(decode_xml_bytes(text))
        strip_xml_namespaces(root)
        ndhdon = find_invoice_payload_node(root)
        if ndhdon is None:
            raise ValueError('Không tìm thấy NDHDon/DLHDon trong XML')
        ttchung = ndhdon.find('.//TTChung')
        if ttchung is None:
            ttchung = root.find('.//TTChung')
        nban = ndhdon.find('.//NBan')
        if nban is None:
            nban = root.find('.//NBan')
        data = {
            'SHDon': (ttchung.findtext('SHDon') if ttchung is not None else '') or '',
            'NLap': (ttchung.findtext('NLap') if ttchung is not None else '') or '',
            'KHHDon': (ttchung.findtext('KHHDon') if ttchung is not None else '') or '',
            'NBanTen': (nban.findtext('Ten') if nban is not None else '') or '',
            'NBanMST': (nban.findtext('MST') if nban is not None else '') or '',
            'NBanDChi': (nban.findtext('DChi') if nban is not None else '') or '',
            'DSHHDVu': [],
        }
        hh_nodes = list(ndhdon.findall('.//HHDVu')) or list(root.findall('.//HHDVu'))
        for hh in hh_nodes:
            try:
                line = _line_from_xml_hh(hh)
                if line.get('THHDVu'):
                    data['DSHHDVu'].append(line)
            except Exception:
                continue
    elif isinstance(text, str):
        data = json.loads(text)
    else:
        data = text

    if not isinstance(data, dict):
        raise ValueError('Định dạng hóa đơn không hợp lệ')

    lines = data.get('DSHHDVu') or []
    if not lines:
        lines = _extract_lines_from_payload(data)

    normalized_lines = []
    for row in lines:
        if isinstance(row, dict):
            mapped = _line_from_dict(row)
            if mapped:
                normalized_lines.append(mapped)

    header = {
        'SHDon': str(data.get('SHDon') or data.get('shDon') or data.get('SoHDon') or '').strip(),
        'NLap': str(data.get('NLap') or data.get('nLap') or data.get('NgayLap') or '').strip(),
        'KHHDon': str(data.get('KHHDon') or data.get('khHDon') or data.get('Serial') or '').strip(),
        'NBanTen': str(data.get('NBanTen') or data.get('NBan_Ten') or data.get('nBanTen') or '').strip(),
        'NBanMST': str(data.get('NBanMST') or data.get('NBan_MST') or data.get('nBanMST') or '').strip(),
        'NBanDChi': str(data.get('NBanDChi') or data.get('NBan_DChi') or data.get('nBanDChi') or '').strip(),
        'DSHHDVu': normalized_lines,
    }
    return header


def next_pc_voucher_no(cursor):
    cursor.execute(
        "SELECT voucher_no FROM phieu_chi WHERE voucher_no LIKE 'PC%' ORDER BY id DESC LIMIT 1"
    )
    last = cursor.fetchone()
    if last and last[0] and str(last[0]).startswith('PC'):
        try:
            return f"PC{int(str(last[0])[2:]) + 1:06d}"
        except ValueError:
            pass
    return 'PC000001'


def ensure_import_service_schema(conn):
    """Đảm bảo bảng import/import_details hỗ trợ hạch toán dịch vụ trên DB hiện tại."""
    migrate_import_for_service(conn)
    migrate_import_details_for_service(conn)


def import_details_allows_null_product_id(conn):
    c = conn.cursor()
    c.execute('PRAGMA table_info(import_details)')
    for row in c.fetchall():
        if row[1] == 'product_id':
            return row[3] == 0
    return False


def migrate_import_details_for_service(conn):
    """Thêm cột dịch vụ; cho phép product_id NULL (rebuild bảng nếu cần)."""
    c = conn.cursor()
    c.execute('PRAGMA table_info(import_details)')
    cols = {row[1]: row for row in c.fetchall()}
    if not cols:
        return

    new_cols = {
        'product_name': 'TEXT',
        'product_code': 'TEXT',
        'unit': 'TEXT',
        'line_type': "TEXT DEFAULT 'goods'",
        'payment_amt': 'REAL DEFAULT 0',
    }
    for col, col_type in new_cols.items():
        if col not in cols:
            try:
                c.execute(f'ALTER TABLE import_details ADD COLUMN {col} {col_type}')
            except sqlite3.OperationalError as exc:
                logging.warning('migrate import_details.%s: %s', col, exc)

    c.execute('PRAGMA table_info(import_details)')
    cols = {row[1]: row for row in c.fetchall()}
    existing_cols = set(cols.keys())
    pid_notnull = cols.get('product_id', (None, None, None, 0))[3] == 1
    if not pid_notnull:
        return

    fk_enabled = True
    try:
        c.execute('PRAGMA foreign_keys')
        fk_enabled = c.fetchone()[0]
    except Exception:
        pass

    def _payment_amt_expr():
        parts = []
        if 'payment_amt' in existing_cols:
            parts.append('payment_amt')
        if {'subtotal', 'discount', 'tax'} <= existing_cols:
            parts.append('(COALESCE(subtotal, 0) - COALESCE(discount, 0) + COALESCE(tax, 0))')
        elif 'subtotal' in existing_cols:
            parts.append('subtotal')
        if not parts:
            return '0'
        return f"COALESCE({', '.join(parts)}, 0)"

    def _subtotal_expr():
        if 'subtotal' in existing_cols:
            return 'COALESCE(subtotal, 0)'
        if {'qty', 'buyprice'} <= existing_cols:
            return 'COALESCE(qty, 0) * COALESCE(buyprice, 0)'
        return '0'

    column_map = [
        ('id', 'id' if 'id' in existing_cols else 'NULL'),
        ('import_id', 'import_id'),
        ('product_id', 'product_id' if 'product_id' in existing_cols else 'NULL'),
        ('qty', 'COALESCE(qty, 0)' if 'qty' in existing_cols else '0'),
        ('buyprice', 'COALESCE(buyprice, 0)' if 'buyprice' in existing_cols else '0'),
        ('cost_price', 'COALESCE(cost_price, 0)' if 'cost_price' in existing_cols else '0'),
        ('discount', 'COALESCE(discount, 0)' if 'discount' in existing_cols else '0'),
        ('tax', 'COALESCE(tax, 0)' if 'tax' in existing_cols else '0'),
        ('subtotal', _subtotal_expr()),
        ('payment_amt', _payment_amt_expr()),
        ('unit_type', 'COALESCE(unit_type, 0)' if 'unit_type' in existing_cols else '0'),
        ('tax_pct', 'COALESCE(tax_pct, 0)' if 'tax_pct' in existing_cols else '0'),
        ('discount_pct', 'COALESCE(discount_pct, 0)' if 'discount_pct' in existing_cols else '0'),
        ('product_name', 'product_name' if 'product_name' in existing_cols else 'NULL'),
        ('product_code', 'product_code' if 'product_code' in existing_cols else 'NULL'),
        ('unit', 'unit' if 'unit' in existing_cols else 'NULL'),
        ('line_type', "COALESCE(line_type, 'goods')" if 'line_type' in existing_cols else "'goods'"),
    ]

    insert_cols = [name for name, _ in column_map]
    select_exprs = [expr for _, expr in column_map]

    c.execute('PRAGMA foreign_keys=OFF')
    try:
        c.execute('DROP TABLE IF EXISTS import_details_new')
        c.execute("""
            CREATE TABLE import_details_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER NOT NULL,
                product_id INTEGER,
                qty REAL NOT NULL,
                buyprice REAL DEFAULT 0,
                cost_price REAL DEFAULT 0,
                discount REAL DEFAULT 0,
                tax REAL DEFAULT 0,
                subtotal REAL NOT NULL DEFAULT 0,
                payment_amt REAL NOT NULL DEFAULT 0,
                unit_type INTEGER DEFAULT 0,
                tax_pct REAL DEFAULT 0,
                discount_pct REAL DEFAULT 0,
                product_name TEXT,
                product_code TEXT,
                unit TEXT,
                line_type TEXT DEFAULT 'goods',
                FOREIGN KEY(import_id) REFERENCES import(id) ON DELETE CASCADE,
                FOREIGN KEY(product_id) REFERENCES products(id)
            )
        """)
        c.execute(f"""
            INSERT INTO import_details_new ({', '.join(insert_cols)})
            SELECT {', '.join(select_exprs)} FROM import_details
        """)
        c.execute('DROP TABLE import_details')
        c.execute('ALTER TABLE import_details_new RENAME TO import_details')
    finally:
        c.execute(f'PRAGMA foreign_keys={"ON" if fk_enabled else "OFF"}')


def migrate_import_for_service(conn):
    c = conn.cursor()
    extras = [
        ('import', 'from_invoice_id', 'INTEGER'),
        ('import', 'doc_type', "TEXT DEFAULT 'stock'"),
        ('import', 'import_type', "TEXT DEFAULT 'DOMESTIC'"),
        ('import', 'currency', "TEXT DEFAULT 'VND'"),
        ('import', 'exchange_rate', 'REAL DEFAULT 1'),
        ('import', 'import_tax_amount', 'REAL DEFAULT 0'),
        ('import', 'excise_tax_amount', 'REAL DEFAULT 0'),
        ('import', 'env_tax_amount', 'REAL DEFAULT 0'),
        ('import', 'payment_method', 'TEXT'),
        ('import', 'warehouse_code', 'TEXT'),
        ('import', 'receipt_stage', "TEXT DEFAULT 'RECEIVED'"),
        ('import', 'customs_decl_no', 'TEXT'),
        ('import', 'customs_decl_date', 'TEXT'),
        ('import', 'customs_fx_rate', 'REAL'),
        ('import', 'tax_payment_voucher_id', 'INTEGER'),
        ('import', 'tax_payment_journal_id', 'INTEGER'),
        ('import', 'receive_journal_id', 'INTEGER'),
        ('import', 'transit_posted_at', 'TEXT'),
        ('import', 'received_at', 'TEXT'),
        # Dòng nhập khẩu: thuế NK / TTĐB / BVMT phân bổ vào giá vốn HH·NVL·TSCĐ·CCDC
        ('import_details', 'import_tax_pct', 'REAL DEFAULT 0'),
        ('import_details', 'import_tax_amount', 'REAL DEFAULT 0'),
        ('import_details', 'excise_tax_pct', 'REAL DEFAULT 0'),
        ('import_details', 'excise_tax_amount', 'REAL DEFAULT 0'),
        ('import_details', 'env_tax_pct', 'REAL DEFAULT 0'),
        ('import_details', 'env_tax_amount', 'REAL DEFAULT 0'),
    ]
    for table, col, col_type in extras:
        c.execute(f'PRAGMA table_info({table})')
        names = {r[1] for r in c.fetchall()}
        if col not in names:
            try:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {col} {col_type}')
            except sqlite3.OperationalError as exc:
                logging.warning('migrate %s.%s: %s', table, col, exc)


def reset_supplier_invoice_after_import_removed(cursor, *, from_invoice_id=None, bill_no=None, tax_code=None):
    """Đưa hóa đơn đầu vào về trạng thái chờ xử lý sau khi hủy phiếu nhập."""
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='supplier_invoice'"
    )
    if not cursor.fetchone():
        return

    if from_invoice_id:
        cursor.execute(
            "UPDATE supplier_invoice SET status = 'new' WHERE id = ?",
            (int(from_invoice_id),),
        )
        return

    bill = (bill_no or '').strip()
    if not bill or bill.lower() in ('none', 'nan'):
        return

    tax = (tax_code or '').strip()
    if tax:
        cursor.execute(
            """
            UPDATE supplier_invoice SET status = 'new'
            WHERE TRIM(COALESCE(invoice_no, '')) = ?
              AND TRIM(COALESCE(seller_tax_code, '')) = ?
            """,
            (bill, tax),
        )
    else:
        cursor.execute(
            """
            UPDATE supplier_invoice SET status = 'new'
            WHERE TRIM(COALESCE(invoice_no, '')) = ?
            """,
            (bill,),
        )


def create_manual_supplier_invoice(conn, payload: dict) -> dict:
    """
    Tạo HĐ mua nhập tay (CP vận chuyển nước ngoài / không có HĐĐT).
    Lưu xml_data dạng JSON tương thích normalize + phân bổ landed cost.
    """
    from datetime import datetime
    from decimal import Decimal, ROUND_HALF_UP

    def money(v):
        try:
            return Decimal(str(v or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except Exception:
            return Decimal('0.00')

    invoice_no = str(payload.get('invoice_no') or '').strip()
    if not invoice_no:
        raise ValueError('Số chứng từ / số hóa đơn là bắt buộc')

    seller_name = str(payload.get('seller_name') or '').strip()
    if not seller_name:
        raise ValueError('Tên nhà cung cấp / đơn vị cung cấp là bắt buộc')

    invoice_date = str(payload.get('invoice_date') or '').strip()[:10]
    if not invoice_date:
        invoice_date = datetime.now().strftime('%Y-%m-%d')

    serial = str(payload.get('serial') or 'NGOAI').strip() or 'NGOAI'
    seller_tax = str(payload.get('seller_tax_code') or '').strip()
    seller_address = str(payload.get('seller_address') or '').strip()
    description = (
        str(payload.get('description') or '').strip()
        or 'Chi phí vận chuyển quốc tế'
    )
    unit = str(payload.get('unit') or 'Lần').strip() or 'Lần'
    qty = money(payload.get('qty') or 1)
    if qty <= 0:
        qty = Decimal('1.00')

    amount_net = money(payload.get('amount_net') or payload.get('amount') or 0)
    tax_pct = money(payload.get('tax_pct') or 0)
    tax_amount = money(payload.get('tax_amount'))
    if tax_amount <= 0 and tax_pct > 0 and amount_net > 0:
        tax_amount = money(amount_net * tax_pct / Decimal('100'))
    if amount_net <= 0:
        total_in = money(payload.get('total') or 0)
        if total_in <= 0:
            raise ValueError('Số tiền chưa thuế (hoặc tổng thanh toán) phải > 0')
        amount_net = total_in - tax_amount if total_in > tax_amount else total_in
    total = money(payload.get('total'))
    if total <= 0:
        total = amount_net + tax_amount
    if tax_pct <= 0 and amount_net > 0 and tax_amount > 0:
        tax_pct = money(tax_amount / amount_net * 100)

    unit_price = money(amount_net / qty) if qty else amount_net
    currency = str(payload.get('currency') or 'VND').strip().upper() or 'VND'
    try:
        fx_rate = float(payload.get('exchange_rate') or 1) or 1.0
    except (TypeError, ValueError):
        fx_rate = 1.0
    foreign_amount = payload.get('foreign_amount')
    cost_category = str(payload.get('cost_category') or 'FREIGHT').strip().upper() or 'FREIGHT'

    # Trùng chứng từ
    c = conn.cursor()
    if seller_tax:
        c.execute(
            """
            SELECT id FROM supplier_invoice
            WHERE TRIM(COALESCE(invoice_no, '')) = ?
              AND TRIM(COALESCE(seller_tax_code, '')) = ?
            LIMIT 1
            """,
            (invoice_no, seller_tax),
        )
    else:
        c.execute(
            """
            SELECT id FROM supplier_invoice
            WHERE TRIM(COALESCE(invoice_no, '')) = ?
              AND TRIM(COALESCE(seller_name, '')) = ?
              AND TRIM(COALESCE(serial, '')) = ?
            LIMIT 1
            """,
            (invoice_no, seller_name, serial),
        )
    dup = c.fetchone()
    if dup:
        raise ValueError(f'Đã tồn tại chứng từ số {invoice_no} (id #{dup[0]})')

    payload_json = {
        'SourceType': 'manual',
        'IsForeign': True,
        'CostCategory': cost_category,
        'Currency': currency,
        'ExchangeRate': fx_rate,
        'ForeignAmount': foreign_amount,
        'SHDon': invoice_no,
        'NLap': invoice_date,
        'KHHDon': serial,
        'NBanTen': seller_name,
        'NBanMST': seller_tax,
        'NBanDChi': seller_address,
        'TgTCThue': float(amount_net),
        'TgTThue': float(tax_amount),
        'TgTTTBSo': float(total),
        'DSHHDVu': [{
            'THHDVu': description,
            'DVTinh': unit,
            'SLuong': float(qty),
            'DGia': float(unit_price),
            'ThTien': float(amount_net),
            'TSuat': str(float(tax_pct)),
            'TyLeCK': 0,
        }],
    }
    xml_data = json.dumps(payload_json, ensure_ascii=False)
    entry_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    c.execute('PRAGMA table_info(supplier_invoice)')
    cols = {row[1] for row in c.fetchall()}

    fields = [
        'invoice_date', 'serial', 'invoice_no', 'seller_name', 'seller_tax_code',
        'amount', 'discount_percent', 'discount_amount', 'tax_percent', 'tax_amount',
        'total', 'status', 'xml_data', 'date',
    ]
    values = [
        invoice_date, serial, invoice_no, seller_name, seller_tax,
        float(amount_net), 0.0, 0.0, float(tax_pct), float(tax_amount),
        float(total), 'new', xml_data, invoice_date,
    ]
    if 'pdf_url' in cols:
        fields.append('pdf_url')
        values.append(None)
    if 'address' in cols:
        fields.append('address')
        values.append(seller_address or None)

    placeholders = ', '.join(['?'] * len(values))
    c.execute(
        f"INSERT INTO supplier_invoice ({', '.join(fields)}) VALUES ({placeholders})",
        values,
    )
    invoice_id = int(c.lastrowid)
    return {
        'id': invoice_id,
        'invoice_no': invoice_no,
        'invoice_date': invoice_date,
        'seller_name': seller_name,
        'amount_net': float(amount_net),
        'tax_amount': float(tax_amount),
        'total': float(total),
        'source_type': 'manual',
        'cost_category': cost_category,
    }


_INWARD_LIST_SCHEMA_READY = 'inward_list_schema_v1'


def ensure_inward_list_schema(conn) -> None:
    from db_utils import sqlite_is_ready, sqlite_mark_ready

    if sqlite_is_ready(conn, _INWARD_LIST_SCHEMA_READY):
        return
    ensure_import_service_schema(conn)
    sqlite_mark_ready(conn, _INWARD_LIST_SCHEMA_READY)


def _inward_invoice_day_sql() -> str:
    return (
        "LEFT(COALESCE(NULLIF(BTRIM(CAST(si.invoice_date AS text)), ''), "
        "CAST(si.date AS text)), 10)"
    )


def _inward_branch_link_sets(conn, branch_code: str | None):
    if not branch_code or str(branch_code).strip().upper() in ('', 'ALL'):
        return None
    from Services.sme.branches import import_branch_filter_sql

    bf, bp = import_branch_filter_sql(conn, branch_code, alias='i')
    ids: set[int] = set()
    bills: set[str] = set()
    for row in conn.execute(
        f"""
        SELECT i.from_invoice_id, TRIM(COALESCE(i.bill_no, '')) AS bill_no
        FROM import i
        WHERE 1=1 {bf}
        """,
        bp,
    ).fetchall():
        r = dict(row) if hasattr(row, 'keys') else {
            'from_invoice_id': row[0], 'bill_no': row[1],
        }
        fid = r.get('from_invoice_id')
        if fid:
            ids.add(int(fid))
        bill = (r.get('bill_no') or '').strip()
        if bill:
            bills.add(bill)
    return ids, bills


def _inward_visible_for_branch(inv: dict, link_sets) -> bool:
    if link_sets is None:
        return True
    if not inv.get('has_import') and not inv.get('has_accounted'):
        return True
    ids, bills = link_sets
    inv_id = int(inv.get('id') or 0)
    if inv_id in ids:
        return True
    inv_no = (inv.get('invoice_no') or '').strip()
    return inv_no in bills


def list_inward_invoices(
    conn,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    keyword: str | None = None,
    status: str | None = None,
    branch_code: str | None = None,
    limit: int = 120,
) -> list[dict]:
    """Danh sách HĐ mua — JOIN thay EXISTS, không SELECT xml_data (nhanh trên PG)."""
    import logging

    ensure_inward_list_schema(conn)
    conn.row_factory = sqlite3.Row
    inv_day = _inward_invoice_day_sql()
    limit = min(max(int(limit or 120), 1), 500)

    query = f"""
        SELECT
            si.id, si.invoice_no, si.serial, si.invoice_date, si.date,
            si.seller_name, si.seller_tax_code, si.amount, si.tax_amount,
            si.total, si.status, si.pdf_url, si.address,
            CASE WHEN sb.bill_no IS NOT NULL THEN 1 ELSE 0 END AS has_import,
            CASE WHEN si.status = 'accounted' OR sv.invoice_id IS NOT NULL THEN 1 ELSE 0 END AS has_accounted
        FROM supplier_invoice si
        LEFT JOIN (
            SELECT DISTINCT TRIM(COALESCE(bill_no, '')) AS bill_no
            FROM import
            WHERE TRIM(COALESCE(bill_no, '')) != ''
              AND COALESCE(doc_type, 'stock') NOT IN ('service', 'landed_cost')
        ) sb ON sb.bill_no = TRIM(COALESCE(si.invoice_no, ''))
        LEFT JOIN (
            SELECT DISTINCT from_invoice_id AS invoice_id
            FROM import
            WHERE from_invoice_id IS NOT NULL
            UNION
            SELECT DISTINCT si2.id AS invoice_id
            FROM import i
            INNER JOIN supplier_invoice si2
                ON TRIM(COALESCE(i.bill_no, '')) = TRIM(COALESCE(si2.invoice_no, ''))
            WHERE TRIM(COALESCE(i.bill_no, '')) != ''
              AND COALESCE(i.doc_type, '') IN ('service', 'landed_cost')
        ) sv ON sv.invoice_id = si.id
        WHERE 1=1
    """
    params: list = []

    kw = (keyword or '').strip()
    if kw:
        like = f'%{kw}%'
        query += ' AND (si.seller_name LIKE ? OR si.seller_tax_code LIKE ? OR si.invoice_no LIKE ?)'
        params.extend([like, like, like])

    if from_date:
        query += f' AND {inv_day} >= date(?)'
        params.append(from_date[:10])
    if to_date:
        query += f' AND {inv_day} <= date(?)'
        params.append(to_date[:10])

    query += f' ORDER BY {inv_day} DESC, si.id DESC LIMIT ?'
    params.append(limit)

    rows = conn.execute(query, params).fetchall()

    landed_ids: set[int] = set()
    try:
        from db_utils import sqlite_table_exists
        if sqlite_table_exists(conn, 'sme_landed_cost_docs'):
            for r in conn.execute(
                """
                SELECT cost_invoice_id FROM sme_landed_cost_docs
                WHERE COALESCE(status, 'posted') = 'posted'
                """
            ).fetchall():
                if r[0] is not None:
                    landed_ids.add(int(r[0]))
    except Exception:
        logging.exception('inward landed_cost ids')

    link_sets = _inward_branch_link_sets(conn, branch_code)
    result: list[dict] = []
    for row in rows:
        inv = dict(row)
        inv['has_hach_toan'] = bool(inv.get('has_accounted'))
        inv_id = int(inv.get('id') or 0)
        inv['has_landed_cost'] = 1 if inv_id in landed_ids else 0
        serial_up = str(inv.get('serial') or '').strip().upper()
        inv['is_manual'] = 1 if serial_up in ('MANUAL', 'NGOẠI', 'NGOAI', 'FOREIGN') else 0
        inv['is_foreign'] = 1 if serial_up in ('NGOẠI', 'NGOAI', 'FOREIGN', 'MANUAL') else 0
        inv['cost_category'] = None
        if not _inward_visible_for_branch(inv, link_sets):
            continue
        result.append(inv)

    st = (status or '').strip().lower()
    if st == 'imported':
        result = [x for x in result if x.get('has_import') == 1]
    elif st == 'hach_toan':
        result = [x for x in result if x.get('has_accounted') == 1]
    elif st == 'new':
        result = [
            x for x in result
            if x.get('has_import') == 0 and x.get('has_accounted') == 0
        ]
    return result
