"""Tiêu chí áp dụng Kế toán SME TT58 — cảnh báo chuyển sang TT99.

Chỉ nên dùng TT58 khi đồng thời thỏa cả 3 điều kiện (NĐ 80/2021/NĐ-CP):
  - Doanh thu năm ≤ 10 tỷ đồng
  - Lao động tham gia BHXH bình quân năm ≤ 10 người
  - Vốn chủ sở hữu cuối năm ≤ 3 tỷ đồng

Vượt bất kỳ ngưỡng nào → cảnh báo chuyển ``SME_MICRO_TT58`` → ``SME_TT99``.
Không tự đổi chế độ (cần Master đổi ``accounting_regime``).
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

from Services.tenant_profile import (
    is_sme_regime,
    normalize_accounting_regime,
    update_registry_settings,
)

ALERT_KEY = 'micro_enterprise_tt99_alert'

SECTOR_AGRI_INDUSTRY = 'agri_industry'  # NLTS + CN&XD
SECTOR_TRADE_SERVICE = 'trade_service'  # TM&DV

MAX_BHXH_HEADCOUNT = 10
EQUITY_LIMIT = 3_000_000_000
REVENUE_LIMIT = 10_000_000_000
# Giữ alias cũ cho tương thích import/API nội bộ
CAPITAL_LIMIT = EQUITY_LIMIT
REVENUE_LIMIT_AGRI_INDUSTRY = REVENUE_LIMIT
REVENUE_LIMIT_TRADE_SERVICE = REVENUE_LIMIT

TT58_ELIGIBILITY_HINT = (
    'Kế toán SME (TT58) chỉ phù hợp khi: doanh thu năm ≤ 10 tỷ, '
    'lao động BHXH bình quân ≤ 10 người, vốn chủ sở hữu ≤ 3 tỷ. '
    'Vượt ngưỡng → chuyển sang Kế toán SME (TT99) theo quy định.'
)

SECTOR_LABELS = {
    SECTOR_AGRI_INDUSTRY: 'Nông–lâm–thủy sản / Công nghiệp–Xây dựng',
    SECTOR_TRADE_SERVICE: 'Thương mại–Dịch vụ',
}


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def normalize_enterprise_sector(value: str | None, default: str = SECTOR_TRADE_SERVICE) -> str:
    raw = str(value or '').strip().lower()
    if raw in (
        SECTOR_AGRI_INDUSTRY, 'agri', 'agriculture', 'industry', 'construction',
        'nlts', 'cnxd', 'nong_lam', 'cong_nghiep',
    ):
        return SECTOR_AGRI_INDUSTRY
    if raw in (
        SECTOR_TRADE_SERVICE, 'trade', 'service', 'services', 'commerce',
        'tmdv', 'thuong_mai', 'dich_vu', 'pos', 'fb_service', 'fb',
    ):
        return SECTOR_TRADE_SERVICE
    # Map từ business_line phổ biến
    if raw in ('manufacturing', 'production', 'farm', 'construction'):
        return SECTOR_AGRI_INDUSTRY
    return default if default in (SECTOR_AGRI_INDUSTRY, SECTOR_TRADE_SERVICE) else SECTOR_TRADE_SERVICE


def resolve_enterprise_sector(settings: dict | None) -> str:
    settings = settings or {}
    return normalize_enterprise_sector(
        settings.get('enterprise_sector')
        or settings.get('sme_enterprise_sector')
        or settings.get('business_line'),
        default=SECTOR_TRADE_SERVICE,
    )


def average_bhxh_headcount(conn: sqlite3.Connection, fiscal_year: int) -> dict[str, Any]:
    """
    Bình quân năm số LĐ có tham gia BHXH.
    Ưu tiên salary_detail (bhxh > 0); fallback employees đang active.
    """
    year = int(fiscal_year)
    monthly: list[int] = []
    source = 'none'

    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except sqlite3.Error:
        tables = set()

    if 'salary_detail' in tables:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(salary_detail)').fetchall()}
        if {'employee_id', 'month', 'year'}.issubset(cols):
            bhxh_filter = 'AND COALESCE(bhxh, 0) > 0' if 'bhxh' in cols else ''
            rows = conn.execute(
                f"""
                SELECT month, COUNT(DISTINCT employee_id) AS n
                FROM salary_detail
                WHERE year = ? {bhxh_filter}
                GROUP BY month
                """,
                (year,),
            ).fetchall()
            by_m = {int(r[0]): int(r[1] or 0) for r in rows}
            if by_m:
                # Đủ 12 tháng: tháng không có bảng lương = 0; hoặc trung bình các tháng có dữ liệu
                monthly = [by_m.get(m, 0) for m in range(1, 13)]
                source = 'salary_detail'

    if not monthly and 'employees' in tables:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(employees)').fetchall()}
        where = '1=1'
        if 'status' in cols:
            where = 'COALESCE(status, 1) = 1'
        elif 'is_active' in cols:
            where = 'COALESCE(is_active, 1) = 1'
        n = conn.execute(f'SELECT COUNT(*) FROM employees WHERE {where}').fetchone()
        count = int(n[0] or 0) if n else 0
        monthly = [count] * 12
        source = 'employees_active'

    if not monthly:
        return {
            'average': 0.0,
            'months': [],
            'source': 'none',
            'manual': False,
        }

    avg = sum(monthly) / 12.0
    return {
        'average': round(avg, 2),
        'months': monthly,
        'source': source,
        'manual': False,
    }


def year_owner_equity(conn: sqlite3.Connection, fiscal_year: int) -> float:
    """Vốn chủ sở hữu cuối năm (B01 mã 400; fallback cộng 411+421)."""
    from Services.sme.bctc_report import balance_sheet

    bs = balance_sheet(conn, fiscal_year=fiscal_year, period_to=12)
    rows = bs.get('rows') or []
    for row in rows:
        if str(row.get('code') or '').strip() == '400':
            return max(float(row.get('amount') or 0), 0.0)

    # Fallback: cộng trực tiếp các tài khoản vốn CSH
    try:
        from Services.sme.dashboard_metrics import _closing_balances, _sum_balance
        from decimal import Decimal

        bals = _closing_balances(conn, fiscal_year, 12)
        equity = (
            _sum_balance(bals, ('411',), normal='credit')
            + _sum_balance(bals, ('421',), normal='credit')
            + _sum_balance(bals, ('412', '413', '418', '422'), normal='credit')
            - _sum_balance(bals, ('419',), normal='debit')
        )
        return max(float(equity or Decimal('0')), 0.0)
    except Exception:
        return 0.0


def year_total_capital(conn: sqlite3.Connection, fiscal_year: int) -> float:
    """Alias — trả về vốn chủ sở hữu (mã 400)."""
    return year_owner_equity(conn, fiscal_year)


def evaluate_micro_criteria(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int,
    settings: dict | None = None,
) -> dict[str, Any]:
    """Đánh giá tenant còn đủ điều kiện dùng TT58 hay không."""
    from Services.sme.vat_filing_alert import compute_year_sales_revenue

    settings = settings or {}
    sector = resolve_enterprise_sector(settings)
    revenue_limit = REVENUE_LIMIT
    equity_limit = EQUITY_LIMIT

    # Cho phép ghi đè thủ công số LĐ BHXH bình quân
    manual_head = settings.get('avg_bhxh_headcount')
    head_info = average_bhxh_headcount(conn, fiscal_year)
    if manual_head is not None and str(manual_head).strip() != '':
        try:
            head_info = {
                'average': float(manual_head),
                'months': head_info.get('months') or [],
                'source': 'manual',
                'manual': True,
            }
        except (TypeError, ValueError):
            pass

    revenue = compute_year_sales_revenue(conn, fiscal_year)
    owner_equity = year_owner_equity(conn, fiscal_year)
    avg_head = float(head_info.get('average') or 0)

    labor_ok = avg_head <= MAX_BHXH_HEADCOUNT + 1e-9
    revenue_ok = revenue <= float(revenue_limit) + 1e-6
    equity_ok = owner_equity <= float(equity_limit) + 1e-6
    is_micro = bool(labor_ok and revenue_ok and equity_ok)

    fail_reasons: list[str] = []
    if not revenue_ok:
        fail_reasons.append(
            f'Doanh thu năm {revenue:,.0f} đ vượt ngưỡng {revenue_limit:,.0f} đ '
            f'(> 10 tỷ) — cần chuyển Kế toán SME (TT99)'
        )
    if not labor_ok:
        fail_reasons.append(
            f'Lao động BHXH bình quân năm = {avg_head:g} (> {MAX_BHXH_HEADCOUNT} người) '
            f'— cần chuyển Kế toán SME (TT99)'
        )
    if not equity_ok:
        fail_reasons.append(
            f'Vốn chủ sở hữu {owner_equity:,.0f} đ vượt ngưỡng {equity_limit:,.0f} đ '
            f'(> 3 tỷ) — cần chuyển Kế toán SME (TT99)'
        )

    return {
        'fiscal_year': int(fiscal_year),
        'sector': sector,
        'sector_label': SECTOR_LABELS.get(sector, sector),
        'avg_bhxh_headcount': avg_head,
        'headcount_source': head_info.get('source'),
        'revenue': round(revenue, 2),
        'owner_equity': round(owner_equity, 2),
        'capital': round(owner_equity, 2),
        'limits': {
            'max_bhxh_headcount': MAX_BHXH_HEADCOUNT,
            'revenue_limit': revenue_limit,
            'equity_limit': equity_limit,
            'capital_limit': equity_limit,
        },
        'labor_ok': labor_ok,
        'revenue_ok': revenue_ok,
        'equity_ok': equity_ok,
        'capital_ok': equity_ok,
        'finance_ok': revenue_ok and equity_ok,
        'is_micro_enterprise': is_micro,
        'tt58_eligible': is_micro,
        'fail_reasons': fail_reasons,
    }


def get_tt99_switch_alert(settings: dict | None) -> dict[str, Any] | None:
    settings = settings or {}
    regime = normalize_accounting_regime(settings.get('accounting_regime'))
    if regime != 'SME_MICRO_TT58':
        return None
    alert = settings.get(ALERT_KEY)
    if not isinstance(alert, dict) or not alert.get('active'):
        return None
    return dict(alert)


def evaluate_tt58_to_tt99_alert(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    fiscal_year: int,
    settings: dict | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Nếu TT58 mà không còn siêu nhỏ → tạo cảnh báo chuyển TT99."""
    settings = dict(settings or {})
    regime = normalize_accounting_regime(settings.get('accounting_regime'))
    result: dict[str, Any] = {
        'regime': regime,
        'fiscal_year': int(fiscal_year),
        'needs_tt99': False,
        'alert': None,
        'criteria': None,
        'persisted': False,
    }
    if regime != 'SME_MICRO_TT58':
        result['message'] = 'Không áp dụng — tenant không dùng TT58 siêu nhỏ.'
        # Gỡ cảnh báo cũ nếu đã chuyển TT99
        old = settings.get(ALERT_KEY)
        if persist and tenant_id and isinstance(old, dict) and old.get('active'):
            update_registry_settings(tenant_id, {
                ALERT_KEY: {**old, 'active': False, 'status': 'cleared_regime_changed', 'cleared_at': _now()},
            })
            result['persisted'] = True
        return result

    criteria = evaluate_micro_criteria(conn, fiscal_year=fiscal_year, settings=settings)
    result['criteria'] = criteria

    if criteria['is_micro_enterprise']:
        old = settings.get(ALERT_KEY)
        if persist and tenant_id and isinstance(old, dict) and old.get('active'):
            update_registry_settings(tenant_id, {
                ALERT_KEY: {
                    **old,
                    'active': False,
                    'status': 'cleared_still_micro',
                    'cleared_at': _now(),
                    'criteria': criteria,
                },
            })
            result['persisted'] = True
        result['message'] = (
            f'Năm {fiscal_year}: vẫn đủ điều kiện Kế toán SME (TT58) '
            f'(DT ≤ 10 tỷ, BHXH ≤ 10 người, vốn CSH ≤ 3 tỷ).'
        )
        return result

    reasons = '; '.join(criteria['fail_reasons']) or 'vượt ngưỡng TT58'
    alert = {
        'active': True,
        'status': 'pending',
        'source_year': int(fiscal_year),
        'required_regime': 'SME_TT99',
        'current_regime': 'SME_MICRO_TT58',
        'criteria': criteria,
        'created_at': _now(),
        'message': (
            f'Năm {fiscal_year}: đơn vị không còn đủ điều kiện Kế toán SME (TT58) '
            f'({reasons}). Theo quy định, vui lòng chuyển sang '
            f'Kế toán SME (TT99). Liên hệ quản trị Master đổi '
            f'«SME_MICRO_TT58» → «SME_TT99»; hệ thống sẽ tự đồng bộ COA/quy tắc '
            f'và kiểm tra toàn vẹn số liệu theo TT99.'
        ),
    }
    prev = settings.get(ALERT_KEY) if isinstance(settings.get(ALERT_KEY), dict) else {}
    if int(prev.get('source_year') or 0) == int(fiscal_year) and prev.get('created_at'):
        alert['created_at'] = prev['created_at']
        alert['updated_at'] = _now()

    result['needs_tt99'] = True
    result['alert'] = alert
    result['message'] = alert['message']
    if persist and tenant_id:
        ok = update_registry_settings(tenant_id, {ALERT_KEY: alert})
        result['persisted'] = bool(ok)
    return result


