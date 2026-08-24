"""Routes kê khai thuế HKD — tách từ app.py."""
import calendar
import re
import sqlite3
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime
from io import BytesIO
from xml.dom import minidom

import requests
from flask import jsonify, render_template, request, send_file

from auth import login_required
from db_utils import get_db_connection, sqlite_commit
from Services.payment_bank import get_tax_default_bank_accounts
from helpers import parse_number_vn
from Services.hkd_sector import (
    HKD_SECTOR_OPTIONS,
    HKD_TAX_RATES,
    HKD_XML_GTGT_TIEU_MUC,
    HKD_XML_SECTOR_ROWS,
    HKD_XML_TNCN_TIEU_MUC,
)


def _sector_form_amounts(data, prefix):
    """Đọc số liệu G1–G4 từ form (dt_g1, thue_gtgt_g2, …)."""
    keys = ('g1', 'g2', 'g3', 'g4')
    vals = [round(parse_number_vn(data.get(f'{prefix}_{k}', 0) or 0)) for k in keys]
    return vals, sum(vals)


def _fill_kk_tax_block(kk_parent, tag_name, sector_vals, total):
    """Ghi khối DoanhThuThueGTGT / SoThueGTGT / … theo cột G1–G4 (ct28–ct31) + tổng ct32."""
    tag = ET.SubElement(kk_parent, tag_name)
    for idx, val in enumerate(sector_vals, start=28):
        ET.SubElement(tag, f"ct{idx}").text = str(val)
    ET.SubElement(tag, "ct32").text = str(round(total))


def _supplemental_sector_items():
    """Các chỉ tiêu so sánh tờ khai bổ sung (doanh thu/thuế G1–G4)."""
    return [
        ('dt_g1', '[28a]', 'Doanh thu GTGT G1', 'dt_g1'),
        ('dt_g2', '[29a]', 'Doanh thu GTGT G2', 'dt_g2'),
        ('dt_g3', '[30a]', 'Doanh thu GTGT G3', 'dt_g3'),
        ('dt_g4', '[31a]', 'Doanh thu GTGT G4', 'dt_g4'),
        ('doanhthu', '[28]', 'Tổng doanh thu GTGT', 'dt_gtgt_ct28'),
        ('thue_gtgt_g1', '[28b]', 'Thuế GTGT G1', 'thue_gtgt_g1'),
        ('thue_gtgt_g2', '[29b]', 'Thuế GTGT G2', 'thue_gtgt_g2'),
        ('thue_gtgt_g3', '[30b]', 'Thuế GTGT G3', 'thue_gtgt_g3'),
        ('thue_gtgt_g4', '[31b]', 'Thuế GTGT G4', 'thue_gtgt_g4'),
        ('thue_gtgt', '[28]', 'Tổng thuế GTGT', 'so_gtgt_ct28'),
        ('thue_tncn_g1', '[28d]', 'Thuế TNCN G1', 'thue_tncn_g1'),
        ('thue_tncn_g2', '[29d]', 'Thuế TNCN G2', 'thue_tncn_g2'),
        ('thue_tncn_g3', '[30d]', 'Thuế TNCN G3', 'thue_tncn_g3'),
        ('thue_tncn_g4', '[31d]', 'Thuế TNCN G4', 'thue_tncn_g4'),
        ('thue_tncn', '[28]', 'Tổng thuế TNCN', 'so_tncn_ct28'),
    ]


