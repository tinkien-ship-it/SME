"""Nền tảng cấu hình tenant — chế độ kế toán, nhóm doanh thu R1–R4, feature flags."""
from __future__ import annotations

import sqlite3
from functools import wraps

from flask import g, jsonify, redirect, request, url_for, flash

from Services.subscription_service import parse_tenant_settings

# ---------------------------------------------------------------------------
# Chế độ kế toán (SME triển khai sau)
# ---------------------------------------------------------------------------
ACCOUNTING_REGIMES = {
    'HKD': {
        'code': 'HKD',
        'label': 'Hộ kinh doanh / cá thể',
        'active': True,
        'coming_soon': False,
    },
    'SME_MICRO_TT58': {
        'code': 'SME_MICRO_TT58',
        'label': 'Doanh nghiệp siêu nhỏ (TT58 / VAS)',
        'active': False,
        'coming_soon': True,
    },
    'SME_TT99': {
        'code': 'SME_TT99',
        'label': 'Doanh nghiệp lớn (TT99 / VAS)',
        'active': False,
        'coming_soon': True,
    },
}

# ---------------------------------------------------------------------------
# Nhóm doanh thu pháp lý HKD — DT1–DT4 (alias legacy R1–R4)
# ---------------------------------------------------------------------------
REVENUE_TIERS = {
    'DT1': {
        'code': 'DT1',
        'label': 'DT1 — Doanh thu ≤ 1 tỷ/năm',
        'short_label': '≤ 1 tỷ',
        'revenue_min': 0,
        'revenue_max': 1_000_000_000,
        'legacy_group': '1',
        'filing_period': 'quarterly',
        'tncn_on_profit': False,
    },
    'DT2': {
        'code': 'DT2',
        'label': 'DT2 — Doanh thu 1–3 tỷ/năm',
        'short_label': '1–3 tỷ',
        'revenue_min': 1_000_000_000,
        'revenue_max': 3_000_000_000,
        'legacy_group': '2',
        'filing_period': 'quarterly',
        'tncn_on_profit': False,
    },
    'DT3': {
        'code': 'DT3',
        'label': 'DT3 — Doanh thu 3–50 tỷ/năm',
        'short_label': '3–50 tỷ',
        'revenue_min': 3_000_000_000,
        'revenue_max': 50_000_000_000,
        'legacy_group': '3',
        'filing_period': 'quarterly',
        'tncn_on_profit': True,
        'tncn_rate': 0.17,
    },
    'DT4': {
        'code': 'DT4',
        'label': 'DT4 — Doanh thu > 50 tỷ/năm',
        'short_label': '> 50 tỷ',
        'revenue_min': 50_000_000_000,
        'revenue_max': None,
        'legacy_group': '3',
        'filing_period': 'monthly',
        'tncn_on_profit': True,
        'tncn_rate': 0.20,
    },
}

LEGACY_R_TO_DT = {'R1': 'DT1', 'R2': 'DT2', 'R3': 'DT3', 'R4': 'DT4'}
LEGACY_GROUP_TO_TIER = {'1': 'DT1', '2': 'DT2', '3': 'DT3'}
TIER_ORDER = ('DT1', 'DT2', 'DT3', 'DT4')

PLAN_TO_TIER = {
    'trial': 'DT1',
    'DV001': 'DT1',
    'DV002': 'DT1',
    'DV003': 'DT2',
    'DV004': 'DT3',
}

TIER_BASE_FEATURES = {
    'DT1': {
        'ledger_profile': 'minimal',
        'filing_period': 'quarterly',
        'nsnn_s4': True,
        'tax_debt_summary': True,
        'profit_report_s2c': True,
        'monthly_vat_filing': False,
        'einvoice_required': False,
    },
    'DT2': {
        'ledger_profile': 'standard',
        'filing_period': 'quarterly',
        'nsnn_s4': True,
        'tax_debt_summary': True,
        'profit_report_s2c': True,
        'monthly_vat_filing': False,
        'einvoice_required': False,
    },
    'DT3': {
        'ledger_profile': 'full',
        'filing_period': 'quarterly',
        'nsnn_s4': True,
        'tax_debt_summary': True,
        'profit_report_s2c': True,
        'monthly_vat_filing': False,
        'einvoice_required': True,
    },
    'DT4': {
        'ledger_profile': 'full',
        'filing_period': 'monthly',
        'nsnn_s4': True,
        'tax_debt_summary': True,
        'profit_report_s2c': True,
        'monthly_vat_filing': True,
        'einvoice_required': True,
    },
}

