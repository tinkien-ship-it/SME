# -*- coding: utf-8 -*-
"""Xuất XML tờ khai TNDN SME — khung HTKK rút gọn (đối chiếu)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from xml.dom import minidom

from Services.sme.cit_declaration import cit_declaration_worksheet

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
        row = None
    if not row:
        return {"mst": "", "tenNNT": "", "dchiNNT": "", "nguoiKy": ""}
    d = dict(row)
    name = (d.get("business_name") or "").strip()
    rep = (d.get("representative_name") or "").strip()
    return {
        "mst": (d.get("tax_code") or "").strip(),
        "tenNNT": name or rep,
        "dchiNNT": (d.get("address") or "").strip(),
        "nguoiKy": rep or name,
    }


def generate_sme_cit_xml(
    conn,
    *,
    fiscal_year: int,
    period_to: int = 12,
    tax_rate: float | None = None,
    adjustments: dict | None = None,
    company: dict | None = None,
) -> dict[str, Any]:
    """Sinh XML khung gần HTKK 03/TNDN từ worksheet sổ kép."""
    ws = cit_declaration_worksheet(
        conn,
        fiscal_year=fiscal_year,
        period_to=period_to,
        tax_rate=tax_rate,
        adjustments=adjustments,
    )
    co = company or _company_from_conn(conn)
    now = datetime.now()

    root = ET.Element("HSoThueDTu")
    root.set("xmlns", NS)
    ttin = ET.SubElement(root, "TTinChung")
    _txt(ttin, "maTKhai", "03/TNDN")
    _txt(ttin, "tenTKhai", "To khai quyet toan thue TNDN (SME doi chieu)")
    _txt(ttin, "pbanTKhaiXML", "2.0.0")
    _txt(ttin, "kyKKhai", f"Quyết toán {fiscal_year}" if period_to >= 12 else f"Lũy kế T{period_to}/{fiscal_year}")
    _txt(ttin, "kyKKhaiTuNgay", f"{fiscal_year}-01-01")
    _txt(ttin, "kyKKhaiDenNgay", f"{fiscal_year}-{period_to:02d}-28")
    _txt(ttin, "maCQTNoiNop", "")
    nnt = ET.SubElement(ttin, "NNT")
    _txt(nnt, "mst", co.get("mst") or "")
    _txt(nnt, "tenNNT", co.get("tenNNT") or "")
    _txt(nnt, "dchiNNT", co.get("dchiNNT") or "")

    clieu = ET.SubElement(root, "CTieuTKhaiChinh")
    by_code = {ln["code"]: ln["amount"] for ln in ws["lines"]}
    mapping = [
        ("ctA1", "A1"),
        ("ctB1", "B1"),
        ("ctB2", "B2"),
        ("ctC", "C"),
        ("ctC1", "C1"),
        ("ctD", "D"),
        ("ctE", "E"),
        ("ctF", "F"),
    ]
    for tag, code in mapping:
        _txt(clieu, tag, _money_str(by_code.get(code)))

    _txt(clieu, "thueSuat", f"{ws['tax_rate']:.2f}")
    _txt(clieu, "ngayLap", now.strftime("%Y-%m-%d"))
    _txt(clieu, "nguoiKy", co.get("nguoiKy") or "")

    xml_str = _pretty(root)
    filename = f"TNDN_{fiscal_year}_T{period_to:02d}_{co.get('mst') or 'SME'}.xml"
    return {
        "success": True,
        "filename": filename,
        "xml": xml_str,
        "worksheet": ws,
    }