def _fetch_original_sector_data(ky_str):
    """Lấy số liệu G1–G4 từ tờ khai chính thức đã lưu + sổ doanh thu."""
    old = {}
    if not ky_str or '/' not in ky_str:
        return old
    try:
        q, y = map(int, ky_str.split('/'))
        start_month = (q - 1) * 3 + 1
        end_month = q * 3
        _, last_day = calendar.monthrange(y, end_month)
        start_date = f"{y}-{start_month:02d}-01"
        end_date = f"{y}-{end_month:02d}-{last_day:02d}"

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""
            SELECT doanh_thu, thue_gtgt, thue_tncn
            FROM tax_declarations
            WHERE ky_khai = ? AND loai_tkhai = 'C'
            ORDER BY created_at DESC LIMIT 1
        """, (ky_str,))
        row = c.fetchone()

        from Services.hkd_revenue import fetch_hkd_revenue_ledger
        ledger = fetch_hkd_revenue_ledger(c, start_date, end_date)
        totals = ledger.get('totals') or {}
        taxes = ledger.get('taxes') or {}
        conn.close()

        old = {
            'dt_g1': round(float(totals.get('g1') or 0)),
            'dt_g2': round(float(totals.get('g2') or 0)),
            'dt_g3': round(float(totals.get('g3') or 0)),
            'dt_g4': round(float(totals.get('g4') or 0)),
            'doanhthu': round(float(row['doanh_thu'] if row else totals.get('total') or 0)),
            'thue_gtgt_g1': round(float((taxes.get('g1') or {}).get('gtgt') or 0)),
            'thue_gtgt_g2': round(float((taxes.get('g2') or {}).get('gtgt') or 0)),
            'thue_gtgt_g3': round(float((taxes.get('g3') or {}).get('gtgt') or 0)),
            'thue_gtgt_g4': round(float((taxes.get('g4') or {}).get('gtgt') or 0)),
            'thue_gtgt': round(float(row['thue_gtgt'] if row else taxes.get('total_gtgt') or 0)),
            'thue_tncn_g1': round(float((taxes.get('g1') or {}).get('tncn') or 0)),
            'thue_tncn_g2': round(float((taxes.get('g2') or {}).get('tncn') or 0)),
            'thue_tncn_g3': round(float((taxes.get('g3') or {}).get('tncn') or 0)),
            'thue_tncn_g4': round(float((taxes.get('g4') or {}).get('tncn') or 0)),
            'thue_tncn': round(float(row['thue_tncn'] if row else taxes.get('total_tncn') or 0)),
        }
    except Exception as e:
        print(f"Error fetching original sector data: {e}")
    return old


def _append_pluc_bk_stk(pluc_root, data):
    """Phụ lục 01/BK-STK — thông báo số tài khoản / ví điện tử (TT 50/2026)."""
    pl_stk = ET.SubElement(pluc_root, "PLuc_01_BK_STK")
    idx = 1
    while f"stk_so_tk_{idx}" in data or f"stk_ten_ddkd_{idx}" in data:
        so_tk = (data.get(f"stk_so_tk_{idx}") or '').strip()
        ten_ddkd = (data.get(f"stk_ten_ddkd_{idx}") or '').strip()
        if not so_tk and not ten_ddkd:
            idx += 1
            continue
        item = ET.SubElement(pl_stk, "CTietSTK", {"id": f"ID_{idx}"})
        ET.SubElement(item, "ct04").text = ten_ddkd
        ET.SubElement(item, "ct05").text = data.get(f"stk_ma_ddkd_{idx}", '')
        ET.SubElement(item, "ct06").text = so_tk
        ET.SubElement(item, "ct07").text = data.get(f"stk_chu_tk_{idx}", '')
        ET.SubElement(item, "ct08").text = data.get(f"stk_noi_mo_{idx}", '')
        ET.SubElement(item, "ct09").text = data.get(f"stk_trang_thai_{idx}", 'KhaiLanDau')
        idx += 1


def prettify(elem):
    """Pretty print XML — định dạng giống file xuất HTKK mẫu."""
    try:
        rough_string = ET.tostring(elem, encoding='utf-8')
        reparsed = minidom.parseString(rough_string)
        xml_str = reparsed.toprettyxml(indent=" ")
        if xml_str.startswith('<?xml'):
            xml_str = '<?xml version="1.0" ?>\n' + xml_str.split('\n', 1)[1]
        return xml_str
    except Exception:
        xml_str = ET.tostring(elem, encoding='unicode')
        if not xml_str.startswith('<?xml'):
            xml_str = '<?xml version="1.0" ?>\n' + xml_str
        return xml_str


def _load_nganh_list(data):
    """Ngành nghề từ form (maNNghe_1…) hoặc bảng hkd_nganh_nghe."""
    mst = str(data.get('mst') or '').strip()
    rows = []
    idx = 1
    while f"maNNghe_{idx}" in data or f"tenNNghe_{idx}" in data:
        ma = (data.get(f"maNNghe_{idx}") or '').strip()
        ten = (data.get(f"tenNNghe_{idx}") or '').strip()
        if ma or ten:
            rows.append((ma, ten or ma))
        idx += 1
    if rows:
        return rows
    if not mst:
        return []
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(
            """SELECT ma_nganh, ten_nganh FROM hkd_nganh_nghe
               WHERE TRIM(mst) = ? ORDER BY thu_tu ASC""",
            (mst,),
        )
        db_rows = c.fetchall()
        conn.close()
        return [
            (str(r['ma_nganh'] or '').strip(), str(r['ten_nganh'] or '').strip())
            for r in db_rows
            if (r['ma_nganh'] or r['ten_nganh'])
        ]
    except Exception:
        return []


def _append_pluc_01_2_content(pluc_root, data):
    """Nội dung PLuc_01_2_BK_HDKD bên trong thẻ PLuc."""
    pl_01_2 = ET.SubElement(pluc_root, "PLuc_01_2_BK_HDKD")
    vlieu = ET.SubElement(pl_01_2, "VlieuDcuSPHH")
    bke = ET.SubElement(vlieu, "BKeVLDCSPHH")
    tong_ct09 = tong_ct11 = tong_ct13 = tong_ct15 = 0.0
    j = 1
    while f"ct06_{j}" in data:
        item = ET.SubElement(bke, "CTietHKDCNKD", {"id": f"ID_{j}"})
        for c in range(6, 16):
            tag = f"ct{c:02d}"
            val = data.get(f"{tag}_{j}", '0') or '0'
            ET.SubElement(item, tag).text = str(val)
            try:
                fval = float(val)
            except (TypeError, ValueError):
                fval = 0.0
            if c == 9:
                tong_ct09 += fval
            elif c == 11:
                tong_ct11 += fval
            elif c == 13:
                tong_ct13 += fval
            elif c == 15:
                tong_ct15 += fval
        j += 1
    ET.SubElement(bke, "ct17").text = str(round(tong_ct09))
    ET.SubElement(bke, "ct19").text = str(round(tong_ct11))
    ET.SubElement(bke, "ct21").text = str(round(tong_ct13))
    ET.SubElement(bke, "ct23").text = str(round(tong_ct15))
    chiphi = ET.SubElement(pl_01_2, "ChiPhiQL")
    for c in range(24, 32):
        ET.SubElement(chiphi, f"ct{c}").text = str(data.get(f"ct{c}", '0') or '0')


def _append_pluc_01_2_bk_hdkd(hso, data):
    """Phụ lục 01-2/BK-HĐKD — tạo thẻ PLuc + nội dung."""
    pluc_root = ET.SubElement(hso, "PLuc")
    _append_pluc_01_2_content(pluc_root, data)


def has_pluc_01_2_data(data):
    j = 1
    while f"ct06_{j}" in data:
        for c in range(6, 16):
            val = str(data.get(f"ct{c:02d}_{j}", '') or '').strip()
            if val and val != '0':
                return True
        j += 1
    for c in range(24, 32):
        if parse_number_vn(data.get(f"ct{c}", 0)) != 0:
            return True
    return False


def has_pluc_bk_stk_data(data):
    idx = 1
    while f"stk_so_tk_{idx}" in data or f"stk_ten_ddkd_{idx}" in data:
        if (data.get(f"stk_so_tk_{idx}") or '').strip() or (data.get(f"stk_ten_ddkd_{idx}") or '').strip():
            return True
        idx += 1
    return False


def has_supplemental_pluc_data(data):
    return (data.get('loaiTKhai') or 'C').upper() == 'B'


def _pluc_bk_stk_rows(data):
    """Các dòng BK-STK có ít nhất một trường đã nhập."""
    rows = []
    idx = 1
    while f"stk_so_tk_{idx}" in data or f"stk_ten_ddkd_{idx}" in data:
        row = {
            'ten_ddkd': (data.get(f"stk_ten_ddkd_{idx}") or '').strip(),
            'ma_ddkd': (data.get(f"stk_ma_ddkd_{idx}") or '').strip(),
            'so_tk': (data.get(f"stk_so_tk_{idx}") or '').strip(),
            'chu_tk': (data.get(f"stk_chu_tk_{idx}") or '').strip(),
            'noi_mo': (data.get(f"stk_noi_mo_{idx}") or '').strip(),
        }
        if any(row.values()):
            rows.append((idx, row))
        idx += 1
    return rows


def validate_tax_pluc_export(data):
    """
    Kiểm tra phụ lục trước khi xuất XML.
    Trả về (errors, include_pluc, pluc_started).
    Phụ lục chỉ được gắn vào file khi điền đủ thông tin bắt buộc.
    """
    errors = []
    bk_rows = _pluc_bk_stk_rows(data)
    required_bk = ('ten_ddkd', 'so_tk', 'chu_tk', 'noi_mo')
    bk_labels = {
        'ten_ddkd': '[04] Tên ĐĐKD',
        'so_tk': '[06] Số TK/ví',
        'chu_tk': '[07] Chủ TK',
        'noi_mo': '[08] Nơi mở',
    }

    for idx, row in bk_rows:
        missing = [bk_labels[k] for k in required_bk if not row.get(k)]
        if missing:
            errors.append(f'Phụ lục 01/BK-STK — dòng {idx}: thiếu {", ".join(missing)}')

    if data.get('ct01b') == '1' and not bk_rows:
        errors.append(
            'Đã chọn [01b] — cần kê khai đầy đủ ít nhất một tài khoản ở Phụ lục 01/BK-STK'
        )

    loai_b = has_supplemental_pluc_data(data)
    has_01_2 = has_pluc_01_2_data(data)
    bk_complete = bool(bk_rows) and not any(
        not all(row.get(k) for k in required_bk) for _, row in bk_rows
    )

    include_pluc = bk_complete or has_01_2 or loai_b
    pluc_started = (
        data.get('ct01b') == '1'
        or bool(bk_rows)
        or has_01_2
        or loai_b
    )
    return errors, include_pluc, pluc_started


def has_tax_pluc_data(data):
    """True khi phụ lục đủ điều kiện gắn vào file XML."""
    _, include_pluc, _ = validate_tax_pluc_export(data)
    return include_pluc


def _bk_stk_rows_complete(data):
    rows = _pluc_bk_stk_rows(data)
    required = ('ten_ddkd', 'so_tk', 'chu_tk', 'noi_mo')
    return bool(rows) and all(all(row.get(k) for k in required) for _, row in rows)


def split_xml_preview(full_xml, has_pluc):
    """Tách nội dung xem trước: tờ khai chính và phụ lục (cùng một file gốc)."""
    if not has_pluc or not full_xml:
        return full_xml, None
    pluc_parts = re.findall(r'<PLuc>.*?</PLuc>', full_xml, re.DOTALL)
    if not pluc_parts:
        return full_xml, None
    pluc_preview = '\n'.join(pluc_parts)
    main_preview = re.sub(r'\s*<PLuc>.*?</PLuc>', '', full_xml, flags=re.DOTALL)
    return main_preview.strip(), pluc_preview.strip()


def _ensure_pluc_node(hso):
    for child in list(hso):
        if child.tag == 'PLuc':
            return child
    return ET.SubElement(hso, "PLuc")


def _append_all_pluc_sections(hso, data, loai_tkhai, ky_str, start_month, end_month, last_day, y, xsi):
    """Gắn các phụ lục có dữ liệu vào HSoKhaiThue. Trả về True nếu có ít nhất một PLuc."""
    added = False
    pluc_root = None
    if has_pluc_01_2_data(data):
        pluc_root = _ensure_pluc_node(hso)
        _append_pluc_01_2_content(pluc_root, data)
        added = True
    if _bk_stk_rows_complete(data):
        pluc_root = _ensure_pluc_node(hso) if pluc_root is None else pluc_root
        _append_pluc_bk_stk(pluc_root, data)
        added = True
    if has_supplemental_pluc_data(data) and start_month:
        _append_supplemental_pluc(
            hso, data, loai_tkhai, ky_str, start_month, end_month, last_day, y, xsi,
        )
        added = True
    return added


def _append_kkhai_thue_ttdb_tn(ctieu):
    """KKhaiThueTTDB + KKhaiTBVMT_TN — khối cố định như mẫu HTKK."""
    kkhai_tt = ET.SubElement(ctieu, "KKhaiThueTTDB")
    ct_tt = ET.SubElement(kkhai_tt, "CTietKKhaiThueTTDB", {"id": "ID_1"})
    for tag in ("ct2_ma", "ct2_ten", "ct3", "ct4"):
        ET.SubElement(ct_tt, tag).text = ""
    for tag in ("ct5", "ct6", "ct7"):
        ET.SubElement(ct_tt, tag).text = "0"
    ET.SubElement(kkhai_tt, "tong_ct5").text = "0"
    ET.SubElement(kkhai_tt, "tong_ct7").text = "0"

    kkhai_tn = ET.SubElement(ctieu, "KKhaiTBVMT_TN")
    thue_tn = ET.SubElement(kkhai_tn, "ThueTaiNguyen")
    ct_tn = ET.SubElement(thue_tn, "CTietThueTaiNguyen", {"id": "ID_1"})
    for tag in ("ct2_ma", "ct2_ten", "ct3", "ct4"):
        ET.SubElement(ct_tn, tag).text = ""
    for tag in ("ct5", "ct6", "ct7", "ct8"):
        ET.SubElement(ct_tn, tag).text = "0"
    ET.SubElement(thue_tn, "tongCong").text = "0"

    thue_bvmt = ET.SubElement(kkhai_tn, "ThueBVMT")
    ct_bvmt = ET.SubElement(thue_bvmt, "CTietThueBVMT", {"id": "ID_1"})
    for tag in ("ct2_ma", "ct2_ten", "ct3", "ct4"):
        ET.SubElement(ct_bvmt, tag).text = ""
    for tag in ("ct5", "ct6", "ct8"):
        ET.SubElement(ct_bvmt, tag).text = "0"
    ET.SubElement(thue_bvmt, "tongCong").text = "0"

    phi_bvmt = ET.SubElement(kkhai_tn, "PhiBVMT")
    ct_phi = ET.SubElement(phi_bvmt, "CTietPhiBVMT", {"id": "ID_1"})
    for tag in ("ct2_ma", "ct2_ten", "ct3", "ct4"):
        ET.SubElement(ct_phi, tag).text = ""
    for tag in ("ct5", "ct6", "ct8"):
        ET.SubElement(ct_phi, tag).text = "0"
    ET.SubElement(phi_bvmt, "tongCong").text = "0"


def _sub_nil(parent, tag, xsi):
    el = ET.SubElement(parent, tag)
    el.set(f'{{{xsi}}}nil', 'true')
    return el


def _fmt_vn_date(value, iso_fallback=True):
    """Chuyển ngày form → dd/mm/yyyy (ngayLapTKhai) hoặc yyyy-mm-dd (ngayKy)."""
    raw = (value or '').strip()
    if not raw:
        raw = datetime.now().strftime('%Y-%m-%d')
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            d = datetime.strptime(raw[:10], fmt)
            if iso_fallback:
                return d.strftime('%Y-%m-%d')
            return d.strftime('%d/%m/%Y')
        except ValueError:
            continue
    return raw


def _resolve_tax_payload(data):
    """Lấy số liệu G1–G4 + thuế từ sổ doanh thu (ưu tiên) hoặc form."""
    from Services.hkd_revenue import fetch_hkd_revenue_ledger, _ytd_total_before_period

    ky_str = (data.get('kyKKhai') or '').strip()
    lines = []
    ytd_before = 0.0
    totals = {'g1': 0, 'g2': 0, 'g3': 0, 'g4': 0, 'total': 0}
    taxes = {'total_gtgt': 0, 'total_tncn': 0, 'tncn_meta': {}}

    if '/' in ky_str:
        try:
            q, y = map(int, ky_str.split('/'))
            start_month = (q - 1) * 3 + 1
            end_month = q * 3
            _, last_day = calendar.monthrange(y, end_month)
            start_date = f"{y}-{start_month:02d}-01"
            end_date = f"{y}-{end_month:02d}-{last_day:02d}"
            conn = get_db_connection()
            c = conn.cursor()
            ytd_before = _ytd_total_before_period(c, start_date)
            ledger = fetch_hkd_revenue_ledger(c, start_date, end_date)
            conn.close()
            totals = ledger.get('totals') or totals
            taxes = ledger.get('taxes') or taxes
        except Exception:
            pass  # fallback form bên dưới

    if not any(float(totals.get(k) or 0) for k in ('g1', 'g2', 'g3', 'g4')):
        dt_sectors, _ = _sector_form_amounts(data, 'dt')
        gtgt_sectors, _ = _sector_form_amounts(data, 'thue_gtgt')
        tncn_sectors, _ = _sector_form_amounts(data, 'thue_tncn')
        for i, (key, *_rest) in enumerate(HKD_XML_SECTOR_ROWS):
            totals[key] = dt_sectors[i]
        totals['total'] = sum(dt_sectors)
        from Services.hkd_sector import calc_sector_taxes
        taxes = calc_sector_taxes(
            {key: totals[key] for key, *_ in HKD_XML_SECTOR_ROWS},
            ytd_before=ytd_before,
        )

    for key, ma, ten, col in HKD_XML_SECTOR_ROWS:
        rev = round(float(totals.get(key) or 0))
        if rev <= 0:
            continue
        sec = taxes.get(key) or {}
        dt_tncn = round(float(sec.get('dt_tncn') or 0))
        gtgt = round(float(sec.get('gtgt') or 0))
        tncn = round(float(sec.get('tncn') or 0))
        ct16_tru = rev - dt_tncn  # phần DT được trừ (chưa/chưa hết ngưỡng 1 tỷ)
        lines.append({
            'key': key,
            'ma': ma,
            'ten': ten,
            'col': col,
            'ct11': rev,
            'ct12': 0,
            'ct13': 0,
            'ct14': gtgt,
            'ct15': rev,
            'ct16': ct16_tru,
            'ct17': tncn,
        })

    return lines, ytd_before, totals, taxes


def _append_khai_thue_gtgt_tncn(parent, data, lines, ytd_before, taxes):
    """Khối KhaiThueGTGTTNCN theo HTKK 2.9.8."""
    kk = ET.SubElement(parent, "KhaiThueGTGTTNCN")
    kd = ET.SubElement(kk, "KinhDoanhDiaDiemCoDinh")

    mst_cu = (data.get('mst_cu') or data.get('mst') or '').strip()
    ten_ddkd = (data.get('ct05') or data.get('tenNNT') or '').strip()
    ma_ddkd = (data.get('ma_diaDiemKD') or '00999').strip()

    dd = ET.SubElement(kd, "DoanhThuThueGTGTTNCN", {"id": "1"})
    ET.SubElement(dd, "stt").text = "1"
    ET.SubElement(dd, "truSoChinh").text = "true"
    ET.SubElement(dd, "mst_diaDiemKD").text = mst_cu
    ET.SubElement(dd, "ma_diaDiemKD").text = ma_ddkd
    ET.SubElement(dd, "ten_diaDiemKD").text = ten_ddkd

    dia_chi = ET.SubElement(dd, "diaChi_diaDiemKD")
    ET.SubElement(dia_chi, "diaChi").text = (data.get('ct12b_soNha') or '').strip()
    ET.SubElement(dia_chi, "ma_xaPhuong").text = (data.get('ct12c_maPhuong') or '').strip()
    ET.SubElement(dia_chi, "ten_xaPhuong").text = (data.get('ct12c_tenPhuong') or '').strip()
    ET.SubElement(dia_chi, "ma_tinh").text = (data.get('ct12d_maTinh') or '701').strip()
    ET.SubElement(dia_chi, "ten_tinh").text = (data.get('ct12d_tenTinh') or 'Thành phố Hồ Chí Minh').strip()

    gtgt_ma, gtgt_ten = HKD_XML_GTGT_TIEU_MUC
    tncn_ma, tncn_ten = HKD_XML_TNCN_TIEU_MUC

    sums = {f'tongCT{n}': 0 for n in (11, 12, 13, 14, 15, 16, 17)}
    for idx, line in enumerate(lines, 1):
        ct = ET.SubElement(dd, "CTietDoanhThu", {"id": str(idx)})
        ET.SubElement(ct, "ct09_ma").text = line['ma']
        ET.SubElement(ct, "ct09_ten").text = line['ten']
        ET.SubElement(ct, "ct10").text = line['col']
        ET.SubElement(ct, "ct11").text = str(line['ct11'])
        ET.SubElement(ct, "ct12").text = str(line['ct12'])
        ET.SubElement(ct, "ct13").text = str(line['ct13'])
        ET.SubElement(ct, "ct14_maTieuMuc").text = gtgt_ma
        ET.SubElement(ct, "ct14_tenTieuMuc").text = gtgt_ten
        ET.SubElement(ct, "ct14_soThue").text = str(line['ct14'])
        ET.SubElement(ct, "ct15").text = str(line['ct15'])
        ET.SubElement(ct, "ct16").text = str(line['ct16'])
        ET.SubElement(ct, "ct17_maTieuMuc").text = tncn_ma
        ET.SubElement(ct, "ct17_tenTieuMuc").text = tncn_ten
        ET.SubElement(ct, "ct17_soThue").text = str(line['ct17'])
        for n in (11, 12, 13, 14, 15, 16, 17):
            sums[f'tongCT{n}'] += line[f'ct{n}'] if n != 14 and n != 17 else line['ct14' if n == 14 else 'ct17']

    tong = ET.SubElement(kk, "TongCongCT18")
    for n in (11, 12, 13, 14, 15, 16, 17):
        val = sums[f'tongCT{n}']
        if n in (14, 17):
            val = round(float(taxes.get('total_gtgt' if n == 14 else 'total_tncn') or 0))
        ET.SubElement(tong, f"tongCT{n}").text = str(val)

    mien = ET.SubElement(kk, "SoThueMienCT19")
    ET.SubElement(mien, "soThueDuocMienCT14").text = "0"
    ET.SubElement(mien, "soThueDuocMienCT17").text = "0"

    nop = ET.SubElement(kk, "SoThueConPhaiNopCT20")
    tong_gtgt = round(float(taxes.get('total_gtgt') or 0))
    tong_tncn = round(float(taxes.get('total_tncn') or 0))
    ET.SubElement(nop, "soThueGTGT_ct14").text = str(tong_gtgt)
    ET.SubElement(nop, "soThueTNCN_ct17").text = str(tong_tncn)

    return tong_gtgt, tong_tncn


def _append_empty_tax_sections(ctieu):
    """KKhaiThueTTDB + KKhaiTaiNguyen — mặc định 0 như mẫu HTKK."""
    ttdb = ET.SubElement(ctieu, "KKhaiThueTTDB")
    tc22 = ET.SubElement(ttdb, "TongCongCT22")
    ET.SubElement(tc22, "tongCT5").text = "0"
    ET.SubElement(tc22, "tongCT7").text = "0"
    ET.SubElement(ttdb, "soThueMienCT23").text = "0"
    ET.SubElement(ttdb, "soThueConPhaiNopCT24").text = "0"

    tn = ET.SubElement(ctieu, "KKhaiTaiNguyen")
    for tag, tc in (("ThueTaiNguyen", "26"), ("ThueBVMT", "30"), ("PhiBVMT", "34")):
        block = ET.SubElement(tn, tag)
        ET.SubElement(block, f"tongCongCT{tc}").text = "0"
        ET.SubElement(block, f"soThueMienCT{int(tc)+1}").text = "0"
        ET.SubElement(block, f"soThueConPhaiNopCT{int(tc)+2}").text = "0"


def _append_ho_tro_nop_thue(ctieu, data, tong_gtgt):
    """HoTroThongTinNopThue — một dòng GTGT nếu có số thuế."""
    if tong_gtgt <= 0:
        return
    block = ET.SubElement(ctieu, "HoTroThongTinNopThue")
    chi_tiet = ET.SubElement(block, "ChiTietThongTinNopTHue", {"id": "1"})
    mst_cu = (data.get('mst_cu') or data.get('mst') or '').strip()
    ET.SubElement(chi_tiet, "ct38_mst").text = mst_cu
    ET.SubElement(chi_tiet, "ct38_ma").text = (data.get('ma_diaDiemKD') or '00999').strip()
    ET.SubElement(chi_tiet, "ct38_ten").text = (data.get('ct05') or data.get('tenNNT') or '').strip()
    ET.SubElement(chi_tiet, "ct39").text = HKD_XML_GTGT_TIEU_MUC[1]
    ET.SubElement(chi_tiet, "ct40").text = str(tong_gtgt)
    ET.SubElement(chi_tiet, "soTienChenhLech").text = str(tong_gtgt)
    ET.SubElement(chi_tiet, "ct41_ma").text = (data.get('ct41_ma') or '').strip()
    ET.SubElement(chi_tiet, "ct42_ma").text = HKD_XML_GTGT_TIEU_MUC[0]
    ET.SubElement(chi_tiet, "ct43_maDBHC").text = (data.get('ct43_maDBHC') or '').strip()
    ET.SubElement(chi_tiet, "ct43_tenDBHC").text = (data.get('ct12c_tenPhuong') or '').strip()
    ET.SubElement(chi_tiet, "ct44_maCQThu").text = (data.get('ct44_maCQThu') or '').strip()
    ET.SubElement(chi_tiet, "ct44_tenCQThu").text = (data.get('ct44_tenCQThu') or '').strip()
    ET.SubElement(chi_tiet, "ct45_maCQThue").text = (data.get('maCQTNoiNop') or '').strip()
    ET.SubElement(chi_tiet, "ct45_tenCQThue").text = (data.get('tenCQTNoiNop') or '').strip()
    han_nop = _fmt_vn_date(data.get('hanNopThue') or data.get('ngayKy'), iso_fallback=True)
    ET.SubElement(chi_tiet, "ct46").text = han_nop
    id_khoan = (data.get('idKhoanNopTam') or '').strip()
    if id_khoan:
        ET.SubElement(chi_tiet, "idKhoanNopTam").text = id_khoan
    ET.SubElement(block, "tongTienCT47").text = str(tong_gtgt)


def _append_supplemental_pluc(hso, data, loai_tkhai, ky_str, start_month, end_month, last_day, y, xsi):
    """Phụ lục khai bổ sung (giữ cấu trúc PL01 — maTKhai 1266)."""
    if loai_tkhai != 'B' or not ky_str:
        return
    pluc_root = ET.SubElement(hso, "PLuc")
    old_data = _fetch_original_sector_data(ky_str)
    sector_items = _supplemental_sector_items()
    pl01_khbs = ET.SubElement(pluc_root, "PL01_KHBS")
    header_pl = ET.SubElement(pl01_khbs, "Header")
    ET.SubElement(header_pl, "maTKhai").text = "1266"
    ET.SubElement(header_pl, "tenTKhai").text = (
        "Tờ khai thuế đối với hộ kinh doanh, cá nhân kinh doanh (TT50/2026)"
    )
    ET.SubElement(header_pl, "maGiaoDich").text = ""
    ky_pl = ET.SubElement(header_pl, "KyKKhaiThue")
    ET.SubElement(ky_pl, "kieuKy").text = "Q"
    ET.SubElement(ky_pl, "kyKKhai").text = ky_str
    ET.SubElement(ky_pl, "kyKKhaiTuNgay").text = f"01/{start_month:02d}/{y}"
    ET.SubElement(ky_pl, "kyKKhaiDenNgay").text = f"{last_day:02d}/{end_month:02d}/{y}"
    ET.SubElement(ky_pl, "kyKKhaiTuThang").text = f"{start_month:02d}/{y}"
    ET.SubElement(ky_pl, "kyKKhaiDenThang").text = f"{end_month:02d}/{y}"
    ET.SubElement(header_pl, "soLan").text = str(data.get('soLan', '0'))
    ET.SubElement(header_pl, "mst").text = data.get('mst', '')
    ET.SubElement(header_pl, "tenNNT").text = data.get('tenNNT', '')
    _sub_nil(header_pl, "mstDLyThue", xsi)
    ET.SubElement(header_pl, "tenDLyThue").text = ""
    ET.SubElement(header_pl, "soHDongDLyThue").text = ""
    _sub_nil(header_pl, "ngayKyHDDLyThue", xsi)
    ET.SubElement(pl01_khbs, "ma_DonViTien").text = "VND"
    ET.SubElement(pl01_khbs, "ten_DonViTien").text = "Đồng Việt Nam"
    muc_a = ET.SubElement(pl01_khbs, "Muc_A")
    muc_i = ET.SubElement(muc_a, "Muc_I")
    muc_1 = ET.SubElement(muc_i, "Muc_1")
    chitiet_1 = ET.SubElement(muc_1, "ChiTiet", {"id": "ID_1"})
    ET.SubElement(chitiet_1, "ct2_ma").text = ""
    ET.SubElement(chitiet_1, "ct2_ten").text = ""
    ET.SubElement(chitiet_1, "ct3").text = "0"
    ET.SubElement(muc_1, "tongCong_ct10").text = "0"
    pl01_1_khbs = ET.SubElement(pluc_root, "PL01_1_KHBS")
    header_pl1 = ET.SubElement(pl01_1_khbs, "Header")
    ET.SubElement(header_pl1, "maTKhai").text = "1266"
    ET.SubElement(header_pl1, "tenTKhai").text = (
        "Tờ khai thuế đối với hộ kinh doanh, cá nhân kinh doanh (TT50/2026)"
    )
    ET.SubElement(header_pl1, "maGiaoDich").text = ""
    ky_pl1 = ET.SubElement(header_pl1, "KyKKhaiThue")
    ET.SubElement(ky_pl1, "kieuKy").text = "Q"
    ET.SubElement(ky_pl1, "kyKKhai").text = ky_str
    ET.SubElement(ky_pl1, "kyKKhaiTuNgay").text = f"01/{start_month:02d}/{y}"
    ET.SubElement(ky_pl1, "kyKKhaiDenNgay").text = f"{last_day:02d}/{end_month:02d}/{y}"
    ET.SubElement(ky_pl1, "kyKKhaiTuThang").text = f"{start_month:02d}/{y}"
    ET.SubElement(ky_pl1, "kyKKhaiDenThang").text = f"{end_month:02d}/{y}"
    ET.SubElement(header_pl1, "soLan").text = str(data.get('soLan', '0'))
    ET.SubElement(header_pl1, "mst").text = data.get('mst', '')
    ET.SubElement(header_pl1, "tenNNT").text = data.get('tenNNT', '')
    _sub_nil(header_pl1, "mstDLyThue", xsi)
    ET.SubElement(header_pl1, "tenDLyThue").text = ""
    ET.SubElement(header_pl1, "soHDongDLyThue").text = ""
    _sub_nil(header_pl1, "ngayKyHDDLyThue", xsi)
    ET.SubElement(pl01_1_khbs, "ma_DonViTien").text = "VND"
    ET.SubElement(pl01_1_khbs, "ten_DonViTien").text = "Đồng Việt Nam"
    muc_a1 = ET.SubElement(pl01_1_khbs, "Muc_A")
    dsach_hso = ET.SubElement(muc_a1, "DSachHSo")
    bke_hso = ET.SubElement(dsach_hso, "BKeHSo", {"id": "ID_1"})
    ET.SubElement(bke_hso, "ma_HSo").text = "010501"
    ET.SubElement(bke_hso, "ten_HSo").text = "01/CNKD"
    ctiet_hso = ET.SubElement(bke_hso, "CTietHSo")
    id_k = 1
    for key, ma_label, ten_label, form_key in sector_items:
        old_val = float(old_data.get(key, 0) or 0)
        new_val = float(data.get(form_key, data.get(key, '0')) or 0)
        chenh = new_val - old_val
        chitiet = ET.SubElement(ctiet_hso, "ChiTiet", {"id": f"ID_{id_k}"})
        ET.SubElement(chitiet, "ct2_ma").text = ""
        ET.SubElement(chitiet, "ct2_ten").text = ""
        ET.SubElement(chitiet, "ct3_ma").text = ma_label
        ET.SubElement(chitiet, "ct3_ten").text = ten_label
        ET.SubElement(chitiet, "ct3_1_ma").text = ""
        ET.SubElement(chitiet, "ct3_1_ten").text = ""
        ET.SubElement(chitiet, "ct3_2_ma").text = ""
        ET.SubElement(chitiet, "ct3_2_ten").text = ""
        ET.SubElement(chitiet, "ct04").text = str(round(old_val))
        ET.SubElement(chitiet, "ct05").text = str(round(new_val))
        ET.SubElement(chitiet, "ct06").text = str(round(chenh))
        ET.SubElement(chitiet, "ct7").text = "0"
        ET.SubElement(chitiet, "ct8").text = data.get(f"ghiChu_{key}", "")
        id_k += 1
    tong_cong = ET.SubElement(dsach_hso, "TongCong")
    ET.SubElement(tong_cong, "tongCong_7").text = "0"
    ET.SubElement(tong_cong, "tongCong_8").text = "0"
    ET.SubElement(tong_cong, "tongCong_9").text = "0"
    muc_b1 = ET.SubElement(pl01_1_khbs, "Muc_B")
    ctiet_tl = ET.SubElement(muc_b1, "CTietTaiLieu", {"id": "ID_1"})
    ET.SubElement(ctiet_tl, "ma_TLieu").text = ""
    ET.SubElement(ctiet_tl, "ten_TLieu").text = ""


def _tax_settings(cursor):
    """Các field tờ khai bổ sung lưu trong bảng settings (key tax_*)."""
    try:
        rows = cursor.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'tax_%'"
        ).fetchall()
        return {row[0]: (row[1] or '') for row in rows}
    except sqlite3.Error:
        return {}


def fetch_tax_company_info(conn=None):
    """
    Thông tin NNT cho tờ khai — lấy từ Cài đặt (business_info + settings tax_*).
    Không dùng tờ khai đã lưu trước đó làm nguồn chính.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        biz = c.execute("SELECT * FROM business_info LIMIT 1").fetchone()
        biz = dict(biz) if biz else {}
        extra = _tax_settings(c)

        mst = (biz.get('tax_code') or extra.get('tax_mst') or '').strip()
        if not mst:
            inv = c.execute(
                """SELECT tax_code FROM invoice_settings
                   WHERE tax_code IS NOT NULL AND tax_code != ''
                   ORDER BY is_active DESC, updated_at DESC LIMIT 1"""
            ).fetchone()
            if inv:
                mst = (inv['tax_code'] or '').strip()

        rep = (biz.get('representative_name') or '').strip()
        addr = (biz.get('address') or '').strip()
        ct13 = (extra.get('tax_ct13a_soNha') or addr).strip()

        info = {
            'mst': mst,
            'mst_cu': (extra.get('tax_mst_cu') or '').strip(),
            'tenNNT': rep,
            'ct05': (biz.get('business_name') or '').strip(),
            'business_name': (biz.get('business_name') or '').strip(),
            'dthoaiNNT': (biz.get('phone') or '').strip(),
            'emailNNT': (biz.get('email') or extra.get('tax_email') or '').strip(),
            'ct06': (biz.get('bank_account') or '').strip(),
            'bank_account': (biz.get('bank_account') or '').strip(),
            'bank_name': (biz.get('bank_name') or '').strip(),
            'account_holder': (biz.get('account_holder') or '').strip(),
            'ct12b_soNha': addr,
            'ct12c_maPhuong': (extra.get('tax_ct12c_maPhuong') or '').strip(),
            'ct12c_tenPhuong': (extra.get('tax_ct12c_tenPhuong') or '').strip(),
            'ct12d_maTinh': (extra.get('tax_ct12d_maTinh') or '').strip(),
            'ct12d_tenTinh': (extra.get('tax_ct12d_tenTinh') or '').strip(),
            'ct13a_soNha': ct13,
            'ct13b_maPhuong': (extra.get('tax_ct13b_maPhuong') or '').strip(),
            'ct13b_tenPhuong': (extra.get('tax_ct13b_tenPhuong') or '').strip(),
            'ct13d_maTinh': (extra.get('tax_ct13d_maTinh') or '').strip(),
            'ct13d_tenTinh': (extra.get('tax_ct13d_tenTinh') or '').strip(),
            'nguoiKy': rep,
            'maCQTNoiNop': (extra.get('tax_maCQTNoiNop') or '').strip(),
            'tenCQTNoiNop': (extra.get('tax_tenCQTNoiNop') or '').strip(),
            'tuGio': (extra.get('tax_tuGio') or '7').strip(),
            'tuPhut': (extra.get('tax_tuPhut') or '0').strip(),
            'denGio': (extra.get('tax_denGio') or '18').strip(),
            'denPhut': (extra.get('tax_denPhut') or '0').strip(),
            'ct10': (extra.get('tax_ct10') or '0').strip(),
        }

        info['nganh_nghe'] = []
        if mst:
            rows = c.execute(
                """SELECT ma_nganh, ten_nganh FROM hkd_nganh_nghe
                   WHERE mst = ? ORDER BY thu_tu ASC""",
                (mst,),
            ).fetchall()
            info['nganh_nghe'] = [{'ma': r['ma_nganh'], 'ten': r['ten_nganh']} for r in rows]

        qr_accounts = get_tax_default_bank_accounts()
        info['default_qr_bank'] = qr_accounts
        if qr_accounts:
            qr = qr_accounts[0]
            info['bank_account'] = qr['so_tk']
            info['bank_name'] = qr['noi_mo']
            info['account_holder'] = qr['chu_tk']
            if not info['ct05']:
                info['ct05'] = qr.get('ten_ddkd') or info['ct05']

        return {k: (v if v is not None else '') for k, v in info.items()}
    finally:
        if own_conn and conn:
            conn.close()


