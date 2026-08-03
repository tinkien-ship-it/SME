# -*- coding: utf-8 -*-
"""Sổ tờ khai hải quan điện tử — lớp nghiệp vụ kế toán (không thay phần mềm đầu cuối VNACCS).

Thực tế kỹ thuật / pháp lý:
  - Kết nối trực tiếp VNACCS/VCIS chỉ dành cho phần mềm đã được TCHQ chứng nhận
    (CCS4EO miễn phí, ECUS, FPT.VNACCS, CDS, …) + chữ ký số + Terminal ID.
  - Module này tối ưu SME: lưu tờ khai, nhập JSON/CSV từ phần mềm HQ, gắn phiếu XK/NK,
    đồng bộ số tờ khai / tỷ giá / thuế vào chứng từ kế toán.

Cổng khai HQ chính thức (tham chiếu): https://e-declaration.customs.gov.vn:8443/
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

MONEY_Q = Decimal('0.01')
FX_Q = Decimal('0.0001')

_SCHEMA_VERSION = '2026-08-03hq1'
_schema_ready: dict[str, str] = {}

OFFICIAL_EDECLARATION_URL = 'https://e-declaration.customs.gov.vn:8443/'
DIRECTION_EXPORT = 'export'
DIRECTION_IMPORT = 'import'
STATUSES = ('draft', 'declared', 'cleared', 'cancelled')


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _money(val) -> Decimal:
    if val is None or val == '':
        return Decimal('0.00')
    return Decimal(str(val)).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _fx(val) -> Decimal:
    rate = Decimal(str(val or 1))
    if rate <= 0:
        return Decimal('1')
    return rate.quantize(FX_Q, rounding=ROUND_HALF_UP)


def _db_key(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute('PRAGMA database_list').fetchone()
        if row:
            path = row[2] if not isinstance(row, sqlite3.Row) else row['file']
            if path:
                return str(path)
    except sqlite3.Error:
        pass
    return f'conn:{id(conn)}'


def ensure_customs_declaration_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    key = _db_key(conn)
    if _schema_ready.get(key) == _SCHEMA_VERSION:
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_customs_declarations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            declaration_no TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'export',
            declaration_date TEXT,
            customs_office TEXT,
            partner_name TEXT,
            partner_country TEXT,
            currency TEXT DEFAULT 'USD',
            customs_fx_rate REAL DEFAULT 1,
            amount_fc REAL DEFAULT 0,
            amount_vnd REAL DEFAULT 0,
            export_tax_fc REAL DEFAULT 0,
            export_tax_vnd REAL DEFAULT 0,
            import_duty_vnd REAL DEFAULT 0,
            import_vat_vnd REAL DEFAULT 0,
            special_consume_tax_vnd REAL DEFAULT 0,
            incoterms TEXT,
            bl_no TEXT,
            invoice_no TEXT,
            hs_codes TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            source TEXT DEFAULT 'manual',
            sale_id INTEGER,
            import_id INTEGER,
            note TEXT,
            raw_json TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(declaration_no, direction)
        )
        """
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_customs_decl_date '
        'ON sme_customs_declarations(declaration_date)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_customs_decl_sale '
        'ON sme_customs_declarations(sale_id)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_sme_customs_decl_import '
        'ON sme_customs_declarations(import_id)'
    )
    if commit:
        try:
            conn.commit()
        except sqlite3.Error:
            pass
    _schema_ready[key] = _SCHEMA_VERSION


def _row_to_dict(row, columns: list[str] | None = None) -> dict[str, Any]:
    if row is None:
        return {}
    if hasattr(row, 'keys'):
        d = dict(row)
    elif columns:
        d = dict(zip(columns, row))
    else:
        return {}
    if d.get('hs_codes') and isinstance(d['hs_codes'], str):
        try:
            d['hs_codes'] = json.loads(d['hs_codes'])
        except Exception:
            d['hs_codes'] = [x.strip() for x in d['hs_codes'].split(',') if x.strip()]
    if d.get('raw_json') and isinstance(d['raw_json'], str):
        try:
            d['raw_payload'] = json.loads(d['raw_json'])
        except Exception:
            d['raw_payload'] = None
    return d


