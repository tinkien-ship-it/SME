"""Cảnh báo / tự chuyển kỳ kê khai GTGT khi doanh thu năm > 50 tỷ.

Luồng:
1. Cuối năm N (sau chạy T12 / KC cuối năm): nếu DT năm N > 50 tỷ → ghi cảnh báo
   bắt buộc kê khai **tháng** từ năm N+1.
2. Cảnh báo hiển thị đến khi tenant đã thiết lập kỳ = tháng (+ ngưỡng > 50 tỷ).
3. Sang năm N+1 (lịch 01/01 hoặc lần đầu vào SME): nếu vẫn còn quý →
   hệ thống **tự chuyển** sang tháng, ghi audit, đánh dấu auto_applied.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

from Services.tenant_profile import (
    VAT_MONTHLY_REVENUE_THRESHOLD,
    is_sme_regime,
    normalize_accounting_regime,
    normalize_vat_filing_period,
    resolve_vat_filing_policy,
    update_registry_settings,
)

ALERT_KEY = 'vat_filing_year_end_alert'


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def compute_year_sales_revenue(conn: sqlite3.Connection, fiscal_year: int) -> float:
    """Doanh thu bán hàng & cung cấp DV năm tài chính (B02 mã 01, fallback mã 10)."""
    from Services.sme.bctc_report import income_statement

    rep = income_statement(conn, fiscal_year=fiscal_year, period_from=1, period_to=12)
    by_code = {
        r['code']: float(r['amount'] or 0)
        for r in (rep.get('rows') or [])
        if r.get('amount') is not None
    }
    gross = float(by_code.get('01') or 0)
    net = float((rep.get('totals') or {}).get('revenue_net') or by_code.get('10') or 0)
    return max(gross, net, 0.0)


def _alert_resolved(settings: dict, alert: dict | None = None) -> bool:
    """Đã thiết lập kê khai tháng (và vượt ngưỡng) → hết cảnh báo."""
    policy = resolve_vat_filing_policy(settings.get('accounting_regime'), settings)
    period = normalize_vat_filing_period(
        settings.get('vat_filing_period') or settings.get('filing_period'),
        default=policy['default_period'],
    )
    if period != 'monthly':
        return False
    # Phải gắn ngưỡng > 50 tỷ để không bị clamp lại quý
    if policy['must_monthly'] or policy['revenue_over_50b']:
        return True
    alert = alert or settings.get(ALERT_KEY) or {}
    if alert.get('auto_applied_at') or alert.get('user_confirmed_at'):
        return True
    return False


def get_vat_filing_alert(settings: dict | None) -> dict[str, Any] | None:
    settings = settings or {}
    alert = settings.get(ALERT_KEY)
    if not isinstance(alert, dict) or not alert.get('active'):
        return None
    if _alert_resolved(settings, alert):
        return None
    return dict(alert)


def evaluate_year_end_vat_filing(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    fiscal_year: int,
    settings: dict | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """
    Đánh giá DT năm ``fiscal_year``. Nếu > 50 tỷ → tạo/ cập nhật cảnh báo cho năm sau.
    """
    settings = dict(settings or {})
    revenue = compute_year_sales_revenue(conn, fiscal_year)
    over = revenue > float(VAT_MONTHLY_REVENUE_THRESHOLD)
    effective_year = int(fiscal_year) + 1
    result: dict[str, Any] = {
        'fiscal_year': int(fiscal_year),
        'effective_year': effective_year,
        'revenue': round(revenue, 2),
        'threshold': VAT_MONTHLY_REVENUE_THRESHOLD,
        'over_threshold': over,
        'alert': None,
        'persisted': False,
    }
    if not over:
        old = settings.get(ALERT_KEY)
        if (
            persist and tenant_id
            and isinstance(old, dict)
            and int(old.get('source_year') or 0) == int(fiscal_year)
            and old.get('active')
        ):
            update_registry_settings(tenant_id, {
                ALERT_KEY: {
                    **old,
                    'active': False,
                    'status': 'cleared_under_threshold',
                    'cleared_at': _now(),
                },
            })
        result['message'] = (
            f'Doanh thu năm {fiscal_year} = {revenue:,.0f} đ ≤ 50 tỷ — '
            f'giữ kê khai theo quý cho năm {effective_year}.'
        )
        return result

    alert = {
        'active': True,
        'status': 'pending',
        'source_year': int(fiscal_year),
        'effective_year': effective_year,
        'revenue': round(revenue, 2),
        'threshold': VAT_MONTHLY_REVENUE_THRESHOLD,
        'required_period': 'monthly',
        'created_at': _now(),
        'message': (
            f'Doanh thu năm {fiscal_year} đạt {revenue:,.0f} đ (> 50 tỷ). '
            f'Từ năm {effective_year} đơn vị phải kê khai GTGT theo tháng. '
            f'Vui lòng vào Thuế & NSNN chọn «> 50 tỷ / theo tháng». '
            f'Nếu không đổi, hệ thống sẽ tự chuyển sang tháng khi sang năm mới.'
        ),
    }
    # Giữ mốc xác nhận cũ nếu cùng source_year
    prev = settings.get(ALERT_KEY) if isinstance(settings.get(ALERT_KEY), dict) else {}
    if int(prev.get('source_year') or 0) == int(fiscal_year):
        for k in ('user_confirmed_at', 'auto_applied_at', 'created_at'):
            if prev.get(k):
                alert[k] = prev[k]
        if prev.get('created_at'):
            alert['created_at'] = prev['created_at']
        alert['updated_at'] = _now()

    result['alert'] = alert
    result['message'] = alert['message']

    if persist and tenant_id:
        patch = {
            ALERT_KEY: alert,
            # Ghi nhận DT năm nguồn để chính sách 50 tỷ hoạt động ngay
            'prior_year_revenue': round(revenue, 2),
            'vat_revenue_over_50b': True,
        }
        ok = update_registry_settings(tenant_id, patch)
        result['persisted'] = bool(ok)
    return result


def confirm_vat_filing_monthly(
    *,
    tenant_id: str,
    settings: dict | None = None,
    confirmed_by: str | None = None,
) -> dict[str, Any]:
    """User đã chọn kê khai tháng — đánh dấu hết cảnh báo."""
    settings = dict(settings or {})
    alert = settings.get(ALERT_KEY) if isinstance(settings.get(ALERT_KEY), dict) else {}
    revenue = float(
        alert.get('revenue')
        or settings.get('prior_year_revenue')
        or (VAT_MONTHLY_REVENUE_THRESHOLD + 1)
    )
    patch = {
        'vat_filing_period': 'monthly',
        'filing_period': 'monthly',
        'prior_year_revenue': revenue,
        'vat_revenue_over_50b': True,
        ALERT_KEY: {
            **alert,
            'active': False,
            'status': 'resolved_user',
            'required_period': 'monthly',
            'user_confirmed_at': _now(),
            'confirmed_by': confirmed_by,
            'message': alert.get('message') or 'Đã xác nhận kê khai theo tháng.',
        },
    }
    ok = update_registry_settings(tenant_id, patch)
    return {'success': bool(ok), 'vat_filing_period': 'monthly', 'alert_cleared': True}


def auto_apply_monthly_filing_if_due(
    *,
    tenant_id: str,
    settings: dict | None = None,
    today: date | None = None,
    applied_by: str = 'system',
) -> dict[str, Any]:
    """
    Nếu đã sang năm hiệu lực của cảnh báo mà vẫn kê khai quý → tự chuyển tháng.
    """
    today = today or date.today()
    settings = dict(settings or {})
    alert = settings.get(ALERT_KEY) if isinstance(settings.get(ALERT_KEY), dict) else None
    if not alert or not alert.get('active'):
        return {'applied': False, 'reason': 'no_active_alert'}

    effective_year = int(alert.get('effective_year') or 0)
    if today.year < effective_year:
        return {
            'applied': False,
            'reason': 'not_yet_effective',
            'effective_year': effective_year,
            'alert': get_vat_filing_alert(settings),
        }

    if _alert_resolved(settings, alert):
        # Đánh dấu inactive nếu còn active
        if alert.get('active'):
            update_registry_settings(tenant_id, {
                ALERT_KEY: {**alert, 'active': False, 'status': 'resolved'},
            })
        return {'applied': False, 'reason': 'already_monthly'}

    revenue = float(alert.get('revenue') or VAT_MONTHLY_REVENUE_THRESHOLD + 1)
    new_alert = {
        **alert,
        'active': False,
        'status': 'auto_applied',
        'auto_applied_at': _now(),
        'applied_by': applied_by,
        'message': (
            f'Hệ thống đã tự chuyển kỳ kê khai GTGT sang tháng từ năm {effective_year} '
            f'(doanh thu năm {alert.get("source_year")} = {revenue:,.0f} đ > 50 tỷ).'
        ),
    }
    patch = {
        'vat_filing_period': 'monthly',
        'filing_period': 'monthly',
        'prior_year_revenue': revenue,
        'vat_revenue_over_50b': True,
        ALERT_KEY: new_alert,
    }
    ok = update_registry_settings(tenant_id, patch)
    try:
        from Services.audit_log import write_audit
        write_audit(
            'update',
            'sme_vat_filing',
            new_alert['message'],
            entity_type='vat_filing_period',
            entity_id=tenant_id,
            old_data={'vat_filing_period': settings.get('vat_filing_period')},
            new_data=patch,
            username=applied_by,
        )
    except Exception:
        pass
    return {
        'applied': bool(ok),
        'vat_filing_period': 'monthly',
        'alert': new_alert,
        'message': new_alert['message'],
    }


def sync_vat_filing_alert_for_tenant(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    settings: dict | None = None,
    fiscal_year: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Gọi cuối năm + khi vào app: đánh giá DT / tự apply nếu đến hạn."""
    today = today or date.today()
    settings = dict(settings or {})
    regime = normalize_accounting_regime(settings.get('accounting_regime'))
    if not is_sme_regime(regime):
        return {'skipped': True, 'reason': 'not_sme'}

    # 1) Tự apply nếu đã sang năm mới
    applied = auto_apply_monthly_filing_if_due(
        tenant_id=tenant_id, settings=settings, today=today,
    )
    if applied.get('applied'):
        return {'phase': 'auto_apply', **applied}

    # 2) Cuối năm / đã có đủ 12 tháng: đánh giá lại DT năm vừa kết thúc
    year = int(fiscal_year or (today.year - 1 if today.month == 1 else today.year))
    # Chỉ evaluate năm đã kết thúc hoặc đang T12
    if today.month == 12 or today.year > year:
        ev = evaluate_year_end_vat_filing(
            conn, tenant_id=tenant_id, fiscal_year=year, settings=settings, persist=True,
        )
        return {'phase': 'evaluate', **ev}

    return {
        'phase': 'idle',
        'alert': get_vat_filing_alert(settings),
        'applied': False,
    }


