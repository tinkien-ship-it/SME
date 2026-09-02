"""Doanh thu hoãn lại (3387) — gói dịch vụ trả trước dài hạn (phần mềm/năm…).

Luồng dịch vụ A:
1. Khách trả trước cả năm → Nợ 112 / Có 3387 (hợp đồng + lịch phân bổ).
2. Cuối mỗi tháng → Nợ 3387 / Có 5113 (tự động qua đóng kỳ hoặc thủ công).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from dateutil.relativedelta import relativedelta

from db_utils import sqlite_commit
from Services.sme.journal_engine import (
    ensure_sme_journal_ready,
    post_journal_entry,
    resolve_postable_account,
)

VOUCHER_PREFIX = 'HDDV'
REVENUE_MODE_DEFERRED = 'deferred'
REVENUE_MODE_IMMEDIATE = 'immediate'
STATUS_ACTIVE = 'active'
STATUS_COMPLETED = 'completed'
STATUS_CANCELLED = 'cancelled'
PERIOD_PENDING = 'pending'
PERIOD_POSTED = 'posted'


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _today() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def _money(v) -> float:
    return round(float(v or 0), 2)


def _split_monthly_amounts(total: float, months: int) -> list[float]:
    months = max(1, int(months or 1))
    total_m = _money(total)
    base = _money(total_m / months)
    amounts = [base] * months
    diff = _money(total_m - sum(amounts))
    if diff:
        amounts[-1] = _money(amounts[-1] + diff)
    return amounts


def ensure_product_revenue_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute('PRAGMA table_info(products)').fetchall()}
    alters = (
        ('revenue_mode', "TEXT DEFAULT 'immediate'"),
        ('deferred_months', 'INTEGER DEFAULT 12'),
        ('revenue_account', "TEXT DEFAULT '5113'"),
    )
    for col, decl in alters:
        if col not in cols:
            try:
                conn.execute(f'ALTER TABLE products ADD COLUMN {col} {decl}')
            except sqlite3.Error:
                pass
    # Gói subscription mặc định = DT hoãn 12 tháng
    try:
        conn.execute(
            """
            UPDATE products
            SET revenue_mode = ?, deferred_months = COALESCE(deferred_months, 12)
            WHERE COALESCE(is_subscription_plan, 0) = 1
              AND COALESCE(revenue_mode, '') != ?
            """,
            (REVENUE_MODE_DEFERRED, REVENUE_MODE_DEFERRED),
        )
    except sqlite3.Error:
        pass


def ensure_deferred_revenue_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    ensure_product_revenue_columns(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_deferred_revenue_contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_no TEXT NOT NULL UNIQUE,
            service_product_id INTEGER NOT NULL,
            customer_id INTEGER,
            customer_name TEXT NOT NULL,
            customer_tax_code TEXT,
            start_date TEXT NOT NULL,
            period_months INTEGER NOT NULL DEFAULT 12,
            total_amount REAL NOT NULL DEFAULT 0,
            monthly_amount REAL NOT NULL DEFAULT 0,
            recognized_amount REAL NOT NULL DEFAULT 0,
            receipt_voucher_id INTEGER,
            receipt_journal_id INTEGER,
            liability_account TEXT NOT NULL DEFAULT '3387',
            revenue_account TEXT NOT NULL DEFAULT '5113',
            status TEXT NOT NULL DEFAULT 'active',
            note TEXT,
            created_by TEXT,
            created_at TEXT,
            updated_at TEXT,
            branch_code TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_deferred_revenue_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id INTEGER NOT NULL,
            period_year INTEGER NOT NULL,
            period_month INTEGER NOT NULL,
            recognize_date TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            journal_entry_id INTEGER,
            posted_at TEXT,
            UNIQUE(contract_id, period_year, period_month),
            FOREIGN KEY (contract_id) REFERENCES sme_deferred_revenue_contracts(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_def_rev_contract_status "
        "ON sme_deferred_revenue_contracts(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_def_rev_period_ym "
        "ON sme_deferred_revenue_periods(period_year, period_month, status)"
    )
    from Services.sme.branch_filter import ensure_branch_column
    ensure_branch_column(conn, 'sme_deferred_revenue_contracts')
    if commit:
        sqlite_commit(conn, label='deferred_revenue')


def _next_contract_no(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT contract_no FROM sme_deferred_revenue_contracts "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return f'{VOUCHER_PREFIX}000001'
    raw = row[0] if not isinstance(row, sqlite3.Row) else row['contract_no']
    digits = ''.join(ch for ch in str(raw or '') if ch.isdigit()) or '0'
    return f'{VOUCHER_PREFIX}{int(digits) + 1:06d}'


def _is_deferred_product(row: dict) -> bool:
    if int(row.get('is_subscription_plan') or 0) == 1:
        return True
    return str(row.get('revenue_mode') or '').strip().lower() == REVENUE_MODE_DEFERRED


def get_deferred_product(conn: sqlite3.Connection, product_id: int) -> dict | None:
    ensure_deferred_revenue_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT id, name, product_code, unit, base_price, price,
               COALESCE(is_subscription_plan, 0) AS is_subscription_plan,
               COALESCE(revenue_mode, 'immediate') AS revenue_mode,
               COALESCE(deferred_months, 12) AS deferred_months,
               COALESCE(revenue_account, '5113') AS revenue_account
        FROM products WHERE id = ?
        """,
        (int(product_id),),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    if not _is_deferred_product(data):
        raise ValueError(
            f'Mã dịch vụ «{data.get("name")}» chưa bật DT hoãn (deferred). '
            'Chỉ dùng gói subscription hoặc dịch vụ có revenue_mode=deferred.'
        )
    return data