def _fetchall_dicts(conn: sqlite3.Connection, sql: str, params=()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [_row_to_dict(r, cols) for r in cur.fetchall()]


def _fetchone_dict(conn: sqlite3.Connection, sql: str, params=()) -> dict[str, Any] | None:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    return _row_to_dict(row, cols) if row else None


def normalize_declaration_payload(data: dict[str, Any] | None) -> dict[str, Any]:
    """Chuẩn hóa payload trung gian SME (từ form / JSON import)."""
    raw = dict(data or {})
    direction = (raw.get('direction') or DIRECTION_EXPORT).strip().lower()
    if direction not in (DIRECTION_EXPORT, DIRECTION_IMPORT):
        direction = DIRECTION_EXPORT
    status = (raw.get('status') or 'draft').strip().lower()
    if status not in STATUSES:
        status = 'draft'
    decl_no = (raw.get('declaration_no') or raw.get('customs_decl_no') or '').strip()
    if not decl_no:
        raise ValueError('Thiếu số tờ khai hải quan')

    fx = _fx(raw.get('customs_fx_rate') or raw.get('exchange_rate') or 1)
    amount_fc = _money(raw.get('amount_fc') or raw.get('total_fc') or 0)
    amount_vnd = _money(raw.get('amount_vnd') or 0)
    if amount_vnd <= 0 and amount_fc > 0:
        amount_vnd = _money(amount_fc * fx)

    export_tax_fc = _money(raw.get('export_tax_fc') or 0)
    export_tax_vnd = _money(raw.get('export_tax_vnd') or 0)
    if export_tax_vnd <= 0 and export_tax_fc > 0:
        export_tax_vnd = _money(export_tax_fc * fx)

    hs = raw.get('hs_codes') or raw.get('hs_code') or []
    if isinstance(hs, str):
        hs_list = [x.strip() for x in hs.replace(';', ',').split(',') if x.strip()]
    elif isinstance(hs, list):
        hs_list = [str(x).strip() for x in hs if str(x).strip()]
    else:
        hs_list = []

    return {
        'declaration_no': decl_no,
        'direction': direction,
        'declaration_date': (raw.get('declaration_date') or raw.get('date') or '')[:10] or None,
        'customs_office': (raw.get('customs_office') or raw.get('office') or '').strip() or None,
        'partner_name': (raw.get('partner_name') or raw.get('customer_name') or raw.get('supplier_name') or '').strip() or None,
        'partner_country': (raw.get('partner_country') or raw.get('country') or '').strip() or None,
        'currency': (raw.get('currency') or 'USD').strip().upper() or 'USD',
        'customs_fx_rate': float(fx),
        'amount_fc': float(amount_fc),
        'amount_vnd': float(amount_vnd),
        'export_tax_fc': float(export_tax_fc),
        'export_tax_vnd': float(export_tax_vnd),
        'import_duty_vnd': float(_money(raw.get('import_duty_vnd') or raw.get('import_tax_vnd') or 0)),
        'import_vat_vnd': float(_money(raw.get('import_vat_vnd') or raw.get('vat_import_vnd') or 0)),
        'special_consume_tax_vnd': float(_money(raw.get('special_consume_tax_vnd') or 0)),
        'incoterms': (raw.get('incoterms') or '').strip().upper() or None,
        'bl_no': (raw.get('bl_no') or raw.get('bill_of_lading') or '').strip() or None,
        'invoice_no': (raw.get('invoice_no') or '').strip() or None,
        'hs_codes': hs_list,
        'status': status,
        'source': (raw.get('source') or 'manual').strip() or 'manual',
        'sale_id': int(raw['sale_id']) if raw.get('sale_id') not in (None, '', 0, '0') else None,
        'import_id': int(raw['import_id']) if raw.get('import_id') not in (None, '', 0, '0') else None,
        'note': (raw.get('note') or '').strip() or None,
        'raw_json': json.dumps(raw.get('raw_payload') or raw, ensure_ascii=False),
    }


def upsert_declaration(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    *,
    created_by: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    ensure_customs_declaration_schema(conn, commit=False)
    payload = normalize_declaration_payload(data)
    hs_json = json.dumps(payload['hs_codes'], ensure_ascii=False)
    now = _now()
    existing = conn.execute(
        """
        SELECT id FROM sme_customs_declarations
        WHERE declaration_no = ? AND direction = ?
        """,
        (payload['declaration_no'], payload['direction']),
    ).fetchone()
    fields = (
        'declaration_date', 'customs_office', 'partner_name', 'partner_country',
        'currency', 'customs_fx_rate', 'amount_fc', 'amount_vnd',
        'export_tax_fc', 'export_tax_vnd', 'import_duty_vnd', 'import_vat_vnd',
        'special_consume_tax_vnd', 'incoterms', 'bl_no', 'invoice_no',
        'hs_codes', 'status', 'source', 'sale_id', 'import_id', 'note', 'raw_json',
    )
    if existing:
        decl_id = int(existing[0] if not hasattr(existing, 'keys') else existing['id'])
        sets = ', '.join(f'{f} = ?' for f in fields) + ', updated_at = ?'
        vals = [hs_json if f == 'hs_codes' else payload[f] for f in fields]
        vals.append(now)
        vals.append(decl_id)
        conn.execute(
            f'UPDATE sme_customs_declarations SET {sets} WHERE id = ?',
            vals,
        )
    else:
        cols = ['declaration_no', 'direction', *fields, 'created_by', 'created_at', 'updated_at']
        vals = [
            payload['declaration_no'], payload['direction'],
            *[hs_json if f == 'hs_codes' else payload[f] for f in fields],
            created_by, now, now,
        ]
        conn.execute(
            f"""
            INSERT INTO sme_customs_declarations ({', '.join(cols)})
            VALUES ({', '.join('?' for _ in cols)})
            """,
            vals,
        )
        decl_id = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])

    if commit:
        conn.commit()
    return get_declaration(conn, decl_id)


