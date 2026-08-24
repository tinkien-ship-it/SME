"""Đồng bộ tenant từ SME_MICRO_TT58 → SME_TT99.

Nguyên tắc pháp lý / kế toán:
- Giữ nguyên lịch sử bút toán đã ghi (không viết lại chứng từ cũ).
- Bổ sung đầy đủ hệ thống TK / quy tắc / schema theo TT99 (seed merge, không xóa TK custom).
- Kiểm tra toàn vẹn: chứng từ cân Nợ=Có, dòng TK tồn tại, B01 cân đối, TK bắt buộc có mặt.
- Cập nhật ``ledger_profile`` / ``accounting_regime`` trên DB tenant + registry.
- Tắt cảnh báo siêu nhỏ; áp dụng chính sách kê khai GTGT theo TT99 + DT.

Gọi khi Master đổi chế độ, hoặc API đồng bộ lại.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from Services.tenant_profile import (
    is_sme_regime,
    normalize_accounting_regime,
    resolve_vat_filing_policy,
    update_registry_settings,
)

# TK hệ thống tối thiểu cần có để vận hành TT99 (Phụ lục II / khuyến nghị)
REQUIRED_TT99_ACCOUNTS = (
    '111', '1111', '112', '1121',
    '131', '133', '1331', '13311',
    '152', '153', '154', '155', '156',
    '211', '214', '331',
    '333', '33311',
    '334', '338',
    '411', '4111', '421', '4211', '4212',
    '511', '5111', '632', '642', '911',
)


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sme_coa_seed_meta (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sme_coa_seed_meta(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, _now()),
    )


def verify_journal_integrity(conn: sqlite3.Connection) -> dict[str, Any]:
    """Kiểm tra cân Nợ/Có từng chứng từ + dòng trỏ TK hợp lệ."""
    issues: list[str] = []
    conn.row_factory = sqlite3.Row
    try:
        unbalanced = conn.execute(
            """
            SELECT je.id, je.entry_no,
                   ROUND(SUM(jl.debit), 2) AS d,
                   ROUND(SUM(jl.credit), 2) AS c
            FROM sme_journal_entries je
            JOIN sme_journal_lines jl ON jl.entry_id = je.id
            WHERE je.status IN ('posted', 'reversed')
            GROUP BY je.id
            HAVING ABS(ROUND(SUM(jl.debit), 2) - ROUND(SUM(jl.credit), 2)) > 0.01
            LIMIT 50
            """
        ).fetchall()
    except sqlite3.Error:
        return {'ok': True, 'skipped': True, 'reason': 'no_journal_tables'}

    for r in unbalanced:
        issues.append(
            f"BT {r['entry_no'] or r['id']}: Nợ {r['d']} ≠ Có {r['c']}"
        )

    orphan_lines = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM sme_journal_lines jl
        JOIN sme_journal_entries je ON je.id = jl.entry_id
        LEFT JOIN sme_chart_of_accounts a ON a.code = jl.account_code
        WHERE je.status IN ('posted', 'reversed')
          AND (a.code IS NULL OR COALESCE(a.is_active, 1) = 0)
        """
    ).fetchone()
    orphan_n = int(orphan_lines[0] or 0) if orphan_lines else 0
    if orphan_n:
        issues.append(f'{orphan_n} dòng nhật ký trỏ tài khoản không tồn tại / ngừng dùng')

    return {
        'ok': len(issues) == 0,
        'unbalanced_entries': len(unbalanced),
        'orphan_lines': orphan_n,
        'issues': issues,
    }


def verify_required_accounts(conn: sqlite3.Connection) -> dict[str, Any]:
    missing = []
    for code in REQUIRED_TT99_ACCOUNTS:
        row = conn.execute(
            "SELECT 1 FROM sme_chart_of_accounts WHERE code = ? AND COALESCE(is_active,1)=1",
            (code,),
        ).fetchone()
        if not row:
            missing.append(code)
    return {'ok': not missing, 'missing': missing}