def list_deferred_service_products(conn: sqlite3.Connection, q: str = '') -> list[dict]:
    ensure_deferred_revenue_schema(conn)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT id, name, product_code, unit,
               COALESCE(base_price, price, 0) AS price,
               COALESCE(deferred_months, 12) AS deferred_months,
               COALESCE(revenue_account, '5113') AS revenue_account,
               COALESCE(is_subscription_plan, 0) AS is_subscription_plan,
               COALESCE(revenue_mode, 'immediate') AS revenue_mode
        FROM products
        WHERE COALESCE(is_subscription_plan, 0) = 1
           OR LOWER(COALESCE(revenue_mode, '')) = ?
    """
    params: list[Any] = [REVENUE_MODE_DEFERRED]
    if q:
        like = f"%{q.strip()}%"
        sql += " AND (name LIKE ? OR product_code LIKE ?)"
        params.extend([like, like])
    sql += " ORDER BY name COLLATE NOCASE"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def preview_deferred_schedule(
    *,
    start_date: str,
    total_amount: float,
    period_months: int,
) -> dict[str, Any]:
    months = max(1, int(period_months or 12))
    start_s = str(start_date or '')[:10]
    if not start_s:
        raise ValueError('Thiếu ngày bắt đầu hợp đồng')
    amounts = _split_monthly_amounts(total_amount, months)
    lines = []
    base = datetime.strptime(start_s, '%Y-%m-%d')
    for i, amt in enumerate(amounts):
        dt = base + relativedelta(months=i)
        lines.append({
            'period_year': dt.year,
            'period_month': dt.month,
            'recognize_date': dt.strftime('%Y-%m-%d'),
            'amount': amt,
        })
    return {
        'start_date': start_s,
        'period_months': months,
        'total_amount': _money(total_amount),
        'monthly_amount': amounts[0] if amounts else 0,
        'lines': lines,
    }


def get_deferred_contract(conn: sqlite3.Connection, contract_id: int) -> dict | None:
    ensure_deferred_revenue_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT c.*,
               p.name AS service_name, p.product_code AS service_code,
               v.voucher_no AS receipt_no
        FROM sme_deferred_revenue_contracts c
        JOIN products p ON p.id = c.service_product_id
        LEFT JOIN sme_vouchers v ON v.id = c.receipt_voucher_id
        WHERE c.id = ?
        """,
        (int(contract_id),),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    periods = conn.execute(
        """
        SELECT * FROM sme_deferred_revenue_periods
        WHERE contract_id = ?
        ORDER BY period_year, period_month
        """,
        (int(contract_id),),
    ).fetchall()
    data['periods'] = [dict(p) for p in periods]
    data['remain_amount'] = _money(
        _money(data.get('total_amount') or 0) - _money(data.get('recognized_amount') or 0)
    )
    return data