def _merge_tax_company_defaults(data):
    """Điền field trống trên form bằng thông tin từ Cài đặt."""
    try:
        defaults = fetch_tax_company_info()
    except Exception:
        return data
    skip = {'nganh_nghe', 'default_qr_bank', 'business_name', 'bank_account', 'bank_name', 'account_holder'}
    merged = dict(data)
    for key, val in defaults.items():
        if key in skip:
            continue
        if val is None or val == '':
            continue
        current = merged.get(key)
        if current is None or str(current).strip() == '':
            merged[key] = val
    return merged


def generate_tax_xml(data, *, include_pluc=None, pluc_only=False):
    """
    Xuất XML tờ khai 01/CNKD — schema HTKK pbanTKhaiXML 2.8.3.
    include_pluc: None = tự động (chỉ gắn PLuc khi có dữ liệu phụ lục),
                    True/False = ép có/không PLuc trong file.
    pluc_only: True → file chỉ gồm TTinChung + PLuc (+ chữ ký).
    """
    data = _merge_tax_company_defaults(dict(data))
    NS = "http://kekhaithue.gdt.gov.vn/TKhaiThue"
    xsi = "http://www.w3.org/2001/XMLSchema-instance"
    root = ET.Element("HSoThueDTu", {
        "xmlns": NS,
        "xmlns:xsi": xsi,
        "xsi:schemaLocation": f"{NS} ToKhaiThue.xsd",
    })

    ky_str = (data.get('kyKKhai') or '').strip()
    start_month = end_month = last_day = y = None
    if '/' in ky_str:
        q, y = map(int, ky_str.split('/'))
        start_month = (q - 1) * 3 + 1
        end_month = q * 3
        _, last_day = calendar.monthrange(y, end_month)

    _lines, _ytd, totals, taxes = _resolve_tax_payload(data)
    loai_tkhai = data.get('loaiTKhai', 'C')
    so_lan = str(data.get('soLan') or '1')
    auto_pluc = has_tax_pluc_data(data)
    if include_pluc is None:
        include_pluc = auto_pluc

    hso = ET.SubElement(root, "HSoKhaiThue", {"id": "ID_1"})

    ttinchung = ET.SubElement(hso, "TTinChung")
    ttindvu = ET.SubElement(ttinchung, "TTinDVu")
    ET.SubElement(ttindvu, "maDVu").text = "HTKK"
    ET.SubElement(ttindvu, "tenDVu").text = "HỖ TRỢ KÊ KHAI THUẾ"
    ET.SubElement(ttindvu, "pbanDVu").text = "5.5.6"
    ET.SubElement(ttindvu, "ttinNhaCCapDVu").text = "3D73CFFAB5DA6133D754BE7D6DB20D0B"

    ttintkhaithue = ET.SubElement(ttinchung, "TTinTKhaiThue")
    tkhaithue = ET.SubElement(ttintkhaithue, "TKhaiThue")
    ET.SubElement(tkhaithue, "maTKhai").text = "473"
    ET.SubElement(tkhaithue, "tenTKhai").text = (
        "Tờ khai thuế đối với hộ kinh doanh, cá nhân kinh doanh"
    )
    ET.SubElement(tkhaithue, "moTaBMau").text = (
        "(Ban hành kèm theo Thông tư số 50/2026/TT-BTC ngày 13/5/2026 "
        "sửa đổi, bổ sung Thông tư 18/2026/TT-BTC)"
    )
    ET.SubElement(tkhaithue, "pbanTKhaiXML").text = data.get('pbanTKhaiXML', '2.8.3')
    ET.SubElement(tkhaithue, "loaiTKhai").text = loai_tkhai
    ET.SubElement(tkhaithue, "soLan").text = so_lan

    ky = ET.SubElement(tkhaithue, "KyKKhaiThue")
    ET.SubElement(ky, "kieuKy").text = "Q"
    ET.SubElement(ky, "kyKKhai").text = ky_str
    if start_month and y and last_day:
        ET.SubElement(ky, "kyKKhaiTuNgay").text = f"01/{start_month:02d}/{y}"
        ET.SubElement(ky, "kyKKhaiDenNgay").text = f"{last_day:02d}/{end_month:02d}/{y}"
        ET.SubElement(ky, "kyKKhaiTuThang").text = f"{start_month:02d}/{y}"
        ET.SubElement(ky, "kyKKhaiDenThang").text = f"{end_month:02d}/{y}"

    ET.SubElement(tkhaithue, "maCQTNoiNop").text = (data.get('maCQTNoiNop') or '').strip()
    ET.SubElement(tkhaithue, "tenCQTNoiNop").text = (data.get('tenCQTNoiNop') or '').strip()
    today_iso = datetime.now().strftime('%Y-%m-%d')
    ngay_lap = _fmt_vn_date(data.get('ngayLapTKhai') or today_iso, iso_fallback=True)
    ET.SubElement(tkhaithue, "ngayLapTKhai").text = ngay_lap
    ET.SubElement(tkhaithue, "nguoiKy").text = (data.get('nguoiKy') or '').strip()
    ET.SubElement(tkhaithue, "ngayKy").text = _fmt_vn_date(data.get('ngayKy') or today_iso, iso_fallback=True)

    giahan = ET.SubElement(tkhaithue, "GiaHan")
    ET.SubElement(giahan, "maLyDoGiaHan").text = ""
    ET.SubElement(giahan, "lyDoGiaHan").text = ""

    nganh_list = _load_nganh_list(data)
    if nganh_list:
        parts = []
        for ma, ten in nganh_list:
            parts.append(ten if ten.startswith(f"{ma}.-") else f"{ma}.-{ten}")
        nganh_text = ";".join(parts)
    else:
        nganh_text = ""
    ET.SubElement(tkhaithue, "nganhNgheKD").text = nganh_text

    nnt = ET.SubElement(ttintkhaithue, "NNT")
    addr_parts = [
        (data.get('ct12b_soNha') or '').strip(),
        (data.get('ct12c_tenPhuong') or '').strip(),
        (data.get('ct12d_tenTinh') or '').strip(),
    ]
    ET.SubElement(nnt, "mst").text = (data.get('mst') or '').strip()
    ET.SubElement(nnt, "tenNNT").text = (data.get('tenNNT') or '').strip()
    ET.SubElement(nnt, "dchiNNT").text = ", ".join(p for p in addr_parts if p)
    ET.SubElement(nnt, "dthoaiNNT").text = (data.get('dthoaiNNT') or '').strip()
    ET.SubElement(nnt, "emailNNT").text = (data.get('emailNNT') or '').strip()

    if not pluc_only:
        ctieu = ET.SubElement(hso, "CTieuTKhaiChinh")
        ET.SubElement(ctieu, "mst_cu").text = (data.get('mst_cu') or '').strip()

        header = ET.SubElement(ctieu, "Header")
        ET.SubElement(header, "hkdcnkdnopthuekhoan").text = "0"
        ET.SubElement(header, "cnkdnopps").text = "0"
        ET.SubElement(header, "tccnkhainopthay").text = "0"
        ET.SubElement(header, "hkdcnkdnopkekhai").text = "1"
        ET.SubElement(header, "hkdcnkdnnxddoanhthu").text = "1" if data.get('ct01b') == '1' else "0"
        ET.SubElement(header, "hkdchuyendoipptinhthue").text = "0"
        ET.SubElement(header, "ct05").text = (data.get('ct05') or '').strip()
        ET.SubElement(header, "ct06").text = (
            (data.get('ct06') or data.get('bank_account') or '').strip()
        )

        ct08 = ET.SubElement(header, "CT08")
        for idx, (ma, ten) in enumerate(nganh_list, 1):
            nn = ET.SubElement(ct08, "NNgheKDoanh", {"id": f"ID_{idx}"})
            ET.SubElement(nn, "maNNgheKDoanh").text = ma
            ET.SubElement(nn, "tenNNgheKDoanh").text = (
                ten if ten.startswith(f"{ma}.-") else f"{ma}.-{ten}"
            )

        ET.SubElement(header, "ct08a").text = "0"
        ET.SubElement(header, "ct09").text = str(data.get('ct09') or '1.00')
        ET.SubElement(header, "ct09a").text = str(data.get('ct09a') or '0.50')
        ET.SubElement(header, "ct10").text = str(data.get('ct10') or '0')

        ct11 = ET.SubElement(header, "CT11")
        ET.SubElement(ct11, "tuGio").text = str(data.get('tuGio') or '7')
        ET.SubElement(ct11, "tuPhut").text = str(data.get('tuPhut') or '0')
        ET.SubElement(ct11, "denGio").text = str(data.get('denGio') or '18')
        ET.SubElement(ct11, "denPhut").text = str(data.get('denPhut') or '0')

        ct12 = ET.SubElement(header, "CT12")
        ET.SubElement(ct12, "ct12a_tdtt").text = "0"
        ET.SubElement(ct12, "ct12b_soNha").text = (data.get('ct12b_soNha') or '').strip()
        ET.SubElement(ct12, "ct12c_maPhuong").text = (data.get('ct12c_maPhuong') or '').strip()
        ET.SubElement(ct12, "ct12c_tenPhuong").text = (data.get('ct12c_tenPhuong') or '').strip()
        ET.SubElement(ct12, "ct12d_maQuan").text = ""
        ET.SubElement(ct12, "ct12d_tenQuan").text = ""
        ET.SubElement(ct12, "ct12d_maTinh").text = (data.get('ct12d_maTinh') or '').strip()
        ET.SubElement(ct12, "ct12d_tenTinh").text = (data.get('ct12d_tenTinh') or '').strip()
        ET.SubElement(ct12, "ct12e_kdbiengioi").text = "0"

        ct13 = ET.SubElement(header, "CT13")
        ET.SubElement(ct13, "ct13a_soNha").text = (data.get('ct13a_soNha') or '').strip()
        ET.SubElement(ct13, "ct13b_maPhuong").text = (data.get('ct13b_maPhuong') or '').strip()
        ET.SubElement(ct13, "ct13b_tenPhuong").text = (data.get('ct13b_tenPhuong') or '').strip()
        ET.SubElement(ct13, "ct13c_maQuan").text = ""
        ET.SubElement(ct13, "ct13c_tenQuan").text = ""
        ET.SubElement(ct13, "ct13d_maTinh").text = (data.get('ct13d_maTinh') or '').strip()
        ET.SubElement(ct13, "ct13d_tenTinh").text = (data.get('ct13d_tenTinh') or '').strip()

        sector_keys = ('g1', 'g2', 'g3', 'g4')
        dt_sectors = [round(float(totals.get(k) or 0)) for k in sector_keys]
        dt_total = round(float(totals.get('total') or sum(dt_sectors)))
        gtgt_sectors = [
            round(float((taxes.get(k) or {}).get('gtgt') or 0)) for k in sector_keys
        ]
        gtgt_total = round(float(taxes.get('total_gtgt') or sum(gtgt_sectors)))
        tncn_dt_sectors = [
            round(float((taxes.get(k) or {}).get('dt_tncn') or 0)) for k in sector_keys
        ]
        tncn_dt_total = round(sum(tncn_dt_sectors))
        tncn_sectors = [
            round(float((taxes.get(k) or {}).get('tncn') or 0)) for k in sector_keys
        ]
        tncn_total = round(float(taxes.get('total_tncn') or sum(tncn_sectors)))

        if any(parse_number_vn(data.get(f'dt_{k}', 0)) for k in sector_keys):
            dt_sectors, dt_total = _sector_form_amounts(data, 'dt')
        if any(parse_number_vn(data.get(f'thue_gtgt_{k}', 0)) for k in sector_keys):
            gtgt_sectors, gtgt_total = _sector_form_amounts(data, 'thue_gtgt')
        if any(parse_number_vn(data.get(f'dt_tncn_{k}', 0)) for k in sector_keys):
            tncn_dt_sectors, tncn_dt_total = _sector_form_amounts(data, 'dt_tncn')
        if any(parse_number_vn(data.get(f'thue_tncn_{k}', 0)) for k in sector_keys):
            tncn_sectors, tncn_total = _sector_form_amounts(data, 'thue_tncn')

        kk = ET.SubElement(ctieu, "KKThueGTGT_TNCN")
        _fill_kk_tax_block(kk, "DoanhThuThueGTGT", dt_sectors, dt_total)
        _fill_kk_tax_block(kk, "SoThueGTGT", gtgt_sectors, gtgt_total)
        _fill_kk_tax_block(kk, "DoanhThuThueTNCN", tncn_dt_sectors, tncn_dt_total)
        _fill_kk_tax_block(kk, "SoThueTNCN", tncn_sectors, tncn_total)

        _append_kkhai_thue_ttdb_tn(ctieu)

    if pluc_only or include_pluc:
        _append_all_pluc_sections(
            hso, data, loai_tkhai, ky_str, start_month, end_month, last_day, y, xsi,
        )

    sig_ns = "http://www.w3.org/2000/09/xmldsig#"
    signature = ET.SubElement(root, "Signature")
    signature.set("xmlns", sig_ns)
    ET.SubElement(signature, "SignedInfo")
    sig_val = ET.SubElement(signature, "SignatureValue")
    sig_val.text = "GIẢ LẬP CHỮ KÝ SỐ - THAY BẰNG KÝ THẬT"

    return prettify(root)