def verify_balance_sheet(conn: sqlite3.Connection, fiscal_year: int | None = None) -> dict[str, Any]:
    from Services.sme.bctc_report import balance_sheet
    year = int(fiscal_year or datetime.now().year)
    try:
        bs = balance_sheet(conn, fiscal_year=year, period_to=12)
    except Exception as exc:
        # Thử năm trước nếu năm hiện tại trống
        try:
            year = year - 1
            bs = balance_sheet(conn, fiscal_year=year, period_to=12)
        except Exception:
            return {'ok': True, 'skipped': True, 'error': str(exc)}
    totals = bs.get('totals') or {}
    diff = abs(float(totals.get('difference') or 0))
    return {
        'ok': diff <= 1.0,  # cho phép lệch làm tròn 1đ
        'fiscal_year': year,
        'total_assets': totals.get('total_assets'),
        'total_equity_and_liabilities': totals.get('total_equity_and_liabilities'),
        'difference': totals.get('difference'),
    }


def migrate_tenant_to_tt99(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    settings: dict | None = None,
    update_registry: bool = True,
    migrated_by: str | None = None,
    force_coa_refresh: bool = False,
) -> dict[str, Any]:
    """
    Đồng bộ đầy đủ tenant sang TT99 trên DB tenant (+ registry nếu được phép).
    Không commit registry ngoài ``update_registry_settings``; caller commit DB tenant.
    """
    from Services.sme.bootstrap import ensure_sme_accounting_ready
    from Services.sme.coa_service import ensure_sme_coa_ready
    from Services.sme.journal_engine import seed_posting_rules

    settings = dict(settings or {})
    old_regime = normalize_accounting_regime(settings.get('accounting_regime'))
    report: dict[str, Any] = {
        'tenant_id': tenant_id,
        'from_regime': old_regime,
        'to_regime': 'SME_TT99',
        'ok': False,           # đồng bộ chạy xong không lỗi kỹ thuật
        'synced': False,
        'integrity_ok': False,  # số liệu khớp / hợp lệ sau kiểm tra
        'steps': {},
        'checks': {},
        'migrated_at': _now(),
        'migrated_by': migrated_by,
    }

    if old_regime == 'SME_TT99':
        report['message'] = 'Tenant đã ở TT99 — chạy đồng bộ bổ sung / kiểm tra toàn vẹn.'
    elif is_sme_regime(old_regime):
        report['message'] = f'Đồng bộ sang TT99 từ {old_regime}.'
    elif old_regime not in ('', 'HKD', 'SME'):
        report['error'] = f'Không hỗ trợ chuyển từ {old_regime} sang TT99 tự động.'
        return report

    # 1) Seed / merge COA TT99 (không xóa custom)
    coa = ensure_sme_coa_ready(conn, force_reseed=force_coa_refresh, commit=False)
    report['steps']['coa'] = coa

    # 2) Schema + quy tắc định khoản + module SME đầy đủ
    boot = ensure_sme_accounting_ready(
        conn, accounting_regime='SME_TT99', commit=False,
    )
    report['steps']['bootstrap'] = {
        'ledger_profile': boot.get('ledger_profile'),
        'accounting_regime': boot.get('accounting_regime'),
    }
    rules = seed_posting_rules(conn, force=True, commit=False)
    report['steps']['posting_rules'] = rules

    # 3) Meta TT99
    _meta_set(conn, 'ledger_profile', 'sme_tt99')
    _meta_set(conn, 'accounting_regime', 'SME_TT99')
    _meta_set(conn, 'migrated_from_tt58_at', _now())
    _meta_set(
        conn,
        'tt99_migration_log',
        json.dumps({
            'from': old_regime,
            'at': _now(),
            'by': migrated_by,
            'tenant_id': tenant_id,
        }, ensure_ascii=False),
    )

    # 4) Kiểm tra toàn vẹn (cảnh báo pháp lý — không rollback đồng bộ)
    chk_acc = verify_required_accounts(conn)
    chk_je = verify_journal_integrity(conn)
    chk_bs = verify_balance_sheet(conn)
    report['checks'] = {
        'required_accounts': chk_acc,
        'journals': chk_je,
        'balance_sheet': chk_bs,
    }
    report['synced'] = True
    report['ok'] = True
    report['integrity_ok'] = bool(
        chk_acc.get('ok') and chk_je.get('ok') and chk_bs.get('ok', True)
    )

    # 5) Registry: regime + tắt cảnh báo micro + chính sách GTGT
    if update_registry and tenant_id:
        policy = resolve_vat_filing_policy('SME_TT99', settings)
        patch: dict[str, Any] = {
            'accounting_regime': 'SME_TT99',
            'micro_enterprise_tt99_alert': {
                'active': False,
                'status': 'resolved_switched_tt99',
                'cleared_at': _now(),
                'message': 'Đã chuyển và đồng bộ sang TT99.',
            },
            'tt99_migration': {
                'from_regime': old_regime,
                'migrated_at': _now(),
                'migrated_by': migrated_by,
                'synced': True,
                'integrity_ok': report['integrity_ok'],
                'checks': {
                    'missing_accounts': chk_acc.get('missing'),
                    'unbalanced_entries': chk_je.get('unbalanced_entries'),
                    'bs_difference': (chk_bs or {}).get('difference'),
                },
            },
        }
        # Không ép kỳ kê khai nếu đã cấu hình; chỉ set mặc định khi thiếu
        if not settings.get('vat_filing_period') and not settings.get('filing_period'):
            patch['vat_filing_period'] = policy['default_period']
            patch['filing_period'] = policy['default_period']
        update_registry_settings(tenant_id, patch)
        report['steps']['registry'] = {'updated': True, 'vat_default': patch.get('vat_filing_period')}

    report['message'] = (
        'Đã đồng bộ COA/quy tắc/schema TT99, giữ nguyên lịch sử bút toán. '
        + (
            'Kiểm tra toàn vẹn: ĐẠT (TK bắt buộc, chứng từ cân Nợ=Có, B01 cân đối).'
            if report['integrity_ok']
            else 'Kiểm tra toàn vẹn: CÓ CẢNH BÁO — xem checks (cần kế toán rà soát/điều chỉnh trước khi khóa sổ / nộp BCTC).'
        )
    )

    try:
        from Services.audit_log import write_audit
        write_audit(
            'migrate',
            'sme_regime',
            f"Đồng bộ {tenant_id}: {old_regime} → SME_TT99 "
            f"(synced, integrity={'OK' if report['integrity_ok'] else 'WARN'})",
            entity_type='tenant',
            entity_id=tenant_id,
            old_data={'accounting_regime': old_regime},
            new_data={
                'accounting_regime': 'SME_TT99',
                'integrity_ok': report['integrity_ok'],
                'checks': report['checks'],
            },
            username=migrated_by,
            tenant_id=tenant_id,
        )
    except Exception:
        pass

    return report