def list_deferred_contracts(
    conn: sqlite3.Connection,
    *,
    status: str = '',
    q: str = '',
    limit: int = 200,
) -> list[dict]:
    ensure_deferred_revenue_schema(conn)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT c.*,
               p.name AS service_name, p.product_code AS service_code,
               v.voucher_no AS receipt_no,
               (SELECT COUNT(*) FROM sme_deferred_revenue_periods pp
                WHERE pp.contract_id = c.id AND pp.status = 'posted') AS posted_periods,
               (SELECT COUNT(*) FROM sme_deferred_revenue_periods pp
                WHERE pp.contract_id = c.id) AS total_periods
        FROM sme_deferred_revenue_contracts c
        JOIN products p ON p.id = c.service_product_id
        LEFT JOIN sme_vouchers v ON v.id = c.receipt_voucher_id
        WHERE 1=1
    """
    params: list[Any] = []
    st = (status or '').strip().lower()
    if st and st != 'all':
        sql += " AND c.status = ?"
        params.append(st)
    if q:
        like = f"%{q.strip()}%"
        sql += " AND (c.contract_no LIKE ? OR c.customer_name LIKE ? OR p.name LIKE ?)"
        params.extend([like, like, like])
    sql += " ORDER BY c.start_date DESC, c.id DESC LIMIT ?"
    params.append(min(max(int(limit or 200), 1), 500))
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        r['remain_amount'] = _money(
            _money(r.get('total_amount') or 0) - _money(r.get('recognized_amount') or 0)
        )
    return rows


def create_deferred_contract(
    conn: sqlite3.Connection,
    *,
    service_product_id: int,
    customer_name: str,
    total_amount: float | None = None,
    start_date: str | None = None,
    period_months: int | None = None,
    customer_id: int | None = None,
    customer_tax_code: str = '',
    payment_method: str = 'bank',
    voucher_date: str | None = None,
    note: str = '',
    created_by: str = '',
    commit: bool = True,
) -> dict:
    """Lập HĐ gói năm: PT Nợ 112 / Có 3387 + lịch phân bổ DT."""
    from Services.sme.branches import resolve_posting_branch
    from Services.sme.vouchers import create_receipt

    ensure_sme_journal_ready(conn, commit=False)
    ensure_deferred_revenue_schema(conn)
    prod = get_deferred_product(conn, int(service_product_id))
    months = int(period_months or prod.get('deferred_months') or 12)
    if months <= 0:
        months = 12
    amt = _money(total_amount if total_amount is not None else prod.get('price') or 0)
    if amt <= 0:
        raise ValueError('Số tiền hợp đồng phải > 0')
    name = (customer_name or '').strip()
    if not name:
        raise ValueError('Thiếu tên khách hàng')
    start_s = (start_date or _today())[:10]
    date_s = (voucher_date or start_s)[:10]
    branch = resolve_posting_branch(conn, None)
    liability = resolve_postable_account(conn, '3387')
    revenue_acct = resolve_postable_account(
        conn, str(prod.get('revenue_account') or '5113').strip() or '5113',
    )
    contract_no = _next_contract_no(conn)
    monthly = _split_monthly_amounts(amt, months)[0]
    desc = (note or '').strip() or (
        f'Thu trước gói DV {prod.get("name")} — {contract_no}'
    )

    voucher = create_receipt(
        conn,
        voucher_date=date_s,
        party_name=name,
        amount=amt,
        payment_method=payment_method or 'bank',
        credit_account=liability,
        reason=desc,
        reference_document=contract_no,
        source_type='deferred_revenue',
        purpose='deferred_revenue',
        created_by=created_by,
        commit=False,
    )

    c = conn.cursor()
    c.execute(
        """
        INSERT INTO sme_deferred_revenue_contracts (
            contract_no, service_product_id, customer_id, customer_name,
            customer_tax_code, start_date, period_months, total_amount,
            monthly_amount, recognized_amount, receipt_voucher_id,
            receipt_journal_id, liability_account, revenue_account,
            status, note, created_by, created_at, updated_at, branch_code
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contract_no, int(service_product_id),
            int(customer_id) if customer_id else None,
            name, (customer_tax_code or '').strip() or None,
            start_s, months, amt, monthly,
            int(voucher.get('id') or 0),
            int(voucher.get('journal_entry_id') or 0),
            liability, revenue_acct, STATUS_ACTIVE,
            (note or '').strip() or None,
            (created_by or '').strip() or None, _now(), _now(), branch,
        ),
    )
    contract_id = int(c.lastrowid)
    schedule = preview_deferred_schedule(
        start_date=start_s, total_amount=amt, period_months=months,
    )
    for line in schedule['lines']:
        c.execute(
            """
            INSERT INTO sme_deferred_revenue_periods (
                contract_id, period_year, period_month, recognize_date,
                amount, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                contract_id, line['period_year'], line['period_month'],
                line['recognize_date'], line['amount'], PERIOD_PENDING,
            ),
        )
    if commit:
        sqlite_commit(conn, label='deferred_revenue')
    out = get_deferred_contract(conn, contract_id) or {}
    out['receipt_voucher'] = voucher
    return out


def _post_period_recognition(
    conn: sqlite3.Connection,
    *,
    contract: dict,
    period_row: dict,
    posting_date: str,
    created_by: str,
) -> dict:
    amt = _money(period_row.get('amount') or 0)
    if amt <= 0:
        raise ValueError('Số tiền phân bổ tháng phải > 0')
    liab = resolve_postable_account(
        conn, str(contract.get('liability_account') or '3387'),
    )
    rev = resolve_postable_account(
        conn, str(contract.get('revenue_account') or '5113'),
    )
    desc = (
        f"Ghi nhận DT tháng {period_row['period_month']:02d}/"
        f"{period_row['period_year']} — {contract.get('contract_no')}"
    )
    entry = post_journal_entry(
        conn,
        posting_date=posting_date[:10],
        document_date=posting_date[:10],
        document_type='PBDT',
        document_no=f"{contract.get('contract_no')}-{period_row['period_year']}{period_row['period_month']:02d}",
        document_id=int(period_row['id']),
        business_type='PHAN_BO_DT_3387',
        description=desc,
        reference_document=contract.get('contract_no'),
        created_by=created_by,
        branch_code=contract.get('branch_code'),
        lines=[
            {
                'sequence': 1,
                'account_code': liab,
                'debit': float(amt),
                'credit': 0,
                'description': desc,
            },
            {
                'sequence': 2,
                'account_code': rev,
                'debit': 0,
                'credit': float(amt),
                'description': desc,
            },
        ],
    )
    conn.execute(
        """
        UPDATE sme_deferred_revenue_periods
        SET status = ?, journal_entry_id = ?, posted_at = ?
        WHERE id = ?
        """,
        (PERIOD_POSTED, int(entry['id']), _now(), int(period_row['id'])),
    )
    new_rec = _money(_money(contract.get('recognized_amount') or 0) + amt)
    conn.execute(
        """
        UPDATE sme_deferred_revenue_contracts
        SET recognized_amount = ?, updated_at = ?,
            status = CASE
                WHEN ? >= total_amount - 0.01 THEN ?
                ELSE status
            END
        WHERE id = ?
        """,
        (
            new_rec, _now(), new_rec, STATUS_COMPLETED,
            int(contract['id']),
        ),
    )
    return {'journal_entry_id': entry['id'], 'amount': amt, 'period_id': period_row['id']}


def recognize_deferred_period(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    period: int,
    posting_date: str | None = None,
    created_by: str = '',
    commit: bool = True,
) -> dict[str, Any]:
    """Ghi nhận DT phát sinh trong tháng (Nợ 3387 / Có 5113) cho mọi kỳ pending."""
    ensure_deferred_revenue_schema(conn)
    ensure_sme_journal_ready(conn, commit=False)
    fy = int(fiscal_year)
    pm = int(period)
    if pm < 1 or pm > 12:
        raise ValueError('Tháng không hợp lệ')
    date_s = (posting_date or f'{fy:04d}-{pm:02d}-28')[:10]
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT p.*, c.contract_no, c.customer_name, c.liability_account,
               c.revenue_account, c.recognized_amount, c.total_amount,
               c.status AS contract_status, c.branch_code
        FROM sme_deferred_revenue_periods p
        JOIN sme_deferred_revenue_contracts c ON c.id = p.contract_id
        WHERE p.period_year = ? AND p.period_month = ?
          AND p.status = ?
          AND c.status = ?
        ORDER BY p.id
        """,
        (fy, pm, PERIOD_PENDING, STATUS_ACTIVE),
    ).fetchall()
    posted = []
    total = 0.0
    for r in rows:
        contract = {
            'id': r['contract_id'],
            'contract_no': r['contract_no'],
            'liability_account': r['liability_account'],
            'revenue_account': r['revenue_account'],
            'recognized_amount': r['recognized_amount'],
            'branch_code': r['branch_code'],
        }
        period_row = dict(r)
        res = _post_period_recognition(
            conn,
            contract=contract,
            period_row=period_row,
            posting_date=date_s,
            created_by=created_by,
        )
        posted.append({
            'contract_no': r['contract_no'],
            'customer_name': r['customer_name'],
            **res,
        })
        total = _money(total + res['amount'])
    if commit:
        sqlite_commit(conn, label='deferred_revenue')
    return {
        'fiscal_year': fy,
        'period': pm,
        'posted_count': len(posted),
        'posted_amount': total,
        'lines': posted,
    }