HKD_MENU_FEATURE_MAP = {
    'SoTheoDoiNSNN': 'nsnn_s4',
    'tax_report': 'nsnn_s4',
    'SoCongNoThueNSNN': 'tax_debt_summary',
    'SoChiTietDoanhThu_ChiPhi_S2c': 'profit_report_s2c',
}


def normalize_accounting_regime(value, default='HKD'):
    code = (value or default).strip().upper()
    return code if code in ACCOUNTING_REGIMES else default


def normalize_revenue_tier(value, default='DT1'):
    if value is None:
        return default
    raw = str(value).strip().upper()
    if raw in REVENUE_TIERS:
        return raw
    if raw in LEGACY_R_TO_DT:
        return LEGACY_R_TO_DT[raw]
    if raw in LEGACY_GROUP_TO_TIER:
        return LEGACY_GROUP_TO_TIER[raw]
    if raw.isdigit() and raw in LEGACY_GROUP_TO_TIER:
        return LEGACY_GROUP_TO_TIER[raw]
    return default


def tier_from_subscription_plan(plan_code, default='DT1'):
    code = (plan_code or '').strip().upper()
    if not code:
        return default
    return PLAN_TO_TIER.get(code, default)


def revenue_tier_to_legacy_group(tier):
    """Map R1–R4 → nhóm S4 cũ (1–3) để tương thích."""
    tier = normalize_revenue_tier(tier)
    return REVENUE_TIERS[tier]['legacy_group']


def legacy_group_to_revenue_tier(group):
    return normalize_revenue_tier(group, default='DT3')


def infer_enabled_nn_sectors(settings, business_line='pos'):
    from Services.hkd_sector import (
        default_nn_sectors_for_business_line,
        normalize_enabled_nn_sectors,
        normalize_nn_code,
    )

    settings = settings or {}
    if settings.get('enabled_nn_sectors'):
        return normalize_enabled_nn_sectors(settings['enabled_nn_sectors'])
    if settings.get('enabled_hkd_sectors'):
        return normalize_enabled_nn_sectors(settings['enabled_hkd_sectors'])
    legacy = settings.get('default_hkd_sector') or settings.get('primary_nn_sector')
    if legacy:
        return normalize_enabled_nn_sectors([legacy])
    bl = settings.get('business_line') or business_line
    return default_nn_sectors_for_business_line(bl)


def infer_revenue_tier(settings):
    """Suy luận DT từ settings registry (tenant cũ R1/G1 vẫn đọc được)."""
    if not settings:
        return 'DT3'
    for key in ('revenue_tier_effective', 'revenue_tier', 'revenue_tier_declared'):
        if settings.get(key):
            return normalize_revenue_tier(settings[key])
    if settings.get('legacy_business_group'):
        return legacy_group_to_revenue_tier(settings['legacy_business_group'])
    if settings.get('plan'):
        return tier_from_subscription_plan(settings.get('plan'))
    return 'DT3'


