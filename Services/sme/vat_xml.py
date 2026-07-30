# -*- coding: utf-8 -*-
"""Xuất XML tờ khai GTGT SME — khung HTKK (đối chiếu sổ kép)."""
from __future__ import annotations

import calendar
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from xml.dom import minidom

from Services.sme.vat_declaration import vat_declaration_worksheet

NS = "http://kekhaithue.gdt.gov.vn/TKhaiThue"
XSI = "http://www.w3.org/2001/XMLSchema-instance"


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
    """Lấy MST/tên DN từ business_info (tenant DB)."""
    try:
        row = conn.execute("SELECT * FROM business_info LIMIT 1").fetchone()
    except Exception:
        row = None
    if not row:
        return {
            "mst": "",
            "tenNNT": "",
            "dchiNNT": "",
            "dthoaiNNT": "",
            "emailNNT": "",
            "nguoiKy": "",
        }
    d = dict(row) if not isinstance(row, dict) else row
    name = (d.get("business_name") or "").strip()
    rep = (d.get("representative_name") or "").strip()
    return {
        "mst": (d.get("tax_code") or "").strip(),
        "tenNNT": name or rep,
        "dchiNNT": (d.get("address") or "").strip(),
        "dthoaiNNT": (d.get("phone") or "").strip(),
        "emailNNT": (d.get("email") or "").strip(),
        "nguoiKy": rep or name,
    }


