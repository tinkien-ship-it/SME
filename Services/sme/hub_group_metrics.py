# -*- coding: utf-8 -*-
"""Chỉ số giá trị trên trang nhóm hub SME (chỉ mục có số liệu mới trả về)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import sqlite3

from Services.sme.cash_books import cash_fund_balances
from Services.sme.dashboard_metrics import (
    debt_hub_metrics,
    fixed_asset_hub_metrics,
    tools_hub_metrics,
    warehouse_hub_metrics,
)


def _fmt_currency(value: Any) -> str:
    v = round(float(value or 0))
    return f'{v:,.0f}'.replace(',', '.') + ' đ'


def _metric(value: Any, *, detail: str | None = None) -> dict[str, Any]:
    v = float(value or 0)
    out = {
        'value': v,
        'kind': 'currency',
        'display': _fmt_currency(v),
    }
    if detail:
        out['detail'] = detail
    return out


def _endpoint_metric(
    conn: sqlite3.Connection,
    endpoint: str,
    *,
    fiscal_year: int,
    period_to: int,
    branch_code: str | None,
) -> dict[str, Any] | None:
    """Công thức số dư / giá trị — trả None nếu mục không có số liệu."""
    cash = None

    def _cash():
        nonlocal cash
        if cash is None:
            cash = cash_fund_balances(conn, fiscal_year=fiscal_year, branch_code=branch_code)
        return cash

    if endpoint == 'SME_SoQuyTienMat':
        bal = _cash().get('so_du_tien_mat') or 0
        return _metric(bal, detail='Số dư tài khoản 111')

    if endpoint == 'SME_SoTienGuiNganHang':
        bal = _cash().get('so_du_ngan_hang') or 0
        return _metric(bal, detail='Số dư tài khoản 112')

    if endpoint in (
        'SME_SoCongNoPhaiThu',
        'SME_SoCongNoPhaiTra',
        'SME_PhaiThuCongNhanVien',
        'SME_PhaiTraCongNhanVien',
        'SME_dashboard_debt',
    ):
        d = debt_hub_metrics(
            conn, fiscal_year=fiscal_year, period_to=period_to, branch_code=branch_code,
        )
        if endpoint == 'SME_SoCongNoPhaiThu':
            return _metric(d.get('receivable'), detail='Số dư tài khoản 131')
        if endpoint == 'SME_SoCongNoPhaiTra':
            return _metric(d.get('payable'), detail='Số dư tài khoản 331')
        if endpoint == 'SME_PhaiThuCongNhanVien':
            return _metric(d.get('employee_advance'), detail='Số dư tài khoản 141')
        if endpoint == 'SME_PhaiTraCongNhanVien':
            # Dùng số phải trả lương nếu có trong HR; fallback 0 từ debt hub
            from Services.sme.dashboard_metrics import hr_hub_metrics
            hr = hr_hub_metrics(
                conn, fiscal_year=fiscal_year, period_to=period_to, branch_code=branch_code,
            )
            return _metric(hr.get('salary_payable'), detail='Số dư tài khoản 334')
        return None

    if endpoint in ('SME_fixed_assets', 'SME_TSCD'):
        fa = fixed_asset_hub_metrics(
            conn, fiscal_year=fiscal_year, period_to=period_to, branch_code=branch_code,
        )
        if endpoint == 'SME_fixed_assets':
            return _metric(
                fa.get('register_cost') or fa.get('gross_cost') or fa.get('original_cost'),
                detail='Nguyên giá đăng ký',
            )
        # Tổng quan: giá trị còn lại / nguyên giá
        net = fa.get('net_book') or fa.get('net_value')
        gross = fa.get('gross_cost') or fa.get('original_cost') or fa.get('register_cost')
        if net is not None:
            return _metric(net, detail='Giá trị còn lại')
        return _metric(gross, detail='Nguyên giá')

    if endpoint in ('SME_tools', 'SME_CCDC'):
        tools = tools_hub_metrics(
            conn, fiscal_year=fiscal_year, period_to=period_to, branch_code=branch_code,
        )
        if endpoint == 'SME_tools':
            return _metric(tools.get('register_cost') or tools.get('balance'), detail='Nguyên giá đăng ký')
        return _metric(tools.get('balance'), detail='Số dư tài khoản 153')

    if endpoint in ('inventory', 'inventory_detail', 'SME_dashboard_warehouse'):
        wh = warehouse_hub_metrics(
            conn, fiscal_year=fiscal_year, period_to=period_to, branch_code=branch_code,
        )
        total = wh.get('inventory_total')
        if endpoint == 'inventory_detail':
            return None
        if endpoint == 'SME_dashboard_warehouse':
            return _metric(total, detail='Tổng hàng tồn kho (152+155+156+154)')
        return _metric(total, detail='Giá trị tồn kho')

    return None


def fetch_hub_group_metrics(
    conn: sqlite3.Connection,
    group: dict,
    *,
    fiscal_year: int | None = None,
    period_to: int | None = None,
    branch_code: str | None = None,
) -> dict[str, Any]:
    year = int(fiscal_year or date.today().year)
    period = int(period_to or datetime.now().month)
    items_out: dict[str, Any] = {}
    for item in group.get('items') or ():
        ep = item.get('endpoint')
        if not ep:
            continue
        try:
            m = _endpoint_metric(
                conn, ep,
                fiscal_year=year, period_to=period, branch_code=branch_code,
            )
        except Exception:
            m = None
        if m:
            items_out[ep] = m
    return {
        'year': year,
        'period_to': period,
        'group_id': group.get('id'),
        'items': items_out,
        'branch_code': branch_code or 'ALL',
    }