def _tax_export_filename(data):
    """Tên file XML xuất HTKK: {mst}-01_CNKD_TT40-Q{quy}{nam}-L{lan}.xml"""
    mst = (data.get('mst') or 'UNKNOWN').replace('-', '')
    ky = (data.get('kyKKhai') or '').replace('/', '')
    loai_tkhai = (data.get('loaiTKhai') or 'C').upper()
    if loai_tkhai in ('C', 'CHINHTHUC', 'CHÍNH THỨC'):
        lan = '00'
    else:
        try:
            lan = str(int(data.get('soLan') or '1')).zfill(2)
        except ValueError:
            lan = '01'
    return f"{mst}-01_CNKD_TT40-Q{ky}-L{lan}.xml"


def generate_tax_xml_packages(data):
    """
    Một file XML duy nhất; preview tách 2 phần (tờ khai / phụ lục) khi phụ lục đủ dữ liệu.
    """
    merged = _merge_tax_company_defaults(dict(data))
    errors, include_pluc, pluc_started = validate_tax_pluc_export(merged)

    if pluc_started and errors:
        return {
            'success': False,
            'errors': errors,
            'has_pluc': False,
            'xml': None,
            'filename': _tax_export_filename(merged),
            'preview_main': None,
            'preview_pluc': None,
        }

    full_xml = generate_tax_xml(merged, include_pluc=include_pluc, pluc_only=False)
    preview_main, preview_pluc = split_xml_preview(full_xml, include_pluc)

    return {
        'success': True,
        'errors': [],
        'has_pluc': include_pluc,
        'xml': full_xml,
        'filename': _tax_export_filename(merged),
        'preview_main': preview_main,
        'preview_pluc': preview_pluc,
    }


