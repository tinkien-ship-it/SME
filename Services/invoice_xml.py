"""Tạo XML hóa đơn POS và helper ký eSign — dùng chung mock-sign / sign / api/esign."""
import base64
import logging
import os
from datetime import datetime

import requests
import xml.etree.ElementTree as ET
from xml.dom import minidom

logger = logging.getLogger(__name__)

from Services.invoice_buyer import DEFAULT_RETAIL_BUYER_NAME, normalize_retail_buyer_name

DEFAULT_SELLER = {
    "name": "DOANH NGHIỆP",
    "tax_code": "",
    "address": "",
}


def normalize_invoice_config(config):
    """Chuẩn hóa key từ invoice_settings (app_id/api_key/provider_name)."""
    cfg = dict(config or {})
    cfg["provider"] = cfg.get("provider") or cfg.get("provider_name") or ""
    cfg["client_id"] = cfg.get("client_id") or cfg.get("api_key") or cfg.get("app_id") or ""
    cfg["client_secret"] = cfg.get("client_secret") or cfg.get("app_secret") or ""
    cfg["esign_username"] = cfg.get("esign_username") or cfg.get("username") or ""
    cfg["sign_service_url"] = cfg.get("sign_service_url") or cfg.get("esign_api_url") or ""
    if cfg.get("misa_has_code") is not None:
        cfg["misa_has_code"] = 1 if str(cfg.get("misa_has_code")).lower() in ("1", "true", "yes") else 0
    if cfg.get("minvoice_has_code") is not None:
        cfg["minvoice_has_code"] = 1 if str(cfg.get("minvoice_has_code")).lower() in ("1", "true", "yes") else 0
    return cfg


def get_seller_from_db(conn):
    """Lấy thông tin người bán từ business_info."""
    try:
        row = conn.execute(
            "SELECT business_name, tax_code, address FROM business_info LIMIT 1"
        ).fetchone()
        if row:
            data = dict(row)
            return {
                "name": data.get("business_name") or DEFAULT_SELLER["name"],
                "tax_code": data.get("tax_code") or "",
                "address": data.get("address") or "",
            }
    except Exception as exc:
        logger.warning("Không đọc được business_info: %s", exc)
    return dict(DEFAULT_SELLER)


def get_access_token(config):
    """Lấy OAuth token cho provider hóa đơn điện tử."""
    cfg = normalize_invoice_config(config)
    provider = (cfg.get("provider") or "").lower()
    api_url = (cfg.get("api_url") or "").rstrip("/")

    if provider not in ("misa", "mobifone"):
        return None

    if provider == "misa":
        if not api_url or not cfg.get("client_id"):
            raise ValueError("MISA: thiếu api_url hoặc App ID (api_key)")
        if not cfg.get("tax_code") or not (cfg.get("username") or cfg.get("esign_username")):
            raise ValueError("MISA: thiếu mã số thuế hoặc username")
        base = api_url
        if "/api/v3" in api_url.lower():
            base = api_url[: api_url.lower().index("/api/v3") + len("/api/v3")]
        url = f"{base.rstrip('/')}/auth/token"
        payload = {
            "appid": cfg["client_id"],
            "taxcode": cfg.get("tax_code") or "",
            "username": cfg["esign_username"] or cfg.get("username") or "",
            "password": cfg.get("password") or "",
        }
        response = requests.post(url, json=payload, timeout=30)
        if not response.ok:
            raise RuntimeError(f"Không lấy được token MISA: {response.text}")
        data = response.json()
        if not data.get("Success"):
            raise RuntimeError(f"MISA token lỗi: {data.get('ErrorCode') or data}")
        return data.get("Data")

    if provider == "mobifone":
        if not api_url or not cfg.get("tax_code"):
            raise ValueError("M-Invoice: thiếu api_url hoặc mã số thuế")
        if not (cfg.get("username") or cfg.get("esign_username")) or not cfg.get("password"):
            raise ValueError("M-Invoice: thiếu username hoặc password")
        base = api_url
        if "/api/" in api_url.lower():
            base = api_url.split("/api/")[0].rstrip("/")
        url = f"{base.rstrip('/')}/api/Account/Login"
        payload = {
            "username": cfg.get("esign_username") or cfg.get("username") or "",
            "password": cfg.get("password") or "",
            "tax_code": cfg.get("tax_code") or "",
        }
        response = requests.post(url, json=payload, timeout=30)
        if not response.ok:
            raise RuntimeError(f"Không lấy được token M-Invoice: {response.text}")
        data = response.json()
        if data.get("error"):
            raise RuntimeError(f"M-Invoice login lỗi: {data.get('error')}")
        return data.get("token")

    if not api_url or not cfg.get("client_id") or not cfg.get("client_secret"):
        raise ValueError("Thiếu api_url, api_key hoặc app_secret trong cấu hình")

    url = f"{api_url}/auth/token"
    payload = {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "grant_type": "client_credentials",
    }

    response = requests.post(url, data=payload, timeout=30)
    if not response.ok:
        raise RuntimeError(f"Không lấy được token: {response.text}")

    data = response.json()
    return data.get("access_token") or data.get("token")


