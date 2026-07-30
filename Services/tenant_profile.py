"""Nền tảng cấu hình tenant — chế độ kế toán, nhóm doanh thu R1–R4, feature flags."""
from __future__ import annotations

import sqlite3
from functools import wraps

from flask import g, jsonify, redirect, request, url_for, flash

from Services.subscription_service import parse_tenant_settings

# ---------------------------------------------------------------------------
# Chế độ kế toán
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
        'active': True,
        'coming_soon': False,
    },
    'SME_TT99': {
        'code': 'SME_TT99',
        'label': 'Doanh nghiệp (TT99 / VAS)',
        'active': True,
        'coming_soon': False,
    },
}

SME_BASE_FEATURES = {
    'SME_MICRO_TT58': {
        'accounting_enabled': True,
        'regime_coming_soon': False,
        'ledger_profile': 'sme_tt58',
        'double_entry': True,
        'coa_enabled': True,
        'journal_posting': True,
        'auto_depreciation': True,
        'auto_period_close': True,
        'auto_vat_settlement': True,
        'auto_lock_period': True,
        'bctc_enabled': True,
        # Mặc định kê khai GTGT theo quý; tenant có thể đổi sang tháng
        'filing_period': 'quarterly',
        'vat_filing_period': 'quarterly',
        'nsnn_s4': False,
        'tax_debt_summary': True,
        'profit_report_s2c': False,
        'monthly_vat_filing': False,
        'einvoice_required': True,
        'einvoice_enabled': True,
    },
    'SME_TT99': {
        'accounting_enabled': True,
        'regime_coming_soon': False,
        'ledger_profile': 'sme_tt99',
        'double_entry': True,
        'coa_enabled': True,
        'journal_posting': True,
        'auto_depreciation': True,
        'auto_period_close': True,
        'auto_vat_settlement': True,
        'auto_lock_period': True,
        'bctc_enabled': True,
        # Mặc định kê khai GTGT theo tháng; tenant có thể đổi sang quý
        'filing_period': 'monthly',
        'vat_filing_period': 'monthly',
        'nsnn_s4': False,
        'tax_debt_summary': True,
        'profit_report_s2c': False,
        'monthly_vat_filing': True,
        'einvoice_required': True,
        'einvoice_enabled': True,
    },
}

VAT_FILING_PERIODS = ('monthly', 'quarterly')


def normalize_vat_filing_period(value, default='quarterly'):
    """Chuẩn hoá kỳ kê khai GTGT: monthly | quarterly."""
    raw = str(value or '').strip().lower()
    if raw in ('month', 'monthly', 'thang', 'tháng', 'm'):
        return 'monthly'
    if raw in ('quarter', 'quarterly', 'quy', 'quý', 'q'):
        return 'quarterly'
    fallback = str(default or 'quarterly').strip().lower()
    return fallback if fallback in VAT_FILING_PERIODS else 'quarterly'


def default_vat_filing_period_for_regime(accounting_regime):
    regime = normalize_accounting_regime(accounting_regime)
    base = SME_BASE_FEATURES.get(regime) or SME_BASE_FEATURES['SME_TT99']
    return normalize_vat_filing_period(
        base.get('vat_filing_period') or base.get('filing_period'),
        default='monthly' if base.get('monthly_vat_filing') else 'quarterly',
    )


def apply_vat_filing_period_to_features(features, settings=None, accounting_regime=None):
    """Ghi đè filing_period / monthly_vat_filing theo lựa chọn tenant."""
    features = dict(features or {})
    settings = settings or {}
    if accounting_regime:
        default = default_vat_filing_period_for_regime(accounting_regime)
    elif features.get('vat_filing_period') or features.get('filing_period'):
        default = normalize_vat_filing_period(
            features.get('vat_filing_period') or features.get('filing_period'),
            default='quarterly',
        )
    elif features.get('monthly_vat_filing') is True:
        default = 'monthly'
    else:
        default = 'quarterly'

    raw = (
        settings.get('vat_filing_period')
        or settings.get('filing_period')
        or (settings.get('features') or {}).get('vat_filing_period')
        or (settings.get('features') or {}).get('filing_period')
        or features.get('vat_filing_period')
        or features.get('filing_period')
    )
    period = normalize_vat_filing_period(raw, default=default)
    features['vat_filing_period'] = period
    features['filing_period'] = period
    features['monthly_vat_filing'] = period == 'monthly'
    return features, period

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
    if not raw:
        return default
    if raw in REVENUE_TIERS:
        return raw
    if raw in LEGACY_R_TO_DT:
        return LEGACY_R_TO_DT[raw]
    if raw in LEGACY_GROUP_TO_TIER:
        return LEGACY_GROUP_TO_TIER[raw]
    if raw.isdigit() and raw in LEGACY_GROUP_TO_TIER:
        return LEGACY_GROUP_TO_TIER[raw]
    return default