def get_quarter_data(quarter_year):
    conn = get_db_connection()
    c = conn.cursor()
    quarter, year = map(int, quarter_year.split('/'))
    start_month = (quarter - 1) * 3 + 1
    end_month = quarter * 3
    start = f"{year}-{start_month:02d}-01"
    # tính ngày cuối cùng đúng của end_month
    last_day = calendar.monthrange(year, end_month)[1]
    end = f"{year}-{end_month:02d}-{last_day:02d}"
    c.execute("SELECT SUM(total) FROM sale WHERE date BETWEEN ? AND ?", (start, end))
    total = c.fetchone()[0] or 0
    tax_gtgt = total * 0.1
    c.execute("SELECT id, name, unit FROM products")
    products = []
    for row in c.fetchall():
        products.append({
            'id': row['id'],
            'name': row['name'],
            'unit': row['unit'],
            'begin_qty': 0, 'begin_value': 0,
            'import_qty': 0, 'import_value': 0,
            'sale_qty': 0, 'sale_value': 0,
            'end_qty': 0, 'end_value': 0
        })
    conn.close()
    return int(total), int(tax_gtgt), products

# === GỬI eTax API (ví dụ) ===
def submit_to_etax(signed_xml_path, mst):
    url = "https://api.etax.gdt.gov.vn/submit"
    try:
        with open(signed_xml_path, 'rb') as f:
            files = {'file': f}
            data = {'mst': mst, 'loaiTK': '01/CNKD'}
            response = requests.post(url, files=files, data=data, timeout=30)
        if response.status_code == 200:
            return {"status": "success", "data": response.json()}
        else:
            return {"status": "error", "message": response.text}
    except Exception as e:
        return {"status": "error", "message": str(e)}


