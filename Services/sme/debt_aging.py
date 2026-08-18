"""Tuổi nợ phải thu (131) / phải trả (331) theo chứng từ gốc."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

BUCKETS = (
    (0, 30, '0–30 ngày'),
    (31, 60, '31–60 ngày'),
    (61, 90, '61–90 ngày'),
    (91, None, 'Trên 90 ngày'),
)


def _days(as_of: str, doc_date: str) -> int:
    try:
        a = datetime.strptime(str(as_of)[:10], '%Y-%m-%d').date()
        d = datetime.strptime(str(doc_date)[:10], '%Y-%m-%d').date()
        return max(0, (a - d).days)
    except (TypeError, ValueError):
        return 0


def _empty_buckets() -> list[dict[str, Any]]:
    return [
        {'key': lab, 'label': lab, 'from_days': lo, 'to_days': hi, 'amount': 0.0, 'count': 0}
        for lo, hi, lab in BUCKETS
    ]


def _put(buckets: list[dict], days: int, amount: float) -> None:
    if amount <= 0:
        return
    for b in buckets:
        lo = b['from_days']
        hi = b['to_days']
        if days >= lo and (hi is None or days <= hi):
            b['amount'] = round(b['amount'] + amount, 0)
            b['count'] += 1
            return


def _cong_no_remaining_sql(conn: sqlite3.Connection) -> str:
    cols = {r[1] for r in conn.execute('PRAGMA table_info(cong_no)').fetchall()}
    if 'remaining_amount' in cols:
        return 'COALESCE(cn.remaining_amount, 0)'
    return '(COALESCE(cn.unpaid_amount, 0) - COALESCE(cn.paid_amount, 0))'


def ar_aging(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    as_of_s = (as_of or datetime.now().strftime('%Y-%m-%d'))[:10]
    buckets = _empty_buckets()
    details: list[dict[str, Any]] = []
    try:
        rem = _cong_no_remaining_sql(conn)
        rows = conn.execute(
            f"""
            SELECT cn.customer_name, cn.sale_no, cn.date_of_debt,
                   {rem} AS remaining
            FROM cong_no cn
            WHERE {rem} > 0.5
            ORDER BY cn.date_of_debt
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    total = 0.0
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else r
        amt = float(d.get('remaining') or 0)
        days = _days(as_of_s, d.get('date_of_debt') or as_of_s)
        _put(buckets, days, amt)
        total += amt
        details.append({
            'party': d.get('customer_name') or '',
            'doc_no': d.get('sale_no') or '',
            'doc_date': str(d.get('date_of_debt') or '')[:10],
            'days': days,
            'amount': round(amt, 0),
        })
    return {
        'kind': 'ar',
        'as_of': as_of_s,
        'total': round(total, 0),
        'buckets': buckets,
        'details': details[:80],
    }


def ap_aging(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    as_of_s = (as_of or datetime.now().strftime('%Y-%m-%d'))[:10]
    buckets = _empty_buckets()
    details: list[dict[str, Any]] = []
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(import)').fetchall()}
        if 'bill_date' in cols and 'date' in cols:
            date_expr = "COALESCE(NULLIF(TRIM(i.bill_date), ''), i.date, '')"
        elif 'bill_date' in cols:
            date_expr = "COALESCE(i.bill_date, '')"
        elif 'date' in cols:
            date_expr = "COALESCE(i.date, '')"
        else:
            date_expr = "''"
        paid_expr = 'COALESCE(i.paid_amount, 0)' if 'paid_amount' in cols else '0'
        total_expr = 'COALESCE(i.total_value, 0)' if 'total_value' in cols else '0'
        rem_expr = f'({total_expr} - {paid_expr})'
        rows = conn.execute(
            f"""
            SELECT COALESCE(s.name, '') AS supplier_name,
                   COALESCE(i.import_no, '') AS import_no,
                   {date_expr} AS doc_date,
                   {rem_expr} AS remaining
            FROM import i
            LEFT JOIN suppliers s ON s.id = i.supplier_id
            WHERE {rem_expr} > 0.5
            ORDER BY i.id
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    total = 0.0
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else r
        amt = float(d.get('remaining') or 0)
        days = _days(as_of_s, d.get('doc_date') or as_of_s)
        _put(buckets, days, amt)
        total += amt
        details.append({
            'party': d.get('supplier_name') or '',
            'doc_no': d.get('import_no') or '',
            'doc_date': str(d.get('doc_date') or '')[:10],
            'days': days,
            'amount': round(amt, 0),
        })
    return {
        'kind': 'ap',
        'as_of': as_of_s,
        'total': round(total, 0),
        'buckets': buckets,
        'details': details[:80],
    }


def debt_aging_summary(conn: sqlite3.Connection, *, as_of: str | None = None) -> dict[str, Any]:
    ar = ar_aging(conn, as_of=as_of)
    ap = ap_aging(conn, as_of=as_of)
    return {
        'as_of': ar['as_of'],
        'ar': ar,
        'ap': ap,
    }


def subledger_open_totals(conn: sqlite3.Connection) -> dict[str, float]:
    """Tổng còn phải thu / phải trả trên sổ chi tiết (không giới hạn dòng)."""
    ar = 0.0
    ap = 0.0
    try:
        rem = _cong_no_remaining_sql(conn)
        row = conn.execute(
            f"SELECT COALESCE(SUM({rem}), 0) FROM cong_no cn WHERE {rem} > 0.5"
        ).fetchone()
        ar = float(row[0] if row else 0)
    except sqlite3.Error:
        ar = 0.0
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(import)').fetchall()}
        paid_expr = 'COALESCE(i.paid_amount, 0)' if 'paid_amount' in cols else '0'
        total_expr = 'COALESCE(i.total_value, 0)' if 'total_value' in cols else '0'
        rem_expr = f'({total_expr} - {paid_expr})'
        row = conn.execute(
            f"SELECT COALESCE(SUM({rem_expr}), 0) FROM import i WHERE {rem_expr} > 0.5"
        ).fetchone()
        ap = float(row[0] if row else 0)
    except sqlite3.Error:
        ap = 0.0
    return {'ar': round(ar, 0), 'ap': round(ap, 0)}