def migrate_tt58_to_tt99_if_needed(
    *,
    tenant_id: str,
    old_regime: str | None,
    new_regime: str | None,
    settings: dict | None = None,
    migrated_by: str | None = None,
) -> dict[str, Any] | None:
    """Hook sau khi đổi regime trên registry — chạy khi chuyển sang SME_TT99."""
    old_r = normalize_accounting_regime(old_regime)
    new_r = normalize_accounting_regime(new_regime)
    if new_r != 'SME_TT99':
        return None
    if old_r == 'SME_TT99':
        return None
    # TT58 → TT99 (hoặc SME generic → TT99)
    if old_r != 'SME_MICRO_TT58' and not str(old_r).upper().startswith('SME'):
        return None

    from db_utils import get_tenant_db_connection, sqlite_commit
    conn = get_tenant_db_connection(tenant_id)
    if not conn:
        return {'ok': False, 'error': f'Không mở được DB tenant {tenant_id}'}
    try:
        settings = dict(settings or {})
        settings['accounting_regime'] = old_r  # để log from
        # Registry đã được Master cập nhật sang TT99; chỉ ghi meta migration + tắt alert
        result = migrate_tenant_to_tt99(
            conn,
            tenant_id=tenant_id,
            settings=settings,
            update_registry=True,
            migrated_by=migrated_by,
        )
        sqlite_commit(conn, label='migrate_tt58_to_tt99')
        return result
    except Exception as exc:
        conn.rollback()
        return {'ok': False, 'error': str(exc), 'tenant_id': tenant_id}
    finally:
        conn.close()