def evaluate_tt58_setup_warning(
    conn: sqlite3.Connection,
    *,
    fiscal_year: int | None = None,
    settings: dict | None = None,
) -> dict[str, Any]:
    """Cảnh báo khi Master chọn/cấu hình TT58 nhưng số liệu vượt ngưỡng."""
    year = int(fiscal_year or date.today().year)
    if date.today().month == 1:
        year = date.today().year - 1
    criteria = evaluate_micro_criteria(conn, fiscal_year=year, settings=settings)
    eligible = bool(criteria.get('tt58_eligible'))
    return {
        'eligible': eligible,
        'fiscal_year': year,
        'criteria': criteria,
        'hint': TT58_ELIGIBILITY_HINT,
        'message': (
            TT58_ELIGIBILITY_HINT
            if eligible
            else (
                'Cảnh báo: số liệu hiện tại không đủ điều kiện TT58 — '
                + '; '.join(criteria.get('fail_reasons') or [])
                + '. Nên chọn Kế toán SME (TT99).'
            )
        ),
    }


def clear_tt99_switch_alert_if_regime_ok(settings: dict | None, tenant_id: str | None) -> bool:
    """Gọi sau khi Master đổi sang TT99."""
    settings = settings or {}
    if normalize_accounting_regime(settings.get('accounting_regime')) == 'SME_MICRO_TT58':
        return False
    alert = settings.get(ALERT_KEY)
    if not isinstance(alert, dict) or not alert.get('active'):
        return False
    if not tenant_id:
        return False
    return bool(update_registry_settings(tenant_id, {
        ALERT_KEY: {
            **alert,
            'active': False,
            'status': 'resolved_switched_tt99',
            'cleared_at': _now(),
        },
    }))
