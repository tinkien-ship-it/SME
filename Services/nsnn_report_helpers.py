"""Báo cáo S4 — Sổ theo dõi nghĩa vụ thuế với NSNN."""
from datetime import datetime

from Services.profit_report_helpers import compute_profit_report
from Services.tenant_profile import (
    legacy_group_to_revenue_tier,
    normalize_revenue_tier,
    revenue_tier_to_legacy_group,
)


def nsnn_reference_key(start_iso, end_iso, tax_type):
    return f"NSNN|{start_iso}|{end_iso}|{tax_type}"


def _resolve_revenue_tier(revenue_tier=None, business_group=None):
    if revenue_tier:
        return normalize_revenue_tier(revenue_tier)
    return legacy_group_to_revenue_tier(business_group)


def compute_nsnn_tax_amounts(
    revenue,
    total_expenses,
    business_group=None,
    *,
    revenue_tier=None,
    default_hkd_sector='NN1',
    sector_totals=None,
    ytd_before=None,
):
    """Tính GTGT / TNCN theo nhóm doanh thu DT1–DT4."""
    tier = _resolve_revenue_tier(revenue_tier, business_group)
    dt = float(revenue or 0)
    lai = dt - float(total_expenses or 0)

    if tier == 'DT1':
        return {
            'revenue_tier': tier,
            'gtgt': 0.0,
            'tncn': 0.0,
            'note_gtgt': 'DT1 — Doanh thu ≤ 1 tỷ (Miễn)',
            'note_tncn': 'DT1 — Doanh thu ≤ 1 tỷ (Miễn)',
        }

    if tier == 'DT2':
        from Services.hkd_sector import calc_sector_taxes, normalize_nn_code, nn_to_totals_key

        totals = {'g1': 0.0, 'g2': 0.0, 'g3': 0.0, 'g4': 0.0}
        if sector_totals:
            for k, v in sector_totals.items():
                key = str(k).lower()
                if key.startswith('nn'):
                    key = nn_to_totals_key(key)
                if key in totals:
                    totals[key] += float(v or 0)
        else:
            from Services.hkd_sector import storage_code_to_nn
            nn = normalize_nn_code(storage_code_to_nn(default_hkd_sector))
            totals[nn_to_totals_key(nn)] = dt
        taxes = calc_sector_taxes(totals, ytd_before=ytd_before)
        gtgt = float(taxes.get('total_gtgt') or 0)
        tncn = float(taxes.get('total_tncn') or 0)
        return {
            'revenue_tier': tier,
            'gtgt': gtgt,
            'tncn': tncn,
            'note_gtgt': 'GTGT theo NN1–NN4 (S2a)',
            'note_tncn': 'TNCN theo NN1–NN4 (S2a)',
            'sector_breakdown': taxes,
        }

    tncn_rate_pct = None
    try:
        from Services.tax_rate_helpers import get_tax_rate_pct
        as_of = datetime.now().strftime('%Y-%m-%d')
        if tier in ('DT3', 'DT4'):
            gtgt_pct = get_tax_rate_pct(
                'hkd_gtgt_on_revenue', revenue_tier=tier, as_of=as_of, default=1.0,
            )
            tncn_pct = get_tax_rate_pct(
                'hkd_tncn_on_profit', revenue_tier=tier, as_of=as_of,
                default=17.0 if tier == 'DT3' else 20.0,
            )
            return {
                'revenue_tier': tier,
                'gtgt': round(dt * float(gtgt_pct or 0) / 100.0),
                'tncn': round(max(0.0, lai) * float(tncn_pct or 0) / 100.0),
                'note_gtgt': f'{gtgt_pct:g}% Doanh thu',
                'note_tncn': f'{tncn_pct:g}% Lãi (S2c)',
            }
    except Exception:
        pass

    tncn_rate = 0.20 if tier == 'DT4' else 0.17
    tncn_pct = int(tncn_rate * 100)
    return {
        'revenue_tier': tier,
        'gtgt': round(dt * 0.01),
        'tncn': round(max(0.0, lai) * tncn_rate),
        'note_gtgt': '1% Doanh thu',
        'note_tncn': f'{tncn_pct}% Lãi (S2c)',
    }