def run_vat_filing_alerts_for_all_tenants(*, today: date | None = None) -> dict[str, Any]:
    """Job lịch: đánh giá / tự chuyển kỳ kê khai cho mọi tenant SME."""
    from db_utils import get_main_db_connection, get_tenant_db_connection
    from Services.subscription_service import parse_tenant_settings as _parse

    today = today or date.today()
    main = get_main_db_connection()
    try:
        rows = main.execute(
            "SELECT tenant_id, settings FROM tenants WHERE is_active = 1"
        ).fetchall()
    finally:
        main.close()

    results = []
    for row in rows:
        tid = row['tenant_id'] if hasattr(row, 'keys') else row[0]
        raw = row['settings'] if hasattr(row, 'keys') else row[1]
        settings = _parse(raw) if not isinstance(raw, dict) else raw
        if not isinstance(settings, dict):
            settings = {}
        if not is_sme_regime(settings.get('accounting_regime')):
            continue
        conn = get_tenant_db_connection(tid)
        if not conn:
            continue
        try:
            out = sync_vat_filing_alert_for_tenant(
                conn, tenant_id=tid, settings=settings, today=today,
            )
            results.append({'tenant_id': tid, **{k: out[k] for k in out if k != 'alert' or out.get('alert')}})
        except Exception as exc:
            results.append({'tenant_id': tid, 'error': str(exc)})
        finally:
            conn.close()
    return {'today': today.isoformat(), 'tenants': len(results), 'results': results}