def recognize_deferred_period_by_id(
    conn: sqlite3.Connection,
    period_id: int,
    *,
    posting_date: str | None = None,
    created_by: str = '',
    commit: bool = True,
) -> dict:
    ensure_deferred_revenue_schema(conn)
    ensure_sme_journal_ready(conn, commit=False)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT p.*, c.contract_no, c.liability_account, c.revenue_account,
               c.recognized_amount, c.branch_code, c.status AS contract_status
        FROM sme_deferred_revenue_periods p
        JOIN sme_deferred_revenue_contracts c ON c.id = p.contract_id
        WHERE p.id = ?
        """,
        (int(period_id),),
    ).fetchone()
    if not row:
        raise ValueError('Không tìm thấy kỳ phân bổ')
    if row['status'] == PERIOD_POSTED:
        raise ValueError('Kỳ này đã ghi nhận doanh thu')
    if row['contract_status'] != STATUS_ACTIVE:
        raise ValueError('Hợp đồng không còn hiệu lực')
    date_s = (posting_date or row['recognize_date'] or _today())[:10]
    contract = {
        'id': row['contract_id'],
        'contract_no': row['contract_no'],
        'liability_account': row['liability_account'],
        'revenue_account': row['revenue_account'],
        'recognized_amount': row['recognized_amount'],
        'branch_code': row['branch_code'],
    }
    res = _post_period_recognition(
        conn,
        contract=contract,
        period_row=dict(row),
        posting_date=date_s,
        created_by=created_by,
    )
    if commit:
        sqlite_commit(conn, label='deferred_revenue')
    return res


def run_deferred_revenue_for_all_tenants(
    *,
    fiscal_year: int | None = None,
    period: int | None = None,
) -> dict[str, Any]:
    """Job lịch — ghi nhận DT hoãn tháng trước cho mọi tenant SME."""
    from datetime import timedelta
    from db_utils import get_main_db_connection, get_tenant_db_connection
    from Services.subscription_service import parse_tenant_settings
    from Services.tenant_profile import normalize_accounting_regime, resolve_features

    today = datetime.now()
    first = datetime(today.year, today.month, 1)
    prev = first - timedelta(days=1)
    fiscal_year = fiscal_year or prev.year
    period = period or prev.month

    main = get_main_db_connection()
    try:
        tenants = main.execute(
            "SELECT tenant_id, settings FROM tenants WHERE is_active = 1"
        ).fetchall()
    finally:
        main.close()

    results = []
    for row in tenants:
        tid = row['tenant_id']
        settings = parse_tenant_settings(row['settings'])
        regime = normalize_accounting_regime(
            settings.get('accounting_regime') if isinstance(settings, dict) else None,
        )
        if not str(regime).startswith('SME'):
            continue
        features = resolve_features(
            regime, (settings or {}).get('revenue_tier') or 'DT1', settings or {},
        )
        if not features.get('journal_posting'):
            continue
        conn = get_tenant_db_connection(tid)
        if not conn:
            continue
        try:
            out = recognize_deferred_period(
                conn,
                fiscal_year=fiscal_year,
                period=period,
                created_by='scheduler',
                commit=True,
            )
            if out.get('posted_count'):
                results.append({'tenant_id': tid, **out})
        except Exception as exc:
            results.append({'tenant_id': tid, 'error': str(exc)})
        finally:
            conn.close()
    return {
        'fiscal_year': fiscal_year,
        'period': period,
        'tenants': len(results),
        'results': results,
    }