def normalize_revenue_tier_optional(value):
    """Cho SME: cho phép DT trống (None). HKD vẫn normalize về DT hợp lệ."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return normalize_revenue_tier(raw)


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
    bl = (business_line or 'pos').strip()

    # Doanh nghiệp (TT58/TT99): không dùng nhóm DT / NN của HKD
    if is_sme_regime(regime):
        features = resolve_features(regime, None, {})
        default_fp = default_vat_filing_period_for_regime(regime)
        settings = {
            'accounting_regime': regime,
            'revenue_tier': None,
            'revenue_tier_declared': None,
            'revenue_tier_effective': None,
            'business_line': bl,
            'enabled_nn_sectors': [],
            'primary_nn_sector': None,
            'default_hkd_sector': None,
            'onboarding_completed': bool(onboarding_completed),
            'filing_period': default_fp,
            'vat_filing_period': default_fp,
            'features': features,
        }
        plan = (subscription_plan or '').strip().lower()
        if plan:
            settings['plan'] = plan
        if extra:
            settings.update(extra)
            # Giữ SME: không để patch lỡ gán lại DT/NN
            settings['accounting_regime'] = regime
            settings['revenue_tier'] = None
            settings['revenue_tier_declared'] = None
            settings['revenue_tier_effective'] = None
            settings['enabled_nn_sectors'] = []
            settings['primary_nn_sector'] = None
            settings['default_hkd_sector'] = None
        fp = normalize_vat_filing_period(
            settings.get('vat_filing_period') or settings.get('filing_period'),
            default=default_fp,
        )
        settings['vat_filing_period'] = fp
        settings['filing_period'] = fp
        settings['features'] = resolve_features(regime, None, settings)
        return settings

    tier = normalize_revenue_tier(revenue_tier)
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


def is_sme_regime(regime) -> bool:
    return str(regime or '').strip().upper().startswith('SME')


def resolve_features(accounting_regime, revenue_tier, settings=None):
    regime = normalize_accounting_regime(accounting_regime)
    regime_meta = ACCOUNTING_REGIMES.get(regime, ACCOUNTING_REGIMES['HKD'])
    settings = settings or {}

    if not regime_meta.get('active'):
        return {
            'accounting_enabled': False,
            'regime_coming_soon': regime_meta.get('coming_soon', True),
            'ledger_profile': 'disabled',
            'filing_period': None,
            'nsnn_s4': False,
            'tax_debt_summary': False,
            'profit_report_s2c': False,
            'monthly_vat_filing': False,
            'einvoice_required': False,
            'einvoice_enabled': False,
            'double_entry': False,
            'journal_posting': False,
            'bctc_enabled': False,
        }

    if is_sme_regime(regime):
        features = dict(SME_BASE_FEATURES.get(regime, SME_BASE_FEATURES['SME_TT99']))
        opt_tier = normalize_revenue_tier_optional(revenue_tier)
        if opt_tier:
            features['revenue_tier'] = opt_tier
        else:
            features['revenue_tier'] = None
        plan = (settings.get('plan') or '').upper()
        if plan in ('DV001',):
            features['einvoice_enabled'] = False
        elif plan in ('DV002', 'DV003', 'DV004'):
            features['einvoice_enabled'] = True
        if settings.get('features') and isinstance(settings['features'], dict):
            features.update({
                k: v for k, v in settings['features'].items()
                if k in features or k.startswith('custom_')
            })
        features, _period = apply_vat_filing_period_to_features(
            features, settings, accounting_regime=regime,
        )
        return features

    # HKD
    tier = normalize_revenue_tier(revenue_tier)
    features = dict(TIER_BASE_FEATURES.get(tier, TIER_BASE_FEATURES['DT1']))
    features['accounting_enabled'] = True
    features['regime_coming_soon'] = False
    features['revenue_tier'] = tier
    features['double_entry'] = False
    features['journal_posting'] = False
    features['bctc_enabled'] = False

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
    regime_meta = ACCOUNTING_REGIMES.get(regime, ACCOUNTING_REGIMES['HKD'])
    sme = is_sme_regime(regime)

    if sme:
        tier = normalize_revenue_tier_optional(
            settings.get('revenue_tier_effective')
            or settings.get('revenue_tier')
            or settings.get('revenue_tier_declared')
        )
        features = resolve_features(regime, tier, settings)
        nn_sectors = list(settings.get('enabled_nn_sectors') or [])
        primary = settings.get('primary_nn_sector') or None
        storage = settings.get('default_hkd_sector') or None
        tier_meta = REVENUE_TIERS.get(tier) if tier else None
        return {
            'tenant_id': registry_row.get('tenant_id'),
            'business_name': registry_row.get('business_name'),
            'accounting_regime': regime,
            'accounting_regime_label': regime_meta['label'],
            'regime_active': regime_meta['active'],
            'regime_coming_soon': regime_meta.get('coming_soon', False),
            'revenue_tier': tier,
            'revenue_tier_label': (tier_meta or {}).get('label'),
            'revenue_tier_short': (tier_meta or {}).get('short_label'),
            'legacy_business_group': (tier_meta or {}).get('legacy_group'),
            'filing_period': features.get('filing_period') or 'quarterly',
            'vat_filing_period': features.get('vat_filing_period')
                or features.get('filing_period')
                or 'quarterly',
            'business_line': settings.get('business_line', registry_row.get('business_type') or 'pos'),
            'enabled_nn_sectors': nn_sectors,
            'primary_nn_sector': primary,
            'default_hkd_sector': storage,
            'features': features,
            'settings': settings,
            'is_sme': True,
        }

    tier = infer_revenue_tier(settings)
    features = resolve_features(regime, tier, settings)
    tier_meta = REVENUE_TIERS.get(tier, REVENUE_TIERS['DT1'])
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
        'is_sme': False,
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
        if is_sme_regime(regime):
            current['accounting_regime'] = regime
            current['revenue_tier'] = None
            current['revenue_tier_declared'] = None
            current['revenue_tier_effective'] = None
            current['enabled_nn_sectors'] = []
            current['primary_nn_sector'] = None
            current['default_hkd_sector'] = None
            if 'vat_filing_period' in settings_patch or 'filing_period' in settings_patch:
                fp = normalize_vat_filing_period(
                    current.get('vat_filing_period') or current.get('filing_period'),
                    default=default_vat_filing_period_for_regime(regime),
                )
                current['vat_filing_period'] = fp
                current['filing_period'] = fp
            current['features'] = resolve_features(regime, None, current)
            current['filing_period'] = (current.get('features') or {}).get('filing_period') or 'quarterly'
            current['vat_filing_period'] = (
                (current.get('features') or {}).get('vat_filing_period')
                or current['filing_period']
            )
        else:
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
        if is_master_session():
            return view_func(*args, **kwargs)
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


def require_sme_regime(view_func):
    """Cho phép regime SME_MICRO_TT58 / SME_TT99 đã kích hoạt. HKD bị chặn (trừ master)."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if is_master_session():
            return view_func(*args, **kwargs)
        profile = get_current_tenant_profile()
        regime = (profile.get('accounting_regime') or '').upper()
        if not is_sme_regime(regime):
            msg = (
                'Chức năng này dành cho chế độ Kế toán Doanh nghiệp (TT58/TT99). '
                'Tenant hiện tại đang dùng chế độ Hộ kinh doanh.'
            )
            if request.path.startswith('/api/') or request.is_json:
                return jsonify({'success': False, 'error': msg}), 403
            flash(msg, 'warning')
            try:
                return redirect(url_for('HKD_dashboard'))
            except Exception:
                return redirect('/HKD_dashboard')
        if not profile.get('regime_active'):
            msg = 'Chế độ kế toán doanh nghiệp chưa được kích hoạt trên hệ thống.'
            if profile.get('regime_coming_soon'):
                msg = 'Kế toán doanh nghiệp (TT58/TT99) sẽ được triển khai trong phiên bản tới.'
            if request.path.startswith('/api/') or request.is_json:
                return jsonify({'success': False, 'error': msg}), 403
            flash(msg, 'warning')
            try:
                return redirect(url_for('HKD_dashboard'))
            except Exception:
                return redirect('/')
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