def get_declaration(conn: sqlite3.Connection, decl_id: int) -> dict[str, Any] | None:
    ensure_customs_declaration_schema(conn, commit=False)
    return _fetchone_dict(
        conn, 'SELECT * FROM sme_customs_declarations WHERE id = ?', (int(decl_id),),
    )


def list_declarations(
    conn: sqlite3.Connection,
    *,
    direction: str | None = None,
    q: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    ensure_customs_declaration_schema(conn, commit=False)
    sql = 'SELECT * FROM sme_customs_declarations WHERE 1=1'
    params: list[Any] = []
    if direction in (DIRECTION_EXPORT, DIRECTION_IMPORT):
        sql += ' AND direction = ?'
        params.append(direction)
    if q:
        sql += (
            ' AND (declaration_no LIKE ? OR partner_name LIKE ? OR bl_no LIKE ? '
            'OR invoice_no LIKE ?)'
        )
        like = f'%{q.strip()}%'
        params.extend([like, like, like, like])
    sql += ' ORDER BY COALESCE(declaration_date, created_at) DESC, id DESC LIMIT ?'
    params.append(max(1, min(int(limit or 100), 500)))
    return _fetchall_dicts(conn, sql, params)


def import_declarations_bulk(
    conn: sqlite3.Connection,
    items: list[dict[str, Any]],
    *,
    created_by: str | None = None,
    default_source: str = 'json_import',
    commit: bool = True,
) -> dict[str, Any]:
    """Nhập hàng loạt từ JSON array (export từ ECUS/CCS4EO sau khi map cột)."""
    ensure_customs_declaration_schema(conn, commit=False)
    ok, errors = [], []
    for i, item in enumerate(items or []):
        try:
            payload = dict(item or {})
            payload.setdefault('source', default_source)
            doc = upsert_declaration(conn, payload, created_by=created_by, commit=False)
            ok.append(doc.get('id'))
        except Exception as exc:
            errors.append({'index': i, 'error': str(exc)})
    if commit:
        conn.commit()
    return {'success': True, 'imported': len(ok), 'ids': ok, 'errors': errors}


def apply_declaration_to_export_sale(
    conn: sqlite3.Connection,
    *,
    declaration_id: int,
    sale_id: int,
    commit: bool = True,
) -> dict[str, Any]:
    """Đồng bộ số TK / TG tờ khai / thuế XK / B/L / Incoterms vào phiếu bán XK."""
    ensure_customs_declaration_schema(conn, commit=False)
    from Services.sme.export_payment import ensure_export_sale_schema
    ensure_export_sale_schema(conn, commit=False)

    decl = get_declaration(conn, declaration_id)
    if not decl:
        raise ValueError('Không tìm thấy tờ khai')
    if decl.get('direction') != DIRECTION_EXPORT:
        raise ValueError('Tờ khai không phải chiều xuất khẩu')

    sale = conn.execute('SELECT id, sale_type FROM sale WHERE id = ?', (int(sale_id),)).fetchone()
    if not sale:
        raise ValueError('Không tìm thấy phiếu bán')

    cols = {r[1] for r in conn.execute('PRAGMA table_info(sale)').fetchall()}
    updates = []
    vals: list[Any] = []
    mapping = [
        ('customs_decl_no', decl.get('declaration_no')),
        ('customs_fx_rate', decl.get('customs_fx_rate')),
        ('export_tax_fc', decl.get('export_tax_fc')),
        ('export_tax_vnd', decl.get('export_tax_vnd')),
        ('incoterms', decl.get('incoterms')),
        ('bl_no', decl.get('bl_no')),
        ('currency', decl.get('currency')),
    ]
    if decl.get('declaration_date') and 'risk_transfer_date' in cols:
        mapping.append(('risk_transfer_date', decl.get('declaration_date')))
    if decl.get('partner_name') and 'customer_name' in cols:
        # Chỉ điền nếu trống
        cur = conn.execute('SELECT customer_name FROM sale WHERE id = ?', (int(sale_id),)).fetchone()
        name = (cur[0] if cur and not hasattr(cur, 'keys') else (cur['customer_name'] if cur else '')) or ''
        if not str(name).strip():
            mapping.append(('customer_name', decl.get('partner_name')))

    for col, val in mapping:
        if col in cols and val not in (None, ''):
            updates.append(f'{col} = ?')
            vals.append(val)
    if not updates:
        raise ValueError('Không có trường nào để đồng bộ')
    vals.append(int(sale_id))
    conn.execute(f"UPDATE sale SET {', '.join(updates)} WHERE id = ?", vals)
    conn.execute(
        """
        UPDATE sme_customs_declarations
        SET sale_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (int(sale_id), _now(), int(declaration_id)),
    )
    if commit:
        conn.commit()
    return {
        'success': True,
        'sale_id': int(sale_id),
        'declaration_id': int(declaration_id),
        'declaration_no': decl.get('declaration_no'),
        'updated_fields': [u.split('=')[0].strip() for u in updates],
    }


def apply_declaration_to_import(
    conn: sqlite3.Connection,
    *,
    declaration_id: int,
    import_id: int,
    commit: bool = True,
) -> dict[str, Any]:
    """Đồng bộ số TK / ngày / TG tờ khai vào phiếu nhập (nếu có cột)."""
    ensure_customs_declaration_schema(conn, commit=False)
    decl = get_declaration(conn, declaration_id)
    if not decl:
        raise ValueError('Không tìm thấy tờ khai')
    if decl.get('direction') != DIRECTION_IMPORT:
        raise ValueError('Tờ khai không phải chiều nhập khẩu')

    exists = conn.execute('SELECT id FROM import WHERE id = ?', (int(import_id),)).fetchone()
    if not exists:
        raise ValueError('Không tìm thấy phiếu nhập')

    cols = {r[1] for r in conn.execute('PRAGMA table_info(import)').fetchall()}
    updates, vals = [], []
    mapping = [
        ('customs_decl_no', decl.get('declaration_no')),
        ('customs_decl_date', decl.get('declaration_date')),
        ('customs_fx_rate', decl.get('customs_fx_rate')),
    ]
    for col, val in mapping:
        if col in cols and val not in (None, ''):
            updates.append(f'{col} = ?')
            vals.append(val)
    if updates:
        vals.append(int(import_id))
        conn.execute(f"UPDATE import SET {', '.join(updates)} WHERE id = ?", vals)
    conn.execute(
        """
        UPDATE sme_customs_declarations
        SET import_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (int(import_id), _now(), int(declaration_id)),
    )
    if commit:
        conn.commit()
    return {
        'success': True,
        'import_id': int(import_id),
        'declaration_id': int(declaration_id),
        'declaration_no': decl.get('declaration_no'),
        'updated_fields': [u.split('=')[0].strip() for u in updates],
    }


def parse_import_text(raw: str) -> list[dict[str, Any]]:
    """Nhận JSON object/array hoặc CSV đơn giản (header dòng 1)."""
    text = (raw or '').strip()
    if not text:
        return []
    if text.startswith('{') or text.startswith('['):
        data = json.loads(text)
        if isinstance(data, dict):
            if isinstance(data.get('items'), list):
                return data['items']
            return [data]
        if isinstance(data, list):
            return data
        raise ValueError('JSON không hợp lệ')

    # CSV
    import csv
    from io import StringIO
    reader = csv.DictReader(StringIO(text))
    return [dict(row) for row in reader]
