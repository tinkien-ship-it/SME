# -*- coding: utf-8 -*-
"""CRM Analytics — funnel, nguồn KH, CPL, KPI, leaderboard, retention, ticket SLA."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from Services.crm import OPP_STAGE_LABELS, OPP_STAGES, ready
from Services.crm_schema import ensure_crm_schema


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _row(r) -> dict:
    if r is None:
        return {}
    if isinstance(r, dict):
        return dict(r)
    if hasattr(r, 'keys'):
        return dict(r)
    return {}


def _rows(cur) -> list[dict]:
    return [_row(r) for r in cur.fetchall()]


def _period_key(period_type: str = 'month', dt: datetime | None = None) -> str:
    dt = dt or datetime.now()
    if period_type == 'quarter':
        q = (dt.month - 1) // 3 + 1
        return f'{dt.year}-Q{q}'
    return dt.strftime('%Y-%m')


def source_pie(conn: sqlite3.Connection) -> dict:
    ready(conn)
    rows = _rows(conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(source), ''), 'Khác') AS source, COUNT(*) AS n
        FROM crm_leads
        GROUP BY COALESCE(NULLIF(TRIM(source), ''), 'Khác')
        ORDER BY n DESC
        """
    ))
    if not rows:
        rows = _rows(conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(crm_source), ''), 'Khác') AS source, COUNT(*) AS n
            FROM customers
            WHERE crm_source IS NOT NULL AND TRIM(crm_source) != ''
            GROUP BY COALESCE(NULLIF(TRIM(crm_source), ''), 'Khác')
            ORDER BY n DESC
            """
        ))
    labels = [r['source'] for r in rows]
    values = [int(r['n'] or 0) for r in rows]
    total = sum(values) or 1
    return {
        'labels': labels,
        'values': values,
        'percents': [round(v * 100.0 / total, 1) for v in values],
        'total': sum(values),
    }


def cpl_by_month(conn: sqlite3.Connection, months: int = 6) -> dict:
    """Chi phí / lead theo tháng từ crm_campaigns.spend + số lead tạo trong tháng."""
    ready(conn)
    now = datetime.now()
    labels = []
    spends = []
    leads = []
    cpls = []
    for i in range(months - 1, -1, -1):
        y = now.year
        m = now.month - i
        while m <= 0:
            m += 12
            y -= 1
        key = f'{y:04d}-{m:02d}'
        labels.append(key)
        spend_row = conn.execute(
            """
            SELECT COALESCE(SUM(spend), 0) AS s FROM crm_campaigns
            WHERE (start_date IS NULL OR start_date <= ?)
              AND (end_date IS NULL OR end_date >= ?)
              AND status IN ('active', 'ended', 'paused')
            """,
            (f'{key}-31', f'{key}-01'),
        ).fetchone()
        # Prefer spend booked in month via updated notes — also sum spend for campaigns
        # overlapping month. Additionally allow period-tagged spend in period_key style:
        spend2 = conn.execute(
            """
            SELECT COALESCE(SUM(spend), 0) AS s FROM crm_campaigns
            WHERE substr(COALESCE(start_date, created_at), 1, 7) = ?
            """,
            (key,),
        ).fetchone()
        spend = max(_f(_row(spend_row).get('s')), _f(_row(spend2).get('s')))
        lead_n = conn.execute(
            """
            SELECT COUNT(*) AS n FROM crm_leads
            WHERE substr(COALESCE(created_at, ''), 1, 7) = ?
            """,
            (key,),
        ).fetchone()
        n = int(_row(lead_n).get('n') or 0)
        spends.append(round(spend, 0))
        leads.append(n)
        cpls.append(round(spend / n, 0) if n else 0)
    return {'labels': labels, 'spend': spends, 'leads': leads, 'cpl': cpls}


def sales_funnel(conn: sqlite3.Connection) -> dict:
    ready(conn)
    stages = [s for s in OPP_STAGES if s not in ('won', 'lost')] + ['won', 'lost']
    counts = []
    amounts = []
    labels = []
    for st in stages:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amt
            FROM crm_opportunities WHERE stage = ?
            """,
            (st,),
        ).fetchone()
        d = _row(row)
        labels.append(OPP_STAGE_LABELS.get(st, st))
        counts.append(int(d.get('n') or 0))
        amounts.append(round(_f(d.get('amt')), 0))
    # conversion rates between open stages
    open_stages = [s for s in OPP_STAGES if s not in ('won', 'lost')]
    conversions = []
    prev = None
    for st in open_stages:
        n = counts[stages.index(st)]
        if prev is None:
            conversions.append(None)
        else:
            conversions.append(round(100.0 * n / prev, 1) if prev else 0)
        prev = n if n else prev
    return {
        'stages': stages,
        'labels': labels,
        'counts': counts,
        'amounts': amounts,
        'conversions': conversions,
    }


