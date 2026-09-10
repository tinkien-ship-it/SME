# -*- coding: utf-8 -*-
"""Sổ công nợ phải thu (131) / phải trả (331) — Kế toán SME.

Nguồn sự thật: sổ nhật ký (sme_journal_lines) theo tài khoản 131* / 331*,
gom theo tên khách hàng / nhà cung cấp. Phải trả: Tổng nợ = phát sinh Có TK 331
(không gồm thuế 133). Khoản còn mở để chi lấy từ import (giữ nút thanh toán UI).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from Services.sme.journal_engine import ensure_sme_journal_ready


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _row_dict(row) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    if hasattr(row, 'keys'):
        return dict(row)
    return {}


def _norm_name(raw) -> str:
    return ' '.join(str(raw or '').strip().split())


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    """Lấy cột của bảng trên cả SQLite và PostgreSQL, theo schema tenant hiện tại."""
    try:
        from db_utils import is_postgres
        if is_postgres():
            rows = conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = ?
                ORDER BY ordinal_position
                """,
                (str(table),),
            ).fetchall()
            cols = set()
            for r in rows:
                d = _row_dict(r)
                value = d.get('column_name') if d else (r[0] if r else None)
                if value:
                    cols.add(str(value))
            return cols
        return {str(r[1]) for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    except Exception:
        return set()

def _customer_name_by_id(conn: sqlite3.Connection, pid: int) -> str | None:
    # Chọn cột theo schema thật của từng tenant; không giả định có company_name.
    cols = _table_cols(conn, 'customers')
    if not cols or 'name' not in cols:
        return None
    fields = ['name']
    if 'company_name' in cols:
        fields.append('company_name')
    try:
        row = conn.execute(
            f"SELECT {', '.join(fields)} FROM customers WHERE id = ?", (pid,)
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    d = _row_dict(row)
    return _norm_name(d.get('company_name') or d.get('name')) or None

def _supplier_name_by_id(conn: sqlite3.Connection, pid: int) -> str | None:
    # PostgreSQL sẽ lỗi UndefinedColumn nếu tenant không có company_name.
    # Vì vậy chỉ SELECT các cột thực sự tồn tại.
    cols = _table_cols(conn, 'suppliers')
    if not cols or 'name' not in cols:
        return None
    fields = ['name']
    if 'company_name' in cols:
        fields.append('company_name')
    try:
        row = conn.execute(
            f"SELECT {', '.join(fields)} FROM suppliers WHERE id = ?", (pid,)
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    d = _row_dict(row)
    return _norm_name(d.get('company_name') or d.get('name')) or None

def _sale_party_name(conn: sqlite3.Connection, sale_id: int | None) -> str | None:
    if not sale_id:
        return None
    cols = _table_cols(conn, 'sale')
    if not cols:
        return None
    fields = []
    if 'company_name' in cols:
        fields.append('company_name')
    if 'customer_name' in cols:
        fields.append('customer_name')
    if not fields:
        return None
    try:
        row = conn.execute(
            f"SELECT {', '.join(fields)} FROM sale WHERE id = ?", (int(sale_id),)
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    d = dict(row)
    return _norm_name(d.get('company_name') or d.get('customer_name')) or None


def _import_supplier_name(conn: sqlite3.Connection, import_id: int | None) -> str | None:
    if not import_id:
        return None
    try:
        row = conn.execute(
            """
            SELECT s.name
            FROM import i
            JOIN suppliers s ON s.id = i.supplier_id
            WHERE i.id = ?
            """,
            (int(import_id),),
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return _norm_name(row[0] if not isinstance(row, sqlite3.Row) else row['name']) or None


def _resolve_ar_name(conn: sqlite3.Connection, jl: dict, je: dict) -> str | None:
    pt = str(jl.get('partner_type') or '').strip().lower()
    pid = jl.get('partner_id')
    if pid and pt in ('customer', 'khach_hang', 'ar', ''):
        name = _customer_name_by_id(conn, int(pid))
        if name:
            return name
    doc_type = str(je.get('document_type') or '').upper()
    doc_id = je.get('document_id')
    if doc_id and (
        doc_type.startswith('SALE')
        or doc_type.startswith('EXPORT')
        or doc_type in ('PT', 'RECEIPT', 'THU')
    ):
        name = _sale_party_name(conn, int(doc_id))
        if name:
            return name
        try:
            row = conn.execute(
                'SELECT customer_name, company_name FROM cong_no WHERE sale_id = ? LIMIT 1',
                (int(doc_id),),
            ).fetchone()
            if row:
                d = dict(row)
                name = _norm_name(d.get('company_name') or d.get('customer_name'))
                if name:
                    return name
        except Exception:
            pass
    return None


def _resolve_ap_name(conn: sqlite3.Connection, jl: dict, je: dict) -> str | None:
    pt = str(jl.get('partner_type') or '').strip().lower()
    pid = jl.get('partner_id')
    if pid and pt in ('supplier', 'ncc', 'ap', ''):
        name = _supplier_name_by_id(conn, int(pid))
        if name:
            return name
    if pid:
        name = _supplier_name_by_id(conn, int(pid))
        if name:
            return name
    doc_type = str(je.get('document_type') or '').upper()
    doc_id = je.get('document_id')
    if doc_id and (
        doc_type.startswith('IMPORT')
        or doc_type in ('PC', 'PAYMENT', 'TTNCC', 'PN', 'MUA')
        or 'NCC' in doc_type
        or 'IMPORT' in doc_type
    ):
        name = _import_supplier_name(conn, int(doc_id))
        if name:
            return name
    return None


def _branch_filter(conn, branch, alias_je='je'):
    from Services.sme.branches import branch_sql_filter
    return branch_sql_filter(branch, alias=alias_je)


def _ar_party_key(company_name, customer_name) -> str:
    return _norm_name(company_name or customer_name)


def _gl_ar_balances(
    conn: sqlite3.Connection,
    *,
    branch: str | None = None,
) -> dict[str, dict[str, float]]:
    """Số dư TK 131 theo tên KH: total=Nợ phát sinh, paid=Có phát sinh, remaining=Nợ−Có."""
    ensure_sme_journal_ready(conn, commit=False)
    bf, bp = _branch_filter(conn, branch, 'je')
    rows = conn.execute(
        f"""
        SELECT jl.partner_id, jl.partner_type, jl.description AS jl_desc,
               jl.debit, jl.credit,
               je.document_type, je.document_id, je.description AS je_desc
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND jl.account_code LIKE '131%'
          {bf}
        """,
        bp,
    ).fetchall()
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        jl = _row_dict(r)
        je = {
            'document_type': jl.get('document_type'),
            'document_id': jl.get('document_id'),
            'description': jl.get('je_desc'),
        }
        name = _resolve_ar_name(conn, jl, je)
        if not name:
            continue
        slot = out.setdefault(name, {'total': 0.0, 'paid': 0.0, 'remaining': 0.0})
        slot['total'] += _f(jl.get('debit'))
        slot['paid'] += _f(jl.get('credit'))
        slot['remaining'] += _f(jl.get('debit')) - _f(jl.get('credit'))
    return out


def _gl_ap_balances(
    conn: sqlite3.Connection,
    *,
    branch: str | None = None,
) -> dict[str, dict[str, float]]:
    """Số dư TK 331 theo tên NCC: total=Có phát sinh, paid=Nợ phát sinh, remaining=Có−Nợ."""
    ensure_sme_journal_ready(conn, commit=False)
    bf, bp = _branch_filter(conn, branch, 'je')
    rows = conn.execute(
        f"""
        SELECT jl.partner_id, jl.partner_type, jl.description AS jl_desc,
               jl.debit, jl.credit,
               je.document_type, je.document_id, je.description AS je_desc
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND jl.account_code LIKE '331%'
          {bf}
        """,
        bp,
    ).fetchall()
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        jl = _row_dict(r)
        je = {
            'document_type': jl.get('document_type'),
            'document_id': jl.get('document_id'),
            'description': jl.get('je_desc'),
        }
        name = _resolve_ap_name(conn, jl, je)
        if not name:
            continue
        slot = out.setdefault(name, {'total': 0.0, 'paid': 0.0, 'remaining': 0.0})
        slot['total'] += _f(jl.get('credit'))
        slot['paid'] += _f(jl.get('debit'))
        slot['remaining'] += _f(jl.get('credit')) - _f(jl.get('debit'))
    return out


def _ap_gl_by_import(
    conn: sqlite3.Connection,
    *,
    branch: str | None = None,
) -> dict[int, dict[str, float]]:
    """Phát sinh TK 331 theo phiếu nhập (document_id): total=Có, paid=Nợ, remaining=Có−Nợ."""
    ensure_sme_journal_ready(conn, commit=False)
    bf, bp = _branch_filter(conn, branch, 'je')
    rows = conn.execute(
        f"""
        SELECT je.document_id, jl.debit, jl.credit
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND jl.account_code LIKE '331%'
          AND je.document_id IS NOT NULL
          {bf}
        """,
        bp,
    ).fetchall()
    out: dict[int, dict[str, float]] = {}
    for r in rows:
        doc_id = r['document_id'] if isinstance(r, sqlite3.Row) else r[0]
        try:
            iid = int(doc_id)
        except (TypeError, ValueError):
            continue
        jl = _row_dict(r)
        slot = out.setdefault(iid, {'total': 0.0, 'paid': 0.0, 'remaining': 0.0})
        slot['total'] += _f(jl.get('credit'))
        slot['paid'] += _f(jl.get('debit'))
        slot['remaining'] += _f(jl.get('credit')) - _f(jl.get('debit'))
    return out


def _apply_ap_gl_to_record(record: dict, gl_slot: dict[str, float] | None) -> dict:
    """Gắn số liệu 331 lên phiếu nhập; giữ giá trị hóa đơn (có thuế) để tham chiếu."""
    imp_total = _f(record.get('total_value'))
    imp_paid = _f(record.get('paid_amount'))
    imp_remaining = _f(record.get('remaining_amount'))
    record['invoice_total'] = imp_total
    record['invoice_paid'] = imp_paid
    record['invoice_remaining'] = imp_remaining
    if gl_slot and (gl_slot.get('total', 0) > 0.5 or gl_slot.get('paid', 0) > 0.5):
        record['total_value'] = round(gl_slot['total'], 0)
        record['paid_amount'] = round(gl_slot['paid'], 0)
        record['remaining_amount'] = round(max(gl_slot['remaining'], 0.0), 0)
        record['amount_source'] = '331'
    else:
        record['amount_source'] = 'import'
    return record


def _ar_open_totals(
    conn: sqlite3.Connection,
    *,
    branch: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Open-item phải thu nếu tenant có cong_no.

    Với SME, đây chỉ là dữ liệu chi tiết/fallback. Số dư kế toán chính thức
    vẫn lấy từ TK 131 trong sme_journal_lines.
    """
    from Services.sme.cong_no_ops import remaining_sql
    from Services.sme.branches import sale_branch_filter_sql

    cn_cols = _table_cols(conn, 'cong_no')
    required = {'customer_name', 'unpaid_amount', 'sale_id'}
    if not required.issubset(cn_cols):
        return {}

    sale_cols = _table_cols(conn, 'sale')
    has_sale = bool(sale_cols and 'id' in sale_cols)
    paid_expr = 'COALESCE(cn.paid_amount, 0)' if 'paid_amount' in cn_cols else '0'
    rem = f'(COALESCE(cn.unpaid_amount, 0) - {paid_expr})'

    cn_company = "NULLIF(TRIM(cn.company_name), '')" if 'company_name' in cn_cols else "NULL"
    sale_company = "NULLIF(TRIM(s.company_name), '')" if has_sale and 'company_name' in sale_cols else "NULL"
    company_expr = f"COALESCE({cn_company}, {sale_company})"

    sbf, sbp = ('', [])
    if has_sale:
        sbf, sbp = sale_branch_filter_sql(conn, branch, alias='s')

    join_sql = 'LEFT JOIN sale s ON s.id = cn.sale_id' if has_sale else ''
    out: dict[str, dict[str, Any]] = {}
    try:
        rows = conn.execute(
            f"""
            SELECT
                cn.customer_name,
                {company_expr} AS company_name,
                cn.unpaid_amount AS total_debt,
                {paid_expr} AS paid_amount,
                ({rem}) AS remaining
            FROM cong_no cn
            {join_sql}
            WHERE ({rem}) > 0.5
            {sbf}
            """,
            sbp,
        ).fetchall()
    except Exception:
        return out

    for r in rows:
        d = _row_dict(r)
        key = _ar_party_key(d.get('company_name'), d.get('customer_name'))
        if not key:
            continue
        slot = out.setdefault(key, {
            'customer_name': key,
            'company_name': _norm_name(d.get('company_name') or ''),
            'total': 0.0,
            'paid': 0.0,
            'remaining': 0.0,
        })
        slot['total'] += _f(d.get('total_debt'))
        slot['paid'] += _f(d.get('paid_amount'))
        slot['remaining'] += _f(d.get('remaining'))
        if d.get('company_name'):
            slot['company_name'] = _norm_name(d.get('company_name'))
    return out

def _ap_open_totals(
    conn: sqlite3.Connection,
    *,
    branch: str | None = None,
) -> dict[str, dict[str, float]]:
    """Gom phiếu nhập còn nợ theo tên NCC — khớp bảng chi tiết."""
    from Services.sme.branches import import_branch_filter_sql

    ibf, ibp = import_branch_filter_sql(conn, branch, alias='i')
    out: dict[str, dict[str, float]] = {}
    try:
        rows = conn.execute(
            f"""
            SELECT s.name,
                   COALESCE(i.total_value, 0) AS total_value,
                   COALESCE(i.paid_amount, 0) AS paid_amount,
                   (COALESCE(i.total_value, 0) - COALESCE(i.paid_amount, 0)) AS remaining
            FROM import i
            JOIN suppliers s ON s.id = i.supplier_id
            WHERE (COALESCE(i.total_value, 0) - COALESCE(i.paid_amount, 0)) > 0.5
            {ibf}
            """,
            ibp,
        ).fetchall()
    except Exception:
        return out

    for r in rows:
        d = _row_dict(r)
        key = _norm_name(d.get('name'))
        if not key:
            continue
        slot = out.setdefault(key, {'total': 0.0, 'paid': 0.0, 'remaining': 0.0})
        slot['total'] += _f(d.get('total_value'))
        slot['paid'] += _f(d.get('paid_amount'))
        slot['remaining'] += _f(d.get('remaining'))
    return out


def _summary_from_records(
    records: list[dict],
    *,
    total_key: str,
    paid_key: str,
    remaining_key: str,
    account_code: str,
    source: str,
) -> dict[str, Any]:
    total = sum(_f(x.get(total_key)) for x in records)
    paid = sum(_f(x.get(paid_key)) for x in records)
    remaining = sum(_f(x.get(remaining_key)) for x in records)
    return {
        'total': round(total, 0),
        'paid': round(paid, 0),
        'remaining': round(remaining, 0),
        'account_code': account_code,
        'source': source,
    }


def list_ar_customers(
    conn: sqlite3.Connection,
    *,
    branch: str | None = None,
) -> list[dict[str, Any]]:
    """Danh sách KH còn nợ SME.

    Ưu tiên tuyệt đối số dư TK 131 (GL). cong_no chỉ fallback khi bút toán
    chưa tồn tại, để không làm mất dấu khoản mở trong giai đoạn chuyển đổi.
    """
    gl_map = _gl_ar_balances(conn, branch=branch)
    open_map = _ar_open_totals(conn, branch=branch)

    names = set(gl_map) | set(open_map)
    out: list[dict[str, Any]] = []
    for name in sorted(names, key=lambda x: x.casefold()):
        gl = gl_map.get(name) or {}
        gl_has_activity = abs(_f(gl.get('total'))) + abs(_f(gl.get('paid'))) > 0.5
        if gl_has_activity:
            bal = max(_f(gl.get('remaining')), 0.0)
            if bal <= 0.5:
                continue
            out.append({
                'customer_name': name,
                'company_name': (open_map.get(name) or {}).get('company_name') or '',
                'account_code': '131',
                'balance': round(bal, 0),
                'source': 'gl',
            })
            continue

        open_slot = open_map.get(name) or {}
        bal = max(_f(open_slot.get('remaining')), 0.0)
        if bal > 0.5:
            out.append({
                'customer_name': name,
                'company_name': open_slot.get('company_name') or '',
                'account_code': '131',
                'balance': round(bal, 0),
                'source': 'open_item_fallback',
            })
    return out

def ledger_open_totals(
    conn: sqlite3.Connection,
    *,
    branch: str | None = None,
) -> dict[str, float]:
    """Tổng còn phải thu/trả — bằng cộng số trên trang chi tiết (dropdown + bảng)."""
    ar = sum(_f(x.get('balance')) for x in list_ar_customers(conn, branch=branch))
    ap = sum(_f(x.get('balance')) for x in list_ap_suppliers(conn, branch=branch))
    return {'ar': round(ar, 0), 'ap': round(ap, 0)}


def ar_customer_detail(
    conn: sqlite3.Connection,
    customer_name: str,
    *,
    branch: str | None = None,
) -> dict[str, Any]:
    """Chi tiết phải thu SME.

    Nguồn sự thật: TK 131 trong sổ nhật ký SME.
    Bảng cong_no (nếu tenant có) chỉ phục vụ danh sách open-item/đối chiếu.
    """
    ensure_sme_journal_ready(conn, commit=False)
    from Services.sme.branches import sale_branch_filter_sql

    target = _norm_name(customer_name)
    gl_map = _gl_ar_balances(conn, branch=branch)
    gl = gl_map.get(target) or {'total': 0.0, 'paid': 0.0, 'remaining': 0.0}
    total_debit = _f(gl.get('total'))
    total_credit = _f(gl.get('paid'))
    gl_remaining = _f(gl.get('remaining'))
    gl_has_activity = abs(total_debit) + abs(total_credit) > 0.5

    records: list[dict[str, Any]] = []
    cn_cols = _table_cols(conn, 'cong_no')
    sale_cols = _table_cols(conn, 'sale')
    required = {'customer_name', 'unpaid_amount', 'sale_id'}

    if required.issubset(cn_cols):
        has_sale = bool(sale_cols and 'id' in sale_cols)
        paid_expr = 'COALESCE(cn.paid_amount, 0)' if 'paid_amount' in cn_cols else '0'
        rem = f'(COALESCE(cn.unpaid_amount, 0) - {paid_expr})'

        debt_id_expr = 'cn.debt_id' if 'debt_id' in cn_cols else 'cn.sale_id'
        sale_no_cn = "NULLIF(TRIM(cn.sale_no), '')" if 'sale_no' in cn_cols else "NULL"
        sale_no_s = "NULLIF(TRIM(s.sale_no), '')" if has_sale and 'sale_no' in sale_cols else "NULL"
        sale_no_expr = f"COALESCE({sale_no_s}, {sale_no_cn})"
        date_expr = 'cn.date_of_debt' if 'date_of_debt' in cn_cols else (
            's.date' if has_sale and 'date' in sale_cols else 'NULL'
        )
        cn_company = "NULLIF(TRIM(cn.company_name), '')" if 'company_name' in cn_cols else "NULL"
        sale_company = "NULLIF(TRIM(s.company_name), '')" if has_sale and 'company_name' in sale_cols else "NULL"
        company_expr = f"COALESCE({cn_company}, {sale_company})"
        account_expr = "COALESCE(cn.debit_account, '131')" if 'debit_account' in cn_cols else "'131'"
        sale_type_expr = "COALESCE(s.sale_type, '')" if has_sale and 'sale_type' in sale_cols else "''"

        match_parts = ["TRIM(COALESCE(cn.customer_name, '')) = ?"]
        match_params = [target]
        if 'company_name' in cn_cols:
            match_parts.append("TRIM(COALESCE(cn.company_name, '')) = ?")
            match_params.append(target)
        if has_sale and 'customer_name' in sale_cols:
            match_parts.append("TRIM(COALESCE(s.customer_name, '')) = ?")
            match_params.append(target)
        if has_sale and 'company_name' in sale_cols:
            match_parts.append("TRIM(COALESCE(s.company_name, '')) = ?")
            match_params.append(target)

        sbf, sbp = ('', [])
        if has_sale:
            sbf, sbp = sale_branch_filter_sql(conn, branch, alias='s')
        join_sql = 'LEFT JOIN sale s ON cn.sale_id = s.id' if has_sale else ''

        sql_records = f"""
            SELECT
                {debt_id_expr} AS debt_id,
                {sale_no_expr} AS sale_no,
                {date_expr} AS date_of_debt,
                cn.customer_name,
                cn.sale_id,
                {company_expr} AS company_name,
                cn.unpaid_amount AS total_debt,
                {paid_expr} AS paid_amount,
                ({rem}) AS remaining,
                {account_expr} AS account_code,
                {sale_type_expr} AS sale_type
            FROM cong_no cn
            {join_sql}
            WHERE ({rem}) > 0.5
              {sbf}
              AND ({' OR '.join(match_parts)})
            ORDER BY date_of_debt ASC, debt_id ASC
        """
        try:
            params = [*sbp, *match_params]
            for r in conn.execute(sql_records, params).fetchall():
                d = _row_dict(r)
                d['total_debt'] = _f(d.get('total_debt'))
                d['paid_amount'] = _f(d.get('paid_amount'))
                d['remaining'] = _f(d.get('remaining'))
                sn = _norm_name(d.get('sale_no'))
                if sn.upper().startswith('XK'):
                    d['export_voucher_no'] = sn
                    d['sale_no'] = f"ĐH{int(d['sale_id']):06d}" if d.get('sale_id') else sn
                records.append(d)
        except Exception:
            records = []

    open_summary = _summary_from_records(
        records,
        total_key='total_debt',
        paid_key='paid_amount',
        remaining_key='remaining',
        account_code='131',
        source='open_item',
    ) if records else {
        'total': 0.0, 'paid': 0.0, 'remaining': 0.0,
        'account_code': '131', 'source': 'open_item',
    }

    if gl_has_activity:
        summary = {
            'total': round(total_debit, 0),
            'paid': round(total_credit, 0),
            'remaining': round(max(gl_remaining, 0.0), 0),
            'account_code': '131',
            'source': 'gl',
        }
    else:
        # Chỉ fallback trong giai đoạn bút toán SME chưa được tạo.
        summary = {
            'total': round(_f(open_summary.get('total')), 0),
            'paid': round(_f(open_summary.get('paid')), 0),
            'remaining': round(max(_f(open_summary.get('remaining')), 0.0), 0),
            'account_code': '131',
            'source': 'open_item_fallback' if records else 'gl',
        }

    summary['gl_total'] = round(total_debit, 0)
    summary['gl_paid'] = round(total_credit, 0)
    summary['gl_remaining'] = round(max(gl_remaining, 0.0), 0)
    summary['open_item_total'] = round(_f(open_summary.get('total')), 0)
    summary['open_item_paid'] = round(_f(open_summary.get('paid')), 0)
    summary['open_item_remaining'] = round(max(_f(open_summary.get('remaining')), 0.0), 0)
    summary['reconciled'] = abs(
        _f(summary['gl_remaining']) - _f(summary['open_item_remaining'])
    ) <= 0.5 if records and gl_has_activity else None

    return {'summary': summary, 'records': records}

def list_ap_suppliers(
    conn: sqlite3.Connection,
    *,
    branch: str | None = None,
) -> list[dict[str, Any]]:
    """Danh sách NCC còn nợ — số dư TK 331 theo tên NCC; fallback phiếu nhập chưa hạch toán."""
    gl_map = _gl_ap_balances(conn, branch=branch)
    open_map = _ap_open_totals(conn, branch=branch)

    names = set(gl_map) | set(open_map)
    out: list[dict[str, Any]] = []
    for name in sorted(names, key=lambda x: x.casefold()):
        gl_rem = _f(gl_map.get(name, {}).get('remaining'))
        if gl_rem > 0.5:
            out.append({
                'supplier_name': name,
                'account_code': '331',
                'balance': round(gl_rem, 0),
            })
        elif _f(open_map.get(name, {}).get('remaining')) > 0.5:
            out.append({
                'supplier_name': name,
                'account_code': '331',
                'balance': round(open_map[name]['remaining'], 0),
            })
    return out


def ap_supplier_detail(
    conn: sqlite3.Connection,
    supplier_name: str,
    *,
    branch: str | None = None,
) -> dict[str, Any]:
    """Chi tiết phải trả một NCC: summary 331 + phiếu nhập còn mở."""
    ensure_sme_journal_ready(conn, commit=False)
    from Services.sme.branches import import_branch_filter_sql

    target = _norm_name(supplier_name)
    gl_map = _gl_ap_balances(conn, branch=branch)
    gl_by_import = _ap_gl_by_import(conn, branch=branch)
    ibf, ibp = import_branch_filter_sql(conn, branch, alias='i')
    supplier = conn.execute(
        'SELECT id, name, address FROM suppliers WHERE TRIM(name) = ?', (target,)
    ).fetchone()
    records = []
    if supplier:
        s_id = supplier['id'] if isinstance(supplier, sqlite3.Row) else supplier[0]
        s_name = supplier['name'] if isinstance(supplier, sqlite3.Row) else supplier[1]
        s_addr = ''
        if isinstance(supplier, sqlite3.Row):
            s_addr = supplier['address'] or ''
        for r in conn.execute(
            f"""
            SELECT
                i.id,
                i.import_no,
                i.bill_no,
                i.date,
                COALESCE(i.total_value, 0) AS total_value,
                COALESCE(i.paid_amount, 0) AS paid_amount,
                (COALESCE(i.total_value, 0) - COALESCE(i.paid_amount, 0)) AS remaining_amount,
                ? AS supplier_address,
                ? AS supplier_name,
                '331' AS account_code
            FROM import i
            WHERE i.supplier_id = ?
              AND (COALESCE(i.total_value, 0) - COALESCE(i.paid_amount, 0)) > 0.5
              {ibf}
            ORDER BY i.date DESC
            """,
            (s_addr, s_name, s_id, *ibp),
        ).fetchall():
            d = _row_dict(r)
            d['total_value'] = _f(d.get('total_value'))
            d['paid_amount'] = _f(d.get('paid_amount'))
            d['remaining_amount'] = _f(d.get('remaining_amount'))
            _apply_ap_gl_to_record(d, gl_by_import.get(int(d['id'])))
            records.append(d)

    gl = gl_map.get(target) or {'total': 0.0, 'paid': 0.0, 'remaining': 0.0}
    total_credit = _f(gl.get('total'))
    total_debit = _f(gl.get('paid'))
    gl_remaining = _f(gl.get('remaining'))

    import_total = sum(_f(r.get('invoice_total', r.get('total_value'))) for r in records)
    import_paid = sum(_f(r.get('invoice_paid', r.get('paid_amount'))) for r in records)
    import_remaining = sum(_f(r.get('invoice_remaining', r.get('remaining_amount'))) for r in records)

    summary = {
        'total': round(total_credit, 0),
        'paid': round(total_debit, 0),
        'remaining': round(max(gl_remaining, 0.0), 0),
        'account_code': '331',
        'source': 'gl',
    }
    summary['import_total'] = round(import_total, 0)
    summary['import_paid'] = round(import_paid, 0)
    summary['import_remaining'] = round(import_remaining, 0)
    return {'summary': summary, 'records': records}


def ar_print_ledger(
    conn: sqlite3.Connection,
    customer_name: str,
    *,
    start: str | None = None,
    end: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    """Sổ chi tiết 131 theo tên KH (phát sinh Nợ/Có từ nhật ký)."""
    ensure_sme_journal_ready(conn, commit=False)
    target = _norm_name(customer_name)
    bf, bp = _branch_filter(conn, branch, 'je')

    opening = 0.0
    if start:
        for r in conn.execute(
            f"""
            SELECT jl.partner_id, jl.partner_type, jl.description AS jl_desc,
                   jl.debit, jl.credit,
                   je.document_type, je.document_id, je.description AS je_desc
            FROM sme_journal_lines jl
            JOIN sme_journal_entries je ON je.id = jl.entry_id
            WHERE je.status IN ('posted', 'reversed')
              AND jl.account_code LIKE '131%'
              AND je.posting_date < ?
              {bf}
            """,
            (start, *bp),
        ).fetchall():
            jl = _row_dict(r)
            je = {
                'document_type': jl.get('document_type'),
                'document_id': jl.get('document_id'),
                'description': jl.get('je_desc'),
            }
            if _resolve_ar_name(conn, jl, je) == target:
                opening += _f(jl.get('debit')) - _f(jl.get('credit'))

    sql = f"""
        SELECT je.posting_date, je.document_no, je.entry_no, je.document_type, je.document_id,
               jl.partner_id, jl.partner_type, jl.description AS jl_desc,
               je.description AS je_desc, jl.debit, jl.credit, jl.account_code
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND jl.account_code LIKE '131%'
          {bf}
    """
    params: list[Any] = list(bp)
    if start:
        sql += ' AND je.posting_date >= ?'
        params.append(start)
    if end:
        sql += ' AND je.posting_date <= ?'
        params.append(end)
    sql += ' ORDER BY je.posting_date, je.id, jl.id'

    rows = []
    run = opening
    tot_no = tot_co = 0.0
    for r in conn.execute(sql, params).fetchall():
        jl = _row_dict(r)
        je = {
            'document_type': jl.get('document_type'),
            'document_id': jl.get('document_id'),
            'description': jl.get('je_desc'),
        }
        if _resolve_ar_name(conn, jl, je) != target:
            continue
        no = _f(jl.get('debit'))
        co = _f(jl.get('credit'))
        run += no - co
        tot_no += no
        tot_co += co
        rows.append({
            'safe_date': str(jl.get('posting_date') or '')[:10] or '—',
            'sale_no': jl.get('document_no') or jl.get('entry_no') or '',
            'dien_giai': jl.get('jl_desc') or jl.get('je_desc') or 'Phát sinh TK 131',
            'no': round(no, 0),
            'co': round(co, 0),
            'running_balance': round(run, 0),
            'account_code': jl.get('account_code') or '131',
        })

    info = {}
    try:
        biz = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
        if biz:
            info = dict(biz)
    except Exception:
        pass

    return {
        'rows': rows,
        'totals': {
            'opening': round(opening, 0),
            'no': round(tot_no, 0),
            'co': round(tot_co, 0),
            'closing': round(max(run if rows else opening + tot_no - tot_co, 0.0), 0),
        },
        'customer': target,
        'display_name': target,
        'info': info,
        'account_code': '131',
    }


def ap_print_ledger(
    conn: sqlite3.Connection,
    supplier_name: str,
    *,
    start: str | None = None,
    end: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    """Sổ chi tiết 331 theo tên NCC."""
    ensure_sme_journal_ready(conn, commit=False)
    target = _norm_name(supplier_name)
    bf, bp = _branch_filter(conn, branch, 'je')

    opening = 0.0
    if start:
        for r in conn.execute(
            f"""
            SELECT jl.partner_id, jl.partner_type, jl.description AS jl_desc,
                   jl.debit, jl.credit,
                   je.document_type, je.document_id, je.description AS je_desc
            FROM sme_journal_lines jl
            JOIN sme_journal_entries je ON je.id = jl.entry_id
            WHERE je.status IN ('posted', 'reversed')
              AND jl.account_code LIKE '331%'
              AND je.posting_date < ?
              {bf}
            """,
            (start, *bp),
        ).fetchall():
            jl = _row_dict(r)
            je = {
                'document_type': jl.get('document_type'),
                'document_id': jl.get('document_id'),
                'description': jl.get('je_desc'),
            }
            if _resolve_ap_name(conn, jl, je) == target:
                opening += _f(jl.get('credit')) - _f(jl.get('debit'))

    sql = f"""
        SELECT je.posting_date, je.document_no, je.entry_no, je.document_type, je.document_id,
               jl.partner_id, jl.partner_type, jl.description AS jl_desc,
               je.description AS je_desc, jl.debit, jl.credit, jl.account_code
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        WHERE je.status IN ('posted', 'reversed')
          AND jl.account_code LIKE '331%'
          {bf}
    """
    params: list[Any] = list(bp)
    if start:
        sql += ' AND je.posting_date >= ?'
        params.append(start)
    if end:
        sql += ' AND je.posting_date <= ?'
        params.append(end)
    sql += ' ORDER BY je.posting_date, je.id, jl.id'

    rows = []
    run = opening
    tot_no = tot_co = 0.0
    for r in conn.execute(sql, params).fetchall():
        jl = _row_dict(r)
        je = {
            'document_type': jl.get('document_type'),
            'document_id': jl.get('document_id'),
            'description': jl.get('je_desc'),
        }
        if _resolve_ap_name(conn, jl, je) != target:
            continue
        # Sổ phải trả: Nợ = giảm nợ (debit 331), Có = tăng nợ (credit 331)
        no = _f(jl.get('debit'))
        co = _f(jl.get('credit'))
        run += co - no
        tot_no += no
        tot_co += co
        rows.append({
            'purchase_no': jl.get('document_no') or jl.get('entry_no') or '',
            'date': str(jl.get('posting_date') or '')[:10],
            'dien_giai': jl.get('jl_desc') or jl.get('je_desc') or 'Phát sinh TK 331',
            'no': round(no, 0),
            'co': round(co, 0),
            'running_balance': round(run, 0),
            'account_code': jl.get('account_code') or '331',
        })

    info = {}
    try:
        biz = conn.execute('SELECT * FROM business_info LIMIT 1').fetchone()
        if biz:
            info = dict(biz)
    except Exception:
        pass

    closing = run if rows else opening + tot_co - tot_no
    return {
        'rows': rows,
        'totals': {
            'opening': round(opening, 0),
            'no': round(tot_no, 0),
            'co': round(tot_co, 0),
            'closing': round(max(closing, 0.0), 0),
        },
        'supplier': target,
        'info': info,
        'account_code': '331',
    }
