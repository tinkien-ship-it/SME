"""Routes hóa đơn điện tử — tách từ app.py."""
import atexit
import base64
import json
import logging
import os
import sqlite3
import time
import traceback
import urllib3
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime
from email.message import EmailMessage
from io import BytesIO
from xml.dom import minidom

import requests
import smtplib
from flask import (
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import login_required, current_user
from requests.auth import HTTPBasicAuth
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from db_utils import BASE_DIR, get_db_connection
from Services.invoice_buyer import (
    DEFAULT_RETAIL_BUYER_NAME,
    enrich_sale_buyer_identity,
    extract_buyer_invoice_fields,
    normalize_retail_buyer_name,
)
from Services.einvoice_factory import create_einvoice_service
from Services.einvoice_registry import list_providers_api, list_providers_for_ui
from Services.invoice_xml import (
    esign_xml_content,
    normalize_invoice_config,
    prepare_sale_invoice_xml,
    save_invoice_xml,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# SMTP — đọc từ biến môi trường, fallback giống app.py
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.zoho.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "keto@ketoshop.pro.vn")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

VIETTEL_CONFIG = {
    "api_url": os.getenv(
        "VIETTEL_API_URL",
        "https://demo-sinvoice.viettel.vn:8443/InvoiceAPI/InvoiceWS",
    ),
    "username": os.getenv("VIETTEL_USERNAME"),
    "password": os.getenv("VIETTEL_PASSWORD"),
}

_invoice_scheduler_started = False


def _so_thanh_chu(amount):
    """Lazy import tránh circular import với app.py."""
    from helpers import so_thanh_chu

    return so_thanh_chu(amount)


def register_invoice_routes(app):
    """Đăng ký route hóa đơn điện tử (giữ nguyên URL/endpoint)."""
    global _invoice_scheduler_started

    class ViettelService:
        @staticmethod
        def get_business_info():
            """Lấy thông tin doanh nghiệp từ bảng business_info (dòng đầu tiên hoặc mặc định)"""
            from app import BusinessInfo

            business = BusinessInfo.query.first()
            if not business or not business.tax_code:
                raise ValueError("Không tìm thấy thông tin doanh nghiệp trong bảng business_info (thiếu tax_code).")
            return {
                'tax_code': business.tax_code,
                'business_name': business.business_name or "DOANH NGHIỆP TEST"  # Fallback nếu null
            }

        @staticmethod
        def get_supplier_tax_code():
            """Chỉ lấy tax_code (dùng cho URL và sellerTaxCode)"""
            info = ViettelService.get_business_info()
            return info['tax_code']

        @staticmethod
        def get_next_invoice_number(template_code="02GTTT0/003", invoice_series="AA/24E"):
            """Lấy số hóa đơn tiếp theo từ API getNextInvoiceNumber"""
            mst = ViettelService.get_supplier_tax_code()
            url = f"{VIETTEL_CONFIG['api_url']}/getNextInvoiceNumber/{mst}"
        
            # Payload XML đơn giản cho getNextInvoiceNumber
            root = ET.Element("invoiceRequest")
            ET.SubElement(root, "templateCode").text = template_code
            ET.SubElement(root, "invoiceSeries").text = invoice_series  # Nếu có series riêng
        
            xml_payload = ET.tostring(root, encoding='utf-8', method='xml').decode('utf-8')
        
            headers = {
                'Content-Type': 'application/xml',
                'Accept': 'application/xml'
            }
        
            try:
                response = requests.post(
                    url,
                    data=xml_payload.encode('utf-8'),
                    headers=headers,
                    auth=HTTPBasicAuth(VIETTEL_CONFIG["username"], VIETTEL_CONFIG["password"]),
                    verify=False,
                    timeout=20
                )
                if response.status_code == 200:
                    # Parse XML response để lấy invoiceNo
                    root = ET.fromstring(response.text)
                    invoice_no = root.find('.//invoiceNo').text
                    return invoice_no
                else:
                    raise ValueError(f"Error getting invoiceNo: {response.text}")
            except Exception as e:
                raise ValueError(f"Connection Error in getNextInvoiceNumber: {str(e)}")

        @staticmethod
        def build_xml_payload(data, invoice_no):
            """Tạo XML chuẩn theo spec Viettel"""
            root = ET.Element("commonInvoiceInput")
        
            # 1. generalInvoiceInfo
            gen_info = ET.SubElement(root, "generalInvoiceInfo")
            ET.SubElement(gen_info, "invoiceType").text = "02GTTT"
            ET.SubElement(gen_info, "templateCode").text = "02GTTT0/003"
            ET.SubElement(gen_info, "invoiceNo").text = invoice_no
            ET.SubElement(gen_info, "invoiceIssuedDate").text = str(int(time.time() * 1000))
            ET.SubElement(gen_info, "currencyCode").text = "VND"
            ET.SubElement(gen_info, "adjustmentType").text = "1"
            ET.SubElement(gen_info, "paymentStatus").text = "true"
            ET.SubElement(gen_info, "cusGetInvoiceRight").text = "true"
        
            # 2. buyerInfo
            buyer = ET.SubElement(root, "buyerInfo")
            ET.SubElement(buyer, "buyerName").text = normalize_retail_buyer_name(data.get('customer_name'))
            ET.SubElement(buyer, "buyerAddressLine").text = data.get('address', 'N/A')
            ET.SubElement(buyer, "buyerPhoneNumber").text = data.get('phone', '')
        
            # 3. sellerInfo - Lấy động từ DB
            business_info = ViettelService.get_business_info()
            seller = ET.SubElement(root, "sellerInfo")
            ET.SubElement(seller, "sellerLegalName").text = business_info['business_name']
            ET.SubElement(seller, "sellerTaxCode").text = business_info['tax_code']
        
            # 4. payments
            payments = ET.SubElement(root, "payments")
            ET.SubElement(payments, "paymentMethodName").text = "TM"
        
            # 5. itemInfo
            items = data.get('items', [])
            for index, item in enumerate(items):
                item_xml = ET.SubElement(root, "itemInfo")
                ET.SubElement(item_xml, "lineNumber").text = str(index + 1)
                ET.SubElement(item_xml, "itemName").text = item['name']
                ET.SubElement(item_xml, "unitName").text = item['unit']
                ET.SubElement(item_xml, "unitPrice").text = str(item['price'])
                ET.SubElement(item_xml, "quantity").text = str(item['qty'])
                ET.SubElement(item_xml, "itemTotalAmountWithoutTax").text = str(item['price'] * item['qty'])
                ET.SubElement(item_xml, "taxPercentage").text = "10"
                ET.SubElement(item_xml, "taxAmount").text = str((item['price'] * item['qty']) * 0.1)
        
            # 6. summarizeInfo
            summary = ET.SubElement(root, "summarizeInfo")
            ET.SubElement(summary, "sumOfTotalLineAmountWithoutTax").text = str(data.get('subtotal'))
            ET.SubElement(summary, "totalAmountWithoutTax").text = str(data.get('subtotal'))
            ET.SubElement(summary, "totalTaxAmount").text = str(data.get('tax_amount'))
            ET.SubElement(summary, "totalAmountWithTax").text = str(data.get('total'))
            ET.SubElement(summary, "totalAmountWithTaxInWords").text = data.get('total_in_words', 'Hai trăm nghìn đồng')  # Tùy chỉnh convert
        
            # 7. taxBreakdowns
            tax_breakdowns = ET.SubElement(root, "taxBreakdowns")
            tax_item = ET.SubElement(tax_breakdowns, "taxBreakdown")
            ET.SubElement(tax_item, "taxPercentage").text = "10"
            ET.SubElement(tax_item, "taxableAmount").text = str(data.get('subtotal'))
            ET.SubElement(tax_item, "taxAmount").text = str(data.get('tax_amount'))
        
            return ET.tostring(root, encoding='utf-8', method='xml').decode('utf-8')

        @classmethod
        def call_api(cls, data):
            """Gửi yêu cầu createInvoice sau khi lấy invoiceNo"""
            mst = cls.get_supplier_tax_code()
            url = f"{VIETTEL_CONFIG['api_url']}/createInvoice/{mst}"
        
            try:
                # Bước 1: Lấy invoiceNo
                invoice_no = cls.get_next_invoice_number()
            
                # Bước 2: Build XML với invoiceNo
                xml_payload = cls.build_xml_payload(data, invoice_no)
            
                headers = {
                    'Content-Type': 'application/xml',
                    'Accept': 'application/xml'
                }
            
                response = requests.post(
                    url,
                    data=xml_payload.encode('utf-8'),
                    headers=headers,
                    auth=HTTPBasicAuth(VIETTEL_CONFIG["username"], VIETTEL_CONFIG["password"]),
                    verify=False,
                    timeout=30  # Tăng timeout để tránh ECONNRESET
                )
                return response
            except ValueError as ve:
                return str(ve)
            except Exception as e:
                return str(e)

    # --- API ENDPOINT CHO FRONTEND ---
    @app.route('/api/invoice/publish', methods=['POST'])
    def publish_invoice():
        order_data = request.json
        response = ViettelService.call_api(order_data)
    
        if isinstance(response, str):
            return jsonify({"success": False, "message": f"Error: {response}"}), 500
    
        # Trả về XML response từ Viettel (parse nếu cần)
        if response.status_code == 200:
            # Parse response để lấy invoiceNo, reservationCode
            root = ET.fromstring(response.text)
            result = root.find('.//result')
            if result is not None:
                invoice_no = result.find('invoiceNo').text
                reservation_code = result.find('reservationCode').text
                return jsonify({"success": True, "invoiceNo": invoice_no, "reservationCode": reservation_code}), 200
    
        return (response.text, response.status_code, {'Content-Type': 'application/xml'})
    #===HÀM GỬI EMAIL CHO KHÁCH SAU KHI XUẤT HÓA ĐƠN===#
    import smtplib
    from email.message import EmailMessage

    #SMTP_SERVER = 'smtp.gmail.com'
    #SMTP_PORT = 587
    #SENDER_EMAIL = 'tinkien@gmail.com'
    #APP_PASSWORD = 'cqxj mdfm khuk cuqs'

    def send_invoice_email(to_email, invoice_no, pdf_url, xml_url, config=None):
        try:
            msg = EmailMessage()
            msg['Subject'] = f"Hóa đơn điện tử #{invoice_no}"
            msg['From'] = SENDER_EMAIL
            msg['To'] = to_email
            msg.set_content(f"Kính gửi Quý khách,\n\nChúng tôi xin gửi hóa đơn điện tử số {invoice_no} đính kèm.\nTrân trọng!")

            pdf_bytes = _fetch_invoice_attachment_bytes(pdf_url, 'pdf', config=config)
            if pdf_bytes:
                msg.add_attachment(
                    pdf_bytes, maintype='application', subtype='pdf',
                    filename=f"HD_{invoice_no}.pdf",
                )

            xml_bytes = _fetch_invoice_attachment_bytes(xml_url, 'xml', config=config)
            if xml_bytes:
                msg.add_attachment(
                    xml_bytes, maintype='application', subtype='xml',
                    filename=f"HD_{invoice_no}.xml",
                )

            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(SENDER_EMAIL, APP_PASSWORD)
                server.send_message(msg)
            return True
        except Exception as e:
            logging.error(f"Email error: {e}")
            return False
    #========================================================= Bắt ĐẦU PHẦN GIẢ LẬP ĐỂ TEST, KHÔNG CÓ CHỨC NĂNG GỬI CQ.THUẾ==================================================================#

    def mock_signature(signer_name):
        raw = f"{signer_name}|{datetime.now().isoformat()}"
        return base64.b64encode(raw.encode()).decode()


    @app.route("/api/invoice/mock-sign/<int:sale_id>", methods=["POST"])
    def mock_sign_invoice(sale_id):
        conn = get_db_connection()
        try:
            prepared = prepare_sale_invoice_xml(
                conn, sale_id, _so_thanh_chu, fkey_prefix="MOCK"
            )
            save_invoice_xml(prepared["xml_path"], prepared["xml_content"])
            conn.execute(
                "UPDATE sale SET signed_mock = 1 WHERE id = ?",
                (sale_id,),
            )
            conn.commit()
            sh_don = prepared["sh_don"]
            return jsonify({
                "success": True,
                "message": f"XML tạo thành công với số hóa đơn chính thức: {sh_don}",
                "download_url": f"/api/invoice/download/{sale_id}",
                "xml_path": prepared["xml_path"],
            })
        except LookupError:
            abort(404, "Không tìm thấy đơn hàng")
        except ValueError as exc:
            abort(400, str(exc))
        finally:
            conn.close()

    #======================================================================= Kết Thúc Phần Giả Lập==============================================================================#
    #==== API XUẤT HÓA ĐƠN TỰ ĐỘNG CHẠY NGẦM HOẠT ĐANG ĐỘNG TỐT - CHƯA SỬ DỤNG TRONG APP====#
    @app.route('/api/invoice/auto-process', methods=['POST'])
    @login_required
    def api_auto_process_invoices():
        """
        API tích hợp: Tự động quét và xuất hóa đơn ngầm.
        Chỉ chạy khi invoice_settings.is_active=1 và auto_issue_invoice=1
        """
        conn = None
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 1. Kiểm tra cấu hình
            config_row = cursor.execute("""
                SELECT * FROM invoice_settings
                WHERE is_active = 1 AND auto_issue_invoice = 1
            """).fetchone()

            if not config_row:
                return jsonify({
                    "success": False,
                    "error": "Chế độ tự động xuất hóa đơn chưa bật."
                }), 400

            config = dict(config_row)

            # =============================
            # Worker xử lý ngầm
            # =============================
            def background_invoice_worker(cfg):

                worker_conn = None

                try:
                    worker_conn = get_db_connection()
                    worker_conn.row_factory = sqlite3.Row
                    worker_cur = worker_conn.cursor()

                    # Lấy 20 đơn chưa có hóa đơn
                    pending_sales = worker_cur.execute("""
                        SELECT id FROM sale
                        WHERE status = 'completed'
                          AND COALESCE(invoice_status, '') NOT IN ('draft', 'issued')
                          AND (invoice_number IS NULL OR invoice_number = '')
                          AND COALESCE(invoice_id, '') = ''
                        ORDER BY id ASC
                        LIMIT 20
                    """).fetchall()

                    if not pending_sales:
                        logging.info("Worker: Không còn đơn cần xuất hóa đơn.")
                        return

                    provider_key = (cfg.get('provider_name') or 'matbao').strip().lower()
                    try:
                        service = create_einvoice_service(cfg, matbao_cls=MatbaoProvider)
                    except Exception as factory_err:
                        logging.error("Worker: không khởi tạo provider %s: %s", provider_key, factory_err)
                        return

                    for row in pending_sales:

                        sale_id = row['id']

                        try:

                            # Lấy thông tin đơn hàng
                            sale_row = worker_cur.execute(
                                "SELECT * FROM sale WHERE id = ?",
                                (sale_id,)
                            ).fetchone()

                            if not sale_row:
                                logging.error(f"Worker: Không tìm thấy sale {sale_id}")
                                continue

                            sale = dict(sale_row)

                            # Lấy items (FIX giống API gốc)
                            items_rows = worker_cur.execute("""
                                SELECT p.name, si.quantity, si.price,
                                       CASE WHEN si.UseSaleUnit = 1
                                            THEN p.unit1
                                            ELSE p.unit
                                       END as unit
                                FROM sale_items si
                                JOIN products p ON si.product_id = p.id
                                WHERE si.sale_id = ?
                            """, (sale_id,)).fetchall()

                            items = [dict(r) for r in items_rows]

                            if not items:
                                logging.error(f"Worker: Sale {sale_id} không có sản phẩm")
                                continue

                            # Login token (Mắt Bão) nếu provider hỗ trợ
                            inner = getattr(service, '_inner', service)
                            if hasattr(inner, '_get_token') and not inner._get_token():
                                logging.error("Worker: Không đăng nhập được provider %s", provider_key)
                                break

                            # Gọi API xuất hóa đơn chính thức (giống luồng Mắt Bão)
                            result = service.issue(sale, items, loai_hdon=1)

                            if result.get('success'):

                                invoice_no = result.get('invoice_no')
                                pdf_url = result.get('pdf_url')
                                xml_url = result.get('xml_url')
                                invoice_date = _resolve_invoice_date(sale, result)
                                tax_auth_status = result.get('tax_authority_status')

                                # Update DB
                                worker_cur.execute("""
                                    UPDATE sale
                                    SET invoice_number = ?,
                                        invoice_status = 'issued',
                                        invoice_pdf_url = ?,
                                        invoice_xml_file = ?,
                                        invoice_provider = ?,
                                        invoice_date = ?,
                                        tax_authority_status = ?,
                                        updated_at = CURRENT_TIMESTAMP
                                    WHERE id = ?
                                """, (
                                    invoice_no,
                                    pdf_url,
                                    xml_url,
                                    provider_key,
                                    invoice_date,
                                    tax_auth_status,
                                    sale_id,
                                ))

                                worker_conn.commit()

                                logging.info(
                                    f"Worker: Đã xuất hóa đơn {invoice_no} cho sale {sale_id}"
                                )

                                # Gửi email
                                customer_email = sale.get('email', '').strip()

                                if customer_email:
                                    try:
                                        send_invoice_email(
                                            to_email=customer_email,
                                            invoice_no=invoice_no,
                                            pdf_url=pdf_url,
                                            xml_url=xml_url,
                                            config=cfg,
                                        )
                                    except Exception as email_err:
                                        logging.error(
                                            f"⚠️ Hóa đơn {invoice_no} xuất thành công nhưng gửi email lỗi: {str(email_err)}"
                                        )

                            else:
                                logging.error(
                                    f"Worker: Xuất hóa đơn thất bại cho sale {sale_id}: {result.get('error')}"
                                )

                        except Exception as e:

                            logging.error(
                                f"Worker Error sale {sale_id}: {str(e)}",
                                exc_info=True
                            )

                            if worker_conn:
                                worker_conn.rollback()

                    # Chạy tiếp sau 3 giây
                    threading.Timer(
                        3.0,
                        background_invoice_worker,
                        args=[cfg]
                    ).start()

                except Exception as e:
                    logging.error(
                        f"Worker critical error: {str(e)}",
                        exc_info=True
                    )

                finally:
                    if worker_conn:
                        worker_conn.close()

            # =============================
            # Khởi động worker
            # =============================

            thread = threading.Thread(
                target=background_invoice_worker,
                args=(config,)
            )

            thread.daemon = True
            thread.start()

            return jsonify({
                "success": True,
                "message": "Worker tự động xuất hóa đơn đã khởi động."
            })

        except Exception as e:

            logging.error(
                f"API auto-process error: {str(e)}",
                exc_info=True
            )

            return jsonify({
                "success": False,
                "error": str(e)
            }), 500

        finally:
            if conn:
                conn.close()

    # ====================== ADAPTER CHO MẮT BÃO ======================

    # Tắt cảnh báo InsecureRequestWarning khi verify=False
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    class MatbaoProvider:
        def __init__(self, config):
            self.config = config
            self.base_url = config.get('api_url', '').strip().rstrip('/')
            self.tax_code = config.get('tax_code', '').strip()
            self.username = config.get('username', '').strip()
            self.password = config.get('password', '').strip()
            self._token = None

        def _get_token(self):
            """Lấy Token định danh từ hệ thống Mắt Bão"""
            if self._token:
                return True
            
            url = f"{self.base_url}/api/auth/login"
            payload = {
                "MST": self.tax_code,
                "TDNhap": self.username,
                "MKhau": self.password
            }
        
            try:
                # verify=False nếu bạn đang ở môi trường dev hoặc server dùng self-signed cert
                response = requests.post(url, json=payload, timeout=20, verify=False)
                res_data = response.json()
            
                error_code = res_data.get('errorCode') or res_data.get('ErrorCode')
                if error_code == 200:
                    data_obj = res_data.get('data') or res_data.get('Data')
                    # Xử lý trường hợp accessToken nằm trong dict hoặc trả về trực tiếp
                    self._token = data_obj.get('accessToken') if isinstance(data_obj, dict) else data_obj
                    logging.info(f"✅ Matbao: Đăng nhập thành công cho MST {self.tax_code}")
                    return True
                
                logging.error(f"❌ Matbao Login thất bại: {res_data.get('message') or res_data.get('Message')}")
                return False
            except Exception as e:
                logging.error(f"❌ Lỗi kết nối Login Matbao: {str(e)}")
                return False

        def _get_headers(self):
            """Tạo Header chứa Token cho các request tiếp theo"""
            if not self._token and not self._get_token():
                raise Exception("Không thể lấy token từ Matbao. Vui lòng kiểm tra lại cấu hình API.")
            return {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json"
            }

        def _prepare_dsh_hd_vu(self, items):
            """
            Xử lý danh sách hàng hóa: Tính toán Thuế suất và Chiết khấu cho từng dòng hàng.
            """
            dsh_hd_vu = []
            total_untaxed = 0.0
            total_tax_amount = 0.0

            for i, item in enumerate(items):
                # Lấy dữ liệu từ frontend hoặc database qua item
                qty = float(item.get('quantity') or 0)
                price = float(item.get('price') or 0)
                tax_pct = float(item.get('tax_pct') or 0)  # Ví dụ: 0, 5, 8, 10
                discount_pct = float(item.get('discount_pct') or 0)

                # 1. Tính thành tiền trước chiết khấu
                amount_before = round(qty * price, 2)
            
                # 2. Tính số tiền chiết khấu của dòng này
                disc_amount = round(amount_before * (discount_pct / 100), 2)
            
                # 3. Thành tiền sau chiết khấu (Đây là giá trị để tính thuế)
                amount_after = round(amount_before - disc_amount, 2)
            
                # 4. Tính tiền thuế dựa trên thuế suất của dòng
                tax_val = 0.0
                if tax_pct > 0:
                    tax_val = round(amount_after * (tax_pct / 100), 2)

                dsh_hd_vu.append({
                    "TChat": 1, # 1: Hàng hóa/Dịch vụ
                    "STT": i + 1,
                    "MHHDVu": str(item.get('product_code') or ''),
                    "THHDVu": str(item.get('name') or item.get('product_name', '')),
                    "DVTinh": str(item.get('unit') or 'Cái'),
                    "SLuong": qty,
                    "DGia": price,
                    "ThTienChuaCK": amount_before,
                    "TLCKhau": discount_pct,
                    "STCKhau": disc_amount,
                    "ThTien": amount_after,
                    "TSuat": int(tax_pct), # Mắt Bão yêu cầu số nguyên (Int32)
                    "TThue": tax_val,
                    "TgTien": round(amount_after + tax_val, 2)
                })

                total_untaxed += amount_after
                total_tax_amount += tax_val

            return dsh_hd_vu, round(total_untaxed, 2), round(total_tax_amount, 2)

        def issue(self, sale_data, items, loai_hdon=1, replace_unpublished=False):
            """Phát hành hóa đơn gốc. loai_hdon: 0=nháp, 1=chính thức."""
            if not self._token and not self._get_token():
                return {"success": False, "error": "Không thể xác thực với hệ thống Mắt Bão."}

            dsh_hd_vu, total_untaxed, total_tax = self._prepare_dsh_hd_vu(items)
            url_create = f"{self.base_url}/api/invoice/create-invoice"
            loai = _normalize_loai_hdon(loai_hdon, default=1)
            buyer_fields = extract_buyer_invoice_fields(sale_data)
            logging.info(
                "Matbao create-invoice: LoaiHDon=%s replace_unpublished=%s sale_no=%s",
                loai, replace_unpublished, sale_data.get('sale_no'),
            )

            payload = [{
                "KHMSHDon": str(self.config.get('invoice_type', '2')),
                "KHHDon": str(self.config.get('invoice_series', 'C26MES')),
                "LoaiHDon": loai,
                "TCHDon": 0,
                "NLap": datetime.now().strftime('%Y-%m-%dT00:00:00'),
                "DVTTe": 704,
                "TGia": 1.0,
                "HTTToan": "TM/CK",
                "NMua_HVTNMHang": str(sale_data.get('customer_name') or "").strip(),
                "NMua_Ten": str(sale_data.get('company_name') or "").strip(),
                "NMua_MST": buyer_fields['tax_code'],
                "NMua_MDVQHNSach": buyer_fields['budget_unit_code'],
                "NMua_SHChieu": buyer_fields['passport_no'],
                "NMua_DChi": str(sale_data.get('address') or "").strip(),
                "NMua_SDThoai": str(sale_data.get('customer_phone') or sale_data.get('phone') or "").strip(),
                "NMua_DCTDTu": str(sale_data.get('email') or "").strip(),
                "DSHHDVu": dsh_hd_vu,
                "TgThTien": total_untaxed,
                "TgTThue": total_tax,
                "TgTTTBSo": round(total_untaxed + total_tax, 2),
                "TgTTTBChu": "",
                "KTraMTChieuTrung": 2 if replace_unpublished else 1,
                "MTChieu": str(sale_data.get('sale_no') or "").strip()
            }]

            result = self._send_request(url_create, payload)
            if result.get('success'):
                result['is_draft'] = (loai == 0)
            return result

        def sign_draft(self, invoice_id):
            """Ký / phát hành hóa đơn nháp đã tạo (POST sign-invoice)."""
            if not invoice_id:
                return {"success": False, "error": "Thiếu mã hóa đơn nháp."}
            if not self._token and not self._get_token():
                return {"success": False, "error": "Không thể xác thực với hệ thống Mắt Bão."}
            try:
                url = f"{self.base_url}/api/invoice/sign-invoice"
                headers = self._get_headers()
                res = requests.post(
                    url,
                    json={"MaSoHDon": str(invoice_id)},
                    headers=headers,
                    timeout=30,
                    verify=False,
                )
                res_data = res.json()
                logging.info("Matbao sign-invoice: %s", res_data)
                error_code = res_data.get('errorCode') or res_data.get('ErrorCode')
                if error_code != 200:
                    return {
                        "success": False,
                        "error": res_data.get('message') or res_data.get('Message') or "Không thể phát hành hóa đơn nháp",
                    }
                data_list = res_data.get('data') or res_data.get('Data') or []
                item = data_list[0] if isinstance(data_list, list) and data_list else res_data
                if isinstance(item, dict) and isinstance(item.get('data'), dict):
                    actual = item['data']
                elif isinstance(item, dict):
                    actual = item
                else:
                    actual = {}
                return {
                    "success": True,
                    "is_draft": False,
                    "invoice_id": str(actual.get('InvID') or actual.get('maSoHDon') or invoice_id),
                    "invoice_no": str(actual.get('shDon') or actual.get('SHDon') or actual.get('No') or ''),
                    "pdf_url": actual.get('urlDownloadPDF') or actual.get('PDFUrl'),
                    "xml_url": actual.get('urlDownloadXML') or actual.get('XMLUrl'),
                    "invoice_date": str(actual.get('nLap') or actual.get('ArisingDate') or '')[:10],
                    "tax_authority_status": actual.get('tenTThaiHDon') or actual.get('TenTTHDon') or 'Chưa Gửi CQT',
                    "total_amount": float(actual.get('tgTTTBSo') or actual.get('TgTTTBSo') or 0),
                }
            except Exception as e:
                logging.error("sign_draft error: %s", e)
                return {"success": False, "error": f"Lỗi kết nối Mắt Bão: {str(e)}"}

        def issue_replacement(self, sale_data, items, replacement_info):
            """Phát hành hóa đơn thay thế (TCHDon: 1)"""
            if not self._token and not self._get_token():
                return {"success": False, "error": "Lỗi xác thực hệ thống."}

            dsh_hd_vu, total_untaxed, total_tax = self._prepare_dsh_hd_vu(items)
            url_create = f"{self.base_url}/api/invoice/create-invoice"
            buyer_fields = extract_buyer_invoice_fields(sale_data)

            payload = [{
                "KHMSHDon": str(self.config.get('invoice_type', '1')),
                "KHHDon": str(self.config.get('invoice_series', 'C26TAT')),
                "MaTraCuu": datetime.now().strftime('%Y%m%d_%H%M%S'),
                "MTChieu": str(sale_data.get('sale_no') or ""),
                "NLap": datetime.now().strftime('%Y-%m-%d'),
                "LoaiHDon": 1,
                "TCHDon": 1,  # 1: Hóa đơn thay thế
                "LoaiTraHang": 0,
                "DVTTe": 704,
                "TGia": 1.0,
                "HTTToan": "TM/CK",
                "NMua_Ten": str(sale_data.get('company_name') or ""),
                "NMua_MST": buyer_fields['tax_code'],
                "NMua_MDVQHNSach": buyer_fields['budget_unit_code'],
                "NMua_SHChieu": buyer_fields['passport_no'],
                "NMua_DChi": str(sale_data.get('address') or ""),
                "NMua_HVTNMHang": str(sale_data.get('customer_name') or ""),
                "NMua_SDThoai": str(sale_data.get('customer_phone') or sale_data.get('phone') or ""),
                "NMua_DCTDTu": str(sale_data.get('email') or ""),

                # Thông tin hóa đơn gốc cần thay thế (Lấy từ frontend truyền vào)
                "MSHDonDCLQuan": replacement_info.get("MSHDonDCLQuan"),
                "KHMSHDCLQuan": replacement_info.get("KHMSHDCLQuan"),
                "KHHDCLQuan": replacement_info.get("KHHDCLQuan"),
                "SHDCLQuan": int(replacement_info.get("SHDCLQuan") or 0),
                "NLHDCLQuan": replacement_info.get("NLHDCLQuan"),

                "DSHHDVu": dsh_hd_vu,
                "TgThTien": total_untaxed,
                "TgTThue": total_tax,
                "TgTTTBSo": round(total_untaxed + total_tax, 2),
                "TgTTTBChu": "",
                "MTChieu": str(sale_data.get('sale_no') or "").strip()
            }]

            return self._send_request(url_create, payload)

        def _send_request(self, url, payload):
            """Gửi request chính thức đến Mắt Bão và xử lý kết quả trả về"""
            try:
                headers = self._get_headers()
                res = requests.post(url, json=payload, headers=headers, timeout=30, verify=False)
                res_data = res.json()

                logging.info(f"Matbao Response: {res_data}")

                # Kiểm tra mã lỗi từ Mắt Bão (có thể là errorCode hoặc ErrorCode tùy API)
                error_code = res_data.get('errorCode') or res_data.get('ErrorCode')
                if error_code != 200:
                    return {
                        "success": False,
                        "error": res_data.get('message') or res_data.get('Message') or "Lỗi không xác định từ Mắt Bão"
                    }

                data_list = res_data.get('data') or res_data.get('Data') or []
                if not data_list:
                    return {"success": False, "error": "API trả về thành công nhưng không có dữ liệu hóa đơn."}

                # Lấy object hóa đơn đầu tiên trong mảng trả về
                item = data_list[0] if isinstance(data_list, list) else data_list
                actual = item.get('data', {}) if isinstance(item.get('data'), dict) else item

                return {
                    "success": True,
                    "invoice_id": str(actual.get('InvID') or actual.get('maSoHDon') or ''),
                    "invoice_no": str(actual.get('shDon') or actual.get('SO') or actual.get('No') or ''),
                    "pdf_url": actual.get('urlDownloadPDF') or actual.get('PDFUrl'),
                    "xml_url": actual.get('urlDownloadXML') or actual.get('XMLUrl'),
                    "invoice_date": str(actual.get('nLap') or actual.get('ArisingDate') or '')[:10],
                    "tax_authority_status": actual.get('tenTThaiHDon') or actual.get('TenTTHDon') or 'Chưa Gửi CQT',
                    "total_amount": float(actual.get('tgTTTBSo') or actual.get('TgTTTBSo') or 0),
                    "tax_code": actual.get('nMua_MST') or actual.get('NMua_MST'),
                    "address": actual.get('nMua_DChi') or actual.get('NMua_DChi')
                }
            except Exception as e:
                logging.error(f"❌ _send_request error: {str(e)}")
                return {"success": False, "error": f"Lỗi kết nối Mắt Bão: {str(e)}"}

    def _normalize_loai_hdon(value, default=1):
        """Chuẩn hóa LoaiHDon Mắt Bão: 0=nháp, 1=chính thức. Không dùng `or 1` vì 0 là giá trị hợp lệ."""
        if value is None:
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return 0 if parsed == 0 else 1

    def _normalize_invoice_no(value):
        text = str(value or '').strip()
        if not text or text in ('0', '00000000'):
            return '0'
        return text

    def _resolve_invoice_date(sale, result=None):
        """Luôn trả chuỗi ngày HĐ — tránh NOT NULL trên outward_invoices.invoice_date."""
        result = result or {}
        sale = sale or {}
        for candidate in (
            result.get('invoice_date'),
            sale.get('invoice_date'),
            sale.get('created_at'),
        ):
            if candidate is None:
                continue
            text = str(candidate).strip().replace('T', ' ')
            if not text:
                continue
            if len(text) == 10 and text[4] == '-':
                return f"{text} {datetime.now().strftime('%H:%M:%S')}"
            return text[:19]
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _sale_is_draft_invoice(sale_row):
        status = str(sale_row.get('invoice_status') or '').lower()
        inv_no = _normalize_invoice_no(sale_row.get('invoice_number'))
        return status == 'draft' or (inv_no == '0' and bool(sale_row.get('invoice_id')))

    def _sale_has_official_invoice(sale_row):
        if _sale_is_draft_invoice(sale_row):
            return False
        inv_no = str(sale_row.get('invoice_number') or '').strip()
        status = str(sale_row.get('invoice_status') or '').lower()
        return bool(inv_no) and status == 'issued'

    def _fetch_sale_invoice_items(cursor, sale_id):
        items_rows = cursor.execute("""
            SELECT
                COALESCE(si.product_name, p.name) AS name,
                COALESCE(p.product_code, 'DV') AS product_code,
                si.quantity, si.price,
                COALESCE(si.discount_pct, 0) as discount_pct,
                COALESCE(si.tax_pct, 0) as tax_pct,
                CASE
                    WHEN si.unit IS NOT NULL AND si.unit != '' THEN si.unit
                    WHEN si.UseSaleUnit = 1 THEN p.unit1
                    ELSE COALESCE(p.unit, 'Cái')
                END as unit
            FROM sale_items si
            LEFT JOIN products p ON si.product_id = p.id
            WHERE si.sale_id = ?
            ORDER BY si.rowid ASC
        """, (sale_id,)).fetchall()
        return [dict(r) for r in items_rows]

    def _enrich_sale_buyer_identity(cursor, sale):
        tax = (sale.get('tax_code') or '').strip()
        customer_row = None
        if tax:
            row = cursor.execute(
                """
                SELECT budget_unit_code, passport_no
                FROM customers
                WHERE tax_code = ?
                LIMIT 1
                """,
                (tax,),
            ).fetchone()
            if row:
                customer_row = dict(row)
        return enrich_sale_buyer_identity(sale, customer_row)

    def _fetch_invoice_attachment_bytes(url, file_type, config=None):
        """Tải file HĐ đính kèm email — hỗ trợ proxy VNPT và link HTTP thông thường."""
        url = str(url or '').strip()
        if not url:
            return None

        if url.startswith('/api/vnpt/download-file') or 'vnpt-invoice' in url.lower():
            from Services.einvoice_adapters import VNPTInvoiceAdapter
            from urllib.parse import parse_qs, unquote, urlparse

            token = ''
            fkey = ''
            ft = file_type
            if url.startswith('/'):
                parsed = urlparse(url)
                qs = parse_qs(parsed.query)
                token = unquote((qs.get('token') or [''])[0])
                fkey = unquote((qs.get('fkey') or [''])[0])
                ft = (qs.get('type') or [file_type])[0]
            elif 'token=' in url:
                token = unquote(url.split('token=', 1)[1].split('&', 1)[0])
            elif 'fkey=' in url:
                fkey = unquote(url.split('fkey=', 1)[1].split('&', 1)[0])

            if not token and 'downloadInvPDFNoPay?token=' in url:
                token = unquote(url.split('token=', 1)[1].split('&', 1)[0])

            if fkey or token:
                if config is None:
                    cfg_conn = get_db_connection()
                    try:
                        row = cfg_conn.execute(
                            "SELECT * FROM invoice_settings WHERE is_active = 1"
                        ).fetchone()
                        config = dict(row) if row else {}
                    finally:
                        cfg_conn.close()
                adapter = VNPTInvoiceAdapter(config or {})
                if fkey:
                    result = adapter.download_invoice_by_fkey(fkey, ft)
                else:
                    result = adapter.download_invoice_file(token, ft)
                if result.get('success'):
                    return result.get('data')
            return None

        if url.startswith('/'):
            base = (os.environ.get('APP_BASE_URL') or request.host_url or '').rstrip('/')
            if base:
                url = f'{base}{url}'

        try:
            resp = requests.get(url, timeout=20, verify=False)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception as exc:
            logging.warning('fetch invoice attachment failed: %s', exc)
        return None

    def _cleanup_invalid_outward_invoices(cursor):
        """Xóa dòng outward_invoices mồ côi — giữ hóa đơn nháp có sale_id/fkey."""
        cursor.execute("""
            DELETE FROM outward_invoices
            WHERE sale_id IS NULL
              AND COALESCE(invoice_id, '') = ''
              AND COALESCE(fkey, '') = ''
              AND COALESCE(invoice_no, '') IN ('', '0')
        """)

    def _find_outward_invoice_for_sale(cursor, sale_id):
        return cursor.execute("""
            SELECT id FROM outward_invoices
            WHERE sale_id = ?
            ORDER BY
              CASE
                WHEN COALESCE(status, '') = 'draft'
                  OR COALESCE(invoice_no, '') IN ('', '0') THEN 0
                ELSE 1
              END,
              id DESC
            LIMIT 1
        """, (sale_id,)).fetchone()

    def _upsert_outward_invoice(
        cursor, sale_id, sale, *, invoice_no, invoice_id, pdf_url, xml_url,
        invoice_date, status, total_payment=None, fkey=None,
    ):
        customer_name = sale.get('company_name') or sale.get('customer_name')
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        total_payment = total_payment if total_payment is not None else sale.get('total_amount')
        fkey = fkey or invoice_id or ''
        existing = _find_outward_invoice_for_sale(cursor, sale_id)
        if existing:
            cursor.execute("""
                UPDATE outward_invoices SET
                    sale_no = ?, invoice_no = ?, invoice_id = ?, customer_name = ?,
                    total = ?, pdf_url = ?, xml_file = ?, invoice_date = ?,
                    updated_at = ?, status = ?, fkey = ?
                WHERE id = ?
            """, (
                sale.get('sale_no'), invoice_no, invoice_id, customer_name,
                total_payment, pdf_url or '', xml_url or '', invoice_date, current_time,
                status, fkey, existing['id'],
            ))
            return existing['id']
        cursor.execute("""
            INSERT INTO outward_invoices (
                sale_id, sale_no, invoice_no, invoice_id, customer_name,
                total, pdf_url, xml_file, invoice_date, created_at, updated_at, status, fkey
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sale_id, sale.get('sale_no'), invoice_no, invoice_id, customer_name,
            total_payment, pdf_url or '', xml_url or '', invoice_date,
            current_time, current_time, status, fkey,
        ))
        return cursor.lastrowid

    def _repair_vnpt_outward_invoices(cursor, config):
        """Chuẩn hóa link PDF/XML VNPT trong DB (nháp theo Fkey, chính thức theo số HĐ)."""
        _cleanup_invalid_outward_invoices(cursor)
        from Services.einvoice_adapters import VNPTInvoiceAdapter

        adapter = VNPTInvoiceAdapter(config)
        pattern = (config.get('invoice_type') or '1/001').strip()
        serial = (config.get('invoice_series') or 'C26TAA').strip()

        draft_rows = cursor.execute("""
            SELECT o.id, o.fkey, o.invoice_id
            FROM outward_invoices o
            LEFT JOIN sale s ON s.id = o.sale_id
            WHERE COALESCE(s.invoice_provider, '') = 'vnpt'
              AND (
                COALESCE(o.status, '') = 'draft'
                OR COALESCE(o.invoice_no, '') IN ('', '0')
              )
        """).fetchall()
        for row in draft_rows:
            fkey = (row['fkey'] or row['invoice_id'] or '').strip()
            if not fkey:
                continue
            pdf_url = adapter.internal_draft_download_url(fkey, 'pdf')
            xml_url = adapter.internal_draft_download_url(fkey, 'xml')
            cursor.execute(
                "UPDATE outward_invoices SET pdf_url = ?, xml_file = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (pdf_url, xml_url, row['id']),
            )

        rows = cursor.execute("""
            SELECT o.id, o.sale_id, o.invoice_no
            FROM outward_invoices o
            JOIN sale s ON s.id = o.sale_id
            WHERE s.invoice_provider = 'vnpt'
              AND COALESCE(o.invoice_no, '') NOT IN ('', '0')
              AND COALESCE(o.status, '') = 'issued'
        """).fetchall()
        for row in rows:
            inv_no = _normalize_invoice_no(row['invoice_no'])
            if inv_no == '0':
                continue
            pdf_url = adapter.internal_download_url(pattern, serial, inv_no, 'pdf')
            xml_url = adapter.internal_download_url(pattern, serial, inv_no, 'xml')
            cursor.execute(
                "UPDATE outward_invoices SET pdf_url = ?, xml_file = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (pdf_url, xml_url, row['id']),
            )
            cursor.execute(
                "UPDATE sale SET invoice_pdf_url = ?, invoice_xml_file = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (pdf_url, xml_url, row['sale_id']),
            )

    def _persist_invoice_result(cursor, sale_id, sale, result, *, is_draft=False, provider='matbao'):
        invoice_id = result.get('invoice_id')
        invoice_no = _normalize_invoice_no(result.get('invoice_no'))
        if is_draft:
            invoice_no = '0'
        pdf_url = result.get('pdf_url') or ''
        xml_url = result.get('xml_url') or ''
        invoice_date = _resolve_invoice_date(sale, result)
        tax_auth_status = result.get('tax_authority_status') or ('Hóa đơn nháp' if is_draft else 'Chờ phản hồi')
        total_payment = result.get('total_amount') or sale.get('total_amount')
        invoice_status = 'draft' if is_draft else 'issued'

        cursor.execute("""
            UPDATE sale SET invoice_number = ?, invoice_id = ?, invoice_status = ?,
                invoice_pdf_url = ?, invoice_xml_file = ?, invoice_provider = ?,
                invoice_date = ?, tax_authority_status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (
            invoice_no, invoice_id, invoice_status,
            pdf_url, xml_url,
            provider, invoice_date, tax_auth_status, sale_id,
        ))

        _upsert_outward_invoice(
            cursor, sale_id, sale,
            invoice_no=invoice_no,
            invoice_id=invoice_id,
            pdf_url=pdf_url,
            xml_url=xml_url,
            invoice_date=invoice_date,
            status=invoice_status,
            total_payment=total_payment,
            fkey=invoice_id,
        )

    def issue_invoice_for_sale(sale_id, loai_hdon=None):
        """Xuất HĐĐT — loai_hdon: 0=nháp, 1=chính thức (None → 1, giống Mắt Bão)."""
        conn = None
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            sale_row = cursor.execute("SELECT * FROM sale WHERE id = ?", (sale_id,)).fetchone()
            if not sale_row:
                return {"success": False, "error": "Không tìm thấy đơn hàng."}

            sale = dict(sale_row)
            sale['id'] = sale_id

            config_row = cursor.execute(
                "SELECT * FROM invoice_settings WHERE is_active = 1"
            ).fetchone()
            if not config_row:
                return {"success": False, "error": "Chưa cấu hình invoice_settings."}
            config = dict(config_row)
            provider_key = (config.get('provider_name') or 'matbao').strip().lower()
            loai = _normalize_loai_hdon(loai_hdon, default=1)

            if _sale_has_official_invoice(sale):
                return {
                    "success": True,
                    "already_issued": True,
                    "invoice_no": sale.get('invoice_number'),
                }

            if _sale_is_draft_invoice(sale) and loai == 1:
                return {
                    'success': False,
                    'error': (
                        'Đơn đã có hóa đơn nháp. '
                        'Vào trang Hóa đơn đầu ra và bấm "Xuất HĐ chính thức".'
                    ),
                }

            if _sale_is_draft_invoice(sale) and loai == 0:
                replace_unpublished = True
            else:
                replace_unpublished = False

            items = _fetch_sale_invoice_items(cursor, sale_id)
            if not items:
                return {"success": False, "error": "Đơn hàng không có sản phẩm."}

            sale = _enrich_sale_buyer_identity(cursor, sale)

            service = create_einvoice_service(config, matbao_cls=MatbaoProvider)
            result = service.issue(sale, items, loai_hdon=loai, replace_unpublished=replace_unpublished)
            if not result.get('success'):
                return {"success": False, "error": result.get('error', 'Lỗi xuất hóa đơn')}

            is_draft = loai == 0 or result.get('is_draft')
            _persist_invoice_result(
                cursor, sale_id, sale, result, is_draft=is_draft, provider=provider_key,
            )
            conn.commit()

            customer_email = (sale.get('email') or '').strip()
            if customer_email and not is_draft:
                try:
                    send_invoice_email(
                        to_email=customer_email,
                        invoice_no=result.get('invoice_no'),
                        pdf_url=result.get('pdf_url'),
                        xml_url=result.get('xml_url'),
                        config=config,
                    )
                except Exception as email_err:
                    logging.error("Sale %s gửi email HĐ lỗi: %s", sale_id, email_err)

            return {
                "success": True,
                "is_draft": is_draft,
                "invoice_no": '0' if is_draft else result.get('invoice_no'),
                "invoice_id": result.get('invoice_id'),
                "pdf_url": result.get('pdf_url'),
                "xml_url": result.get('xml_url'),
            }
        except Exception as e:
            if conn:
                conn.rollback()
            logging.error("issue_invoice_for_sale sale_id=%s: %s", sale_id, e, exc_info=True)
            return {"success": False, "error": str(e)}
        finally:
            if conn:
                conn.close()

    def publish_draft_invoice_for_sale(sale_id):
        """Ký / phát hành hóa đơn nháp (Mắt Bão, VNPT, …)."""
        conn = None
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            sale_row = cursor.execute("SELECT * FROM sale WHERE id = ?", (sale_id,)).fetchone()
            if not sale_row:
                return {"success": False, "error": "Không tìm thấy đơn hàng."}
            sale = dict(sale_row)
            sale['id'] = sale_id

            if _sale_has_official_invoice(sale):
                return {
                    "success": True,
                    "already_issued": True,
                    "invoice_no": sale.get('invoice_number'),
                }
            if not _sale_is_draft_invoice(sale):
                return {"success": False, "error": "Đơn hàng không có hóa đơn nháp để phát hành."}

            invoice_id = sale.get('invoice_id')
            if not invoice_id:
                return {"success": False, "error": "Thiếu mã hóa đơn nháp (invoice_id)."}

            config_row = cursor.execute(
                "SELECT * FROM invoice_settings WHERE is_active = 1"
            ).fetchone()
            if not config_row:
                return {"success": False, "error": "Chưa cấu hình invoice_settings."}

            config = dict(config_row)
            provider_key = (config.get('provider_name') or 'matbao').strip().lower()
            service = create_einvoice_service(config, matbao_cls=MatbaoProvider)
            if provider_key == 'vnpt':
                items = _fetch_sale_invoice_items(cursor, sale_id)
                if not items:
                    return {"success": False, "error": "Đơn hàng không có sản phẩm."}
                sale = _enrich_sale_buyer_identity(cursor, sale)
                if hasattr(service, 'publish_draft'):
                    result = service.publish_draft(sale, items)
                else:
                    result = service.issue(
                        sale, items, loai_hdon=1, replace_unpublished=True,
                    )
            elif provider_key == 'matbao':
                result = service.sign_draft(invoice_id)
            elif hasattr(service, 'sign_draft'):
                try:
                    result = service.sign_draft(invoice_id, sale_data=sale)
                except TypeError:
                    result = service.sign_draft(invoice_id)
            else:
                return {
                    "success": False,
                    "error": f"Provider {provider_key} chưa hỗ trợ phát hành từ hóa đơn nháp.",
                }
            if not result.get('success'):
                return {"success": False, "error": result.get('error', 'Lỗi phát hành nháp')}

            invoice_no = _normalize_invoice_no(result.get('invoice_no'))
            if invoice_no == '0':
                raw = str(result.get('raw') or '')
                fkey_hint = str(result.get('invoice_id') or invoice_id or '').strip()
                if fkey_hint and raw:
                    import re
                    match = re.search(rf'{re.escape(fkey_hint)}_(\d+)', raw, flags=re.I)
                    if match:
                        invoice_no = match.group(1)
            if invoice_no == '0':
                return {"success": False, "error": "Nhà cung cấp chưa trả về số hóa đơn chính thức."}

            pdf_url = result.get('pdf_url') or ''
            xml_url = result.get('xml_url') or ''
            invoice_date = _resolve_invoice_date(sale, result)
            tax_auth_status = result.get('tax_authority_status') or 'Chưa Gửi CQT'
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            _upsert_outward_invoice(
                cursor, sale_id, sale,
                invoice_no=invoice_no,
                invoice_id=result.get('invoice_id') or invoice_id,
                pdf_url=pdf_url,
                xml_url=xml_url,
                invoice_date=invoice_date,
                status='issued',
                fkey=result.get('invoice_id') or invoice_id,
            )

            cursor.execute("""
                UPDATE sale SET invoice_number = ?, invoice_id = ?, invoice_status = 'issued',
                    invoice_pdf_url = ?, invoice_xml_file = ?, invoice_provider = ?,
                    invoice_date = ?, tax_authority_status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (
                invoice_no, result.get('invoice_id') or invoice_id,
                pdf_url, xml_url, provider_key,
                invoice_date, tax_auth_status, sale_id,
            ))

            if provider_key == 'vnpt':
                _repair_vnpt_outward_invoices(cursor, config)

            conn.commit()

            customer_email = (sale.get('email') or '').strip()
            if customer_email:
                try:
                    send_invoice_email(
                        to_email=customer_email,
                        invoice_no=invoice_no,
                        pdf_url=pdf_url,
                        xml_url=xml_url,
                        config=config,
                    )
                except Exception as email_err:
                    logging.error("Sale %s gửi email HĐ lỗi: %s", sale_id, email_err)

            return {
                "success": True,
                "is_draft": False,
                "invoice_no": invoice_no,
                "pdf_url": pdf_url,
                "xml_url": xml_url,
            }
        except Exception as e:
            if conn:
                conn.rollback()
            logging.error("publish_draft_invoice_for_sale sale_id=%s: %s", sale_id, e, exc_info=True)
            return {"success": False, "error": str(e)}
        finally:
            if conn:
                conn.close()

    app.config['issue_invoice_for_sale'] = issue_invoice_for_sale
    app.config['publish_draft_invoice_for_sale'] = publish_draft_invoice_for_sale

    @app.route('/api/invoice/providers', methods=['GET'])
    @login_required
    def api_invoice_providers():
        return jsonify({'success': True, 'providers': list_providers_api()})

    # --- ROUTE API XUẤT HÓA ĐƠN BÁN HÀNG ---#
    @app.route('/api/invoice/issue/<int:sale_id>', methods=['POST'])
    @login_required
    def api_issue_invoice(sale_id):
        body = request.get_json(silent=True) or {}
        loai_arg = body.get('loai_hdon')
        loai_arg = loai_arg if loai_arg is not None and str(loai_arg).strip() != '' else None
        result = issue_invoice_for_sale(sale_id, loai_hdon=loai_arg)
        if result.get('success'):
            if result.get('is_draft'):
                msg = "Đã tạo hóa đơn nháp. Khách có thể xem trước trên trang Hóa đơn đầu ra."
            elif result.get('already_issued'):
                msg = f"Hóa đơn {result.get('invoice_no')} đã được phát hành trước đó."
            else:
                msg = f"Hóa đơn {result.get('invoice_no')} đã được phát hành thành công."
            return jsonify({
                "success": True,
                "is_draft": result.get('is_draft', False),
                "invoice_no": result.get('invoice_no'),
                "invoice_id": result.get('invoice_id'),
                "pdf_url": result.get('pdf_url'),
                "xml_url": result.get('xml_url'),
                "message": msg,
            })
        status = 404 if 'Không tìm thấy' in str(result.get('error', '')) else 400
        return jsonify({"success": False, "error": result.get('error')}), status

    @app.route('/api/invoice/publish-draft/<int:sale_id>', methods=['POST'])
    @login_required
    def api_publish_draft_invoice(sale_id):
        result = publish_draft_invoice_for_sale(sale_id)
        if result.get('success'):
            return jsonify({
                "success": True,
                "invoice_no": result.get('invoice_no'),
                "pdf_url": result.get('pdf_url'),
                "xml_url": result.get('xml_url'),
                "message": f"Đã phát hành hóa đơn chính thức số {result.get('invoice_no')}.",
            })
        status = 404 if 'Không tìm thấy' in str(result.get('error', '')) else 400
        return jsonify({"success": False, "error": result.get('error')}), status

    #====API XUẤT HÓA ĐƠN THAY THẾ===#
    @app.route('/sale/edit-reissue')
    def edit_order_reissue_invoice():
        """Trang chỉnh sửa & xuất hóa đơn thay thế"""
        invoice_no = request.args.get('invoice_no') or request.args.get('replace')
        sale_id = request.args.get('sale_id')
    
        if not invoice_no:
            flash('Thiếu số hóa đơn gốc để thay thế!', 'danger')
            return redirect(url_for('outward_invoices'))  # hoặc trang danh sách hóa đơn của bạn
    
        return render_template(
            'edit_order_reissue_invoice.html',
            invoice_no=invoice_no,
            sale_id=sale_id,
            title="Thay thế Hóa đơn"
        )

    @app.route('/api/sale/by-invoice', methods=['GET'])
    @login_required
    def api_get_sale_by_invoice():
        invoice_no = request.args.get('invoice_no')
        if not invoice_no:
            return jsonify({"error": "Thiếu tham số invoice_no"}), 400

        invoice_no = str(invoice_no).strip()
    
        conn = None
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # 1. Tìm sale_id theo invoice_number trong bảng sale
            c.execute("""
                SELECT id 
                FROM sale 
                WHERE invoice_number = ? 
                ORDER BY id DESC LIMIT 1
            """, (invoice_no,))
            sale_row = c.fetchone()

            if not sale_row:
                # Thử tìm trong outward_invoices rồi lấy sale_id
                c.execute("""
                    SELECT sale_id 
                    FROM outward_invoices 
                    WHERE invoice_no = ? 
                    ORDER BY id DESC LIMIT 1
                """, (invoice_no,))
                outward_row = c.fetchone()
                if not outward_row or not outward_row['sale_id']:
                    return jsonify({"error": f"Không tìm thấy đơn hàng nào có hóa đơn số {invoice_no}"}), 404
                sale_id = outward_row['sale_id']
            else:
                sale_id = sale_row['id']

            # 2. Lấy đầy đủ thông tin đơn hàng (tái sử dụng logic từ api_get_sale)
            c.execute("""
                SELECT id, date, status,
                       COALESCE(customer_name, ?) AS customer_name,
                       COALESCE(company_name, '') AS company_name,
                       COALESCE(tax_code, '') AS tax_code,
                       COALESCE(address, '') AS address,
                       COALESCE(customer_phone, '') AS customer_phone,
                       COALESCE(email, '') AS email,
                       COALESCE(total_amount, 0) AS total_amount,
                       COALESCE(discount_amount, 0) AS discount_amount,
                       invoice_number,
                       invoice_date
                FROM sale WHERE id = ?
            """, (DEFAULT_RETAIL_BUYER_NAME, sale_id))
        
            sale_data = dict(c.fetchone())

            # 3. Lấy chi tiết sản phẩm
            c.execute("""
                SELECT 
                    si.product_id,
                    COALESCE(si.product_name, p.name) AS name,
                    si.quantity,
                    si.price AS sold_price,
                    COALESCE(si.discount_pct, 0) AS discount_pct,
                    COALESCE(si.tax_pct, 10) AS tax_pct,
                    COALESCE(si.UseSaleUnit, 0) AS UseSaleUnit,
                    p.unit,
                    p.unit1,
                    p.base_price
                FROM sale_items si
                LEFT JOIN products p ON si.product_id = p.id
                WHERE si.sale_id = ?
                ORDER BY si.sale_id ASC
            """, (sale_id,))
        
            items = []
            for row in c.fetchall():
                item = dict(row)
                # Xử lý đơn vị và giá hiển thị
                if item['UseSaleUnit'] == 1 and item.get('unit1'):
                    item['unit'] = item['unit1']
                else:
                    item['unit'] = item['unit'] or 'Cái'
            
                items.append(item)

            sale_data['items'] = items

            return jsonify(sale_data), 200

        except Exception as e:
            print("LỖI API /api/sale/by-invoice:", e)
            return jsonify({"error": "Lỗi server khi tìm đơn hàng theo hóa đơn"}), 500
        finally:
            if conn:
                conn.close()

    #==== Sửa Đơn Hàng Đã Xuất Hóa Đơn để Xuất Hóa Đơn Thay Thế ====#
    from datetime import datetime
    from Services.hkd_sector import requires_stock_check
    from Services.inventory_stock_helpers import revert_sale_stock
    from Services.sale_helpers import deduct_inventory_for_sale, fetch_product_for_checkout

    @app.route('/api/sale/update_for_replacement/<int:sale_id>', methods=['PUT'])
    @login_required
    def api_update_for_replacement(sale_id):
        """
        Cập nhật đơn hàng để xuất hóa đơn thay thế
        """
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "Dữ liệu không hợp lệ."}), 400

        items = data.get('items', [])
        if not items:
            return jsonify({"success": False, "error": "Giỏ hàng trống."}), 400

        # ==================== LẤY DỮ LIỆU ====================
        customer_name = normalize_retail_buyer_name(data.get('customer_name'))
        company_name = data.get('company_name', '')
        email = data.get('email', '')
        address = data.get('address', '')
        tax_code = data.get('tax_code', '')
        customer_phone = data.get('customer_phone', '')
        note = data.get('note', '')

        # Chuẩn hóa phương thức thanh toán
        payment_method = data.get('payment_method') or '111'
        if payment_method in ["công nợ", "cong_no", "debt", "131"]:
            payment_method = "131"
        elif payment_method not in ["111", "112", "131"]:
            payment_method = "111"

        discount_pct = float(data.get('discount_pct') or 0)
        tax_pct_total = float(data.get('tax_pct') or 0) # Thuế tổng đơn hàng (nếu có)

        # Tính toán tổng tiền mới từ items
        subtotal = 0.0
        for item in items:
            qty = float(item.get('quantity', 0))
            price = float(item.get('price', 0))
            # Chiết khấu dòng
            line_disc = round(qty * price * (float(item.get('discount_pct', 0)) / 100))
            subtotal += (qty * price - line_disc)
    
        # Tính tổng sau chiết khấu đơn hàng và thuế
        total_after_order_disc = subtotal * (1 - discount_pct / 100)
        total_amount = round(total_after_order_disc * (1 + tax_pct_total / 100))

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            cursor.execute("BEGIN IMMEDIATE")
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # 1. Kiểm tra đơn hàng cũ
            cursor.execute("SELECT * FROM sale WHERE id = ?", (sale_id,))
            row = cursor.fetchone()
            if not row:
                raise Exception("Không tìm thấy đơn hàng.")

            sale_old = dict(row)
            ref_doc = sale_old.get('sale_no') or f"ĐH{str(sale_id).zfill(6)}"

            cursor.execute("SELECT * FROM sale_items WHERE sale_id = ?", (sale_id,))
            old_items = [dict(r) for r in cursor.fetchall()]
            sale_old['items_detail'] = old_items

            # Ghi log thay đổi
            cursor.execute("""
                INSERT INTO sale_audit_log (sale_id, sale_no, action_type, old_data, changed_by, reason)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (sale_id, ref_doc, 'REPLACEMENT', json.dumps(sale_old, ensure_ascii=False),
                  current_user.username, "Sửa đơn để xuất hóa đơn thay thế"))

            # 2. Hoàn sổ cái đơn cũ (không cộng tay inventory)
            affected_pids = list({r['product_id'] for r in old_items})
            cursor.execute("DELETE FROM phieu_xuat_kho WHERE sale_id = ?", (sale_id,))
            cursor.execute("DELETE FROM phieu_thu WHERE sale_id = ?", (sale_id,))
            cursor.execute("DELETE FROM cong_no WHERE sale_id = ?", (sale_id,))
            revert_sale_stock(cursor, sale_id, affected_pids)

            # 3. Xóa chi tiết cũ
            cursor.execute("DELETE FROM sale_items WHERE sale_id = ?", (sale_id,))

            # 4. Gom nhóm và Xử lý items mới
            merged = defaultdict(lambda: {"deduct": 0.0, "details": [], "product_type": "goods"})
            for item in items:
                pid = int(item['product_id'])
                qty_input = float(item['quantity'])
                price = float(item['price'])
                use_unit1 = bool(item.get('UseSaleUnit', False))

                p_row = fetch_product_for_checkout(cursor, pid)
                if not p_row:
                    raise Exception(f"Sản phẩm ID {pid} không tồn tại.")
                p = dict(p_row)
                product_type = p.get('product_type') or 'goods'
                ratio = float(p['unit_ratio'] or 1)
                deduct_qty = qty_input * ratio if use_unit1 else qty_input

                if requires_stock_check(product_type):
                    if float(p['stock']) < deduct_qty:
                        raise Exception(f"Không đủ hàng cho sản phẩm {p['name']}")
                    merged[pid]["deduct"] += deduct_qty
                merged[pid]["product_type"] = product_type
                merged[pid]["details"].append({
                    "qty_input": qty_input,
                    "price": price,
                    "use_unit1": use_unit1,
                    "ratio": ratio,
                    "avg_cost": float(p['avg_cost']),
                    "discount_pct": float(item.get('discount_pct', 0)),
                    "tax_pct": float(item.get('tax_pct', 0)),
                    "name": p['name'],
                    "unit": p['unit'],
                    "unit1": p['unit1'],
                })

            # 5. Cập nhật thông tin đơn hàng chính
            cursor.execute("""
                UPDATE sale SET
                    date = ?, total_amount = ?, payment_method = ?,
                    customer_name = ?, company_name = ?, tax_code = ?, 
                    customer_phone = ?, email = ?, address = ?, 
                    note = ?, status = 'completed', discount_pct = ?, tax_pct = ?
                WHERE id = ?
            """, (now_str, total_amount, payment_method,
                  customer_name, company_name, tax_code, customer_phone, 
                  email, address, note, discount_pct, tax_pct_total, sale_id))

            # 6. Lưu Chi tiết mới + Trừ kho + Stock move
            px_items = []
            for pid, info in merged.items():
                if not requires_stock_check(info.get("product_type")):
                    for d in info["details"]:
                        cursor.execute("""
                            INSERT INTO sale_items 
                            (sale_id, product_id, quantity, price, cost_price, UseSaleUnit, unit_ratio, discount_pct, tax_pct)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (sale_id, pid, d["qty_input"], d["price"], d["avg_cost"],
                              1 if d["use_unit1"] else 0, d["ratio"], d["discount_pct"], d["tax_pct"]))
                    continue
                total_deduct = round(info["deduct"], 6)
                if total_deduct <= 0:
                    continue
                cost_used = deduct_inventory_for_sale(
                    cursor, pid, total_deduct, info["details"][0]["avg_cost"],
                    sale_id, now_str, ref_doc,
                )

                for d in info["details"]:
                    cursor.execute("""
                        INSERT INTO sale_items 
                        (sale_id, product_id, quantity, price, cost_price, UseSaleUnit, unit_ratio, discount_pct, tax_pct)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (sale_id, pid, d["qty_input"], d["price"], cost_used,
                          1 if d["use_unit1"] else 0, d["ratio"], d["discount_pct"], d["tax_pct"]))

                    px_items.append({
                        "product_id": pid,
                        "product_name": d['name'],
                        "unit": d['unit1'] if d['use_unit1'] else d['unit'],
                        "quantity": d['qty_input'],
                        "price": d['price'],
                        "amount": round(d['qty_input'] * d['price'])
                    })

            # 7. Tạo phiếu xuất kho mới
            last_px = cursor.execute("SELECT voucher_no FROM phieu_xuat_kho WHERE voucher_no LIKE 'PX%' ORDER BY id DESC LIMIT 1").fetchone()
            px_num = (int(last_px['voucher_no'][2:]) + 1) if last_px else 1
            px_vno = f"PX{px_num:06d}"

            cursor.execute("""
                INSERT INTO phieu_xuat_kho (voucher_no, date, customer_name, items_json, total_amount, sale_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (px_vno, now_str, customer_name, json.dumps(px_items, ensure_ascii=False), total_amount, sale_id))

            # 8. Tạo chứng từ tài chính (Sử dụng payment_method đã chuẩn hóa)
            if payment_method in ["111", "112"]:
                last_pt = cursor.execute("SELECT voucher_no FROM phieu_thu WHERE voucher_no LIKE 'PT%' ORDER BY id DESC LIMIT 1").fetchone()
                pt_num = (int(last_pt['voucher_no'][2:]) + 1) if last_pt else 1
                pt_vno = f"PT{pt_num:06d}"

                cursor.execute("""
                    INSERT INTO phieu_thu (voucher_no, payer_name, address, tax_code, amount, debit_account, credit_account, 
                                           reason, reference_document, sale_id, date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (pt_vno, customer_name, address, tax_code, total_amount, payment_method, '511',
                      f"Thu tiền bán hàng thay thế - {ref_doc}", ref_doc, sale_id, now_str))

            elif payment_method == "131":
                cursor.execute("""
                    INSERT INTO cong_no (customer_name, company_name, address, tax_code, debit_account, 
                                       credit_account, date_of_debt, unpaid_amount, sale_id, sale_no)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (customer_name, company_name, address, tax_code, '131', '511', 
                      now_str, total_amount, sale_id, ref_doc))

            conn.commit()
            return jsonify({"success": True, "message": "Cập nhật đơn hàng thay thế thành công."}), 200

        except Exception as e:
            conn.rollback()
            traceback.print_exc()
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            conn.close()

    #====API XUẤT HÓA ĐƠN THAY THẾ===#
    @app.route('/api/invoice/issue-replacement-final/<path:invoice_number>', methods=['POST'])
    @login_required
    def api_issue_replacement_final(invoice_number):
        conn = None
        try:
            invoice_number = str(invoice_number).strip()
            if not invoice_number or invoice_number in ('', '---', 'None', 'null'):
                return jsonify({"success": False, "error": "Số hóa đơn gốc không hợp lệ"}), 400

            # Lấy dữ liệu từ Frontend gửi lên
            form_data = request.get_json(silent=True) or {}
            sale_id = form_data.get('sale_id')

            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 1. Tìm sale_id nếu frontend không truyền
            if not sale_id:
                row = cursor.execute("SELECT sale_id FROM outward_invoices WHERE invoice_no = ? ORDER BY id DESC LIMIT 1", (invoice_number,)).fetchone()
                sale_id = row['sale_id'] if row else None

            if not sale_id:
                return jsonify({"success": False, "error": "Không tìm thấy đơn hàng"}), 404

            # 2. Lấy thông tin sale và cấu hình
            sale_row = cursor.execute("SELECT * FROM sale WHERE id = ?", (sale_id,)).fetchone()
            config_row = cursor.execute("SELECT * FROM invoice_settings WHERE is_active = 1 LIMIT 1").fetchone()
            old_inv_row = cursor.execute("SELECT * FROM outward_invoices WHERE invoice_no = ? ORDER BY id DESC LIMIT 1", (invoice_number,)).fetchone()

            if not sale_row or not config_row or not old_inv_row:
                return jsonify({"success": False, "error": "Thiếu dữ liệu cấu hình hoặc hóa đơn gốc"}), 404

            sale = dict(sale_row)
            config = dict(config_row)
            old_inv = dict(old_inv_row)
            provider_key = (config.get('provider_name') or 'matbao').strip().lower()

            # 3. Lấy chi tiết hàng hóa
            items_rows = cursor.execute("""
                SELECT COALESCE(si.product_name, p.name) AS name, COALESCE(p.product_code, 'DV') AS product_code,
                       si.quantity, si.price, COALESCE(si.discount_pct, 0) as discount_pct, 
                       COALESCE(si.tax_pct, 0) as tax_pct, COALESCE(si.unit, 'Cái') as unit
                FROM sale_items si LEFT JOIN products p ON si.product_id = p.id
                WHERE si.sale_id = ? ORDER BY si.rowid ASC
            """, (sale_id,)).fetchall()
            items = [dict(r) for r in items_rows]

            if provider_key == 'vnpt':
                sale = _enrich_sale_buyer_identity(cursor, sale)
                service = create_einvoice_service(config, matbao_cls=MatbaoProvider)
                sale_data = {
                    "id": sale_id,
                    "sale_no": sale.get('sale_no'),
                    "total_amount": sale.get('total_amount'),
                    "customer_name": form_data.get('customer_name') or sale.get('customer_name'),
                    "company_name": form_data.get('company_name') or sale.get('company_name'),
                    "tax_code": form_data.get('tax_code') or sale.get('tax_code'),
                    "address": form_data.get('address') or sale.get('address'),
                    "email": form_data.get('email') or sale.get('email'),
                    "phone": form_data.get('phone') or sale.get('customer_phone'),
                    "customer_phone": form_data.get('phone') or sale.get('customer_phone'),
                    "budget_unit_code": sale.get('budget_unit_code'),
                    "passport_no": sale.get('passport_no'),
                }
                old_fkey = (
                    old_inv.get('invoice_id')
                    or old_inv.get('fkey')
                    or sale.get('invoice_id')
                    or sale.get('fkey')
                )
                replacement_info = {
                    "old_fkey": old_fkey,
                    "MSHDonDCLQuan": old_fkey,
                    "KHMSHDCLQuan": old_inv.get('pattern') or config.get('invoice_type'),
                    "KHHDCLQuan": old_inv.get('serial') or config.get('invoice_series'),
                    "SHDCLQuan": int(old_inv.get('invoice_no') or invoice_number or 0),
                    "NLHDCLQuan": str(
                        old_inv.get('invoice_date') or sale.get('invoice_date') or ''
                    ).split(' ')[0],
                }
                result = service.issue_replacement(sale_data, items, replacement_info)
            else:
                # Mắt Bão — giữ nguyên luồng gốc, không đi qua adapter wrapper
                service = MatbaoProvider(config)
                sale_data = {
                    "sale_no": sale.get('sale_no'),
                    "customer_name": form_data.get('customer_name') or sale.get('customer_name'),
                    "company_name": form_data.get('company_name') or sale.get('company_name'),
                    "tax_code": form_data.get('tax_code') or sale.get('tax_code'),
                    "address": form_data.get('address') or sale.get('address'),
                    "email": form_data.get('email') or sale.get('email'),
                }
                replacement_info = {
                    "MSHDonDCLQuan": old_inv.get("invoice_id"),
                    "KHMSHDCLQuan": config.get('invoice_type'),
                    "KHHDCLQuan": config.get('invoice_series'),
                    "SHDCLQuan": int(old_inv.get('invoice_no') or 0),
                    "NLHDCLQuan": str(old_inv.get('invoice_date')).split(' ')[0],
                }
                result = service.issue_replacement(sale_data, items, replacement_info)
            if not result.get('success'):
                return jsonify({"success": False, "error": result.get('error')}), 400

            # 5. Cập nhật Database
            res_data = {
                "no": result.get('invoice_no'),
                "id": result.get('invoice_id'),
                "date": _resolve_invoice_date(sale, result),
                "pdf": result.get('pdf_url'),
                "xml": result.get('xml_url'),
                "status": result.get('tax_authority_status') or 'Chờ phản hồi'
            }
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # Cập nhật bảng SALE
            cursor.execute("""
                UPDATE sale SET invoice_number=?, invoice_id=?, invoice_status='issued', 
                invoice_pdf_url=?, invoice_xml_file=?, invoice_date=?, tax_authority_status=?, 
                replacement_invoice_no=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
            """, (res_data["no"], res_data["id"], res_data["pdf"], res_data["xml"], res_data["date"], res_data["status"], invoice_number, sale_id))

            if provider_key == 'vnpt':
                cursor.execute("""
                    UPDATE outward_invoices
                    SET note = COALESCE(note, '') || ' | Đã bị thay thế bởi HĐ ' || ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE invoice_no = ? AND sale_id = ?
                """, (str(res_data["no"]), invoice_number, sale_id))

            # Cập nhật bảng OUTWARD_INVOICES (Đã fix thứ tự 12 cột)
            cursor.execute("""
                INSERT INTO outward_invoices (
                    sale_id, sale_no, invoice_no, invoice_id, customer_name, customer_tax_code, customer_address,
                    total, pdf_url, xml_file, invoice_date, created_at, updated_at, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sale_id, sale.get('sale_no'), res_data["no"], res_data["id"],
                sale_data["company_name"] or sale_data["customer_name"], sale_data.get("tax_code"), sale_data.get("address"),
                sale.get('total_amount'), res_data["pdf"], res_data["xml"],
                res_data["date"], current_time, current_time,
                (
                    f"Thay thế cho HĐ {invoice_number} (Fkey gốc: {replacement_info.get('old_fkey')})"
                    if provider_key == 'vnpt'
                    else f"Thay thế cho {invoice_number}"
                ),
            ))

            conn.commit()
            return jsonify({"success": True, "invoice_no": res_data["no"], "pdf_url": res_data["pdf"]})

        except Exception as e:
            if conn: conn.rollback()
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn: conn.close()

    @app.route('/api/settings/test_invoice_connection', methods=['POST'])
    @login_required
    def test_invoice_connection():
        try:
            from Services.einvoice_factory import test_einvoice_connection
            data = request.get_json() or {}
            result = test_einvoice_connection(data, matbao_cls=MatbaoProvider)
            if result.get('success'):
                return jsonify({
                    'success': True,
                    'message': result.get('message') or 'Kết nối thành công',
                })
            return jsonify({
                'success': False,
                'error': result.get('error') or 'Kiểm tra kết nối thất bại',
            })
        except Exception as e:
            logging.exception('test_invoice_connection: %s', e)
            return jsonify({'success': False, 'error': str(e)}), 200

    #===HÀM VÀ CÁC ROUTE XUẤT HÓA ĐƠN ĐIỆN TỬ THEO GIỜ===#

    def get_active_invoice_config():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        config_row = conn.execute("SELECT * FROM invoice_settings WHERE is_active = 1 AND auto_issue_invoice = 1").fetchone()
        conn.close()
        if config_row:
            config = dict(config_row)
            # Đồng bộ cấu hình như trong API đơn lẻ
            # Ưu tiên lấy từ config DB, nếu rỗng thì dùng default
            if not config.get('invoice_series'): config['invoice_series'] = 'C26MES'
            if not config.get('invoice_type'): config['invoice_type'] = '2'
            return config
        return None

    def batch_issue_pending_invoices():
        stats = {"success": 0, "failed": 0, "remaining": 0}
    
        config = get_active_invoice_config()
        if not config:
            logging.warning("Không tìm thấy cấu hình hóa đơn hoạt động")
            return stats

        conn = None
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 1. Lấy danh sách cần xử lý (batch size = 20)
            # Chỉ lấy những đơn 'completed' và chưa có số hóa đơn
            pending_rows = cursor.execute("""
                SELECT id FROM sale
                WHERE status = 'completed'
                  AND (invoice_number IS NULL OR invoice_number = '')
                ORDER BY id ASC
                LIMIT 20
            """).fetchall()

            if not pending_rows:
                logging.info("Không còn hóa đơn pending nào để xử lý.")
                return stats

            service = create_einvoice_service(config, matbao_cls=MatbaoProvider)
            provider_key = (config.get('provider_name') or 'matbao').strip().lower()

            # 2. Vòng lặp xử lý từng đơn hàng
            for row in pending_rows:
                sale_id = row['id']
                try:
                    # Lấy chi tiết đơn hàng (Sale)
                    sale_row = cursor.execute("SELECT * FROM sale WHERE id = ?", (sale_id,)).fetchone()
                    if not sale_row:
                        continue
                
                    sale = dict(sale_row)

                    # Lấy danh sách sản phẩm (Items) - Đồng bộ logic lấy thuế và chiết khấu
                    items_rows = cursor.execute("""
                        SELECT p.name,
                               p.product_code,
                               si.quantity,
                               si.price,
                               si.discount_pct, 
                               si.tax_pct,
                               CASE WHEN si.UseSaleUnit = 1 THEN p.unit1 ELSE p.unit END as unit
                        FROM sale_items si
                        JOIN products p ON si.product_id = p.id
                        WHERE si.sale_id = ?
                    """, (sale_id,)).fetchall()

                    items = [dict(r) for r in items_rows]

                    if not items:
                        logging.warning(f"⚠️ Sale ID {sale_id}: Đơn hàng không có sản phẩm. Bỏ qua.")
                        stats["failed"] += 1
                        continue

                    # 3. Gọi Matbao Provider xuất hóa đơn
                    result = service.issue(sale, items)

                    if result.get('success'):
                        if result.get('is_draft'):
                            stats["failed"] += 1
                            logging.warning("Batch sale %s trả về nháp — bỏ qua.", sale_id)
                            continue

                        _persist_invoice_result(
                            cursor, sale_id, sale, result,
                            is_draft=False, provider=provider_key,
                        )
                        conn.commit()
                        stats["success"] += 1

                        customer_email = (sale.get('email') or '').strip()
                        if customer_email:
                            try:
                                send_invoice_email(
                                    to_email=customer_email,
                                    invoice_no=result.get('invoice_no'),
                                    pdf_url=result.get('pdf_url'),
                                    xml_url=result.get('xml_url'),
                                    config=config,
                                )
                            except Exception as email_err:
                                logging.error("Batch sale %s gửi email lỗi: %s", sale_id, email_err)

                    else:
                        stats["failed"] += 1
                        error_msg = result.get('error', 'Lỗi không xác định từ provider')
                        logging.error(f"❌ Sale ID {sale_id}: Provider báo lỗi: {error_msg}")

                except Exception as e:
                    if conn:
                        conn.rollback()
                    stats["failed"] += 1
                    logging.error(f"❌ Lỗi xử lý đơn hàng {sale_id} trong batch: {str(e)}")

            # 7. Cập nhật số lượng còn lại cuối cùng
            row_rem = cursor.execute("""
                SELECT COUNT(*) as cnt FROM sale
                WHERE status = 'completed'
                  AND (invoice_number IS NULL OR invoice_number = '')
            """).fetchone()
            stats["remaining"] = row_rem['cnt'] if row_rem else 0

            logging.info(f"✅ Hoàn tất Batch: Thành công {stats['success']}, Thất bại {stats['failed']}, Còn lại {stats['remaining']}")
            return stats

        except Exception as e:
            logging.error(f"🔥 Lỗi nghiêm trọng trong batch_issue_pending_invoices: {str(e)}", exc_info=True)
            return stats

        finally:
            if conn:
                conn.close()

    @app.route('/api/admin/run-batch-invoice', methods=['POST'])
    @login_required
    def run_batch_invoice_manual():
        try:
            # Gọi hàm batch và nhận kết quả thống kê
            result_stats = batch_issue_pending_invoices()
        
            msg = f"Đã xử lý xong: {result_stats['success']} thành công"
            if result_stats['failed'] > 0:
                msg += f", {result_stats['failed']} thất bại"
        
            if result_stats['remaining'] > 0:
                msg += f". Hiện còn {result_stats['remaining']} đơn hàng cũ đang chờ, hãy bấm tiếp để xử lý."
            else:
                msg += ". Tuyệt vời! Đã giải quyết hết toàn bộ đơn hàng tồn đọng."

            return jsonify({
                "success": True,
                "message": msg,
                "stats": result_stats
            }), 200

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    def get_active_invoice_config():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        config = conn.execute("SELECT * FROM invoice_settings WHERE is_active = 1").fetchone()
        conn.close()
        return dict(config) if config else None

    def _load_all_invoice_configs():
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM invoice_settings ORDER BY is_active DESC, provider_name"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _provider_display_name(code):
        from Services.einvoice_registry import get_provider_meta
        key = (code or '').strip().lower()
        if not key:
            return 'Khác'
        meta = get_provider_meta(key)
        if meta:
            return meta.get('label') or key.upper()
        fallback = {'matbao': 'Mắt Bão', 'vnpt': 'VNPT Invoice'}
        return fallback.get(key, key.upper())

    def _config_has_portal_credentials(config):
        if not config:
            return False
        pk = (config.get('provider_name') or '').strip().lower()
        if pk not in ('vnpt', 'matbao'):
            return False
        return bool(
            (config.get('username') or '').strip()
            or (config.get('password') or '').strip()
            or (config.get('api_key') or '').strip()
        )

    def _parse_flask_json_response(resp):
        if isinstance(resp, tuple):
            body, status = resp
            data = body.get_json(silent=True) or {}
            data['_status'] = status
            return data
        return resp.get_json(silent=True) or {}

    def _outward_invoice_dedupe_key(item):
        inv_id = str(item.get('invoice_id') or '').strip()
        if inv_id:
            return f"id:{inv_id}"
        sale_no = str(item.get('sale_no') or '').strip().upper()
        inv_no = _normalize_invoice_no(item.get('invoice_no'))
        return f"{sale_no}|{inv_no}"

    def _merge_outward_formatted_lists(base, extra):
        existing = {_outward_invoice_dedupe_key(x) for x in base}
        for item in extra or []:
            key = _outward_invoice_dedupe_key(item)
            if key in existing:
                continue
            base.append(item)
            existing.add(key)
        return base

    def _dedupe_outward_formatted(formatted):
        out = []
        seen = set()
        for item in formatted:
            key = _outward_invoice_dedupe_key(item)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _attach_provider_labels(items):
        for item in items or []:
            pk = (item.get('provider') or '').strip().lower()
            if pk and not item.get('provider_label'):
                item['provider_label'] = _provider_display_name(pk)
        return items

    @app.route('/outward-invoice')
    @login_required
    def outward_invoice():
        configs = _load_all_invoice_configs()
        active = get_active_invoice_config() or {}
        active_key = (active.get('provider_name') or 'matbao').strip().lower()
        seen_codes = set()
        provider_options = [{'code': 'all', 'label': 'Tất cả NCC HĐĐT'}]
        for cfg in configs:
            pk = (cfg.get('provider_name') or '').strip().lower()
            if not pk or pk in seen_codes:
                continue
            seen_codes.add(pk)
            provider_options.append({'code': pk, 'label': _provider_display_name(pk)})
        return render_template(
            "KeToanHKD/outward_invoice.html",
            invoice_provider=active_key,
            active_provider=active_key,
            provider_options=provider_options,
            provider_label=_provider_display_name(active_key),
            max_range_days=31,
        )

    def _validate_outward_date_range(from_date, to_date, max_days):
        d_from = datetime.strptime(from_date, '%Y-%m-%d')
        d_to = datetime.strptime(to_date, '%Y-%m-%d')
        delta = (d_to - d_from).days
        if delta < 0:
            return None, 'Ngày bắt đầu không thể sau ngày kết thúc'
        if delta > max_days:
            return None, f'Khoảng cách {delta} ngày vượt quá giới hạn {max_days} ngày'
        return (d_from, d_to), None

    def _enrich_vnpt_outward_items(items, config):
        from Services.einvoice_adapters import VNPTInvoiceAdapter
        adapter = VNPTInvoiceAdapter(config)
        pattern = (config.get('invoice_type') or '1/001').strip()
        serial = (config.get('invoice_series') or 'C26TAA').strip()
        for item in items:
            item.setdefault('provider', 'vnpt')
            inv_no = _normalize_invoice_no(item.get('invoice_no'))
            if inv_no == '0':
                continue
            pat = item.get('pattern') or pattern
            ser = item.get('serial') or serial
            item['pdf_url'] = adapter.internal_download_url(pat, ser, inv_no, 'pdf')
            item['xml_url'] = adapter.internal_download_url(pat, ser, inv_no, 'xml')
        return items

    def _sync_vnpt_outward_invoices(from_date, to_date, config, merge_local=True):
        from Services.einvoice_adapters import VNPTInvoiceAdapter

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        updated_count = 0
        try:
            adapter = VNPTInvoiceAdapter(config)
            _repair_vnpt_outward_invoices(cursor, config)
            sync = adapter.list_invoices(from_date, to_date)
            formatted = list(sync.get('data') or [])
            portal_warning = sync.get('warning')
            portal_details = sync.get('details') or []

            for inv in formatted:
                fkey = str(inv.get('invoice_id') or inv.get('fkey') or '').strip()
                inv_no = _normalize_invoice_no(inv.get('invoice_no'))
                sale_no = str(inv.get('sale_no') or '').strip()
                sale_id_val = inv.get('sale_id')

                if sale_no and not sale_id_val:
                    row = cursor.execute(
                        "SELECT id FROM sale WHERE UPPER(TRIM(sale_no)) = ?",
                        (sale_no.upper(),),
                    ).fetchone()
                    if row:
                        sale_id_val = row['id']
                        inv['sale_id'] = sale_id_val
                elif fkey.startswith('SME') and fkey[3:].isdigit() and not sale_id_val:
                    sale_id_val = int(fkey[3:])
                    inv['sale_id'] = sale_id_val
                    row = cursor.execute(
                        "SELECT sale_no FROM sale WHERE id = ?",
                        (sale_id_val,),
                    ).fetchone()
                    if row and row['sale_no']:
                        inv['sale_no'] = row['sale_no']

                if sale_id_val:
                    is_draft = inv.get('is_draft') or inv_no == '0'
                    if is_draft:
                        cursor.execute("""
                            UPDATE sale SET invoice_number = '0', invoice_id = ?, invoice_date = ?,
                                tax_authority_status = ?, invoice_status = 'draft',
                                invoice_provider = 'vnpt', invoice_pdf_url = ?, invoice_xml_file = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (
                            fkey or inv.get('invoice_id'),
                            inv.get('invoice_date') or from_date,
                            inv.get('tax_authority_status') or 'Hóa đơn nháp',
                            inv.get('pdf_url') or '',
                            inv.get('xml_url') or '',
                            sale_id_val,
                        ))
                    elif inv_no != '0':
                        cursor.execute("""
                            UPDATE sale SET invoice_number = ?, invoice_id = ?, invoice_date = ?,
                                tax_authority_status = ?, invoice_status = 'issued',
                                invoice_provider = 'vnpt', invoice_pdf_url = ?,
                                invoice_xml_file = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (
                            inv_no,
                            fkey or inv.get('invoice_id'),
                            inv.get('invoice_date') or from_date,
                            inv.get('tax_authority_status') or 'Đã phát hành',
                            inv.get('pdf_url') or '',
                            inv.get('xml_url') or '',
                            sale_id_val,
                        ))
                    if cursor.rowcount > 0:
                        updated_count += 1

                inv_date = inv.get('invoice_date') or from_date
                ow_invoice_no = '0' if (inv.get('is_draft') or inv_no == '0') else inv_no
                ow_status = 'draft' if (inv.get('is_draft') or inv_no == '0') else 'issued'
                existing_ow = None
                if sale_id_val:
                    existing_ow = _find_outward_invoice_for_sale(cursor, sale_id_val)
                if not existing_ow and fkey:
                    existing_ow = cursor.execute(
                        """
                        SELECT id FROM outward_invoices
                        WHERE invoice_id = ?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (fkey,),
                    ).fetchone()
                ow_values = (
                    inv_date, sale_no, sale_id_val, ow_invoice_no, fkey,
                    inv.get('serial') or config.get('invoice_series'),
                    inv.get('customer') or inv.get('company') or DEFAULT_RETAIL_BUYER_NAME,
                    inv.get('amount') or 0, inv.get('amount') or 0,
                    ow_status,
                    fkey, inv.get('pdf_url') or '', inv.get('xml_url') or '',
                )
                if existing_ow:
                    cursor.execute("""
                        UPDATE outward_invoices SET
                            invoice_date = ?, sale_no = ?, sale_id = ?, invoice_no = ?,
                            invoice_id = ?, serial = ?, customer_name = ?, amount = ?, total = ?,
                            status = ?, fkey = ?, pdf_url = ?, xml_file = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (*ow_values, existing_ow['id']))
                else:
                    cursor.execute("""
                        INSERT INTO outward_invoices (
                            invoice_date, sale_no, sale_id, invoice_no, invoice_id, serial,
                            customer_name, amount, total, status, fkey, pdf_url, xml_file,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """, ow_values)

            if merge_local:
                formatted = _merge_local_outward_invoices(cursor, formatted, from_date, to_date, 'vnpt')
            formatted = _enrich_vnpt_outward_items(formatted, config)
            formatted = _attach_provider_labels(formatted)
            formatted.sort(key=lambda x: (x.get('invoice_date', ''), x.get('invoice_no', '')), reverse=True)
            conn.commit()

            if portal_warning and not sync.get('source'):
                message = portal_warning
            elif sync.get('message'):
                message = sync['message']
            else:
                scope = 'VNPT + cục bộ' if merge_local else 'VNPT'
                message = f'Đã tải {len(formatted)} hóa đơn ({scope}).'
            if portal_details:
                message += f" ({portal_details[0][:120]})"

            return jsonify({
                'success': True,
                'data': formatted,
                'synced_count': updated_count,
                'auto_returned': 0,
                'provider': 'vnpt',
                'message': message,
            })
        except Exception as e:
            conn.rollback()
            logging.error('Error in _sync_vnpt_outward_invoices: %s', e, exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            conn.close()

    @app.route('/api/outward-invoices', methods=['GET'])
    def api_outward_invoices():
        from_date = request.args.get('from')
        to_date = request.args.get('to')
        provider_param = (request.args.get('provider') or 'all').strip().lower()
        if not from_date or not to_date:
            return jsonify({'success': False, 'error': 'Vui lòng chọn khoảng thời gian'}), 400

        try:
            _, err = _validate_outward_date_range(from_date, to_date, 31)
            if err:
                return jsonify({'success': False, 'error': err}), 400
        except ValueError:
            return jsonify({'success': False, 'error': 'Định dạng ngày không hợp lệ'}), 400

        configs = _load_all_invoice_configs()
        if provider_param not in ('all', ''):
            configs = [
                c for c in configs
                if (c.get('provider_name') or '').strip().lower() == provider_param
            ]

        formatted = []
        synced_total = 0
        sync_messages = []
        sync_errors = []
        portal_sync_providers = {'vnpt', 'matbao'}

        for config in configs:
            pk = (config.get('provider_name') or '').strip().lower()
            if pk not in portal_sync_providers:
                continue
            if not _config_has_portal_credentials(config):
                continue

            max_days = 10 if pk == 'matbao' else 31
            _, range_err = _validate_outward_date_range(from_date, to_date, max_days)
            if range_err:
                sync_messages.append(f'{_provider_display_name(pk)}: {range_err}')
                continue

            if pk == 'vnpt':
                resp = _sync_vnpt_outward_invoices(from_date, to_date, config, merge_local=False)
            else:
                resp = _sync_matbao_outward_invoices(from_date, to_date, config, merge_local=False)

            data = _parse_flask_json_response(resp)
            if not data.get('success'):
                sync_errors.append(
                    f"{_provider_display_name(pk)}: {data.get('error') or 'Lỗi đồng bộ portal'}"
                )
                continue

            formatted = _merge_outward_formatted_lists(formatted, data.get('data') or [])
            synced_total += int(data.get('synced_count') or 0)
            if data.get('message'):
                sync_messages.append(data['message'])

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            local_filter = None if provider_param in ('all', '') else provider_param
            formatted = _merge_local_outward_invoices(
                cursor, formatted, from_date, to_date, local_filter,
            )
            formatted = _dedupe_outward_formatted(formatted)
            formatted = _attach_provider_labels(formatted)
            formatted.sort(key=lambda x: (x.get('invoice_date', ''), x.get('invoice_no', '')), reverse=True)
        finally:
            conn.close()

        if not formatted and sync_errors and not configs:
            return jsonify({'success': False, 'error': 'Chưa cấu hình thông tin kết nối HĐĐT'}), 400

        msg_parts = []
        if formatted:
            msg_parts.append(f'Hiển thị {len(formatted)} hóa đơn.')
        if synced_total:
            msg_parts.append(f'Đã cập nhật {synced_total} đơn từ portal.')
        if sync_messages:
            msg_parts.extend(sync_messages[:3])
        if sync_errors:
            msg_parts.append('Lỗi đồng bộ: ' + '; '.join(sync_errors[:3]))

        return jsonify({
            'success': True,
            'data': formatted,
            'synced_count': synced_total,
            'auto_returned': 0,
            'provider': provider_param or 'all',
            'message': ' '.join(msg_parts) or 'Không có dữ liệu trong khoảng thời gian này.',
            'sync_errors': sync_errors,
        })

    def _infer_outward_invoice_provider(row):
        """Suy ra nhà cung cấp HĐ từ sale.invoice_provider hoặc URL/id lưu cục bộ."""
        prov = ''
        try:
            prov = (row['invoice_provider'] or '').strip().lower()
        except (KeyError, IndexError, TypeError):
            pass
        if prov in ('matbao', 'vnpt'):
            return prov

        pdf = str(
            (row['pdf_url'] if 'pdf_url' in row.keys() else '')
            or (row['invoice_pdf_url'] if 'invoice_pdf_url' in row.keys() else '')
            or ''
        ).lower()
        inv_id = str(row['invoice_id'] if 'invoice_id' in row.keys() else '' or '')
        fkey = str(row['fkey'] if 'fkey' in row.keys() else '' or '')
        if 'vnpt-invoice' in pdf or 'portalservice.asmx' in pdf or inv_id.startswith('SME') or fkey.startswith('SME'):
            return 'vnpt'
        if 'matbao' in pdf or 'mifi.vn' in pdf:
            return 'matbao'
        return ''

    def _merge_local_outward_invoices(cursor, formatted, from_date, to_date, provider_key=None):
        """Bổ sung HĐ nháp/cục bộ chưa có trong response portal (mọi NCC khi provider_key=None)."""
        existing = {_outward_invoice_dedupe_key(x) for x in formatted}
        active_provider = (provider_key or '').strip().lower()
        try:
            rows = cursor.execute(
                """
                SELECT o.sale_no, o.sale_id, o.invoice_no, o.invoice_id, o.invoice_date,
                       o.customer_name, o.total, o.amount, o.pdf_url, o.xml_file, o.status, o.fkey,
                       s.tax_authority_status, s.invoice_status, s.invoice_pdf_url, s.invoice_provider,
                       s.company_name, s.customer_name AS sale_customer_name
                FROM outward_invoices o
                LEFT JOIN sale s ON s.id = o.sale_id
                WHERE (
                    (o.invoice_date IS NOT NULL AND o.invoice_date != ''
                     AND date(o.invoice_date) BETWEEN date(?) AND date(?))
                    OR (o.created_at IS NOT NULL
                        AND date(o.created_at) BETWEEN date(?) AND date(?))
                )
                ORDER BY o.id DESC
                """,
                (from_date, to_date, from_date, to_date),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logging.warning("merge local outward invoices: %s", exc)
            return formatted

        for row in rows:
            inferred_provider = _infer_outward_invoice_provider(row)
            if (
                active_provider
                and active_provider not in ('all', '')
                and inferred_provider
                and inferred_provider != active_provider
            ):
                continue

            inv_no = _normalize_invoice_no(row['invoice_no'])
            row_status = str(row['status'] or '').lower()
            sale_inv_status = str(row['invoice_status'] or '').lower()
            is_draft = sale_inv_status == 'draft' or row_status == 'draft' or inv_no == '0'
            item = {
                "sale_no": row['sale_no'] or '',
                "sale_id": row['sale_id'],
                "invoice_no": '0' if is_draft else (row['invoice_no'] or ''),
                "invoice_date": str(row['invoice_date'] or '')[:10],
                "company": row['company_name'] or row['customer_name'] or '',
                "customer": row['sale_customer_name'] or row['customer_name'] or '',
                "amount": float(row['total'] or row['amount'] or 0),
                "fkey": '',
                "invoice_id": row['invoice_id'] or '',
                "pdf_url": row['pdf_url'] or row['invoice_pdf_url'] or '',
                "xml_url": row['xml_file'] or '',
                "ten_LoaiHDon": 'Hóa đơn nháp' if is_draft else '',
                "tax_authority_status": row['tax_authority_status'] or ('Hóa đơn nháp' if is_draft else '—'),
                "is_draft": is_draft,
                "provider": inferred_provider or active_provider or '',
                "provider_label": _provider_display_name(inferred_provider or active_provider or ''),
            }
            key = _outward_invoice_dedupe_key(item)
            if key in existing:
                continue
            formatted.append(item)
            existing.add(key)
        return formatted

    def _sync_matbao_outward_invoices(from_date, to_date, config, merge_local=True):
        provider = MatbaoProvider(config)

        payload = {
            "TNLap": from_date,
            "DNLap": to_date,
            "LoaiHDon": -1,
            "TThaiHDon": -1,
            "KHMSHDon": str(config.get('invoice_type', '2')),
            "KHHDon": str(config.get('invoice_series', ''))
        }

        conn = None
        try:
            url = f"{provider.base_url}/api/invoice/invoice-detail"
            if not getattr(provider, '_token', None):
                provider._get_token()

            headers = {
                "Authorization": f"Bearer {provider._token}",
                "Content-Type": "application/json"
            }
            res = requests.post(url, json=payload, headers=headers, timeout=30, verify=False)

            if res.status_code == 401:
                provider._get_token()
                headers["Authorization"] = f"Bearer {provider._token}"
                res = requests.post(url, json=payload, headers=headers, timeout=30, verify=False)

            if res.status_code != 200:
                return jsonify({"success": False, "error": f"Lỗi hệ thống Mắt Bão (HTTP {res.status_code})"}), 500

            res_data = res.json()
            is_ok = res_data.get('errorCode') == 200 or res_data.get('success') is True
            if not is_ok:
                return jsonify({"success": False, "error": res_data.get('message') or "Sai thông tin Mẫu số/Ký hiệu"}), 400

            raw_list = res_data.get('data') or res_data.get('Data') or []
            if isinstance(raw_list, dict):
                raw_list = [raw_list]

            raw_list.sort(key=lambda x: (x.get('nLap') or x.get('NLap') or ''))

            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            updated_count = 0
            formatted = []

            for inv in raw_list:
                sale_no = str(inv.get('so') or inv.get('SO') or inv.get('mtChieu') or inv.get('MTChieu') or '').strip()
                inv_no = str(inv.get('shDon') or inv.get('SHDon') or '').strip()
                serial = str(inv.get('khHDon') or inv.get('KHHDon') or '').strip()
          
                # Xử lý ngày an toàn
                inv_date_raw = inv.get('nLap') or inv.get('NLap') or ''
                if isinstance(inv_date_raw, (date, datetime)):
                    inv_date_str = inv_date_raw.strftime('%Y-%m-%d')
                else:
                    inv_date_str = str(inv_date_raw)[:10] if inv_date_raw else ''
          
                status_text = inv.get('tenTThaiHDon') or inv.get('TenTTHdon') or 'Đã phát hành'
         
                inv_system_id = inv.get('InvID') or inv.get('maSoHDon') or inv.get('MaSoHDon') or ''
                fkey = inv.get('maTraCuu') or inv.get('MaTraCuu') or inv_system_id
         
                pdf_url = inv.get('urlDownloadPDF') or ''
                xml_url = inv.get('urlDownloadXML') or ''
         
                total_amount = float(inv.get('tgTTTBSo') or inv.get('TgTTTBSo') or 0)
                discount_amount = float(inv.get('stcKhau') or 0)
                tax_amount = float(inv.get('tgTThue') or 0)
                amount_net = total_amount - tax_amount - discount_amount
                 
                ten_loai = inv.get('tenLoaiHDon') or inv.get('TenLoaiHDon') or ''
                is_draft = (
                    _normalize_invoice_no(inv_no) == '0'
                    or 'nháp' in ten_loai.lower()
                    or 'nhap' in ten_loai.lower()
                )
                sale_id_val = None

                if sale_no:
                    cursor.execute(
                        "SELECT id, invoice_number, invoice_status, invoice_id FROM sale WHERE UPPER(TRIM(sale_no)) = ?",
                        (sale_no.upper(),),
                    )
                    current_sale = cursor.fetchone()
             
                    if current_sale:
                        sale_id_val = current_sale['id']
                        exist_inv = str(current_sale['invoice_number'] if 'invoice_number' in current_sale.keys() else '').strip()
                   
                        if is_draft:
                            cursor.execute("""
                                UPDATE sale
                                SET invoice_number = '0', invoice_id = ?, invoice_date = ?,
                                    tax_authority_status = ?, fkey = ?, invoice_status = 'draft',
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE UPPER(TRIM(sale_no)) = ?
                            """, (inv_system_id, inv_date_str, status_text, fkey, sale_no.upper()))
                        elif not exist_inv or exist_inv in ('0', '00000000'):
                            cursor.execute("""
                                UPDATE sale
                                SET invoice_number = ?, invoice_date = ?, tax_authority_status = ?,
                                    fkey = ?, invoice_status = 'issued', updated_at = CURRENT_TIMESTAMP
                                WHERE UPPER(TRIM(sale_no)) = ?
                            """, (inv_no, inv_date_str, status_text, fkey, sale_no.upper()))
                        elif exist_inv == inv_no:
                            cursor.execute("""
                                UPDATE sale
                                SET tax_authority_status = ?, updated_at = CURRENT_TIMESTAMP
                                WHERE UPPER(TRIM(sale_no)) = ?
                            """, (status_text, sale_no.upper()))
                        else:
                            cursor.execute("""
                                UPDATE sale
                                SET replacement_invoice_no = ?, tax_authority_status = ?, updated_at = CURRENT_TIMESTAMP
                                WHERE UPPER(TRIM(sale_no)) = ?
                            """, (inv_no, status_text, sale_no.upper()))
             
                    if cursor.rowcount > 0:
                        updated_count += 1

                if not sale_id_val and inv_system_id:
                    ow = cursor.execute(
                        "SELECT sale_id FROM outward_invoices WHERE invoice_id = ? ORDER BY id DESC LIMIT 1",
                        (inv_system_id,),
                    ).fetchone()
                    if ow and ow['sale_id']:
                        sale_id_val = ow['sale_id']
         
                # Upsert vào outward_invoices (chỉ thêm nếu chưa tồn tại)
                cursor.execute("""
                    SELECT id FROM outward_invoices
                    WHERE invoice_no = ? AND sale_no = ?
                """, (inv_no, sale_no))
          
                if not cursor.fetchone():
                    cursor.execute("""
                        INSERT INTO outward_invoices
                        (invoice_date, sale_no, invoice_no, invoice_id, serial,
                         customer_name, customer_tax_code, customer_address,
                         amount, tax_amount, total, status, fkey, pdf_url, xml_file, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """, (
                        inv_date_str,
                        sale_no,
                        inv_no,
                        inv_system_id,
                        serial,
                        inv.get('nMua_Ten') or inv.get('NMua_Ten') or DEFAULT_RETAIL_BUYER_NAME,
                        inv.get('nMua_MST') or '',
                        inv.get('nMua_DChi') or '',
                        amount_net,
                        tax_amount,
                        total_amount,
                        status_text,
                        fkey,
                        pdf_url,
                        xml_url
                    ))
        
                formatted.append({
                    "sale_no": sale_no,
                    "sale_id": sale_id_val,
                    "invoice_no": inv_no,
                    "invoice_date": inv_date_str,
                    "company": inv.get('nMua_Ten') or inv.get('NMua_Ten') or '',
                    "customer": inv.get('nMua_HVTNMHang') or inv.get('NMua_HVTNMHang') or '',
                    "amount": total_amount,
                    "fkey": fkey,
                    "invoice_id": inv_system_id,
                    "pdf_url": pdf_url,
                    "xml_url": xml_url,
                    "ten_LoaiHDon": ten_loai,
                    "tax_authority_status": status_text,
                    "is_draft": is_draft,
                    "provider": "matbao",
                })
     
            conn.commit()

            if merge_local:
                formatted = _merge_local_outward_invoices(cursor, formatted, from_date, to_date, 'matbao')
            formatted = _attach_provider_labels(formatted)

            # Sắp xếp kết quả trả về
            formatted.sort(key=lambda x: (x.get('invoice_date', ''), x.get('invoice_no', '')), reverse=True)

            scope = 'Mắt Bão + cục bộ' if merge_local else 'Mắt Bão'
            return jsonify({
                "success": True,
                "data": formatted,
                "synced_count": updated_count,
                "auto_returned": 0,
                "provider": "matbao",
                "message": f"Đã đồng bộ {len(formatted)} hóa đơn ({scope})."
            })
        except Exception as e:
            if conn:
                conn.rollback()
            logging.error("Error in _sync_matbao_outward_invoices: %s", e, exc_info=True)
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn:
                conn.close()

    @app.route('/api/matbao/invoices', methods=['GET'])
    def api_matbao_invoices():
        """Giữ route cũ — chuyển sang API thống nhất theo provider đang cấu hình."""
        return api_outward_invoices()

    @app.route('/api/vnpt/download-file', methods=['GET'])
    def api_vnpt_download_proxy():
        """Proxy tải PDF/XML VNPT qua SOAP PortalService."""
        from Services.einvoice_adapters import VNPTInvoiceAdapter

        token = (request.args.get('token') or '').strip()
        fkey = (request.args.get('fkey') or '').strip()
        file_type = (request.args.get('type') or 'pdf').lower()
        inline = request.args.get('inline', '').lower() in ('1', 'true', 'yes')
        if not token and not fkey:
            return 'Thiếu token hoặc Fkey hóa đơn VNPT', 400

        config = get_active_invoice_config()
        if not config:
            return 'Chưa cấu hình HĐĐT', 404

        adapter = VNPTInvoiceAdapter(config)
        if fkey:
            result = adapter.download_invoice_by_fkey(fkey, file_type)
        else:
            result = adapter.download_invoice_file(token, file_type)
        if not result.get('success'):
            return result.get('error') or 'Không tải được file VNPT', 400

        ext = 'pdf' if file_type != 'xml' else 'xml'
        mimetype = 'application/pdf' if ext == 'pdf' else 'text/xml'
        return send_file(
            BytesIO(result['data']),
            mimetype=mimetype,
            as_attachment=not inline,
            download_name=f'invoice.{ext}',
        )

    # --- ROUTE MỚI: PROXY DOWNLOAD (GIẢI QUYẾT VIỆC LẤY FILE) ---
    @app.route('/api/matbao/download-file/<inv_system_id>', methods=['GET'])
    def api_matbao_download_proxy(inv_system_id):
        """
        Route này đóng vai trò trung gian: 
        Nhận ID -> Gọi API Mắt Bão lấy Base64 -> Trả về file cho trình duyệt
        """
        is_pdf = request.args.get('type') == 'pdf'
        inline = request.args.get('inline', '').lower() in ('1', 'true', 'yes')
        config = get_active_invoice_config()
        if not config: return "Config not found", 404
    
        provider = MatbaoProvider(config)
        if not provider._token: provider._get_token()
    
        try:
            url = f"{provider.base_url}/api/invoice/download-file"
            headers = {"Authorization": f"Bearer {provider._token}", "Content-Type": "application/json"}
            payload = {"MaSoHDon": inv_system_id, "IsPdf": is_pdf}
        
            res = requests.post(url, json=payload, headers=headers, timeout=20, verify=False)
            res_data = res.json()
        
            if res_data.get('success') or res_data.get('errorCode') == 200:
                # Dữ liệu file thường nằm trong field 'data' dưới dạng Base64
                file_base64 = res_data.get('data')
                file_data = base64.b64decode(file_base64)
            
                ext = "pdf" if is_pdf else "xml"
                mimetype = "application/pdf" if is_pdf else "text/xml"
            
                return send_file(
                    io.BytesIO(file_data),
                    mimetype=mimetype,
                    as_attachment=not inline,
                    download_name=f"invoice_{inv_system_id}.{ext}"
                )
            return f"Lỗi từ Mắt Bão: {res_data.get('message')}", 400
        except Exception as e:
            return str(e), 500

    import re
    import json
    from flask import request, jsonify

    import os
    import sys
    import logging

    if not os.path.exists('logs'):
        os.makedirs('logs')

    webhook_logger = logging.getLogger('matbao_webhook')
    if not webhook_logger.handlers:
        if sys.platform == 'win32':
            file_handler = logging.FileHandler('logs/webhook.log', encoding='utf-8')
        else:
            from logging.handlers import RotatingFileHandler
            file_handler = RotatingFileHandler(
                'logs/webhook.log', maxBytes=10240, backupCount=10, encoding='utf-8',
            )
        file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        webhook_logger.addHandler(file_handler)
        webhook_logger.setLevel(logging.INFO)
        webhook_logger.propagate = False

    @app.route('/api/matbao/webhook', methods=['POST'])
    def matbao_webhook():
        try:
            # 1. Bắt dữ liệu (Production nên dùng get_json với force=True)
            payload = request.get_json(silent=True, force=True) or request.form.to_dict()
        
            if not payload:
                raw_data = request.get_data(as_text=True)
                webhook_logger.warning("Webhook: khong parse duoc JSON. Raw: %s", raw_data)
                return jsonify({"success": True, "message": "No payload"}), 200

            # 2. Log lại toàn bộ Payload để đối soát sau này
            webhook_logger.info("Webhook nhan du lieu: %s", payload)

            # 3. Trích xuất thông tin
            ma_tham_chieu = str(payload.get('SO') or '').strip()
            invoice_no    = str(payload.get('No') or '').strip()
            status_text   = str(payload.get('TenTTHDon') or '').strip()
        
            if not ma_tham_chieu:
                return jsonify({"success": True, "message": "Missing SO"}), 200

            # 4. Cập nhật Database (Dùng sale_no làm khóa chính)
            conn = get_db_connection()
            cursor = conn.cursor()
        
            cursor.execute("""
                UPDATE sale SET 
                    invoice_number = ?, 
                    tax_authority_status = ?, 
                    invoice_status = 'issued',
                    updated_at = CURRENT_TIMESTAMP
                WHERE UPPER(TRIM(sale_no)) = ?
            """, (invoice_no, status_text, ma_tham_chieu.upper()))
        
            row_affected = cursor.rowcount
            conn.commit()
            conn.close()

            if row_affected > 0:
                webhook_logger.info("Da cap nhat hoa don thanh cong cho don: %s", ma_tham_chieu)
                return jsonify({"success": True, "message": "Update success"}), 200
            else:
                webhook_logger.error("Khong tim thay don %s trong Database", ma_tham_chieu)
                return jsonify({"success": True, "message": "Sale not found"}), 200

        except Exception as e:
            webhook_logger.error("Loi nghiem trong Webhook: %s", str(e))
            return jsonify({"success": False, "error": str(e)}), 200

    # ====================== API LẤY LINK FILE HÓA ĐƠN ĐẦU RA/BÁN HÀNG PDF VÀ XML======================#
    from flask import request, jsonify
    def _normalize_vnpt_file_url(url, config, file_type='pdf'):
        url = str(url or '').strip()
        if not url or url.startswith('/api/vnpt/'):
            return url
        if 'vnpt-invoice' in url.lower() and 'token=' in url.lower():
            from urllib.parse import unquote
            from Services.einvoice_adapters import VNPTInvoiceAdapter
            token = unquote(url.split('token=', 1)[1].split('&', 1)[0])
            parts = token.split(';')
            if len(parts) >= 3:
                adapter = VNPTInvoiceAdapter(config or {})
                return adapter.internal_download_url(parts[0], parts[1], parts[2], file_type)
        return url

    # ====================== LẤY PDF URL TỪ outward_invoices ======================
    @app.route('/api/sale/invoice_pdf_url', methods=['GET'])
    def get_invoice_pdf_url():
        """Lấy link PDF từ outward_invoices hoặc sale (hỗ trợ HĐ nháp số 0)."""
        invoice_number = (request.args.get('invoice_number') or '').strip()
        sale_id = request.args.get('sale_id')
        invoice_id = (request.args.get('invoice_id') or '').strip()

        if not invoice_number and not sale_id and not invoice_id:
            return jsonify({"success": False, "error": "Thiếu tham số tra cứu PDF"}), 400

        conn = None
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            config_row = cursor.execute(
                "SELECT * FROM invoice_settings WHERE is_active = 1"
            ).fetchone()
            config = dict(config_row) if config_row else {}

            def _return_pdf(raw_url):
                return jsonify({
                    "success": True,
                    "pdf_url": _normalize_vnpt_file_url(raw_url, config, 'pdf'),
                })

            if invoice_id:
                row = cursor.execute(
                    """
                    SELECT pdf_url FROM outward_invoices
                    WHERE invoice_id = ? AND pdf_url IS NOT NULL AND pdf_url != ''
                    ORDER BY id DESC LIMIT 1
                    """,
                    (invoice_id,),
                ).fetchone()
                if row and row['pdf_url']:
                    return _return_pdf(row['pdf_url'])

                row = cursor.execute(
                    """
                    SELECT invoice_pdf_url FROM sale
                    WHERE invoice_id = ? AND invoice_pdf_url IS NOT NULL AND invoice_pdf_url != ''
                    LIMIT 1
                    """,
                    (invoice_id,),
                ).fetchone()
                if row and row['invoice_pdf_url']:
                    return _return_pdf(row['invoice_pdf_url'])

            if sale_id:
                row = cursor.execute(
                    """
                    SELECT o.pdf_url, o.fkey, o.invoice_id, o.status,
                           s.invoice_status, s.invoice_id AS sale_invoice_id,
                           s.invoice_pdf_url
                    FROM outward_invoices o
                    LEFT JOIN sale s ON s.id = o.sale_id
                    WHERE o.sale_id = ?
                    ORDER BY o.id DESC
                    LIMIT 1
                    """,
                    (sale_id,),
                ).fetchone()
                if row:
                    if row['pdf_url']:
                        return _return_pdf(row['pdf_url'])
                    if row['invoice_pdf_url']:
                        return _return_pdf(row['invoice_pdf_url'])
                    fkey = (
                        row['fkey'] or row['invoice_id'] or row['sale_invoice_id'] or ''
                    ).strip()
                    is_draft_row = (
                        str(row['status'] or '').lower() == 'draft'
                        or str(row['invoice_status'] or '').lower() == 'draft'
                    )
                    if fkey and is_draft_row and (config.get('provider_name') or '').lower() == 'vnpt':
                        from Services.einvoice_adapters import VNPTInvoiceAdapter
                        adapter = VNPTInvoiceAdapter(config)
                        return _return_pdf(adapter.internal_draft_download_url(fkey, 'pdf'))

                row = cursor.execute(
                    """
                    SELECT invoice_pdf_url, invoice_id, invoice_status
                    FROM sale
                    WHERE id = ?
                    LIMIT 1
                    """,
                    (sale_id,),
                ).fetchone()
                if row and row['invoice_pdf_url']:
                    return _return_pdf(row['invoice_pdf_url'])
                if row and str(row['invoice_status'] or '').lower() == 'draft':
                    fkey = (row['invoice_id'] or '').strip()
                    if fkey and (config.get('provider_name') or '').lower() == 'vnpt':
                        from Services.einvoice_adapters import VNPTInvoiceAdapter
                        adapter = VNPTInvoiceAdapter(config)
                        return _return_pdf(adapter.internal_draft_download_url(fkey, 'pdf'))

            if invoice_number:
                cursor.execute(
                    """
                    SELECT pdf_url
                    FROM outward_invoices
                    WHERE (invoice_no = ? OR sale_no = ?)
                      AND pdf_url IS NOT NULL AND pdf_url != ''
                      AND COALESCE(invoice_no, '') NOT IN ('', '0')
                    ORDER BY (sale_id IS NOT NULL) DESC, id DESC
                    LIMIT 1
                    """,
                    (invoice_number, invoice_number),
                )
                row = cursor.fetchone()
                if row and row['pdf_url']:
                    return _return_pdf(row['pdf_url'])

                row = cursor.execute(
                    """
                    SELECT invoice_pdf_url FROM sale
                    WHERE invoice_number = ? AND invoice_pdf_url IS NOT NULL AND invoice_pdf_url != ''
                    ORDER BY id DESC LIMIT 1
                    """,
                    (invoice_number,),
                ).fetchone()
                if row and row['invoice_pdf_url']:
                    return _return_pdf(row['invoice_pdf_url'])

            return jsonify({
                "success": False,
                "error": "Không tìm thấy link PDF hóa đơn",
            }), 404

        except Exception as e:
            logging.error(f"Error in get_invoice_pdf_url: {str(e)}")
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn:
                conn.close()


    # ====================== LẤY XML URL TỪ outward_invoices ======================
    @app.route('/api/sale/invoice_xml_file', methods=['GET'])
    def get_invoice_xml_file():
        """Lấy link XML từ bảng outward_invoices"""
        invoice_number = request.args.get('invoice_number')
        if not invoice_number:
            return jsonify({"success": False, "error": "Thiếu số hóa đơn"}), 400

        conn = None
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            config_row = cursor.execute(
                "SELECT * FROM invoice_settings WHERE is_active = 1"
            ).fetchone()
            config = dict(config_row) if config_row else {}

            cursor.execute("""
                SELECT xml_file 
                FROM outward_invoices 
                WHERE (invoice_no = ? OR sale_no = ?) 
                AND xml_file IS NOT NULL AND xml_file != ''
                ORDER BY id DESC
                LIMIT 1
            """, (invoice_number, invoice_number))

            row = cursor.fetchone()
            if row and row['xml_file']:
                return jsonify({
                    "success": True,
                    "xml_file": _normalize_vnpt_file_url(row['xml_file'], config, 'xml'),
                })

            row = cursor.execute("""
                SELECT invoice_xml_file FROM sale
                WHERE invoice_number = ? AND invoice_xml_file IS NOT NULL AND invoice_xml_file != ''
                LIMIT 1
            """, (invoice_number,)).fetchone()
            if row and row['invoice_xml_file']:
                return jsonify({
                    "success": True,
                    "xml_file": _normalize_vnpt_file_url(row['invoice_xml_file'], config, 'xml'),
                })

            return jsonify({
                "success": False, 
                "error": "Không tìm thấy link XML trong outward_invoices"
            }), 404

        except Exception as e:
            logging.error(f"Error in get_invoice_xml_file: {str(e)}")
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn:
                conn.close()

    @app.route('/api/view-pdf')
    def view_pdf():
        from urllib.parse import parse_qs, unquote, urlparse
        from Services.einvoice_adapters import VNPTInvoiceAdapter

        url = (request.args.get('url') or '').strip()
        if not url:
            return "Missing URL", 400

        if url.startswith('/api/vnpt/download-file'):
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            token = unquote((qs.get('token') or [''])[0])
            fkey = unquote((qs.get('fkey') or [''])[0])
            config = get_active_invoice_config() or {}
            adapter = VNPTInvoiceAdapter(config)
            if fkey:
                result = adapter.download_invoice_by_fkey(fkey, 'pdf')
            else:
                result = adapter.download_invoice_file(token, 'pdf')
            if not result.get('success'):
                return result.get('error') or 'Không tải được PDF VNPT', 400
            return Response(
                result['data'],
                content_type='application/pdf',
                headers={"Content-Disposition": "inline"},
            )

        if 'vnpt-invoice' in url.lower() and 'token=' in url.lower():
            token = unquote(url.split('token=', 1)[1].split('&', 1)[0])
            config = get_active_invoice_config() or {}
            adapter = VNPTInvoiceAdapter(config)
            result = adapter.download_invoice_file(token, 'pdf')
            if result.get('success'):
                return Response(
                    result['data'],
                    content_type='application/pdf',
                    headers={"Content-Disposition": "inline"},
                )

        r = requests.get(url, timeout=20, verify=False)
        return Response(
            r.content,
            content_type='application/pdf',
            headers={"Content-Disposition": "inline"},
        )

    #===== KẾT THÚC PHẦN HÓA ĐƠN ĐIỆN TỬ MẮT BÃO =======#

    @app.route('/api/esign/sign', methods=['POST'])
    @login_required
    def api_esign_sign():
        data = request.json or {}
        xml_content = data.get('xml_content')
        if not xml_content:
            return jsonify({"success": False, "error": "Thiếu nội dung XML"}), 400

        conn = get_db_connection()
        try:
            config_row = conn.execute(
                "SELECT * FROM invoice_settings WHERE is_active = 1"
            ).fetchone()
        finally:
            conn.close()

        if not config_row:
            return jsonify({"success": False, "error": "Chưa kích hoạt provider nào"}), 400

        config = normalize_invoice_config(dict(config_row))
        if not config.get('api_url'):
            return jsonify({"success": False, "error": "Chưa cấu hình API URL"}), 400

        try:
            signed = esign_xml_content(xml_content, config)
            return jsonify({"success": True, "signed_xml": signed})
        except Exception as e:
            logging.error(f"Lỗi ký eSign: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    # === API XUẤT HÓA ĐƠN ĐIỆN TỬ ===
    logging.basicConfig(level=logging.INFO)

    # ====================== BASE CLASS CHUNG ======================
    class InvoiceProvider:
        def __init__(self, config):
            self.config = config
            self.base_url = config.get('api_url', '').rstrip('/')
            self.headers = {
                'Content-Type': 'application/json',
                'TaxCode': config.get('tax_code', '')
            }
            self.token = None

        def get_token(self):
            """Lấy token chung - override cho từng provider nếu khác"""
            url = f"{self.base_url}/auth/token"
            payload = {
                'grant_type': 'password',
                'username': self.config['username'],
                'password': self.config['password'],
                'client_id': self.config['app_id'],
                'client_secret': self.config['app_secret']
            }
            try:
                resp = requests.post(url, data=payload, timeout=10)
                resp.raise_for_status()
                self.token = resp.json()['access_token']
                self.headers['Authorization'] = f"Bearer {self.token}"
                return self.token
            except Exception as e:
                logging.error(f"{self.__class__.__name__} get_token error: {e}")
                return None

        def create_draft(self, invoice_data, items):
            """Tạo hóa đơn nháp - override cho từng provider"""
            raise NotImplementedError("Provider must implement create_draft")

        def sign_invoice(self, invoice_code):
            """Ký chữ ký số - override"""
            raise NotImplementedError("Provider must implement sign_invoice")

        def issue_invoice(self, invoice_code):
            """Phát hành hóa đơn - override"""
            raise NotImplementedError("Provider must implement issue_invoice")

        def issue_full(self, sale_id, customer_name, tax_code, address, items, total_amount):
            """Quy trình đầy đủ: tạo nháp → ký → phát hành"""
            if not self.get_token():
                return {"success": False, "error": "Không lấy được token"}

            invoice_data = {
                "sale_id": sale_id,
                "customer_name": customer_name,
                "tax_code": tax_code,
                "address": address,
                "total_amount": total_amount
            }

            draft_res = self.create_draft(invoice_data, items)
            if not draft_res['success']:
                return draft_res

            invoice_code = draft_res['invoice_code']

            sign_res = self.sign_invoice(invoice_code)
            if not sign_res['success']:
                return sign_res

            issue_res = self.issue_invoice(invoice_code)
            if not issue_res['success']:
                return issue_res

            return {
                "success": True,
                "invoice_no": issue_res.get('invoice_no'),
                "pdf_url": issue_res.get('pdf_url'),
                "xml_url": issue_res.get('xml_url'),
                "invoice_code": invoice_code
            }

    # ====================== ADAPTER CHO MISA ======================
    class MisaProvider(InvoiceProvider):
        def create_draft(self, invoice_data, items):
            url = f"{self.base_url}/invoices/create-draft"
            payload = {
                "invoiceType": self.config.get('invoice_type', '01GTKT0/001'),
                "invoiceSeries": self.config.get('invoice_series', 'AA/26E'),
                "buyerInfo": {
                    "buyerName": normalize_retail_buyer_name(invoice_data.get("customer_name")),
                    "buyerTaxCode": invoice_data["tax_code"] or "",
                    "buyerAddress": invoice_data["address"] or ""
                },
                "invoiceItems": [
                    {
                        "itemName": item.get('name', ''),
                        "quantity": float(item.get('quantity', 0)),
                        "unitPrice": float(item.get('price', 0)),
                        "vatRate": 10,  # Mặc định 10%, lấy từ item nếu có
                        "discount": 0
                    } for item in items
                ],
                "paymentMethod": "Tiền mặt",  # Lấy từ payment_method
                "issueDate": datetime.now().strftime("%Y-%m-%d")
            }
            try:
                resp = requests.post(url, json=payload, headers=self.headers, timeout=15)
                resp.raise_for_status()
                res = resp.json()
                if res.get('success'):
                    return {"success": True, "invoice_code": res['data']['invoiceCode']}
                return {"success": False, "error": res.get('message', 'Lỗi tạo nháp')}
            except Exception as e:
                return {"success": False, "error": str(e)}

        def sign_invoice(self, invoice_code):
            url = f"{self.base_url}/invoices/sign"
            payload = {
                "invoiceCode": invoice_code,
                "certSerial": self.config['serial_number'],
                "pin": self.config['esign_pin']
            }
            try:
                resp = requests.post(url, json=payload, headers=self.headers, timeout=15)
                resp.raise_for_status()
                res = resp.json()
                return {"success": res.get('success', False), "error": res.get('message')}
            except Exception as e:
                return {"success": False, "error": str(e)}

        def issue_invoice(self, invoice_code):
            url = f"{self.base_url}/invoices/issue"
            payload = {"invoiceCode": invoice_code}
            try:
                resp = requests.post(url, json=payload, headers=self.headers, timeout=15)
                resp.raise_for_status()
                res = resp.json()
                if res.get('success'):
                    return {
                        "success": True,
                        "invoice_no": res['data'].get('invoiceNumber'),
                        "pdf_url": res['data'].get('pdfUrl'),
                        "xml_url": res['data'].get('xmlUrl')
                    }
                return {"success": False, "error": res.get('message', 'Lỗi phát hành')}
            except Exception as e:
                return {"success": False, "error": str(e)}

    # ====================== ADAPTER CHO EASYINVOICE (Softdreams) ======================
    class EasyInvoiceProvider(InvoiceProvider):
        # Lưu ý: EasyInvoice chủ yếu dùng DLL cho tích hợp, không có API REST công khai đầy đủ. 
        # Bạn cần liên hệ Mắt Bão để lấy docs DLL hoặc API (nếu có). Dưới đây là placeholder dựa trên docs chung.
        # Tích hợp ký số thường cần cài EasySigner trên máy server.
        def get_token(self):
            # EasyInvoice có thể dùng API Key thay token
            self.headers['ApiKey'] = self.config['app_id']  # Giả sử
            return self.config['app_id']

        def create_draft(self, invoice_data, items):
            # Placeholder - liên hệ EasyInvoice để lấy URL và payload chính xác
            url = f"{self.base_url}/api/invoice/create"
            payload = {
                # ... tùy chỉnh theo docs EasyInvoice
            }
            try:
                resp = requests.post(url, json=payload, headers=self.headers, timeout=15)
                # ... xử lý response
                return {"success": True, "invoice_code": "CODE_FROM_EASY"}
            except:
                return {"success": False, "error": "Chưa có docs đầy đủ, liên hệ Mắt Bão"}

        def sign_invoice(self, invoice_code):
            # Placeholder - thường dùng DLL, có thể gọi API nếu có
            return {"success": True}

        def issue_invoice(self, invoice_code):
            # Placeholder
            return {"success": True, "invoice_no": "EASY-INV-001", "pdf_url": "https://easyinvoice.vn/pdf/001"}

    # ====================== ADAPTER CHO VNPT ======================
    class VNPTProvider(InvoiceProvider):
        # Docs VNPT: https://vnpt-invoice.com.vn/api-document (liên hệ VNPT để lấy full docs)
        def get_token(self):
            # VNPT dùng OAuth hoặc API Key - tùy cấu hình
            url = f"{self.base_url}/oauth/token"
            # ... tương tự MISA
            return "TOKEN_VNPT"

        def create_draft(self, invoice_data, items):
            # Placeholder - dùng docs VNPT
            return {"success": True, "invoice_code": "VNPT-CODE"}

        def sign_invoice(self, invoice_code):
            # VNPT hỗ trợ eSign qua token
            return {"success": True}

        def issue_invoice(self, invoice_code):
            return {"success": True, "invoice_no": "VNPT-INV-001", "pdf_url": "https://vnpt-invoice.com.vn/pdf/001"}

    # ====================== ADAPTER CHO VIETTEL ======================
    class ViettelProvider(InvoiceProvider):
        # Docs Viettel: https://s-invoice.viettel.vn/api-docs (liên hệ Viettel)
        def get_token(self):
            # Viettel dùng username/password hoặc key
            return "TOKEN_VIETTEL"

        def create_draft(self, invoice_data, items):
            return {"success": True, "invoice_code": "VIETTEL-CODE"}

        def sign_invoice(self, invoice_code):
            return {"success": True}

        def issue_invoice(self, invoice_code):
            return {"success": True, "invoice_no": "VIET-INV-001", "pdf_url": "https://s-invoice.viettel.vn/pdf/001"}

    # ====================== ADAPTER CHO BKAV ======================
    class BKAVProvider(InvoiceProvider):
        # Docs BKAV: https://bkav.com.vn/api-ehoadon (liên hệ BKAV)
        def get_token(self):
            return "TOKEN_BKAV"

        def create_draft(self, invoice_data, items):
            return {"success": True, "invoice_code": "BKAV-CODE"}

        def sign_invoice(self, invoice_code):
            return {"success": True}

        def issue_invoice(self, invoice_code):
            return {"success": True, "invoice_no": "BKAV-INV-001", "pdf_url": "https://bkav.com.vn/pdf/001"}

    # ====================== ADAPTER CHO FPT ======================
    class FPTProvider(InvoiceProvider):
        # Docs FPT: https://fpt-einvoice.com.vn/api (liên hệ FPT)
        def get_token(self):
            return "TOKEN_FPT"

        def create_draft(self, invoice_data, items):
            return {"success": True, "invoice_code": "FPT-CODE"}

        def sign_invoice(self, invoice_code):
            return {"success": True}

        def issue_invoice(self, invoice_code):
            return {"success": True, "invoice_no": "FPT-INV-001", "pdf_url": "https://fpt-einvoice.com.vn/pdf/001"}

    # ====================== ADAPTER CHO FAST ======================
    class FASTProvider(InvoiceProvider):
        # Docs FAST: https://fast.com.vn/api (liên hệ FAST)
        def get_token(self):
            return "TOKEN_FAST"

        def create_draft(self, invoice_data, items):
            return {"success": True, "invoice_code": "FAST-CODE"}

        def sign_invoice(self, invoice_code):
            return {"success": True}

        def issue_invoice(self, invoice_code):
            return {"success": True, "invoice_no": "FAST-INV-001", "pdf_url": "https://fast.com.vn/pdf/001"}

    # ====================== ADAPTER CHO THÁI SƠN ======================
    class ThaiSonProvider(InvoiceProvider):
        # Docs Thái Sơn: https://thaison.vn/e-invoice/api (liên hệ Thái Sơn)
        def get_token(self):
            return "TOKEN_THAISON"

        def create_draft(self, invoice_data, items):
            return {"success": True, "invoice_code": "THAISON-CODE"}

        def sign_invoice(self, invoice_code):
            return {"success": True}

        def issue_invoice(self, invoice_code):
            return {"success": True, "invoice_no": "THAISON-INV-001", "pdf_url": "https://thaison.vn/pdf/001"}

    # ====================== ADAPTER CHO CYBERLOTUS ======================
    class CyberLotusProvider(InvoiceProvider):
        # Docs CyberLotus: https://cyberlotus.com/api (liên hệ CyberLotus)
        def get_token(self):
            return "TOKEN_CYBERLOTUS"

        def create_draft(self, invoice_data, items):
            return {"success": True, "invoice_code": "CYBER-CODE"}

        def sign_invoice(self, invoice_code):
            return {"success": True}

        def issue_invoice(self, invoice_code):
            return {"success": True, "invoice_no": "CYBER-INV-001", "pdf_url": "https://cyberlotus.com/pdf/001"}

    # ====================== FACTORY ĐỂ CHỌN PROVIDER ======================
    PROVIDER_CLASSES = {
        'matbao': MatbaoProvider,
        'misa': MisaProvider,
        'easyinvoice': EasyInvoiceProvider,
        'vnpt': VNPTProvider,
        'viettel': ViettelProvider,
        'bkav': BKAVProvider,
        'fpt': FPTProvider,
        'fast': FASTProvider,
        'thaison': ThaiSonProvider,
        'cyberlotus': CyberLotusProvider,
    }

    def get_invoice_provider(config):
        provider_key = config.get('provider', '').lower()
        provider_class = PROVIDER_CLASSES.get(provider_key)
        if not provider_class:
            raise ValueError(f"Nhà cung cấp không hỗ trợ: {provider_key}")
        return provider_class(config)

    # === PHẦN IN ẤN HÓA ĐƠN PDF ===#
    @app.route('/print/sale/<int:sale_id>')
    @login_required
    def print_sale(sale_id):
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM sale WHERE id=?", (sale_id,))
        sale = c.fetchone()
        c.execute("SELECT si.*, p.name, p.unit FROM sale_items si JOIN products p ON si.product_id = p.id WHERE si.sale_id = ?", (sale_id,))
        items = c.fetchall()
        conn.close()
        html = render_template('print_receipt.html', sale=sale, items=items)
        config = None
        try:
            wk_path = shutil.which("wkhtmltopdf") or r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe'
            config = pdfkit.configuration(wkhtmltopdf=wk_path)
            pdf = pdfkit.from_string(html, False, configuration=config)
        except Exception as e:
            return f"Lỗi tạo PDF: {e}", 500
        response = make_response(pdf)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename=hoa_don_{sale_id}.pdf'
        return response

    # === IN NHIỆT ===
    PRINTER_VENDOR_ID = 0x28e9
    PRINTER_PRODUCT_ID = 0x0289
    def get_printer():
        if not HAS_ESCPOS:
            return None
        try:
            return Usb(PRINTER_VENDOR_ID, PRINTER_PRODUCT_ID, timeout=10)
        except Exception:
            return None

    @app.route('/print/thermal/<int:sale_id>')
    @login_required
    def print_thermal(sale_id):
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT * FROM sale WHERE id=?", (sale_id,))
        sale = c.fetchone()
        c.execute("SELECT si.*, p.name FROM sale_items si JOIN products p ON si.product_id = p.id WHERE si.sale_id = ?", (sale_id,))
        items = c.fetchall()
        conn.close()
        printer = get_printer()
        if not printer:
            flash("Không tìm thấy máy in nhiệt!", "danger")
            return redirect(url_for('sale'))
        try:
            printer.set(align='center', font='a', width=2, height=2)
            printer.text(f"{COMPANY['name']}\n")
            printer.set(align='center', font='b')
            printer.text(f"{COMPANY['address']}\nĐT: {COMPANY['phone']}\n")
            printer.text("-" * 32 + "\nHOÁ ĐƠN BÁN HÀNG\n")
            printer.text(f"#{sale['id']} - {sale['date']}\n")
            if sale['customer_name']: printer.text(f"KH: {sale['customer_name']}\n")
            printer.text("-" * 32 + "\n")
            printer.set(align='left')
            for item in items:
                name = (item['name'][:27] + '...') if len(item['name']) > 30 else item['name']
                line = f"{name}\n{int(item['quantity'])} x {int(item['price']):,} = {int(item['quantity']*item['price']):,}\n"
                printer.text(line)
            printer.text("-" * 32 + "\n")
            printer.set(bold=True)
            printer.text(f"TỔNG: {int(sale['total']):,} VND\n")
            printer.text(f"PTTT: {sale['payment_method']}\n")
            printer.set(bold=False, align='center')
            printer.text("Cảm ơn quý khách!\n")
            printer.text(datetime.now().strftime('%d/%m/%Y %H:%M') + "\n")
            printer.cut()
        except Exception as e:
            flash(f"Lỗi in: {e}", "danger")
        finally:
            try:
                printer.close()
            except:
                pass
        return redirect(url_for('sale'))

    #=== TẮT IN NHIỆT ===
    PRINT_THERMAL = False  # Đặt False nếu không có máy in

    if PRINT_THERMAL:
        try:
            from escpos.printer import Usb
            printer = Usb(0x0416, 0x5011)  # ID máy in
        except Exception as e:
            print("Máy in nhiệt không kết nối:", e)
            printer = None
    else:
        printer = None

    #=======API PHÁT HÀNH HÓA ĐƠN TỪ MÁY TÍNH TIỀN======#
    @app.route('/api/sale/issue-invoice', methods=['POST'])
    def api_issue_invoice1():
        data = request.get_json()
        sale_id = data.get('sale_id')

        if not sale_id:
            return jsonify({"error": "Thiếu sale_id"}), 400

        conn = get_db_connection()
        c = conn.cursor()

        try:
            # ===== 1. KIỂM TRA ĐƠN =====
            c.execute("""
                SELECT id, invoice_number
                FROM sale
                WHERE id = ?
            """, (sale_id,))
            sale = c.fetchone()

            if not sale:
                return jsonify({"error": "Đơn hàng không tồn tại"}), 404

            if sale["invoice_number"]:
                return jsonify({"error": "Đơn hàng đã xuất hóa đơn"}), 409

            # ===== 2. SINH SỐ HÓA ĐƠN (8 KÝ TỰ) =====
            c.execute("""
                SELECT MAX(invoice_number) 
                FROM sale
                WHERE invoice_number IS NOT NULL
            """)
            last = c.fetchone()[0]

            if last:
                next_no = int(last) + 1
            else:
                next_no = 1

            invoice_number = str(next_no).zfill(8)
            invoice_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # ===== 3. UPDATE & LOCK =====
            c.execute("""
                UPDATE sale
                SET 
                    invoice_number = ?,
                    invoice_date   = ?,
                    invoice_status = 'issued'
                WHERE id = ?
            """, (invoice_number, invoice_date, sale_id))

            conn.commit()

            return jsonify({
                "message": "Xuất hóa đơn thành công",
                "invoice_number": invoice_number,
                "invoice_date": invoice_date
            }), 200

        except Exception as e:
            conn.rollback()
            print("ISSUE INVOICE ERROR:", str(e))
            return jsonify({"error": "Lỗi xuất hóa đơn"}), 500

        finally:
            conn.close()

    #HÀM LẤY SALE + ITEMS (CHUẨN)#
    def get_sale_for_invoice(sale_id):
        with get_db_connection() as conn:
            sale = conn.execute(
                "SELECT * FROM sale WHERE id = ?",
                (sale_id,)
            ).fetchone()

            if not sale:
                return None

            items = conn.execute(
                "SELECT * FROM sale_items WHERE sale_id = ?",
                (sale_id,)
            ).fetchall()

        sale = dict(sale)
        sale['items'] = [dict(i) for i in items]
        return sale

    #=== Hóa Đơn Nội Bộ của POS để test không sử dụng để xuất Hóa đơn CQT===#
    @app.route('/IssueInvoice/<int:sale_id>')
    def print_invoice(sale_id):
        conn = get_db_connection()
        cur = conn.cursor()

        # ===== LẤY SALE =====
        cur.execute("""
            SELECT *
            FROM sale
            WHERE id = ?
        """, (sale_id,))
        sale = cur.fetchone()

        if not sale:
            conn.close()
            abort(404, "Không tìm thấy đơn hàng")

        sale = dict(sale)
        discount_amount = sale.get("discount_amount") or 0

        # ===== TỔNG GIÁ TRỊ HÀNG (CHƯA CHIẾT KHẤU) =====
        cur.execute("""
            SELECT SUM(quantity * price)
            FROM sale_items
            WHERE sale_id = ?
        """, (sale_id,))
        total_goods = cur.fetchone()[0] or 0

        # ===== LẤY CHI TIẾT HÀNG HÓA =====
        cur.execute("""
            SELECT
                si.quantity,
                si.price,
                si.UseSaleUnit,
                p.name AS product_name,
                p.unit,
                p.unit1
            FROM sale_items si
            JOIN products p ON p.id = si.product_id
            WHERE si.sale_id = ?
            ORDER BY si.sale_id
        """, (sale_id,))

        rows = cur.fetchall()
        items = []

        allocated_discount = 0

        for idx, r in enumerate(rows):
            r = dict(r)

            qty = r["quantity"] or 0
            price = r["price"] or 0
            line_amount = qty * price

            # ===== PHÂN BỔ CHIẾT KHẤU =====
            if total_goods > 0:
                if idx == len(rows) - 1:
                    line_discount = discount_amount - allocated_discount
                else:
                    line_discount = round(
                        discount_amount * line_amount / total_goods, 0
                    )
                    allocated_discount += line_discount
            else:
                line_discount = 0

            unit = r["unit1"] if r["UseSaleUnit"] == 1 else r["unit"]

            items.append({
                "product_name": r["product_name"],
                "unit": unit,
                "quantity": qty,
                "price": price,
                "discount": line_discount,
                "line_total": line_amount - line_discount
            })

        conn.close()

        # ===== GÓI DỮ LIỆU CHO TEMPLATE =====
        sale_data = {
            "id": sale["id"],
            "invoice_number": sale.get("invoice_number") or sale["id"],
            "date": sale.get("invoice_date") or sale.get("created_at"),
            "symbol": sale.get("symbol", "2K25MMD"),
            "customer_name": normalize_retail_buyer_name(sale.get("customer_name")),
            "tax_code": sale.get("tax_code") or (""),
            "company_name": sale.get("company_name") or (""),
            "address": sale.get("address") or (""),
            "customer_phone": sale.get("customer_phone") or (""),
            "payment_method": sale.get("payment_method", "TM/CK"),
            "items": items
        }

        return render_template("PosInvoice.html", sale=sale_data)

    #===API Tải Invoice Tự Tạo Nội Bộ Để Test====#
    @app.route("/api/invoice_test/download/<int:sale_id>", methods=["GET"])
    def download_invoice_test_xml(sale_id):
        conn = get_db_connection()
        cur = conn.cursor()

        # Lấy thông tin cần thiết từ DB để tái tạo đúng tên file
        sale_row = cur.execute(
            "SELECT invoice_number FROM sale WHERE id = ?",
            (sale_id,)
        ).fetchone()

        if not sale_row:
            abort(404, "Không tìm thấy đơn hàng")

        # Chuyển Row thành dict để dùng .get()
        sale = dict(sale_row)

        # Lấy ký hiệu và số hóa đơn từ DB (giống hệt mock-sign)
        khh_don = sale.get("symbol") or "2K25MMD"

        invoice_num = sale.get("invoice_number")
        if invoice_num is None:
            abort(400, "Đơn hàng chưa có số hóa đơn (invoice_number NULL)")

        try:
            sh_don = f"{int(invoice_num):08d}"  # Đảm bảo 8 chữ số
        except (ValueError, TypeError):
            abort(500, "Số hóa đơn không hợp lệ trong cơ sở dữ liệu")

        # Tái tạo đúng tên file như khi tạo ở mock-sign
        xml_filename = f"{khh_don}_{sh_don}.xml"
        xml_path = os.path.join("invoices_xml", xml_filename)

        if not os.path.exists(xml_path):
            abort(404, f"File XML không tồn tại: {xml_filename}")

        # Tên file tải về đẹp (tùy chọn)
        download_name = f"HD_{sh_don}_{khh_don}.xml"

        return send_file(
            xml_path,
            as_attachment=True,
            download_name=download_name,
            mimetype="application/xml"
        )

    # API route: Lưu đơn hàng, sau đó trả về HTML của hóa đơn để client in
    @app.route('/api/IssueInvoice/print', methods=['POST'])
    def api_print_invoice():
        data = request.get_json(silent=True)
        if not data:
            abort(400, "Không có dữ liệu")

        sale_id = save_sale_and_issue_invoice(data)

        sale = get_sale_for_invoice(sale_id)
        if not sale:
            abort(500, "Không truy xuất được đơn hàng")

        return render_template(
            'PosInvoice.html',
            sale=sale
        )

    # Route: Xem chi tiết hóa đơn đã xuất (có thể mở từ danh sách đơn hàng)
    @app.route('/invoice/view/<int:sale_id>')
    def view_invoice(sale_id):
        sale = get_sale_for_invoice(sale_id)

        if not sale:
            abort(404)

        if not sale.get('invoice_number'):
            abort(400, "Hóa đơn chưa được xuất")

        return render_template(
            'PosInvoice.html',
            sale=sale
        )

    #===API KÝ SỐ HÓA ĐƠN TỰ TẠO TỪ POS (dự phòng — ký thật qua provider)====#
    @app.route("/api/invoice/sign/<int:sale_id>", methods=["POST"])
    @login_required
    def sign_invoice(sale_id):
        conn = get_db_connection()
        try:
            prepared = prepare_sale_invoice_xml(
                conn, sale_id, _so_thanh_chu, fkey_prefix="POS"
            )
            save_invoice_xml(prepared["xml_path"], prepared["xml_content"])

            config_row = conn.execute(
                "SELECT * FROM invoice_settings WHERE is_active = 1"
            ).fetchone()

            signed_xml = None
            signed_path = None
            sign_error = None

            if config_row:
                config = normalize_invoice_config(dict(config_row))
                try:
                    signed_xml = esign_xml_content(prepared["xml_content"], config)
                    signed_path = prepared["xml_path"].replace(".xml", "_signed.xml")
                    save_invoice_xml(signed_path, signed_xml)
                except Exception as exc:
                    sign_error = str(exc)
                    logging.error("Lỗi ký số hóa đơn sale_id=%s: %s", sale_id, exc)
            else:
                sign_error = "Chưa cấu hình provider — chỉ lưu XML chưa ký"

            if signed_xml:
                try:
                    conn.execute(
                        "UPDATE sale SET signed = 1 WHERE id = ?",
                        (sale_id,),
                    )
                except sqlite3.OperationalError:
                    pass
                conn.commit()

            return jsonify({
                "success": True,
                "signed": signed_xml is not None,
                "xml_path": prepared["xml_path"],
                "signed_xml_path": signed_path,
                "sh_don": prepared["sh_don"],
                "sign_error": sign_error,
                "download_url": f"/api/invoice/download/{sale_id}",
            })
        except LookupError:
            return jsonify(success=False, error="Không tìm thấy hóa đơn"), 404
        except ValueError as exc:
            return jsonify(success=False, error=str(exc)), 400
        finally:
            conn.close()

    if not _invoice_scheduler_started:
        inv_scheduler = BackgroundScheduler(daemon=True)
        inv_scheduler.add_job(
            batch_issue_pending_invoices,
            CronTrigger(hour="12,18", minute=0, second=0),
            id="batch_invoice_job",
            name="Batch xuất hóa đơn theo giờ",
            replace_existing=True,
        )
        inv_scheduler.start()
        _invoice_scheduler_started = True
        logger.info("APScheduler batch invoice job started (12:00, 18:00 daily)")

        def _shutdown_invoice_scheduler():
            if inv_scheduler.running:
                inv_scheduler.shutdown(wait=False)
                logger.info("APScheduler invoice scheduler stopped.")

        atexit.register(_shutdown_invoice_scheduler)