def revenue_vs_target(conn: sqlite3.Connection, period_type: str = 'month', owner: str | None = None) -> dict:
    ready(conn)
    key = _period_key(period_type)
    owner_key = (owner or '').strip() or ''
    target = 0.0
    target_source = 'none'
    target_detail = ''

    # 1) Ưu tiên HR KPI SALES_REV (Thiết lập KPI nhân sự)
    try:
        from Services.crm_kpi_bridge import prefer_hr_kpi, resolve_hr_sales_rev_target
        if prefer_hr_kpi(conn):
            hr = resolve_hr_sales_rev_target(
                conn, period_type=period_type, period_key=key, owner=owner_key or None,
            )
            if hr.get('found') and _f(hr.get('target')) > 0:
                target = _f(hr.get('target'))
                target_source = hr.get('source') or 'hr_sales_rev'
                target_detail = hr.get('detail') or ''
            else:
                target_detail = hr.get('detail') or ''
    except Exception:
        pass

    # 2) Fallback: crm_targets local
    if target <= 0:
        trow = conn.execute(
            """
            SELECT target_amount FROM crm_targets
            WHERE period_type = ? AND period_key = ?
              AND (owner = ? OR ((owner IS NULL OR owner = '') AND ? = ''))
            ORDER BY CASE WHEN owner = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (period_type, key, owner_key, owner_key, owner_key),
        ).fetchone()
        if not trow:
            trow = conn.execute(
                """
                SELECT COALESCE(SUM(target_amount), 0) AS target_amount FROM crm_targets
                WHERE period_type = ? AND period_key = ?
                """,
                (period_type, key),
            ).fetchone()
        target = _f(_row(trow).get('target_amount'))
        if target > 0:
            target_source = 'crm_targets'
            target_detail = 'KPI cục bộ trên Cấu hình CRM'

    # actual revenue from sales in period
    if period_type == 'quarter':
        y, q = key.split('-Q')
        q = int(q)
        start_m = (q - 1) * 3 + 1
        months = [f'{y}-{m:02d}' for m in range(start_m, start_m + 3)]
        placeholders = ','.join('?' for _ in months)
        sql = f"""
            SELECT COALESCE(SUM(total_amount), 0) AS amt FROM sale
            WHERE substr(COALESCE(date, ''), 1, 7) IN ({placeholders})
              AND COALESCE(status, '') NOT IN ('cancelled', 'deleted')
        """
        params: list[Any] = list(months)
    else:
        sql = """
            SELECT COALESCE(SUM(total_amount), 0) AS amt FROM sale
            WHERE substr(COALESCE(date, ''), 1, 7) = ?
              AND COALESCE(status, '') NOT IN ('cancelled', 'deleted')
        """
        params = [key]
    # optional filter by CRM owner via customer
    if owner_key:
        sql = sql.replace(
            'FROM sale',
            'FROM sale LEFT JOIN customers c ON c.id = sale.customer_id',
        )
        sql += ' AND c.crm_owner = ?'
        params.append(owner_key)
    actual = _f(_row(conn.execute(sql, params).fetchone()).get('amt'))
    pct = round(100.0 * actual / target, 1) if target else 0
    return {
        'period_type': period_type,
        'period_key': key,
        'owner': owner_key or None,
        'target': round(target, 0),
        'actual': round(actual, 0),
        'percent': pct,
        'target_source': target_source,
        'target_detail': target_detail,
    }


def sales_leaderboard(conn: sqlite3.Connection, period_type: str = 'month', limit: int = 20) -> dict:
    ready(conn)
    key = _period_key(period_type)
    if period_type == 'quarter':
        y, q = key.split('-Q')
        q = int(q)
        start_m = (q - 1) * 3 + 1
        months = [f'{y}-{m:02d}' for m in range(start_m, start_m + 3)]
        ph = ','.join('?' for _ in months)
        period_filter = f"substr(COALESCE(s.date,''),1,7) IN ({ph})"
        params: list[Any] = list(months)
    else:
        period_filter = "substr(COALESCE(s.date,''),1,7) = ?"
        params = [key]

    rows = _rows(conn.execute(
        f"""
        SELECT COALESCE(NULLIF(TRIM(c.crm_owner), ''), '(Chưa gán)') AS owner,
               COUNT(s.id) AS orders,
               COALESCE(SUM(s.total_amount), 0) AS revenue
        FROM sale s
        LEFT JOIN customers c ON c.id = s.customer_id
        WHERE {period_filter}
          AND COALESCE(s.status, '') NOT IN ('cancelled', 'deleted')
        GROUP BY COALESCE(NULLIF(TRIM(c.crm_owner), ''), '(Chưa gán)')
        ORDER BY revenue DESC
        LIMIT ?
        """,
        params + [int(limit)],
    ))
    # also won opportunities by owner
    won = _rows(conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(owner), ''), '(Chưa gán)') AS owner,
               COUNT(*) AS wins,
               COALESCE(SUM(amount), 0) AS won_amount
        FROM crm_opportunities
        WHERE stage = 'won'
        GROUP BY COALESCE(NULLIF(TRIM(owner), ''), '(Chưa gán)')
        """
    ))
    won_map = {r['owner']: r for r in won}
    items = []
    for r in rows:
        w = won_map.get(r['owner']) or {}
        items.append({
            'owner': r['owner'],
            'orders': int(r.get('orders') or 0),
            'revenue': round(_f(r.get('revenue')), 0),
            'wins': int(w.get('wins') or 0),
            'won_amount': round(_f(w.get('won_amount')), 0),
        })
    return {'period_key': key, 'period_type': period_type, 'items': items}


