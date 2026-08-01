"""Routes hóa đơn đầu vào (inward) — tách từ app.py."""
import base64
import json
import logging
import re
import sqlite3
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO

import pandas as pd
import requests
from flask import (
    Response,
    abort,
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
from flask_login import login_required

from db_utils import get_db_connection

logger = logging.getLogger(__name__)
from dateutil import parser
from dateutil.relativedelta import relativedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)



class MatbaoPurchaseProvider:
    def __init__(self, config):
        self.config = config
        self.base_url = config.get('api_url', '').strip().rstrip('/')
        self.api_key  = config.get('api_key', '').strip() # Đây là token để lấy Bearer
        self.name     = config.get('name', 'matbao_purchase')

        if not self.base_url or not self.api_key:
            raise ValueError(f"Thiếu api_url hoặc api_key cho {self.name}")

        self._bearer_token = None
        self.session = requests.Session()
        
        # Thiết lập retry để xử lý lỗi mạng tạm thời
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        self.session.mount('https://', HTTPAdapter(max_retries=retries))

    def _get_bearer_token(self):
        """Lấy Bearer token từ endpoint /auth/token"""
        if self._bearer_token:
            return True
        try:
            url = f"{self.base_url}/auth/token"
            payload = {"token": self.api_key}
            logging.info(f"[{self.name}] Đang lấy token từ: {url} với key: {self.api_key[:5]}***")
            r = self.session.post(url, json=payload, timeout=10, verify=False)
            r.raise_for_status()
            data = r.json()
            if data.get("Success") and data.get("Data"):
                self._bearer_token = data["Data"]
                return True
            else:
                # Log lỗi trả về từ server Mắt Bão (ví dụ: "Không có quyền truy cập")
                logging.error(f"[{self.name}] Server từ chối cấp token: {data.get('Data')}")
                return False
        except Exception as e:
            logging.error(f"[{self.name}] Lỗi lấy Bearer token: {str(e)}")
            return False

    def _get_headers(self):
        """Tạo header chuẩn cho mọi request"""
        if not self._bearer_token and not self._get_bearer_token():
            raise RuntimeError("Không thể xác thực API (Bearer Token)")
        return {
            "Authorization": f"Bearer {self._bearer_token}",
            "Content-Type": "application/json"
        }

    def get_tct_captcha(self):
        """BƯỚC 1: Lấy captcha từ server Mắt Bão"""
        try:
            url = f"{self.base_url}/hoa-don-dau-vao/get-captcha"
            r = self.session.get(url, headers=self._get_headers(), timeout=15, verify=False)
            r.raise_for_status()
            data = r.json()
            if data.get("Success"):
                # Trả về ckey và content (svg) để hiển thị lên UI
                return {"success": True, "key": data['Data']['key'], "content": data['Data']['content']}
            return {"success": False, "error": data.get("Data")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def login_tct(self):
        """BƯỚC 2: Đăng nhập TCT bằng data từ DB (invoice_settings — provider đang active)."""
        conn = None
        try:
            from Services.einvoice_registry import normalize_provider_code
            provider_key = normalize_provider_code(
                self.config.get('provider_name') or self.config.get('name') or 'matbao'
            )
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Ưu tiên đúng provider đang dùng; không hardcode matbao + is_active riêng
            cursor.execute("""
                SELECT tax_code, etax_password, etax_cvalue, etax_ckey
                FROM invoice_settings
                WHERE LOWER(TRIM(provider_name)) = ?
                LIMIT 1
            """, (provider_key,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("""
                    SELECT tax_code, etax_password, etax_cvalue, etax_ckey
                    FROM invoice_settings
                    WHERE is_active = 1
                    LIMIT 1
                """)
                row = cursor.fetchone()

            if not row:
                return {"success": False, "error": "Không tìm thấy cấu hình HĐĐT / eTax trong Settings"}

            payload = {
                "username": str(row['tax_code'] or '').strip(),
                "password": str(row['etax_password'] or '').strip(),
                "cvalue":   str(row['etax_cvalue'] or '').strip(),
                "ckey":     str(row['etax_ckey'] or '').strip(),
            }
            if not payload["username"] or not payload["password"]:
                return {
                    "success": False,
                    "error": "Thiếu MST hoặc mật khẩu eTax (CQT) trong Settings → HĐĐT",
                }

            url = f"{self.base_url}/hoa-don-dau-vao/login-tct"
            r = self.session.post(url, json=payload, headers=self._get_headers(), timeout=20, verify=False)
            r.raise_for_status()
            
            data = r.json()
            if data.get("Success"):
                logging.info(f"[{self.name}] Đăng nhập TCT thành công")
                return {"success": True, "message": data.get("Data")}
            
            return {"success": False, "error": data.get("Data")}

        except Exception as e:
            logging.error(f"[{self.name}] Lỗi Login TCT: {str(e)}")
            return {"success": False, "error": str(e)}
        finally:
            if conn: conn.close()

    def sync_invoices_by_month(self, month_str: str):
        """
        Đồng bộ hóa đơn theo tháng (Hàm chính)
        """
        try:
            # 1. Chuẩn bị thời gian
            dt = parser.parse(month_str.replace('/', '-'), fuzzy=True)
            from_date = dt.strftime("%Y-%m-01")
            next_m = dt + relativedelta(months=1)
            to_date = (next_m - timedelta(seconds=1)).strftime("%Y-%m-%d 23:59:59")

            # 2. Lấy Headers (Sẽ tự động gọi Auth nếu chưa có token)
            try:
                headers = self._get_headers()
            except Exception as auth_err:
                return {"success": False, "error": str(auth_err)}

            # 3. URL và Payload (Theo tài liệu hàm load-data-tct)
            url = f"{self.base_url}/hoa-don-dau-vao/load-data-tct"
            payload = {
                "comName": "",
                "comTaxCode": "",
                "no": 0,
                "fromDateYMD": from_date,
                "toDateYMD": to_date,
                "trangthai": -1,
                "loaihoadon": -1,
                "pattern": "",
                "serial": "",
                "typeDataPDF": 1
            }

            logging.info(f"[{self.name}] Syncing {month_str}...")

            # 4. Thực hiện request GET kèm BODY
            r = self.session.request(
                method="GET",
                url=url,
                json=payload,
                headers=headers,
                timeout=60,
                verify=False
            )

            # Xử lý trường hợp Token hết hạn bất ngờ (401)
            if r.status_code == 401:
                logging.warning(f"[{self.name}] Token hết hạn, đang thử lại...")
                self._bearer_token = None
                return self.sync_invoices_by_month(month_str)

            r.raise_for_status()
            resp = r.json()

            if resp.get("Success"):
                invoices = resp.get("Data") or []
                return {
                    "success": True,
                    "invoices": invoices,
                    "count": len(invoices)
                }
            
            return {"success": False, "error": resp.get("Data") or "API trả về lỗi Success=False"}

        except Exception as e:
            logging.error(f"[{self.name}] Lỗi sync_invoices_by_month: {str(e)}")
            return {"success": False, "error": str(e)}

# ====================== MAPPING DỮ LIỆU HÓA ĐƠN ĐẦU VÀO LƯU DB  ======================
def prepare_invoice_data(inv):
    """
    Map dữ liệu từ API Mắt Bão (inv) sang cấu trúc bảng supplier_invoice
    Dựa trên tài liệu Matbao Purchase Inv API.docx
    """
    try:
        # 1. Xử lý ngày hóa đơn (NLap thường là YYYY-MM-DDTHH:MM:SS)
        n_lap = inv.get('NLap') or ''
        invoice_date = n_lap[:10] if n_lap else datetime.now().strftime('%Y-%m-%d')
        entry_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # 2. Thông tin định danh hóa đơn
        serial = str(inv.get('KHHDon', '')).strip()      # Ký hiệu (ví dụ: 1C23TAA)
        invoice_no = str(inv.get('SHDon', '')).strip()   # Số hóa đơn (ví dụ: 123)
        seller_name = str(inv.get('NBanTen', '')).strip()
        seller_tax = str(inv.get('NBanMST', '')).strip()

        # 3. Hàm xử lý số thực an toàn
        def safe_float(val):
            if val is None or val == '': return 0.0
            try:
                # Xóa dấu phẩy nếu có (định dạng VN) và chuyển sang float
                clean_val = str(val).replace(',', '')
                return float(Decimal(clean_val))
            except:
                return 0.0

        # 4. Các trường số tiền (Theo tài liệu API)
        amount = safe_float(inv.get('TgTCThue', 0))          # Tổng tiền chưa thuế
        discount_pct = safe_float(inv.get('TLCKhau', 0))  # Tổng tiền chiết khấu
        discount_amount = safe_float(inv.get('STCKhau', 0))  # Tổng tiền chiết khấu
        tax_amount = safe_float(inv.get('TgTThue', 0))       # Tổng tiền thuế
        total = safe_float(inv.get('TgTTTBSo', 0))           # Tổng thanh toán bằng số
        pdf_url = inv.get('LinkDownloadPDF')
        address = inv.get('NBanDChi')

        # Tự tính % thuế nếu API không trả về trực tiếp
        tax_percent = 0.0
        if amount > 0:
            tax_percent = round((tax_amount / amount) * 100, 0)

        # Lưu toàn bộ JSON gốc để đối chiếu khi cần
        xml_data = json.dumps(inv, ensure_ascii=False)

        return (
            invoice_date,    # row[0]
            serial,          # row[1]
            invoice_no,      # row[2]
            seller_name,     # row[3]
            seller_tax,      # row[4]
            amount,          # row[5]
            discount_pct,    # row[6] discount_percent
            discount_amount, # row[7]
            tax_percent,     # row[8]
            tax_amount,      # row[9]
            total,           # row[10]
            'new',           # row[11] status
            xml_data,        # row[12]
            entry_date,      # row[13]
            pdf_url          # row[14]
        )
    except Exception as e:
        logging.error(f"Lỗi format dữ liệu hóa đơn: {str(e)}")
        return None

# ====================== LẤY CONFIG TỪ DB (theo Settings) ======================
def get_matbao_config():
    """
    Backward-compatible: trả config đồng bộ HĐ đầu vào theo provider đang active.
    (Trước đây hardcode provider_name='matbao'.)
    """
    from Services.invoice_config import get_purchase_sync_config
    return get_purchase_sync_config()


def _decode_inward_pdf_from_row(row):
    """Lấy bytes PDF từ DataPDFBase64 (ưu tiên) hoặc link đã lưu."""
    inv = {}
    raw_xml = row['xml_data'] if 'xml_data' in row.keys() else None
    if raw_xml:
        try:
            inv = json.loads(raw_xml)
        except (TypeError, ValueError, json.JSONDecodeError):
            inv = {}

    b64 = (inv.get('DataPDFBase64') or '').strip()
    if b64:
        try:
            pdf_data = base64.b64decode(b64, validate=False)
            if pdf_data.startswith(b'%PDF'):
                return pdf_data
        except (ValueError, TypeError):
            pass

    pdf_url = (row['pdf_url'] if 'pdf_url' in row.keys() else '') or ''
    pdf_url = str(pdf_url).strip()
    if pdf_url:
        try:
            r = requests.get(pdf_url, timeout=20, verify=False)
            if r.ok and r.content.startswith(b'%PDF'):
                return r.content
        except requests.RequestException as exc:
            logging.warning("Không tải được PDF từ link lưu: %s", exc)

    return None


def _inward_invoice_visible_for_branch(conn, inv: dict, branch_code: str) -> bool:
    """HĐ mới: hiện mọi CN. HĐ đã nhập/hạch toán: chỉ CN có PN liên kết."""
    if not inv.get('has_import') and not inv.get('has_accounted'):
        return True
    from Services.sme.branches import import_branch_filter_sql

    bf, bp = import_branch_filter_sql(conn, branch_code, alias='i')
    if not bf:
        return True
    inv_id = int(inv.get('id') or 0)
    invoice_no = (inv.get('invoice_no') or '').strip()
    row = conn.execute(
        f"""
        SELECT 1 FROM import i
        WHERE (
            i.from_invoice_id = ?
            OR (
                TRIM(COALESCE(i.bill_no, '')) != ''
                AND TRIM(i.bill_no) = ?
            )
        )
        {bf}
        LIMIT 1
        """,
        [inv_id, invoice_no, *bp],
    ).fetchone()
    return bool(row)


def register_inward_routes(app):
    """Đăng ký route hóa đơn đầu vào (giữ nguyên URL/endpoint)."""

    @app.route('/inward-invoice')
    @login_required
    def inward_invoice():
        # Lấy thông tin doanh nghiệp để hiển thị tiêu đề hoặc MST nếu cần
        conn = get_db_connection()
        info = conn.execute("SELECT * FROM business_info LIMIT 1").fetchone()
        conn.close()
        return render_template('KeToanHKD/inward_invoice.html', info=info)
    @app.route('/api/invoices/sync-gdt', methods=['POST'])
    def sync_gdt():
        """
        Đồng bộ HĐ đầu vào theo nhà cung cấp đang chọn ở Settings.
        """
        data = request.get_json(silent=True) or {}
        month_str = data.get('month') # Định dạng MM/YYYY

        if not month_str:
            return jsonify({"success": False, "error": "Vui lòng chọn tháng đồng bộ"}), 400

        conn = None
        try:
            from Services.invoice_config import get_purchase_sync_config
            from Services.einvoice_registry import normalize_provider_code, get_provider_meta

            config = get_purchase_sync_config()
            provider_key = normalize_provider_code(config.get('provider_name') or 'matbao')
            # Hiện chỉ Matbao có API hoa-don-dau-vao; registry đã chặn provider khác
            if provider_key != 'matbao':
                label = (get_provider_meta(provider_key) or {}).get('label') or provider_key
                return jsonify({
                    "success": False,
                    "error": (
                        f"Đồng bộ HĐ mua hàng chưa hỗ trợ {label}. "
                        "Vào Settings chọn Mắt Bão (hoặc provider có supports_purchase_sync)."
                    ),
                }), 400

            provider = MatbaoPurchaseProvider(config)

            # 2. Gọi API lấy danh sách
            result = provider.sync_invoices_by_month(month_str)

            if not result["success"]:
                return jsonify(result), 400

            invoices = result["invoices"]
        
            # 3. Lưu vào Database
            conn = get_db_connection()
            cursor = conn.cursor()
            new_count = 0
            skip_count = 0

            for inv in invoices:
                row_data = prepare_invoice_data(inv)
                if not row_data:
                    continue

                # Kiểm tra trùng lặp dựa trên MST người bán, Ký hiệu và Số hóa đơn
                cursor.execute("""
                    SELECT id FROM supplier_invoice 
                    WHERE seller_tax_code = ? AND serial = ? AND invoice_no = ?
                """, (row_data[4], row_data[1], row_data[2]))

                if cursor.fetchone():
                    skip_count += 1
                    continue

                # Insert vào bảng supplier_invoice
                cursor.execute("""
                    INSERT INTO supplier_invoice (
                        invoice_date, serial, invoice_no, seller_name, seller_tax_code,
                        amount, discount_percent, discount_amount, tax_percent, tax_amount,
                        total, status, xml_data, date, pdf_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, row_data)
                new_count += 1

            conn.commit()

            return jsonify({
                "success": True,
                "message": f"Đồng bộ thành công tháng {month_str}",
                "summary": {
                    "total_received": len(invoices),
                    "new_inserted": new_count,
                    "duplicates_skipped": skip_count
                }
            })

        except Exception as e:
            logging.exception("Lỗi trong quá trình đồng bộ hóa đơn")
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn: conn.close()

    from flask import jsonify, request
    import sqlite3

    @app.route('/api/invoices/inward/<int:invoice_id>/pdf')
    @login_required
    def get_inward_invoice_pdf(invoice_id):
        """Xem PDF hóa đơn đầu vào — ưu tiên DataPDFBase64 trong xml_data."""
        conn = None
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, xml_data, pdf_url FROM supplier_invoice WHERE id = ?",
                (invoice_id,),
            ).fetchone()
            if not row:
                return "Không tìm thấy hóa đơn", 404

            pdf_data = _decode_inward_pdf_from_row(row)
            if not pdf_data:
                return "Không tìm thấy nội dung PDF cho hóa đơn này", 404

            return Response(
                pdf_data,
                content_type='application/pdf',
                headers={'Content-Disposition': 'inline'},
            )
        except Exception as exc:
            logging.error("get_inward_invoice_pdf(%s): %s", invoice_id, exc)
            return "Lỗi khi tải PDF hóa đơn", 500
        finally:
            if conn:
                conn.close()

    @app.route('/api/invoices/inward', methods=['GET'])
    def get_inward_invoices():
        from_date = request.args.get('from_date')
        to_date = request.args.get('to_date')
        keyword = request.args.get('keyword', '').strip()
        status = request.args.get('status')  # new hoặc imported

        conn = None
        try:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            from Services.inward_invoice_helpers import ensure_import_service_schema
            ensure_import_service_schema(conn)
            conn.commit()

            # Query chính (giữ nguyên cấu trúc code cũ của bạn)
            query = """
                SELECT
                    si.*,
                    CASE WHEN EXISTS (
                        SELECT 1 FROM import i_stock
                        WHERE TRIM(COALESCE(i_stock.bill_no, '')) = TRIM(COALESCE(si.invoice_no, ''))
                          AND COALESCE(i_stock.doc_type, 'stock') NOT IN ('service', 'landed_cost')
                    ) THEN 1 ELSE 0 END AS has_import,
                    CASE WHEN si.status = 'accounted' OR EXISTS (
                        SELECT 1 FROM import i_svc
                        WHERE i_svc.from_invoice_id = si.id
                           OR (
                               TRIM(COALESCE(i_svc.bill_no, '')) = TRIM(COALESCE(si.invoice_no, ''))
                               AND COALESCE(i_svc.doc_type, '') IN ('service', 'landed_cost')
                           )
                    ) THEN 1 ELSE 0 END AS has_accounted
                FROM supplier_invoice si
                WHERE 1=1
            """
            params = []

            if keyword:
                query += " AND (si.seller_name LIKE ? OR si.seller_tax_code LIKE ? OR si.invoice_no LIKE ?)"
                params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])

            # Lọc theo ngày chứng từ (invoice_date) hoặc ngày ghi nhận (date)
            if from_date:
                query += " AND date(COALESCE(NULLIF(TRIM(si.invoice_date), ''), si.date)) >= date(?)"
                params.append(from_date)
            if to_date:
                query += " AND date(COALESCE(NULLIF(TRIM(si.invoice_date), ''), si.date)) <= date(?)"
                params.append(to_date)

            query += " ORDER BY date(COALESCE(NULLIF(TRIM(si.invoice_date), ''), si.date)) DESC, si.id DESC"

            c.execute(query, params)
            rows = c.fetchall()

            result = []
            # Bảng landed cost chỉ có trên tenant SME đã dùng tính năng
            has_lcd_table = bool(c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sme_landed_cost_docs'"
            ).fetchone())
            landed_ids = set()
            if has_lcd_table:
                for r in c.execute(
                    """
                    SELECT cost_invoice_id FROM sme_landed_cost_docs
                    WHERE COALESCE(status, 'posted') = 'posted'
                    """
                ).fetchall():
                    if r[0] is not None:
                        landed_ids.add(int(r[0]))

            for row in rows:
                inv = dict(row)
                inv['has_hach_toan'] = bool(inv.get('has_accounted'))
                inv['has_landed_cost'] = 1 if int(inv.get('id') or 0) in landed_ids else 0
                inv['is_manual'] = 0
                inv['is_foreign'] = 0
                raw = inv.get('xml_data') or ''
                if isinstance(raw, str) and raw.strip().startswith('{'):
                    try:
                        meta = json.loads(raw)
                        if str(meta.get('SourceType') or '').lower() == 'manual':
                            inv['is_manual'] = 1
                        if meta.get('IsForeign') or str(inv.get('serial') or '').upper() in (
                            'NGOAI', 'FOREIGN', 'MANUAL',
                        ):
                            inv['is_foreign'] = 1
                        inv['cost_category'] = meta.get('CostCategory') or None
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                result.append(inv)

            # SME multi-branch: HĐ đã nhập/hạch toán chỉ hiện nếu PN thuộc CN;
            # HĐ mới (chưa nhập) vẫn hiện toàn DN để các CN nhận về.
            try:
                from Services.sme.branches import active_report_branch_filter
                br = active_report_branch_filter()
                if br is not None and str(br).strip().upper() not in ('', 'ALL'):
                    result = [
                        inv for inv in result
                        if _inward_invoice_visible_for_branch(conn, inv, br)
                    ]
            except Exception:
                logging.exception('inward branch filter')

            if status == 'imported':
                result = [inv for inv in result if inv.get('has_import') == 1]
            elif status == 'hach_toan':
                result = [inv for inv in result if inv.get('has_accounted') == 1]
            elif status == 'new':
                result = [
                    inv for inv in result
                    if inv.get('has_import') == 0 and inv.get('has_accounted') == 0
                ]

            return jsonify({
                "success": True,
                "data": result,
                "total": len(result)
            })

        except sqlite3.Error as db_err:
            import traceback
            print("Lỗi database trong /api/invoices/inward:")
            print(traceback.format_exc())
            return jsonify({
                "success": False,
                "error": f"Lỗi cơ sở dữ liệu: {str(db_err)}"
            }), 500

        except Exception as e:
            import traceback
            print("Lỗi server trong /api/invoices/inward:")
            print(traceback.format_exc())
            return jsonify({
                "success": False,
                "error": str(e)
            }), 500

        finally:
            if conn:
                conn.close()