def generate_sme_vat_xml(
    conn,
    *,
    fiscal_year: int,
    period: int | None = None,
    quarter: int | None = None,
    filing_mode: str | None = None,
    company: dict | None = None,
    loai_tkhai: str = "C",
    so_lan: str = "1",
) -> dict[str, Any]:
    """
    Sinh XML khung HTKK từ chỉ tiêu sổ kép SME.

    Đây là file đối chiếu / lưu trữ cấu trúc gần HTKK — không đảm bảo
    import thẳng eTax nếu schema 01/GTGT chính thức thay đổi theo bản HTKK.
    """
    ws = vat_declaration_worksheet(
        conn,
        fiscal_year=fiscal_year,
        period=period,
        quarter=quarter,
        filing_mode=filing_mode,
    )
    co = dict(company or {})
    if not co.get("mst") and not co.get("tenNNT"):
        co = {**_company_from_conn(conn), **{k: v for k, v in co.items() if v}}

    mode = ws["filing_mode"]
    p_from, p_to = int(ws["period_from"]), int(ws["period_to"])
    y = int(ws["fiscal_year"])
    _, last_day = calendar.monthrange(y, p_to)
    today = datetime.now().strftime("%Y-%m-%d")
    ngay_vn = datetime.now().strftime("%d/%m/%Y")

    if mode == "quarterly":
        kieu_ky, ky_kkhai = "Q", f"{ws['quarter']}/{y}"
    else:
        kieu_ky, ky_kkhai = "M", f"{p_from:02d}/{y}"

    inds = {i["code"]: i["amount"] for i in ws.get("indicators") or []}
    summary = ws.get("summary") or {}

    root = ET.Element(
        "HSoThueDTu",
        {
            "xmlns": NS,
            "xmlns:xsi": XSI,
            "xsi:schemaLocation": f"{NS} ToKhaiThue.xsd",
        },
    )
    hso = ET.SubElement(root, "HSoKhaiThue", {"id": "ID_1"})

    ttinchung = ET.SubElement(hso, "TTinChung")
    ttindvu = ET.SubElement(ttinchung, "TTinDVu")
    _txt(ttindvu, "maDVu", "HTKK")
    _txt(ttindvu, "tenDVu", "HỖ TRỢ KÊ KHAI THUẾ")
    _txt(ttindvu, "pbanDVu", "SME")
    _txt(ttindvu, "ttinNhaCCapDVu", "SME-POS")

    ttintk = ET.SubElement(ttinchung, "TTinTKhaiThue")
    tk = ET.SubElement(ttintk, "TKhaiThue")
    _txt(tk, "maTKhai", "01")
    _txt(tk, "tenTKhai", "Tờ khai thuế giá trị gia tăng (đối chiếu sổ kép SME)")
    _txt(
        tk,
        "moTaBMau",
        "Xuất từ sổ kép SME — dùng đối chiếu / lưu trữ; kiểm tra lại trước khi nộp eTax",
    )
    _txt(tk, "pbanTKhaiXML", "SME-1.0")
    _txt(tk, "loaiTKhai", loai_tkhai or "C")
    _txt(tk, "soLan", so_lan or "1")

    ky = ET.SubElement(tk, "KyKKhaiThue")
    _txt(ky, "kieuKy", kieu_ky)
    _txt(ky, "kyKKhai", ky_kkhai)
    _txt(ky, "kyKKhaiTuNgay", f"01/{p_from:02d}/{y}")
    _txt(ky, "kyKKhaiDenNgay", f"{last_day:02d}/{p_to:02d}/{y}")
    _txt(ky, "kyKKhaiTuThang", f"{p_from:02d}/{y}")
    _txt(ky, "kyKKhaiDenThang", f"{p_to:02d}/{y}")

    _txt(tk, "maCQTNoiNop", co.get("maCQTNoiNop") or "")
    _txt(tk, "tenCQTNoiNop", co.get("tenCQTNoiNop") or "")
    _txt(tk, "ngayLapTKhai", ngay_vn)
    _txt(tk, "nguoiKy", co.get("nguoiKy") or "")
    _txt(tk, "ngayKy", ngay_vn)

    giahan = ET.SubElement(tk, "GiaHan")
    _txt(giahan, "maLyDoGiaHan", "")
    _txt(giahan, "lyDoGiaHan", "")

    nnt = ET.SubElement(ttintk, "NNT")
    _txt(nnt, "mst", co.get("mst") or "")
    _txt(nnt, "tenNNT", co.get("tenNNT") or "")
    _txt(nnt, "dchiNNT", co.get("dchiNNT") or "")
    _txt(nnt, "dthoaiNNT", co.get("dthoaiNNT") or "")
    _txt(nnt, "emailNNT", co.get("emailNNT") or "")

    ctieu = ET.SubElement(hso, "CTieuTKhaiChinh")
    _txt(ctieu, "ct21", _money_str(inds.get("21", summary.get("revenue"))))
    _txt(ctieu, "ct22", _money_str(inds.get("22", summary.get("vat_output"))))
    _txt(ctieu, "ct23", _money_str(inds.get("23", summary.get("vat_input"))))
    _txt(ctieu, "ct24", _money_str(inds.get("24", summary.get("vat_credit"))))
    _txt(ctieu, "ct25", _money_str(inds.get("25", summary.get("vat_payable"))))
    _txt(ctieu, "ct26", _money_str(inds.get("26")))
    _txt(ctieu, "ct27", _money_str(inds.get("27")))

    # Chi tiết tháng (hữu ích khi kê khai quý)
    if len(ws.get("monthly_break") or []) > 1:
        bang = ET.SubElement(ctieu, "ChiTietThang")
        for row in ws["monthly_break"]:
            r = ET.SubElement(bang, "Thang", {"ky": str(row["period"])})
            _txt(r, "doanhThu", _money_str(row.get("revenue")))
            _txt(r, "vatRa", _money_str(row.get("vat_output")))
            _txt(r, "vatVao", _money_str(row.get("vat_input")))
            _txt(r, "phaiNop", _money_str(row.get("vat_payable")))

    note = ET.SubElement(ctieu, "GhiChu")
    _txt(
        note,
        "noiDung",
        "File XML SME-1.0 từ sổ kép (511/33311/133). Không thay thế file HTKK chính thức nếu cổng thuế yêu cầu schema mới hơn.",
    )

    xml_text = _pretty(root)
    fname = f"GTGT_SME_{y}_{mode}_{ky_kkhai.replace('/', '-')}.xml"
    return {
        "xml": xml_text,
        "filename": fname,
        "worksheet": ws,
        "company": co,
        "generated_at": today,
    }