def build_tenant_settings(
    *,
    business_line='pos',
    hkd_sector='NN1',
    enabled_nn_sectors=None,
    revenue_tier='DT1',
    accounting_regime='HKD',
    subscription_plan='',
    onboarding_completed=False,
    extra=None,
):
    from Services.hkd_sector import normalize_enabled_nn_sectors, normalize_nn_code, nn_to_storage_code

    regime = normalize_accounting_regime(accounting_regime)
    tier = normalize_revenue_tier(revenue_tier)
    bl = (business_line or 'pos').strip()
    nn_list = normalize_enabled_nn_sectors(
        enabled_nn_sectors or [hkd_sector],
        default=infer_enabled_nn_sectors({'business_line': bl}, bl),
    )
    primary = normalize_nn_code(hkd_sector or nn_list[0])

    plan = (subscription_plan or '').strip().lower()
    if not plan and tier:
        for p, t in PLAN_TO_TIER.items():
            if t == tier and p != 'trial':
                plan = p.lower()
                break

    settings = {
        'accounting_regime': regime,
        'revenue_tier': tier,
        'revenue_tier_declared': tier,
        'revenue_tier_effective': tier,
        'business_line': bl,
        'enabled_nn_sectors': nn_list,
        'primary_nn_sector': primary,
        'default_hkd_sector': nn_to_storage_code(primary),
        'onboarding_completed': bool(onboarding_completed),
        'filing_period': REVENUE_TIERS[tier]['filing_period'],
        'features': resolve_features(regime, tier, {}),
    }
    if plan:
        settings['plan'] = plan
    if extra:
        settings.update(extra)
    return settings


def resolve_features(accounting_regime, revenue_tier, settings=None):
    regime = normalize_accounting_regime(accounting_regime)
    if regime != 'HKD' or not ACCOUNTING_REGIMES[regime]['active']:
        return {
            'accounting_enabled': False,
            'regime_coming_soon': ACCOUNTING_REGIMES.get(regime, {}).get('coming_soon', True),
            'ledger_profile': 'disabled',
            'filing_period': None,
            'nsnn_s4': False,
            'tax_debt_summary': False,
            'profit_report_s2c': False,
            'monthly_vat_filing': False,
            'einvoice_required': False,
        }

    tier = normalize_revenue_tier(revenue_tier)
    features = dict(TIER_BASE_FEATURES.get(tier, TIER_BASE_FEATURES['DT1']))
    features['accounting_enabled'] = True
    features['regime_coming_soon'] = False
    features['revenue_tier'] = tier

    settings = settings or {}
    plan = (settings.get('plan') or '').upper()
    if plan in ('DV002', 'DV003', 'DV004'):
        features['einvoice_enabled'] = True
    elif plan == 'DV001':
        features['einvoice_enabled'] = False
    else:
        features['einvoice_enabled'] = bool(settings.get('einvoice_enabled', features.get('einvoice_required')))

    if settings.get('features') and isinstance(settings['features'], dict):
        features.update({k: v for k, v in settings['features'].items() if k in features or k.startswith('custom_')})
    return features


def build_profile_from_registry(registry_row):
    """Gộp registry tenants.settings → profile dùng trong request."""
    if not registry_row:
        return _empty_profile()
    settings = parse_tenant_settings(registry_row.get('settings'))
    regime = normalize_accounting_regime(settings.get('accounting_regime'))
    tier = infer_revenue_tier(settings)
    features = resolve_features(regime, tier, settings)
    tier_meta = REVENUE_TIERS.get(tier, REVENUE_TIERS['DT1'])
    regime_meta = ACCOUNTING_REGIMES.get(regime, ACCOUNTING_REGIMES['HKD'])
    nn_sectors = infer_enabled_nn_sectors(settings, settings.get('business_line'))
    from Services.hkd_sector import normalize_nn_code, nn_to_storage_code

    primary = normalize_nn_code(
        settings.get('primary_nn_sector') or settings.get('default_hkd_sector') or nn_sectors[0]
    )
    return {
        'tenant_id': registry_row.get('tenant_id'),
        'business_name': registry_row.get('business_name'),
        'accounting_regime': regime,
        'accounting_regime_label': regime_meta['label'],
        'regime_active': regime_meta['active'],
        'regime_coming_soon': regime_meta.get('coming_soon', False),
        'revenue_tier': tier,
        'revenue_tier_label': tier_meta['label'],
        'revenue_tier_short': tier_meta['short_label'],
        'legacy_business_group': tier_meta['legacy_group'],
        'filing_period': features.get('filing_period') or tier_meta['filing_period'],
        'business_line': settings.get('business_line', registry_row.get('business_type') or 'pos'),
        'enabled_nn_sectors': nn_sectors,
        'primary_nn_sector': primary,
        'default_hkd_sector': nn_to_storage_code(primary),
        'features': features,
        'settings': settings,
    }