TAX_PROFILE_SETTING_MAP = {
    'mst_cu': 'tax_mst_cu',
    'maCQTNoiNop': 'tax_maCQTNoiNop',
    'tenCQTNoiNop': 'tax_tenCQTNoiNop',
    'emailNNT': 'tax_email',
    'ct12c_maPhuong': 'tax_ct12c_maPhuong',
    'ct12c_tenPhuong': 'tax_ct12c_tenPhuong',
    'ct12d_maTinh': 'tax_ct12d_maTinh',
    'ct12d_tenTinh': 'tax_ct12d_tenTinh',
    'ct13a_soNha': 'tax_ct13a_soNha',
    'ct13b_maPhuong': 'tax_ct13b_maPhuong',
    'ct13b_tenPhuong': 'tax_ct13b_tenPhuong',
    'ct13d_maTinh': 'tax_ct13d_maTinh',
    'ct13d_tenTinh': 'tax_ct13d_tenTinh',
    'tuGio': 'tax_tuGio',
    'tuPhut': 'tax_tuPhut',
    'denGio': 'tax_denGio',
    'denPhut': 'tax_denPhut',
    'ct10': 'tax_ct10',
}


def _persist_tax_profile_settings(cursor, data):
    """Lưu các field thuế bổ sung (mã CQT, phường/xã…) vào settings để lần sau tự điền."""
    for form_key, setting_key in TAX_PROFILE_SETTING_MAP.items():
        val = data.get(form_key)
        if val is None:
            continue
        val = str(val).strip()
        if val:
            cursor.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (setting_key, val),
            )