def ticket_sla_trend(conn: sqlite3.Connection, days: int = 30) -> dict:
    ready(conn)
    rows = _rows(conn.execute(
        """
        SELECT substr(COALESCE(resolved_at, closed_at, ''), 1, 10) AS d,
               AVG(
                 (julianday(COALESCE(resolved_at, closed_at)) - julianday(COALESCE(opened_at, created_at))) * 24
               ) AS avg_hours,
               COUNT(*) AS n
        FROM crm_tickets
        WHERE COALESCE(resolved_at, closed_at) IS NOT NULL
          AND date(COALESCE(resolved_at, closed_at)) >= date('now', 'localtime', ?)
        GROUP BY substr(COALESCE(resolved_at, closed_at, ''), 1, 10)
        ORDER BY d
        """,
        (f'-{int(days)} day',),
    ))
    return {
        'labels': [r['d'] for r in rows],
        'avg_hours': [round(_f(r.get('avg_hours')), 2) for r in rows],
        'counts': [int(r.get('n') or 0) for r in rows],
    }


def retention_cohort(conn: sqlite3.Connection, months: int = 6) -> dict:
    """Cohort theo tháng đơn đầu; % KH còn mua lại ở tháng +1..+N."""
    ready(conn)
    firsts = _rows(conn.execute(
        """
        SELECT customer_id, substr(MIN(date), 1, 7) AS cohort
        FROM sale
        WHERE customer_id IS NOT NULL AND customer_id > 0
          AND COALESCE(status, '') NOT IN ('cancelled', 'deleted')
        GROUP BY customer_id
        """
    ))
    if not firsts:
        return {'cohorts': [], 'months': list(range(0, months)), 'matrix': []}

    sales_by_cust = {}
    for r in _rows(conn.execute(
        """
        SELECT customer_id, substr(date, 1, 7) AS ym
        FROM sale
        WHERE customer_id IS NOT NULL AND customer_id > 0
          AND COALESCE(status, '') NOT IN ('cancelled', 'deleted')
        """
    )):
        sales_by_cust.setdefault(r['customer_id'], set()).add(r['ym'])

    def _add_months(ym: str, n: int) -> str:
        y, m = map(int, ym.split('-'))
        m += n
        while m > 12:
            m -= 12
            y += 1
        while m < 1:
            m += 12
            y -= 1
        return f'{y:04d}-{m:02d}'

    # last N cohort months
    now = datetime.now()
    cohort_keys = []
    for i in range(months - 1, -1, -1):
        y, m = now.year, now.month - i
        while m <= 0:
            m += 12
            y -= 1
        cohort_keys.append(f'{y:04d}-{m:02d}')

    matrix = []
    sizes = []
    for ck in cohort_keys:
        members = [f['customer_id'] for f in firsts if f.get('cohort') == ck]
        size = len(members) or 0
        sizes.append(size)
        row = []
        for offset in range(months):
            if not size:
                row.append(None)
                continue
            target = _add_months(ck, offset)
            active = sum(1 for cid in members if target in sales_by_cust.get(cid, set()))
            row.append(round(100.0 * active / size, 1))
        matrix.append(row)
    return {'cohorts': cohort_keys, 'sizes': sizes, 'months': list(range(months)), 'matrix': matrix}


def analytics_bundle(conn: sqlite3.Connection) -> dict:
    ensure_crm_schema(conn, commit=False)
    return {
        'source_pie': source_pie(conn),
        'cpl': cpl_by_month(conn),
        'funnel': sales_funnel(conn),
        'kpi': revenue_vs_target(conn, 'month'),
        'kpi_quarter': revenue_vs_target(conn, 'quarter'),
        'leaderboard': sales_leaderboard(conn),
        'ticket_sla': ticket_sla_trend(conn),
        'retention': retention_cohort(conn),
    }