def _empty_profile():
    return build_profile_from_registry({
        'tenant_id': None,
        'settings': build_tenant_settings(),
        'business_type': 'pos',
    })


def load_tenant_profile(tenant_id):
    if not tenant_id:
        return _empty_profile()
    from Services.subscription_service import get_tenant_record
    rec = get_tenant_record(tenant_id, include_inactive=True)
    return build_profile_from_registry(rec or {})


def is_master_session():
    from flask import session
    try:
        user = session.get('user') or {}
        return (user.get('role') or session.get('role')) == 'master'
    except RuntimeError:
        return False


def tenant_has_feature(profile_or_settings, feature_name):
    if is_master_session():
        return True
    if isinstance(profile_or_settings, dict) and 'features' in profile_or_settings:
        features = profile_or_settings.get('features') or {}
    else:
        settings = parse_tenant_settings(profile_or_settings)
        regime = normalize_accounting_regime(settings.get('accounting_regime'))
        tier = infer_revenue_tier(settings)
        features = resolve_features(regime, tier, settings)
    return bool(features.get(feature_name))


def get_current_tenant_profile():
    return getattr(g, 'tenant_profile', None) or _empty_profile()


def profile_options_payload():
    """JSON cho form đăng ký / master."""
    from Services.hkd_sector import HKD_SECTOR_LEGAL_INTRO, get_sector_ui_options

    return {
        'accounting_regimes': [
            {**v, 'selectable': v['active']}
            for v in ACCOUNTING_REGIMES.values()
        ],
        'revenue_tiers': [
            {
                'code': t['code'],
                'label': t['label'],
                'short_label': t['short_label'],
                'filing_period': t['filing_period'],
            }
            for t in (REVENUE_TIERS[k] for k in TIER_ORDER)
        ],
        'nn_sectors': get_sector_ui_options(),
        'hkd_sectors': get_sector_ui_options(),
        'nn_sector_legal_intro': HKD_SECTOR_LEGAL_INTRO,
        'hkd_sector_legal_intro': HKD_SECTOR_LEGAL_INTRO,
    }


# ---------------------------------------------------------------------------
# DB tenant — business_info
# ---------------------------------------------------------------------------
BUSINESS_INFO_PROFILE_COLUMNS = (
    ('accounting_regime', "TEXT DEFAULT 'HKD'"),
    ('revenue_tier_declared', 'TEXT'),
    ('revenue_tier_effective', 'TEXT'),
    ('default_hkd_sector', "TEXT DEFAULT 'G1'"),
    ('filing_period', "TEXT DEFAULT 'quarterly'"),
)


def ensure_business_info_profile_columns(cursor):
    cursor.execute('PRAGMA table_info(business_info)')
    existing = {row[1] for row in cursor.fetchall()}
    for col, ddl in BUSINESS_INFO_PROFILE_COLUMNS:
        if col not in existing:
            cursor.execute(f'ALTER TABLE business_info ADD COLUMN {col} {ddl}')


def sync_business_info_profile(cursor, profile):
    """Đồng bộ profile vào business_info (1 dòng)."""
    ensure_business_info_profile_columns(cursor)
    row = cursor.execute('SELECT id FROM business_info LIMIT 1').fetchone()
    if not row:
        return
    bid = row[0] if not hasattr(row, 'keys') else row['id']
    cursor.execute(
        """
        UPDATE business_info SET
            accounting_regime = ?,
            revenue_tier_declared = ?,
            revenue_tier_effective = ?,
            default_hkd_sector = ?,
            filing_period = ?
        WHERE id = ?
        """,
        (
            profile.get('accounting_regime', 'HKD'),
            profile.get('revenue_tier'),
            profile.get('revenue_tier'),
            profile.get('default_hkd_sector', 'G1'),
            profile.get('filing_period', 'quarterly'),
            bid,
        ),
    )