def register_tax_routes(app):
    @app.route('/api/company_info', methods=['GET'])
    @login_required
    def api_company_info():
        try:
            info = fetch_tax_company_info()
            if not info.get('mst') and not info.get('ct05'):
                return jsonify({
                    "success": False,
                    "message": "Chưa cấu hình thông tin hộ kinh doanh. Vào Hệ Thống → Thiết lập.",
                })
            return jsonify({
                "success": True,
                "data": info,
                "source": "settings",
            })
        except sqlite3.Error as db_err:
            return jsonify({
                "success": False,
                "error": f"Lỗi cơ sở dữ liệu: {str(db_err)}",
            }), 500
        except Exception as e:
            traceback.print_exc()
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route('/api/tax_data')
    @login_required
    def api_tax_data():
        ky = request.args.get('ky')
        if not ky or '/' not in ky:
            return jsonify({"success": False, "error": "Kỳ không hợp lệ"}), 400

        try:
            q, y = map(int, ky.split('/'))
            start_month = (q - 1) * 3 + 1
            end_month = q * 3
            _, last_day = calendar.monthrange(y, end_month)

            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # DOANH THU & THUẾ THEO NHÓM G1–G4 (sổ S2a/S2b — TT 152/2025)
            start_date = f"{y}-{start_month:02d}-01"
            end_date = f"{y}-{end_month:02d}-{last_day:02d}"
            from Services.hkd_revenue import fetch_hkd_revenue_ledger
            ledger = fetch_hkd_revenue_ledger(c, start_date, end_date)
            totals = ledger.get('totals') or {}
            taxes = ledger.get('taxes') or {}
            doanhthu = round(float(totals.get('total') or 0))

            sector_rows = []
            for code in ('G1', 'G2', 'G3', 'G4'):
                key = code.lower()
                rates = HKD_TAX_RATES[code]
                opt = next((o for o in HKD_SECTOR_OPTIONS if o['code'] == code), {})
                sector_rows.append({
                    'code': code,
                    'title': opt.get('title', code),
                    'gtgt_rate': opt.get('vat_rate', ''),
                    'tncn_rate': opt.get('pit_rate', ''),
                    'revenue': round(float(totals.get(key) or 0)),
                    'gtgt': round(float((taxes.get(key) or {}).get('gtgt') or 0)),
                    'tncn': round(float((taxes.get(key) or {}).get('tncn') or 0)),
                    'dt_tncn': round(float((taxes.get(key) or {}).get('dt_tncn') or 0)),
                })

            # Phụ lục 01/BK-STK — STK VietQR mặc định khi bán hàng (POS)
            default_qr_bank = get_tax_default_bank_accounts()

            conn.close()

            return jsonify({
                "success": True,
                "doanhthu": doanhthu,
                "totals": totals,
                "taxes": taxes,
                "tncn_meta": taxes.get('tncn_meta') or {},
                "sectors": sector_rows,
                "summary": ledger.get('summary') or {},
                "default_qr_bank": default_qr_bank,
                "bank_accounts": default_qr_bank,
            })

        except Exception as e:
            traceback.print_exc()
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if 'conn' in locals():
                try:
                    conn.close()
                except Exception:
                    pass

    #===API lấy dữ liệu từ tờ khai gốc để lập tờ khai bổ sung===#
    @app.route('/api/tax_original', methods=['GET'])
    @login_required
    def api_tax_original():
        ky = request.args.get('ky')
        if not ky or '/' not in ky:
            return jsonify({"success": False, "error": "Kỳ không hợp lệ"}), 400

        try:
            q, y = map(int, ky.split('/'))
            start_month = (q - 1) * 3 + 1
            end_month = q * 3
            _, last_day = calendar.monthrange(y, end_month)

            start_date = f"{y}-{start_month:02d}-01"
            end_date = f"{y}-{end_month:02d}-{last_day:02d}"

            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # Ưu tiên số liệu đã lưu trong tờ khai chính thức
            c.execute("""
                SELECT doanh_thu, thue_gtgt, thue_tncn
                FROM tax_declarations
                WHERE ky_khai = ? AND loai_tkhai = 'C'
                ORDER BY created_at DESC
                LIMIT 1
            """, (ky,))
            row = c.fetchone()

            from Services.hkd_revenue import fetch_hkd_revenue_ledger
            ledger = fetch_hkd_revenue_ledger(c, start_date, end_date)
            totals = ledger.get('totals') or {}
            taxes = ledger.get('taxes') or {}

            data = {
                'dt_g1': round(float(totals.get('g1') or 0)),
                'dt_g2': round(float(totals.get('g2') or 0)),
                'dt_g3': round(float(totals.get('g3') or 0)),
                'dt_g4': round(float(totals.get('g4') or 0)),
                'doanhthu': round(float(row['doanh_thu'] if row else totals.get('total') or 0)),
                'thue_gtgt_g1': round(float((taxes.get('g1') or {}).get('gtgt') or 0)),
                'thue_gtgt_g2': round(float((taxes.get('g2') or {}).get('gtgt') or 0)),
                'thue_gtgt_g3': round(float((taxes.get('g3') or {}).get('gtgt') or 0)),
                'thue_gtgt_g4': round(float((taxes.get('g4') or {}).get('gtgt') or 0)),
                'thue_gtgt': round(float(row['thue_gtgt'] if row else taxes.get('total_gtgt') or 0)),
                'thue_tncn_g1': round(float((taxes.get('g1') or {}).get('tncn') or 0)),
                'thue_tncn_g2': round(float((taxes.get('g2') or {}).get('tncn') or 0)),
                'thue_tncn_g3': round(float((taxes.get('g3') or {}).get('tncn') or 0)),
                'thue_tncn_g4': round(float((taxes.get('g4') or {}).get('tncn') or 0)),
                'thue_tncn': round(float(row['thue_tncn'] if row else taxes.get('total_tncn') or 0)),
            }
            conn.close()
            return jsonify({"success": True, "data": data})

        except Exception as e:
            print(f"Lỗi api_tax_original: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if 'conn' in locals():
                conn.close()

    @app.route('/api/save_tax_declaration', methods=['POST'])
    @login_required
    def save_tax_declaration():
        try:
            data = request.form
            mst = data.get('mst')
            ky_khai = data.get('kyKKhai')
            loai_tkhai = data.get('loaiTKhai')

            if not mst:
                return jsonify({"success": False, "error": "Thiếu mã số thuế (mst)"}), 400

            conn = get_db_connection()
            c = conn.cursor()

            # ===== 1. XÓA TỜ KHAI CŨ NẾU TRÙNG =====
            c.execute("""
                DELETE FROM tax_declarations
                WHERE mst = ?
                AND ky_khai = ?
                AND loai_tkhai = ?
            """, (mst, ky_khai, loai_tkhai))

            # ===== 2. INSERT TỜ KHAI MỚI =====
            c.execute("""
                INSERT INTO tax_declarations (
                    mst, mst_cu, tenNNT,
                    ct05, ct06, ct09, ct09a, ct10, ct12b_soNha, ct12c_tenPhuong, ct12d_tenTinh, ct13a_soNha, ct13b_tenPhuong, ct13d_tenTinh, nguoiKy, ct24, ct25, ct26, ct27, ct28, ct29, ct30, ct31, tuGio, denGio, emailNNT, dthoaiNNT,
                    ky_khai, loai_tkhai, so_lan,
                    doanh_thu, thue_gtgt, thue_tncn,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                mst,
                data.get('mst_cu'),
                data.get('tenNNT'),
                data.get('ct05'),
                (data.get('stk_so_tk_1') or data.get('ct06') or '').strip(),
                float(data.get('ct09') or 0),
                float(data.get('ct09a') or 0),
                float(data.get('ct10') or 0),
                data.get('ct12b_soNha'),
                data.get('ct12c_tenPhuong'),
                data.get('ct12d_tenTinh'),
                data.get('ct13a_soNha'),
                data.get('ct13b_tenPhuong'),
                data.get('ct13d_tenTinh'),
                data.get('nguoiKy'),
                float(data.get('ct24') or 0),
                float(data.get('ct25') or 0),
                float(data.get('ct26') or 0),
                float(data.get('ct27') or 0),
                float(data.get('ct28') or 0),
                float(data.get('ct29') or 0),
                float(data.get('ct30') or 0),
                float(data.get('ct31') or 0),
                float(data.get('tuGio') or 0),
                float(data.get('denGio') or 0),
                data.get('emailNNT'),
                data.get('dthoaiNNT'),
                ky_khai,
                loai_tkhai,
                int(data.get('soLan') or 1),
                float(data.get('dt_gtgt_ct28') or 0),
                float(data.get('so_gtgt_ct28') or 0),
                float(data.get('so_tncn_ct28') or 0),
                'completed'
            ))

            # ===== 3. LƯU NGÀNH NGHỀ VÀO BẢNG hkd_nganh_nghe =====
            # Xóa ngành nghề cũ của MST này trước
            c.execute("DELETE FROM hkd_nganh_nghe WHERE mst = ?", (mst,))

            # Insert các ngành mới từ form (maNNghe_1, tenNNghe_1, ...)
            idx = 1
            while f"maNNghe_{idx}" in data:
                ma = data.get(f"maNNghe_{idx}", '').strip()
                ten = data.get(f"tenNNghe_{idx}", '').strip()
                if ma and ten:
                    c.execute("""
                        INSERT OR REPLACE INTO hkd_nganh_nghe 
                        (mst, ma_nganh, ten_nganh, thu_tu)
                        VALUES (?, ?, ?, ?)
                    """, (mst, ma, ten, idx))
                idx += 1

            _persist_tax_profile_settings(c, data)

            sqlite_commit(conn, label='tax')
            conn.close()

            return jsonify({"success": True})

        except Exception as e:
            print("Lỗi save_tax_declaration:", e)
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route('/api/tax_export_xml', methods=['POST'])
    @login_required
    def api_tax_export_xml():
        """Tạo XML tờ khai — một file, preview 2 tab nếu có phụ lục đủ dữ liệu."""
        try:
            data = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
            packages = generate_tax_xml_packages(data)
            if not packages.get('success'):
                return jsonify({
                    'success': False,
                    'errors': packages.get('errors') or ['Phụ lục chưa đủ thông tin'],
                }), 400
            return jsonify({'success': True, **packages})
        except Exception as e:
            traceback.print_exc()
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/tax_report', methods=['GET', 'POST'])
    def tax_report():
        if request.method == 'POST':
            data = request.form.to_dict()
            packages = generate_tax_xml_packages(data)
            if not packages.get('success'):
                err = '; '.join(packages.get('errors') or ['Phụ lục chưa đủ thông tin'])
                return jsonify({'success': False, 'error': err}), 400

            buffer = BytesIO()
            buffer.write(packages['xml'].encode('utf-8'))
            buffer.seek(0)
            return send_file(
                buffer,
                as_attachment=True,
                download_name=packages['filename'],
                mimetype='application/xml',
            )

        try:
            company_info = fetch_tax_company_info()
        except Exception:
            company_info = {}
        now = datetime.now()
        return render_template(
            'tax_report.html',
            company_info=company_info,
            current_year=now.year,
            current_quarter=(now.month - 1) // 3 + 1,
            today_vn=now.strftime('%d/%m/%Y'),
        )

    @app.route('/api/sign_xml', methods=['POST'])
    @login_required
    def sign_xml():
        try:
            data = request.form.to_dict()
            packages = generate_tax_xml_packages(data)
            if not packages.get('success'):
                return jsonify({
                    'success': False,
                    'errors': packages.get('errors') or ['Phụ lục chưa đủ thông tin'],
                }), 400
            return jsonify({'success': True, **packages})
        except Exception as e:
            print(f"Lỗi tạo XML: {str(e)}")
            traceback.print_exc()
            return jsonify({"success": False, "error": str(e)}), 500
