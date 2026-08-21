# -*- coding: utf-8 -*-
"""Xuất XML khung 05/KK-TNCN rút gọn từ bảng lương SME (đối chiếu HTKK — không nộp cổng)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any
from xml.dom import minidom

from Services.sme.pit_declaration import pit_withholding_worksheet

NS = "http://kekhaithue.gdt.gov.vn/TKhaiThue"


def _txt(el: ET.Element, tag: str, value: Any = "") -> ET.Element:
    child = ET.SubElement(el, tag)
    child.text = "" if value is None else str(value)
    return child


def _money_str(val) -> str:
    try:
        return f"{float(val or 0):.0f}"
    except (TypeError, ValueError):
        return "0"


def _pretty(root: ET.Element) -> str:
    rough = ET.tostring(root, encoding="utf-8")
    parsed = minidom.parseString(rough)
    return parsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")


def _company_from_conn(conn) -> dict[str, str]:
    try:
        row = conn.execute("SELECT * FROM business_info LIMIT 1").fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    d = dict(row)
    return {
        'tax_code': str(d.get('tax_code') or d.get('mst') or '').strip(),
        'name': str(d.get('business_name') or d.get('name') or '').strip(),
        'address': str(d.get('address') or '').strip(),
    }


def generate_sme_pit_xml(
    conn,
    *,
    fiscal_year: int,
    period_from: int = 1,
    period_to: int | None = None,
) -> dict[str, Any]:
    """
    XML khung nội bộ — import/đối chiếu HTKK; không thay thế file chính thức CQT.
    """
    ws = pit_withholding_worksheet(
        conn,
        fiscal_year=fiscal_year,
        period_from=period_from,
        period_to=period_to,
    )
    company = _company_from_conn(conn)
    year = int(ws['fiscal_year'])
    p_from = int(ws['period_from'])
    p_to = int(ws['period_to'])
    totals = ws.get('totals') or {}

    root = ET.Element("HSoThueDTu", xmlns=NS)
    tkhai = ET.SubElement(root, "TKhaiThue")
    _txt(tkhai, "maTKhai", "05/KK-TNCN")
    _txt(tkhai, "tenTKhai", "Tờ khai khấu trừ thuế TNCN (rút gọn SME)")
    _txt(tkhai, "moTa", ws.get('form_hint') or '')
    _txt(tkhai, "kyKKhaiTu", f"{p_from:02d}/{year}")
    _txt(tkhai, "kyKKhaiDen", f"{p_to:02d}/{year}")
    _txt(tkhai, "mst", company.get('tax_code') or '')
    _txt(tkhai, "tenNNT", company.get('name') or '')
    _txt(tkhai, "diaChi", company.get('address') or '')

    bang = ET.SubElement(tkhai, "BangKe")
    for i, line in enumerate(ws.get('lines') or [], start=1):
        row = ET.SubElement(bang, "Dong")
        _txt(row, "stt", i)
        _txt(row, "thang", line.get('month'))
        _txt(row, "hoTen", line.get('fullname') or '')
        _txt(row, "mst_cccd", line.get('tax_code') or '')
        _txt(row, "thuNhapChiuThue", _money_str(line.get('taxable_income')))
        _txt(row, "thueTNCN", _money_str(line.get('pit_amount')))
        _txt(row, "thucLinh", _money_str(line.get('net_pay')))

    tong = ET.SubElement(tkhai, "TongHop")
    _txt(tong, "soDong", totals.get('employee_rows') or 0)
    _txt(tong, "tongThuNhapChiuThue", _money_str(totals.get('taxable_income')))
    _txt(tong, "tongThueTNCN", _money_str(totals.get('pit_withheld')))
    _txt(tong, "psCo3335", _money_str(totals.get('journal_3335_net_credit')))
    _txt(tong, "lechVsSo", _money_str(totals.get('difference_vs_journal')))

    note = ET.SubElement(tkhai, "GhiChu")
    note.text = (
        "File XML khung do phần mềm SME tạo để đối chiếu / hỗ trợ kê khai. "
        "Nộp chính thức qua HTKK / eTax của cơ quan thuế."
    )

    xml_str = _pretty(root)
    fname = f"05_KK_TNCN_{year}_{p_from:02d}-{p_to:02d}.xml"
    return {
        'success': True,
        'filename': fname,
        'xml': xml_str,
        'worksheet': ws,
        'disclaimer': note.text,
    }
