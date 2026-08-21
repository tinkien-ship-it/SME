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
from Services.purchase_invoice_sync import (
    SOURCE_BOTH,
    SOURCE_PORTAL,
    SOURCE_TCT,
    MatbaoPurchaseProvider,
    prepare_invoice_data,
    sync_month_to_db,
)

logger = logging.getLogger(__name__)

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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

    @app.route('/api/invoices/purchase/captcha', methods=['GET'])
    @login_required
    def purchase_invoice_captcha():
        """Lấy captcha CQT qua proxy Mắt Bảo."""
        try:
            from Services.invoice_config import get_purchase_sync_config
            from Services.einvoice_registry import normalize_provider_code

            config = get_purchase_sync_config()
            if normalize_provider_code(config.get('provider_name') or '') != 'matbao':
                return jsonify({'success': False, 'error': 'Chỉ hỗ trợ Mắt Bão'}), 400
            provider = MatbaoPurchaseProvider(config)
            return jsonify(provider.get_tct_captcha())
        except Exception as exc:
            logging.exception('purchase captcha')
            return jsonify({'success': False, 'error': str(exc)}), 500

    @app.route('/api/invoices/purchase/login-tct', methods=['POST'])
    @login_required
    def purchase_invoice_login_tct():
        """Đăng nhập cổng CQT (sau khi user nhập captcha)."""
        data = request.get_json(silent=True) or {}
        try:
            from Services.invoice_config import get_purchase_sync_config
            from Services.einvoice_registry import normalize_provider_code

            config = get_purchase_sync_config()
            if normalize_provider_code(config.get('provider_name') or '') != 'matbao':
                return jsonify({'success': False, 'error': 'Chỉ hỗ trợ Mắt Bão'}), 400
            provider = MatbaoPurchaseProvider(config)
            result = provider.login_tct(
                cvalue=data.get('cvalue'),
                ckey=data.get('ckey') or data.get('key'),
                password=data.get('password'),
                username=data.get('username'),
                persist_captcha=True,
            )
            status = 200 if result.get('success') else 400
            return jsonify(result), status
        except Exception as exc:
            logging.exception('purchase login-tct')
            return jsonify({'success': False, 'error': str(exc)}), 500

    @app.route('/api/invoices/sync-gdt', methods=['POST'])
    @login_required
    def sync_gdt():
        """
        Đồng bộ HĐ đầu vào.
        Body: { month: 'MM/YYYY', source: 'portal'|'tct'|'both',
                login: bool, cvalue, ckey, password? }
        """
        data = request.get_json(silent=True) or {}
        month_str = data.get('month')
        source = (data.get('source') or SOURCE_PORTAL).strip().lower()
        if source not in (SOURCE_PORTAL, SOURCE_TCT, SOURCE_BOTH):
            source = SOURCE_PORTAL

        if not month_str:
            return jsonify({"success": False, "error": "Vui lòng chọn tháng đồng bộ"}), 400

        conn = None
        try:
            from Services.invoice_config import get_purchase_sync_config
            from Services.einvoice_registry import normalize_provider_code, get_provider_meta

            config = get_purchase_sync_config()
            provider_key = normalize_provider_code(config.get('provider_name') or 'matbao')
            if provider_key != 'matbao':
                label = (get_provider_meta(provider_key) or {}).get('label') or provider_key
                return jsonify({
                    "success": False,
                    "error": (
                        f"Đồng bộ HĐ mua hàng chưa hỗ trợ {label}. "
                        "Vào Settings chọn Mắt Bão."
                    ),
                }), 400

            login_first = False
            captcha = None
            if source in (SOURCE_TCT, SOURCE_BOTH):
                cvalue = str(data.get('cvalue') or '').strip()
                ckey = str(data.get('ckey') or data.get('key') or '').strip()
                want_login = bool(data.get('login')) or bool(cvalue and ckey)
                if want_login:
                    if not cvalue or not ckey:
                        return jsonify({
                            'success': False,
                            'need_captcha': True,
                            'error': 'Vui lòng lấy và nhập captcha CQT trước khi đồng bộ cổng thuế',
                        }), 400
                    login_first = True
                    captcha = {
                        'cvalue': cvalue,
                        'ckey': ckey,
                        'password': data.get('password'),
                        'username': data.get('username'),
                    }

            conn = get_db_connection()
            result = sync_month_to_db(
                conn,
                config,
                month_str,
                source=source,
                login_first=login_first,
                captcha=captcha,
            )
            if not result.get('success'):
                status = 400
                if result.get('need_captcha') or result.get('need_login'):
                    status = 401
                return jsonify(result), status

            summary = result.get('summary') or {}
            return jsonify({
                "success": True,
                "message": result.get('message') or f"Đồng bộ thành công tháng {month_str}",
                "count": summary.get('new_inserted', 0),
                "summary": summary,
                "sources_ok": result.get('sources_ok') or [],
                "warnings": result.get('warnings') or [],
            })

        except Exception as e:
            logging.exception("Lỗi trong quá trình đồng bộ hóa đơn")
            return jsonify({"success": False, "error": str(e)}), 500
        finally:
            if conn:
                conn.close()

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