def compute_nsnn_tax_amounts_legacy(revenue, total_expenses, business_group):
    """Alias tương thích — map group 1–3 sang R1–R3."""
    return compute_nsnn_tax_amounts(
        revenue, total_expenses, business_group=business_group,
    )


def _paid_for_tax(cursor, start_iso, end_iso, tax_type):
    ref = nsnn_reference_key(start_iso, end_iso, tax_type)
    try:
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS paid,
                   MAX(voucher_no) AS voucher_no,
                   MAX(id) AS phieu_chi_id
            FROM phieu_chi
            WHERE source_type = 'nsnn_tax'
              AND reference_document = ?
            """,
            (ref,),
        )
    except Exception:
        return 0.0, None, None
    row = cursor.fetchone()
    if not row:
        return 0.0, None, None
    keys = row.keys() if hasattr(row, 'keys') else None
    paid = float(row['paid'] if keys else row[0] or 0)
    voucher = (row['voucher_no'] if keys else row[1]) or None
    pc_id = (row['phieu_chi_id'] if keys else row[2]) or None
    return paid, voucher, pc_id


def _tax_status(phai_nop, da_nop):
    phai_nop = float(phai_nop or 0)
    da_nop = float(da_nop or 0)
    if phai_nop <= 0:
        return 'Không có nghĩa vụ', 'bg-secondary-subtle text-secondary'
    if da_nop >= phai_nop:
        return 'Đã nộp đủ', 'bg-success-subtle text-success'
    if da_nop > 0:
        return 'Nộp một phần', 'bg-warning-subtle text-warning'
    return 'Chưa nộp', 'bg-danger-subtle text-danger'


def build_nsnn_report(
    cursor,
    start_iso,
    end_iso,
    business_group=None,
    *,
    revenue_tier=None,
    default_hkd_sector='G1',
    ytd_before=None,
):
    profit = compute_profit_report(cursor, start_iso, end_iso)
    revenue = profit['revenue']
    total_expenses = profit['total_chi_phi']
    tier = _resolve_revenue_tier(revenue_tier, business_group)
    sector_totals = None
    ytd_before_val = ytd_before
    if tier == 'DT2':
        from Services.hkd_revenue import _aggregate_revenue_rows, _ytd_total_before_period
        _, sector_totals, _ = _aggregate_revenue_rows(cursor, start_iso, end_iso)
        if ytd_before_val is None:
            ytd_before_val = _ytd_total_before_period(cursor, start_iso)
    taxes = compute_nsnn_tax_amounts(
        revenue,
        total_expenses,
        revenue_tier=tier,
        default_hkd_sector=default_hkd_sector,
        sector_totals=sector_totals,
        ytd_before=ytd_before_val,
    )

    suffix = end_iso.replace('-', '')
    end_display = '/'.join(reversed(end_iso.split('-')))

    rows = []
    for tax_type, note_key in (('GTGT', 'note_gtgt'), ('TNCN', 'note_tncn')):
        phai_nop = taxes[tax_type.lower()]
        da_nop, voucher_no, phieu_chi_id = _paid_for_tax(cursor, start_iso, end_iso, tax_type)
        con_lai = max(0.0, phai_nop - da_nop)
        status, status_class = _tax_status(phai_nop, da_nop)
        rows.append({
            'tax_type': tax_type,
            'so_hieu': f"{tax_type}{suffix}" if phai_nop > 0 else '',
            'date': end_display if phai_nop > 0 else '',
            'dien_giai': f"Thuế {tax_type} phát sinh trong kỳ",
            'phai_nop': phai_nop,
            'da_nop': round(da_nop),
            'con_lai': round(con_lai),
            'note': taxes[note_key],
            'voucher_no': voucher_no,
            'phieu_chi_id': phieu_chi_id,
            'status': status,
            'status_class': status_class,
        })

    total_phai = sum(r['phai_nop'] for r in rows)
    total_da = sum(r['da_nop'] for r in rows)

    return {
        'period': {'start': start_iso, 'end': end_iso},
        'business_group': revenue_tier_to_legacy_group(tier),
        'revenue_tier': tier,
        'revenue': revenue,
        'total_expenses': total_expenses,
        'summary': {
            'total_phai_nop': total_phai,
            'total_da_nop': total_da,
            'total_con_lai': max(0, total_phai - total_da),
        },
        'rows': rows,
    }