def esign_xml_content(xml_content, config):
    """Gửi XML tới API eSign của provider, trả về XML đã ký."""
    cfg = normalize_invoice_config(config)
    url = (cfg.get("api_url") or "").rstrip("/")
    if not url:
        raise ValueError("Chưa cấu hình API URL")

    headers = {"Content-Type": "application/json"}
    if cfg.get("client_id") and cfg.get("client_secret"):
        token = get_access_token(cfg)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif cfg.get("username") and cfg.get("password"):
        auth = base64.b64encode(
            f"{cfg['username']}:{cfg['password']}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {auth}"

    payload = {"xml": xml_content}
    if cfg.get("esign_username"):
        payload["signer"] = cfg["esign_username"]

    resp = requests.post(f"{url}/esign/sign", headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    if resp.headers.get("Content-Type", "").startswith("application/json"):
        return resp.json().get("signed_xml") or resp.text
    return resp.text


def generate_invoice_xml(
    *,
    khh_don,
    sh_don,
    n_lap,
    ht_ttoan,
    ten_nguoi_mua,
    mst_nguoi_mua,
    dchi_nguoi_mua,
    hvt_nmhang,
    buyer,
    cus_name,
    cus_email,
    items,
    tong_tien_so,
    tong_tien_chu,
    fkey,
    mccqt,
    seller_name,
    seller_tax_code,
    seller_address,
):
    """Tạo nội dung XML hóa đơn bán hàng (POS)."""
    hdon = ET.Element("HDon")

    dlhdon = ET.SubElement(hdon, "DLHDon")
    dlhdon.set("Id", "DL")

    ttchung = ET.SubElement(dlhdon, "TTChung")
    ET.SubElement(ttchung, "PBan").text = "2.1.0"
    ET.SubElement(ttchung, "THDon").text = "Hoá đơn điện tử bán hàng"
    ET.SubElement(ttchung, "KHMSHDon").text = "2"
    ET.SubElement(ttchung, "KHHDon").text = khh_don
    ET.SubElement(ttchung, "SHDon").text = sh_don
    ET.SubElement(ttchung, "NLap").text = n_lap
    ET.SubElement(ttchung, "HTTToan").text = ht_ttoan

    ndhdon = ET.SubElement(dlhdon, "NDHDon")

    nban = ET.SubElement(ndhdon, "NBan")
    ET.SubElement(nban, "Ten").text = seller_name or DEFAULT_SELLER["name"]
    ET.SubElement(nban, "MST").text = seller_tax_code or ""
    ET.SubElement(nban, "DChi").text = seller_address or ""

    nmua = ET.SubElement(ndhdon, "NMua")
    ET.SubElement(nmua, "Ten").text = ten_nguoi_mua or ""
    ET.SubElement(nmua, "MST").text = mst_nguoi_mua or ""
    ET.SubElement(nmua, "DChi").text = dchi_nguoi_mua or ""
    ET.SubElement(nmua, "HVTNMHang").text = hvt_nmhang or normalize_retail_buyer_name(ten_nguoi_mua)

    ttkhac_nmua = ET.SubElement(nmua, "TTKhac")
    for ttruong, dlieu in [
        ("Buyer", buyer or hvt_nmhang),
        ("CusName", cus_name or ten_nguoi_mua),
        ("CusEmail", cus_email or ""),
        ("PaymentMethod", ht_ttoan),
    ]:
        if dlieu:
            ttin = ET.SubElement(ttkhac_nmua, "TTin")
            ET.SubElement(ttin, "TTruong").text = ttruong
            ET.SubElement(ttin, "KDLieu").text = "string"
            ET.SubElement(ttin, "DLieu").text = dlieu

    dshhdvu = ET.SubElement(ndhdon, "DSHHDVu")
    for idx, item in enumerate(items, start=1):
        hhdvu = ET.SubElement(dshhdvu, "HHDVu")
        ET.SubElement(hhdvu, "TChat").text = "1"
        ET.SubElement(hhdvu, "STT").text = str(idx)
        ET.SubElement(hhdvu, "THHDVu").text = item["ten_hang"]
        ET.SubElement(hhdvu, "DVTinh").text = item["don_vi"]
        ET.SubElement(hhdvu, "SLuong").text = str(item["so_luong"])
        ET.SubElement(hhdvu, "DGia").text = str(item["don_gia"])
        ET.SubElement(hhdvu, "TLCKhau").text = "0"
        ET.SubElement(hhdvu, "STCKhau").text = "0"
        ET.SubElement(hhdvu, "ThTien").text = str(item["thanh_tien"])

        ttkhac_hh = ET.SubElement(hhdvu, "TTKhac")
        for ttruong, dlieu in [
            ("ProdType", "1"),
            ("VATAmount", "0.000000000"),
            ("Total", f"{item['thanh_tien']:.9f}"),
            ("Amount", f"{item['thanh_tien']:.9f}"),
        ]:
            ttin = ET.SubElement(ttkhac_hh, "TTin")
            ET.SubElement(ttin, "TTruong").text = ttruong
            ET.SubElement(ttin, "KDLieu").text = "string"
            ET.SubElement(ttin, "DLieu").text = dlieu

    ttoan = ET.SubElement(ndhdon, "TToan")
    ET.SubElement(ttoan, "DSLPhi")
    ET.SubElement(ttoan, "TTCKTMai").text = "0"
    ET.SubElement(ttoan, "TgTTTBSo").text = str(tong_tien_so)
    ET.SubElement(ttoan, "TgTTTBChu").text = tong_tien_chu

    ttkhac = ET.SubElement(dlhdon, "TTKhac")
    fixed_fields = [
        ("Fkey", fkey),
        ("Discount", "0.000000000"),
        ("DiscountAmount", "0.000000000"),
        ("TradeDiscount0", "0.000000000"),
        ("TradeDiscount5", "0.000000000"),
        ("TradeDiscount8", "0.000000000"),
        ("TradeDiscount10", "0.000000000"),
        ("PlusAmountAfterDiscount0", "0.000000000"),
        ("PlusAmountAfterDiscount5", "0.000000000"),
        ("PlusAmountAfterDiscount8", "0.000000000"),
        ("PlusAmountAfterDiscount10", "0.000000000"),
        ("VatAmount0", "0.000000000"),
        ("VatAmount5", "0.000000000"),
        ("VatAmount8", "0.000000000"),
        ("VatAmount10", "0.000000000"),
        ("GrossValue", "0.000000000"),
        ("GrossValue0", "0.000000000"),
        ("GrossValue5", "0.000000000"),
        ("GrossValue8", "0.000000000"),
        ("GrossValue10", "0.000000000"),
        ("AmountVAT0", "0.000000000"),
        ("AmountVAT5", "0.000000000"),
        ("AmountVAT8", "0.000000000"),
        ("AmountVAT10", "0.000000000"),
        ("TGTKhac", "0.000000000"),
    ]
    for ttruong, dlieu in fixed_fields:
        ttin = ET.SubElement(ttkhac, "TTin")
        ET.SubElement(ttin, "TTruong").text = ttruong
        ET.SubElement(ttin, "KDLieu").text = "string"
        ET.SubElement(ttin, "DLieu").text = dlieu

    ET.SubElement(hdon, "DLQRCode")
    ET.SubElement(hdon, "MCCQT").text = mccqt
    dscks = ET.SubElement(hdon, "DSCKS")
    ET.SubElement(dscks, "NBan")
    ET.SubElement(dscks, "NMua")
    ET.SubElement(dscks, "CQT")
    ET.SubElement(dscks, "CCKSKhac")

    rough_string = ET.tostring(hdon, encoding="unicode")
    reparsed = minidom.parseString(rough_string)
    pretty_xml = reparsed.toprettyxml(indent="  ")
    lines = [line for line in pretty_xml.split("\n") if line.strip()]
    return "\n".join(lines)


def prepare_sale_invoice_xml(conn, sale_id, so_thanh_chu_fn, fkey_prefix="POS"):
    """
    Chuẩn bị XML hóa đơn từ sale_id.
    Trả dict: xml_content, xml_path, sh_don, khh_don, tong_tien, sale.
    """
    cur = conn.cursor()
    sale_row = cur.execute("SELECT * FROM sale WHERE id = ?", (sale_id,)).fetchone()
    if not sale_row:
        raise LookupError("Không tìm thấy đơn hàng")

    sale = dict(sale_row)
    invoice_num = sale.get("invoice_number")
    if invoice_num is None or str(invoice_num).strip() == "":
        raise ValueError(
            "Đơn hàng chưa được cấp số hóa đơn (invoice_number). "
            "Vui lòng cấp số trước khi ký."
        )

    try:
        sh_don = f"{int(invoice_num):08d}"
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invoice_number không hợp lệ: {invoice_num}") from exc

    item_rows = cur.execute(
        """
        SELECT
            si.quantity,
            si.price,
            p.name AS product_name,
            CASE WHEN si.UseSaleUnit = 0 THEN p.unit ELSE p.unit1 END AS don_vi
        FROM sale_items si
        JOIN products p ON p.id = si.product_id
        WHERE si.sale_id = ?
        """,
        (sale_id,),
    ).fetchall()

    if not item_rows:
        raise ValueError("Đơn hàng trống")

    danh_sach_items = []
    tong_tien = 0
    for item in item_rows:
        item = dict(item)
        so_luong = float(item["quantity"])
        don_gia = float(item["price"])
        thanh_tien = int(round(so_luong * don_gia))
        danh_sach_items.append(
            {
                "ten_hang": item["product_name"],
                "don_vi": item["don_vi"],
                "so_luong": so_luong,
                "don_gia": don_gia,
                "thanh_tien": thanh_tien,
            }
        )
        tong_tien += thanh_tien

    seller = get_seller_from_db(conn)
    khh_don = sale.get("symbol") or sale.get("invoice_series") or "2K25MMD"
    n_lap = str(sale.get("date") or datetime.now().strftime("%Y-%m-%d")).split(" ")[0]
    customer = normalize_retail_buyer_name(sale.get("customer_name"))

    xml_content = generate_invoice_xml(
        khh_don=khh_don,
        sh_don=sh_don,
        n_lap=n_lap,
        ht_ttoan="TM/CK",
        ten_nguoi_mua=customer,
        mst_nguoi_mua=sale.get("tax_code") or "",
        dchi_nguoi_mua=sale.get("address") or "",
        hvt_nmhang=customer,
        buyer=customer,
        cus_name=customer,
        cus_email=sale.get("customer_email") or "",
        items=danh_sach_items,
        tong_tien_so=tong_tien,
        tong_tien_chu=so_thanh_chu_fn(tong_tien),
        fkey=f"{fkey_prefix}{sh_don}",
        mccqt=f"M2-25-DMDC7-{sh_don.zfill(10)}",
        seller_name=seller["name"],
        seller_tax_code=seller["tax_code"],
        seller_address=seller["address"],
    )

    os.makedirs("invoices_xml", exist_ok=True)
    xml_path = os.path.join("invoices_xml", f"{khh_don}_{sh_don}.xml")

    return {
        "sale": sale,
        "sh_don": sh_don,
        "khh_don": khh_don,
        "tong_tien": tong_tien,
        "xml_content": xml_content,
        "xml_path": xml_path,
    }


def save_invoice_xml(xml_path, xml_content):
    with open(xml_path, "w", encoding="utf-8") as handle:
        handle.write(xml_content)