def update_registry_settings(tenant_id, settings_patch, conn=None):
    """Cập nhật tenants.settings trên registry.

    Truyền ``conn`` khi caller đang giữ transaction trên cùng DB để tránh database is locked.
    """
    import json

    own_conn = conn is None
    if own_conn:
        from db_utils import get_main_db_connection
        conn = get_main_db_connection()

    try:
        row = conn.execute(
            'SELECT settings FROM tenants WHERE tenant_id = ?',
            (tenant_id,),
        ).fetchone()
        if not row:
            return False
        current = parse_tenant_settings(row[0] if not hasattr(row, 'keys') else row['settings'])
        current.update(settings_patch)
        regime = normalize_accounting_regime(current.get('accounting_regime'))
        tier = infer_revenue_tier(current)
        current['features'] = resolve_features(regime, tier, current)
        current['filing_period'] = REVENUE_TIERS[tier]['filing_period']
        conn.execute(
            'UPDATE tenants SET settings = ? WHERE tenant_id = ?',
            (json.dumps(current, ensure_ascii=False), tenant_id),
        )
        if own_conn:
            conn.commit()
        return True
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Cảnh báo vượt ngưỡng doanh thu (không tự nâng tier)
# ---------------------------------------------------------------------------
def check_revenue_tier_drift(cursor, profile, year=None):
    """So sánh DT lũy kế năm với tier đã khai — trả cảnh báo nếu vượt ngưỡng."""
    from datetime import date
    from Services.profit_report_helpers import compute_profit_report

    year = int(year or date.today().year)
    tier = normalize_revenue_tier(profile.get('revenue_tier'))
    start = f'{year}-01-01'
    end = f'{year}-12-31'
    try:
        profit = compute_profit_report(cursor, start, end)
        ytd = float(profit.get('revenue') or 0)
    except Exception:
        return None

    tier_idx = TIER_ORDER.index(tier) if tier in TIER_ORDER else 0
    suggested = tier
    for code in TIER_ORDER:
        meta = REVENUE_TIERS[code]
        cap = meta.get('revenue_max')
        if cap is None or ytd <= cap:
            suggested = code
            break
        suggested = code

    suggested_idx = TIER_ORDER.index(suggested)
    if suggested_idx <= tier_idx:
        return None

    return {
        'declared_tier': tier,
        'declared_label': REVENUE_TIERS[tier]['label'],
        'suggested_tier': suggested,
        'suggested_label': REVENUE_TIERS[suggested]['label'],
        'ytd_revenue': round(ytd),
        'year': year,
        'message': (
            f'Doanh thu lũy kế năm {year} vượt ngưỡng nhóm {tier}. '
            f'Cân nhắc chuyển sang {REVENUE_TIERS[suggested]["label"]} (chỉ Master thay đổi DT).'
        ),
    }


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def require_hkd_regime(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        profile = get_current_tenant_profile()
        if not profile.get('regime_active') or profile.get('accounting_regime') != 'HKD':
            msg = 'Chế độ kế toán này chưa được kích hoạt trên hệ thống.'
            if profile.get('regime_coming_soon'):
                msg = 'Kế toán doanh nghiệp (TT58/TT99) sẽ được triển khai trong phiên bản tới.'
            if request.path.startswith('/api/') or request.is_json:
                return jsonify({'success': False, 'error': msg}), 403
            flash(msg, 'warning')
            return redirect(url_for('HKD_dashboard'))
        return view_func(*args, **kwargs)
    return wrapped


def require_feature(feature_name):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            profile = get_current_tenant_profile()
            if is_master_session():
                return view_func(*args, **kwargs)
            if not tenant_has_feature(profile, feature_name):
                msg = f'Tính năng không khả dụng với cấu hình tenant hiện tại ({feature_name}).'
                if request.path.startswith('/api/') or request.is_json:
                    return jsonify({'success': False, 'error': msg}), 403
                flash(msg, 'warning')
                return redirect(url_for('HKD_dashboard'))
            return view_func(*args, **kwargs)
        return wrapped
    return decorator
