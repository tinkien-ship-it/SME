"""Adapter xuất HĐĐT — giao diện thống nhất cho mọi nhà cung cấp."""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape

import requests
from requests.auth import HTTPBasicAuth

from Services.einvoice_registry import get_provider_meta, normalize_provider_code
from Services.invoice_buyer import (
    DEFAULT_RETAIL_BUYER_NAME,
    is_retail_buyer_name,
    normalize_retail_buyer_name,
    resolve_vnpt_buyer_fields,
)
from Services.invoice_xml import get_seller_from_db, normalize_invoice_config

logger = logging.getLogger(__name__)


def normalize_loai_hdon(value, default=1):
    """
    Chuẩn hóa LoaiHDon theo quy ước Mắt Bão / SME: 0=nháp, 1=chính thức.
    Không dùng `or 1` vì 0 là giá trị hợp lệ.
    VNPT Invoice dùng tham số `type` khác (0=chính thức, 1=thay thế) — xem VNPTInvoiceAdapter.
    """
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return 0 if parsed == 0 else 1


class BaseEInvoiceAdapter:
    provider_key = 'base'
    supports_draft = False
    supports_portal_sync = False

    def __init__(self, config):
        self.config = normalize_invoice_config(config or {})
        self.provider_key = normalize_provider_code(
            self.config.get('provider_name') or self.config.get('provider') or self.provider_key
        )

    def issue(self, sale_data, items, loai_hdon=1, replace_unpublished=False):
        raise NotImplementedError

    def sign_draft(self, invoice_id):
        return {'success': False, 'error': 'Provider không hỗ trợ ký/phát hành hóa đơn nháp.'}

    def issue_replacement(self, sale_data, items, replacement_info):
        return {
            'success': False,
            'error': f'{self.provider_key}: chưa hỗ trợ xuất hóa đơn thay thế.',
        }


class MatbaoAdapterWrapper(BaseEInvoiceAdapter):
    supports_draft = True
    supports_portal_sync = True

    def __init__(self, config, matbao_cls):
        super().__init__(config)
        self.provider_key = 'matbao'
        self._inner = matbao_cls(config)

    def issue(self, sale_data, items, loai_hdon=1, replace_unpublished=False):
        return self._inner.issue(
            sale_data, items, loai_hdon=loai_hdon, replace_unpublished=replace_unpublished,
        )

    def sign_draft(self, invoice_id):
        return self._inner.sign_draft(invoice_id)


class PendingProviderAdapter(BaseEInvoiceAdapter):
    """Provider chưa có tài liệu API đầy đủ — báo lỗi rõ ràng, không trả số HĐ giả."""

    def issue(self, sale_data, items, loai_hdon=1, replace_unpublished=False):
        meta = get_provider_meta(self.provider_key) or {}
        label = meta.get('label', self.provider_key)
        hint = meta.get('doc_hint', 'Liên hệ nhà cung cấp để nhận tài liệu API.')
        return {
            'success': False,
            'error': (
                f"{label}: chưa tích hợp xuất HĐ tự động trong phiên bản này. "
                f"{hint} Gửi tài liệu cho KETO để bật provider này."
            ),
            'provider': self.provider_key,
            'integration_status': meta.get('status', 'planned'),
        }


class MisaInvoiceAdapter(BaseEInvoiceAdapter):
    """
    MISA meInvoice — theo tài liệu https://doc.meinvoice.vn/

    Luồng chính thức: auth/token → createinvoice → SignXML (local) → invoicepublishing
    """
    supports_draft = True

    DEFAULT_API_V3 = 'https://testapi.meinvoice.vn/api/v3'
    DEFAULT_SIGN_URL = 'http://127.0.0.1:12019'

    def __init__(self, config):
        super().__init__(config)
        self.provider_key = 'misa'
        self.tax_code = (self.config.get('tax_code') or '').strip()
        self.username = (self.config.get('username') or '').strip()
        self.password = (self.config.get('password') or '').strip()
        self.app_id = (
            self.config.get('app_id')
            or self.config.get('api_key')
            or self.config.get('client_id')
            or os.environ.get('MISA_API_KEY', '')
        ).strip()
        self.inv_series = (self.config.get('invoice_series') or '1C26TAA').strip()
        self.invoice_name = (self.config.get('invoice_type') or 'Hóa đơn giá trị gia tăng').strip()
        self.has_code = str(self.config.get('misa_has_code') or '').lower() in ('1', 'true', 'yes')
        self.api_base = self._resolve_api_base()
        self.sign_service_url = (
            self.config.get('sign_service_url')
            or self.config.get('esign_api_url')
            or os.environ.get('MISA_SIGN_SERVICE_URL', self.DEFAULT_SIGN_URL)
        ).strip().rstrip('/')
        self.token = None
        self._base_headers = {'Content-Type': 'application/json'}

    def _resolve_api_base(self):
        raw = (
            self.config.get('api_url')
            or os.environ.get('MISA_API_URL')
            or self.DEFAULT_API_V3
        ).strip().rstrip('/')

        lower = raw.lower()
        if '/api/v3' in lower:
            idx = lower.index('/api/v3')
            return raw[: idx + len('/api/v3')]
        if '/api/integration' in lower:
            idx = lower.index('/api/integration')
            return raw[: idx + len('/api/integration')]
        if raw.startswith('http'):
            return f'{raw}/api/v3'
        return self.DEFAULT_API_V3

    def _api_path(self, no_code_path, code_path):
        return code_path if self.has_code else no_code_path

    def _request_headers(self):
        headers = dict(self._base_headers)
        headers['CompanyTaxCode'] = self.tax_code
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        return headers

    def _parse_misa_response(self, resp):
        try:
            body = resp.json()
        except ValueError:
            return {'success': False, 'error': f'MISA: phản hồi không phải JSON — {resp.text[:300]}'}

        success = body.get('Success')
        if success is None:
            success = body.get('success')
        if not success:
            err = body.get('ErrorCode') or body.get('errorCode')
            errors = body.get('Errors') or body.get('errors') or body.get('descriptionErrorCode')
            return {
                'success': False,
                'error': f"MISA: {err or errors or resp.text[:300]}",
                'error_code': err,
            }

        data = body.get('Data')
        if data is None:
            data = body.get('data')
        if isinstance(data, str) and data.strip():
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                pass
        return {'success': True, 'data': data, 'raw': body}

    def _get_token(self, force=False):
        if self.token and not force:
            return self.token
        if not self.app_id or not self.tax_code or not self.username or not self.password:
            logger.error('MISA: thiếu appid/taxcode/username/password')
            return None

        url = f'{self.api_base}/auth/token'
        payload = {
            'appid': self.app_id,
            'taxcode': self.tax_code,
            'username': self.username,
            'password': self.password,
        }
        try:
            resp = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=20)
            parsed = self._parse_misa_response(resp)
            if not parsed.get('success'):
                if parsed.get('error_code') == 'TokenExpiredCode':
                    return self._refresh_token()
                logger.error('MISA get_token: %s', parsed.get('error'))
                return None
            self.token = parsed.get('data')
            return self.token
        except Exception as exc:
            logger.exception('MISA get_token: %s', exc)
            return None

    def _refresh_token(self):
        if not self.token:
            return self._get_token(force=True)
        url = f'{self.api_base}/auth/refreshtoken'
        try:
            resp = requests.post(
                url,
                json=self.token,
                headers={'Content-Type': 'application/json'},
                timeout=20,
            )
            parsed = self._parse_misa_response(resp)
            if parsed.get('success'):
                self.token = parsed.get('data')
            return self.token
        except Exception as exc:
            logger.exception('MISA refresh token: %s', exc)
            return None

    def _vat_rate_name(self, tax_pct):
        pct = float(tax_pct or 0)
        if pct <= 0:
            return '0%'
        if pct == 5:
            return '5%'
        if pct == 8:
            return '8%'
        if pct == 10:
            return '10%'
        return f'{int(pct)}%'

    def _amount_in_words(self, amount):
        try:
            from helpers import so_thanh_chu
            return so_thanh_chu(int(round(float(amount))))
        except Exception:
            return str(int(round(float(amount))))

    def _build_original_invoice_data(self, sale_data, items, ref_id):
        details = []
        tax_groups = {}
        total_sale = 0.0
        total_without_vat = 0.0
        total_vat = 0.0
        total_discount = 0.0

        for idx, item in enumerate(items, start=1):
            qty = float(item.get('quantity') or 0)
            price = float(item.get('price') or 0)
            tax_pct = float(item.get('tax_pct') or 0)
            discount_pct = float(item.get('discount_pct') or 0)
            amount_before = round(qty * price, 2)
            discount_amount = round(amount_before * discount_pct / 100.0, 2)
            amount_without_vat = round(amount_before - discount_amount, 2)
            vat_amount = round(amount_without_vat * tax_pct / 100.0, 2)
            vat_name = self._vat_rate_name(tax_pct)

            details.append({
                'ItemType': 1,
                'LineNumber': idx,
                'SortOrder': idx,
                'ItemCode': str(item.get('product_code') or ''),
                'ItemName': str(item.get('name') or item.get('product_name') or ''),
                'UnitName': str(item.get('unit') or 'Cái'),
                'Quantity': qty,
                'UnitPrice': price,
                'DiscountRate': discount_pct,
                'DiscountAmountOC': discount_amount,
                'DiscountAmount': discount_amount,
                'AmountOC': amount_before,
                'Amount': amount_before,
                'AmountWithoutVATOC': amount_without_vat,
                'AmountWithoutVAT': amount_without_vat,
                'VATRateName': vat_name,
                'VATAmountOC': vat_amount,
                'VATAmount': vat_amount,
            })

            total_sale += amount_before
            total_without_vat += amount_without_vat
            total_vat += vat_amount
            total_discount += discount_amount
            grp = tax_groups.setdefault(vat_name, {'AmountWithoutVATOC': 0.0, 'VATAmountOC': 0.0})
            grp['AmountWithoutVATOC'] += amount_without_vat
            grp['VATAmountOC'] += vat_amount

        total_amount = round(total_without_vat + total_vat, 2)
        now_iso = datetime.now().astimezone().isoformat()

        return {
            'RefID': ref_id,
            'InvSeries': self.inv_series,
            'InvoiceName': self.invoice_name,
            'InvDate': now_iso,
            'CurrencyCode': 'VND',
            'ExchangeRate': 1.0,
            'PaymentMethodName': str(sale_data.get('payment_method') or 'TM/CK'),
            'BuyerLegalName': str(sale_data.get('company_name') or sale_data.get('customer_name') or DEFAULT_RETAIL_BUYER_NAME),
            'BuyerTaxCode': str(sale_data.get('tax_code') or ''),
            'BuyerAddress': str(sale_data.get('address') or ''),
            'BuyerCode': str(sale_data.get('customer_code') or ''),
            'BuyerPhoneNumber': str(sale_data.get('phone') or ''),
            'BuyerEmail': str(sale_data.get('email') or ''),
            'BuyerFullName': normalize_retail_buyer_name(sale_data.get('customer_name')),
            'ReferenceType': None,
            'OrgInvoiceType': None,
            'OrgInvTemplateNo': None,
            'OrgInvSeries': None,
            'OrgInvNo': None,
            'OrgInvDate': None,
            'TotalSaleAmountOC': round(total_sale, 2),
            'TotalAmountWithoutVATOC': round(total_without_vat, 2),
            'TotalVATAmountOC': round(total_vat, 2),
            'TotalDiscountAmountOC': round(total_discount, 2),
            'TotalAmountOC': total_amount,
            'TotalSaleAmount': round(total_sale, 2),
            'TotalAmountWithoutVAT': round(total_without_vat, 2),
            'TotalVATAmount': round(total_vat, 2),
            'TotalDiscountAmount': round(total_discount, 2),
            'TotalAmount': total_amount,
            'TotalAmountInWords': self._amount_in_words(total_amount),
            'OriginalInvoiceDetail': details,
            'TaxRateInfo': [
                {
                    'VATRateName': name,
                    'AmountWithoutVATOC': round(vals['AmountWithoutVATOC'], 2),
                    'VATAmountOC': round(vals['VATAmountOC'], 2),
                }
                for name, vals in tax_groups.items()
            ],
            'OptionUserDefined': {
                'MainCurrency': 'VND',
                'AmountDecimalDigits': '0',
                'AmountOCDecimalDigits': '2',
                'UnitPriceOCDecimalDigits': '0',
                'UnitPriceDecimalDigits': '0',
                'QuantityDecimalDigits': '2',
                'CoefficientDecimalDigits': '2',
                'ExchangRateDecimalDigits': '0',
            },
        }

    def _create_invoice(self, sale_data, items, ref_id):
        path = self._api_path(
            '/itg/invoicepublishing/createinvoice',
            '/code/itg/invoicepublishing/createinvoice',
        )
        url = f'{self.api_base}{path}'
        payload = [self._build_original_invoice_data(sale_data, items, ref_id)]
        resp = requests.post(url, json=payload, headers=self._request_headers(), timeout=45)
        parsed = self._parse_misa_response(resp)
        if not parsed.get('success'):
            return parsed

        rows = parsed.get('data')
        if isinstance(rows, list) and rows:
            row = rows[0]
        elif isinstance(rows, dict):
            row = rows
        else:
            return {'success': False, 'error': 'MISA: createinvoice không trả về dữ liệu hóa đơn.'}

        if row.get('ErrorCode'):
            return {'success': False, 'error': f"MISA createinvoice: {row.get('ErrorCode')}"}
        return {'success': True, 'row': row}

    def _sign_invoice_xml(self, xml_content):
        pin = (self.config.get('esign_pin') or self.config.get('pin') or '').strip()
        if not pin:
            return {
                'success': False,
                'error': (
                    'MISA: thiếu PIN chữ ký số (esign_pin). '
                    'Cài MISA SignedService và cấu hình sign_service_url (mặc định http://127.0.0.1:12019).'
                ),
            }
        url = f'{self.sign_service_url}/api/SignXML'
        headers = {
            'Content-Type': 'application/json',
            'MisaTokenKey': self.config.get('misa_token_key') or '491CB943-E466-4D25-B0A9-7042594F59F2',
        }
        payload = {'PinCode': pin, 'XmlContent': xml_content}
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            if resp.status_code != 200:
                return {'success': False, 'error': f'MISA SignXML HTTP {resp.status_code}: {resp.text[:300]}'}
            body = resp.json()
            if body.get('Status') != 200 and not body.get('Payload'):
                return {'success': False, 'error': f"MISA SignXML: {body.get('Message') or body}"}
            return {'success': True, 'signed_xml': body.get('Payload') or body.get('PayLoad')}
        except requests.RequestException as exc:
            return {
                'success': False,
                'error': (
                    f'MISA: không kết nối được SignService tại {self.sign_service_url} — {exc}. '
                    'Tải MISA_SignedService_Setup từ doc.meinvoice.vn.'
                ),
            }

    def _publish_invoice(self, row, signed_xml):
        path = self._api_path('/itg/invoicepublishing', '/code/itg/invoicepublishing')
        url = f'{self.api_base}{path}'
        payload = [{
            'RefID': row.get('RefID'),
            'TransactionID': row.get('TransactionID'),
            'InvoiceData': signed_xml,
            'IsSendEmail': bool(row.get('ReceiverEmail') or row.get('BuyerEmail')),
            'ReceiverEmail': row.get('ReceiverEmail') or row.get('BuyerEmail') or '',
            'ReceiverName': row.get('ReceiverName') or row.get('BuyerFullName') or '',
            'IsInvoiceSummary': False,
        }]
        resp = requests.post(url, json=payload, headers=self._request_headers(), timeout=45)
        parsed = self._parse_misa_response(resp)
        if not parsed.get('success'):
            return parsed

        rows = parsed.get('data')
        if isinstance(rows, list) and rows:
            pub = rows[0]
        elif isinstance(rows, dict):
            pub = rows
        else:
            pub = {}

        err = pub.get('ErrorCode')
        if err:
            return {'success': False, 'error': f'MISA publish: {err}'}

        return {
            'success': True,
            'invoice_no': pub.get('InvNo') or pub.get('InvoiceNumber') or row.get('InvNo'),
            'invoice_id': pub.get('TransactionID') or row.get('TransactionID') or row.get('RefID'),
            'transaction_id': pub.get('TransactionID') or row.get('TransactionID'),
            'pdf_url': pub.get('PdfUrl') or pub.get('DownloadUrl'),
            'xml_url': pub.get('XmlUrl'),
        }

    def issue(self, sale_data, items, loai_hdon=1, replace_unpublished=False):
        if not self._get_token():
            return {
                'success': False,
                'error': (
                    'MISA: không lấy được token — kiểm tra App ID (api_key), MST, username, password '
                    'và API URL (test: https://testapi.meinvoice.vn/api/v3). Xem doc.meinvoice.vn.'
                ),
            }

        ref_id = str(sale_data.get('invoice_id') or uuid.uuid4())
        if replace_unpublished and sale_data.get('invoice_id'):
            ref_id = str(sale_data.get('invoice_id'))

        try:
            created = self._create_invoice(sale_data, items, ref_id)
            if not created.get('success'):
                return created
            row = created['row']

            if normalize_loai_hdon(loai_hdon) == 0:
                return {
                    'success': True,
                    'is_draft': True,
                    'invoice_id': row.get('RefID') or ref_id,
                    'invoice_no': '0',
                    'transaction_id': row.get('TransactionID'),
                    'xml_content': row.get('InvoiceData'),
                }

            xml_content = row.get('InvoiceData')
            if not xml_content:
                return {'success': False, 'error': 'MISA: createinvoice không trả về InvoiceData (XML).'}

            signed = self._sign_invoice_xml(xml_content)
            if not signed.get('success'):
                return signed

            published = self._publish_invoice(row, signed['signed_xml'])
            if published.get('success'):
                published['is_draft'] = False
            return published
        except Exception as exc:
            logger.exception('MISA issue: %s', exc)
            return {'success': False, 'error': f'MISA: {exc}'}

    def sign_draft(self, invoice_id):
        if not invoice_id:
            return {'success': False, 'error': 'MISA: thiếu RefID hóa đơn nháp.'}
        return {
            'success': False,
            'error': (
                'MISA: phát hành nháp cần XML từ bước createinvoice. '
                'Xuất lại HĐ với LoaiHDon=1 hoặc lưu xml_content khi tạo nháp.'
            ),
        }


class ViettelInvoiceAdapter(BaseEInvoiceAdapter):
    supports_draft = False

    def __init__(self, config):
        super().__init__(config)
        self.provider_key = 'viettel'
        self.api_url = (self.config.get('api_url') or 'https://sinvoice.viettel.vn:8443/InvoiceAPI/InvoiceWS').rstrip('/')
        self.username = self.config.get('username') or ''
        self.password = self.config.get('password') or ''
        self.tax_code = self.config.get('tax_code') or ''
        self.template_code = self.config.get('invoice_type') or '2/001'
        self.invoice_series = self.config.get('invoice_series') or 'C25TAA'

    def _auth(self):
        return HTTPBasicAuth(self.username, self.password)

    def _get_next_number(self):
        url = f"{self.api_url}/getNextInvoiceNumber/{self.tax_code}"
        root = ET.Element('invoiceRequest')
        ET.SubElement(root, 'templateCode').text = self.template_code
        ET.SubElement(root, 'invoiceSeries').text = self.invoice_series
        xml_payload = ET.tostring(root, encoding='utf-8', method='xml')
        resp = requests.post(
            url, data=xml_payload, headers={'Content-Type': 'application/xml'},
            auth=self._auth(), verify=False, timeout=25,
        )
        if resp.status_code != 200:
            raise ValueError(resp.text[:500])
        root = ET.fromstring(resp.text)
        node = root.find('.//invoiceNo')
        if node is None or not node.text:
            raise ValueError('Viettel: không lấy được invoiceNo')
        return node.text

    def _build_xml(self, sale_data, items, invoice_no):
        seller_name = self.config.get('business_name') or 'DOANH NGHIEP'
        seller_tax = self.tax_code
        root = ET.Element('commonInvoiceInput')
        gen = ET.SubElement(root, 'generalInvoiceInfo')
        ET.SubElement(gen, 'invoiceType').text = '1'
        ET.SubElement(gen, 'templateCode').text = self.template_code
        ET.SubElement(gen, 'invoiceSeries').text = self.invoice_series
        ET.SubElement(gen, 'invoiceNo').text = str(invoice_no)
        ET.SubElement(gen, 'invoiceIssuedDate').text = str(int(time.time() * 1000))
        ET.SubElement(gen, 'currencyCode').text = 'VND'
        ET.SubElement(gen, 'paymentStatus').text = 'true'

        buyer = ET.SubElement(root, 'buyerInfo')
        ET.SubElement(buyer, 'buyerName').text = normalize_retail_buyer_name(sale_data.get('customer_name'))
        ET.SubElement(buyer, 'buyerAddressLine').text = sale_data.get('address') or ''
        ET.SubElement(buyer, 'buyerTaxCode').text = sale_data.get('tax_code') or ''

        seller = ET.SubElement(root, 'sellerInfo')
        ET.SubElement(seller, 'sellerLegalName').text = seller_name
        ET.SubElement(seller, 'sellerTaxCode').text = seller_tax

        subtotal = 0.0
        tax_total = 0.0
        for idx, item in enumerate(items, start=1):
            qty = float(item.get('quantity') or 0)
            price = float(item.get('price') or 0)
            tax_pct = float(item.get('tax_pct') or 0)
            line = qty * price
            tax_val = line * tax_pct / 100.0
            subtotal += line
            tax_total += tax_val
            row = ET.SubElement(root, 'itemInfo')
            ET.SubElement(row, 'lineNumber').text = str(idx)
            ET.SubElement(row, 'itemName').text = item.get('name') or item.get('product_name') or ''
            ET.SubElement(row, 'unitName').text = item.get('unit') or 'Cái'
            ET.SubElement(row, 'unitPrice').text = str(price)
            ET.SubElement(row, 'quantity').text = str(qty)
            ET.SubElement(row, 'itemTotalAmountWithoutTax').text = str(round(line, 2))
            ET.SubElement(row, 'taxPercentage').text = str(int(tax_pct))
            ET.SubElement(row, 'taxAmount').text = str(round(tax_val, 2))

        summary = ET.SubElement(root, 'summarizeInfo')
        ET.SubElement(summary, 'totalAmountWithoutTax').text = str(round(subtotal, 2))
        ET.SubElement(summary, 'totalTaxAmount').text = str(round(tax_total, 2))
        ET.SubElement(summary, 'totalAmountWithTax').text = str(round(subtotal + tax_total, 2))
        return ET.tostring(root, encoding='utf-8', method='xml')

    def issue(self, sale_data, items, loai_hdon=1, replace_unpublished=False):
        if normalize_loai_hdon(loai_hdon) == 0:
            return {
                'success': False,
                'error': 'Viettel S-Invoice: chế độ nháp (LoaiHDon=0) chưa được cấu hình — dùng Mắt Bão hoặc liên hệ hỗ trợ.',
            }
        if not self.username or not self.password or not self.tax_code:
            return {'success': False, 'error': 'Viettel: thiếu username, password hoặc mã số thuế trong Cài đặt HĐĐT.'}
        try:
            invoice_no = self._get_next_number()
            xml_payload = self._build_xml(sale_data, items, invoice_no)
            url = f"{self.api_url}/createInvoice/{self.tax_code}"
            resp = requests.post(
                url, data=xml_payload,
                headers={'Content-Type': 'application/xml', 'Accept': 'application/xml'},
                auth=self._auth(), verify=False, timeout=40,
            )
            if resp.status_code != 200:
                return {'success': False, 'error': f'Viettel HTTP {resp.status_code}: {resp.text[:300]}'}
            root = ET.fromstring(resp.text)
            result = root.find('.//result')
            if result is None:
                return {'success': False, 'error': f'Viettel: phản hồi không hợp lệ — {resp.text[:300]}'}
            inv_no_node = result.find('invoiceNo')
            reservation = result.find('reservationCode')
            inv_no = inv_no_node.text if inv_no_node is not None else str(invoice_no)
            return {
                'success': True,
                'is_draft': False,
                'invoice_no': inv_no,
                'invoice_id': reservation.text if reservation is not None else inv_no,
                'tax_authority_status': 'Chờ phản hồi CQT',
            }
        except Exception as exc:
            logger.exception('Viettel issue: %s', exc)
            return {'success': False, 'error': f'Viettel: {exc}'}


class EasyInvoiceInvoiceAdapter(BaseEInvoiceAdapter):
    """
    EasyInvoice (Softdreams) — REST importInvoice.

    Tài liệu tham chiếu:
    - REST: https://api.easyinvoice.vn/api/publish/importInvoice (domain chung từ 01/2026)
    - Auth: signature:nonce:timestamp:username:password:taxCode (HMAC partner key)
    - DLL/COM (Windows): dùng khi tích hợp local — adapter này dùng REST cho server Python.
    """
    supports_draft = True

    DEFAULT_IMPORT_URL = 'https://api.easyinvoice.vn/api/publish/importInvoice'

    def __init__(self, config):
        super().__init__(config)
        self.provider_key = 'easyinvoice'
        self.username = (self.config.get('username') or '').strip()
        self.password = (self.config.get('password') or '').strip()
        self.tax_code = (self.config.get('tax_code') or '').strip()
        self.pattern = (self.config.get('invoice_type') or '1/001').strip()
        self.serial = (self.config.get('invoice_series') or 'C26TAA').strip()
        self.partner_key = (
            self.config.get('app_secret')
            or self.config.get('client_secret')
            or self.config.get('api_key')
            or os.environ.get('EASYINVOICE_PARTNER_KEY', '')
        ).strip()
        self.account = (self.config.get('api_key') or self.config.get('client_id') or self.username).strip()
        self.account_pass = (self.config.get('app_secret') or self.password).strip()

    def _resolve_import_url(self):
        raw = (
            self.config.get('api_url')
            or os.environ.get('EASYINVOICE_API_URL')
            or self.DEFAULT_IMPORT_URL
        ).strip().rstrip('/')

        if 'importinvoice' in raw.lower():
            return raw

        if raw.endswith('/v1/publish') or raw.endswith('/api/publish'):
            return f'{raw}/importInvoice'

        if raw.startswith('http'):
            return f'{raw}/api/publish/importInvoice'

        mst = re.sub(r'[^0-9]', '', self.tax_code)
        if mst:
            return f'https://{mst}.easyinvoice.com.vn/api/publish/importInvoice'
        return self.DEFAULT_IMPORT_URL

    def _compute_signature(self, nonce, timestamp):
        sign_raw = f'{nonce}:{timestamp}:{self.username}:{self.password}:{self.tax_code}'
        algo = (self.config.get('signature_algo') or 'sha256').strip().lower()
        if algo == 'md5':
            digest_input = f'{self.partner_key}{sign_raw}'.encode('utf-8')
            return hashlib.md5(digest_input).hexdigest().upper()
        return hmac.new(
            self.partner_key.encode('utf-8'),
            sign_raw.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()

    def _build_auth_headers(self):
        nonce = secrets.token_hex(8)
        timestamp = str(int(time.time()))
        signature = self._compute_signature(nonce, timestamp)
        auth_value = f'{signature}:{nonce}:{timestamp}:{self.username}:{self.password}:{self.tax_code}'
        return {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Authentication': auth_value,
            'TaxCode': self.tax_code,
        }

    def _amount_in_words(self, amount):
        try:
            from helpers import so_thanh_chu
            return so_thanh_chu(int(round(float(amount))))
        except Exception:
            return str(int(round(float(amount))))

    def _build_invoice_xml(self, sale_data, items, ikey, seller):
        invoices = ET.Element('Invoices')
        inv_wrap = ET.SubElement(invoices, 'Inv')
        ET.SubElement(inv_wrap, 'key').text = ikey

        invoice = ET.SubElement(inv_wrap, 'Invoice')
        ET.SubElement(invoice, 'CusCode').text = str(sale_data.get('customer_code') or '')
        ET.SubElement(invoice, 'CusName').text = normalize_retail_buyer_name(sale_data.get('customer_name'))
        ET.SubElement(invoice, 'Buyer').text = normalize_retail_buyer_name(sale_data.get('customer_name'))
        ET.SubElement(invoice, 'CusAddress').text = str(sale_data.get('address') or '')
        ET.SubElement(invoice, 'CusPhone').text = str(sale_data.get('phone') or '')
        ET.SubElement(invoice, 'CusTaxCode').text = str(sale_data.get('tax_code') or '')
        ET.SubElement(invoice, 'PaymentMethod').text = str(sale_data.get('payment_method') or 'TM/CK')
        ET.SubElement(invoice, 'KindOfService').text = ''
        ET.SubElement(invoice, 'CurrencyUnit').text = 'VND'
        ET.SubElement(invoice, 'ArisingDate').text = datetime.now().strftime('%d/%m/%Y')

        products = ET.SubElement(invoice, 'Products')
        subtotal = 0.0
        tax_total = 0.0
        for item in items:
            qty = float(item.get('quantity') or 0)
            price = float(item.get('price') or 0)
            tax_pct = float(item.get('tax_pct') or 0)
            line = round(qty * price, 2)
            tax_val = round(line * tax_pct / 100.0, 2) if tax_pct else 0.0
            subtotal += line
            tax_total += tax_val

            product = ET.SubElement(products, 'Product')
            ET.SubElement(product, 'ProdName').text = str(item.get('name') or item.get('product_name') or '')
            ET.SubElement(product, 'ProdUnit').text = str(item.get('unit') or 'Cái')
            ET.SubElement(product, 'ProdQuantity').text = str(qty)
            ET.SubElement(product, 'ProdPrice').text = str(price)
            ET.SubElement(product, 'Amount').text = str(line)
            ET.SubElement(product, 'VATRate').text = str(int(tax_pct))
            ET.SubElement(product, 'VATAmount').text = str(tax_val)
            ET.SubElement(product, 'Total').text = str(round(line + tax_val, 2))

        grand_total = round(subtotal + tax_total, 2)
        ET.SubElement(invoice, 'Total').text = str(grand_total)
        ET.SubElement(invoice, 'VATRate').text = ''
        ET.SubElement(invoice, 'VATAmount').text = str(round(tax_total, 2))
        ET.SubElement(invoice, 'Amount').text = str(round(subtotal, 2))
        ET.SubElement(invoice, 'AmountInWords').text = self._amount_in_words(grand_total)

        extra = ET.SubElement(invoice, 'Extra')
        ET.SubElement(extra, 'SellerName').text = str(seller.get('name') or '')
        ET.SubElement(extra, 'SellerTaxCode').text = str(seller.get('tax_code') or self.tax_code)
        ET.SubElement(extra, 'SellerAddress').text = str(seller.get('address') or '')
        ET.SubElement(extra, 'Reference').text = str(sale_data.get('sale_no') or '')

        return ET.tostring(invoices, encoding='unicode')

    def _parse_response(self, resp):
        text = (resp.text or '').strip()
        if not text:
            return {'success': False, 'error': f'EasyInvoice: phản hồi rỗng (HTTP {resp.status_code})'}

        if resp.headers.get('Content-Type', '').startswith('application/json') or text.startswith('{'):
            try:
                data = resp.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                ok = data.get('success')
                if ok is None:
                    ok = str(data.get('status', '')).lower() in ('ok', 'success', 'true', '1')
                if ok:
                    payload = data.get('data') if isinstance(data.get('data'), dict) else data
                    return {
                        'success': True,
                        'invoice_no': (
                            payload.get('invoiceNo')
                            or payload.get('InvoiceNo')
                            or payload.get('invNo')
                            or payload.get('number')
                        ),
                        'invoice_id': (
                            payload.get('ikey')
                            or payload.get('Ikey')
                            or payload.get('key')
                            or payload.get('invoiceId')
                        ),
                        'pdf_url': payload.get('pdfUrl') or payload.get('PdfUrl') or payload.get('linkPdf'),
                        'xml_url': payload.get('xmlUrl') or payload.get('XmlUrl'),
                        'raw': data,
                    }
                return {
                    'success': False,
                    'error': data.get('message') or data.get('Message') or data.get('error') or str(data),
                }

        if text.startswith('<'):
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                root = None
            if root is not None:
                for tag in ('ImportInvByPatternResult', 'ImportInvoiceResult', 'Result', 'Message'):
                    node = root.find(f'.//{tag}')
                    if node is not None and (node.text or '').strip():
                        return self._parse_result_text(node.text.strip())

        return self._parse_result_text(text)

    def _parse_result_text(self, text):
        upper = text.upper()
        if upper.startswith('OK') or upper.startswith('THANH CONG') or upper.startswith('THÀNH CÔNG'):
            parts = text.split(':')
            invoice_no = None
            if len(parts) >= 2:
                tail = parts[1]
                if ';' in tail:
                    invoice_no = tail.split(';')[-1].strip()
                elif '-' in tail:
                    invoice_no = tail.split('-')[-1].strip()
                else:
                    invoice_no = tail.strip()
            return {
                'success': True,
                'invoice_no': invoice_no or '0',
                'invoice_id': parts[0].replace('OK', '').strip(' :-') or None,
                'raw': text,
            }
        if 'ERR' in upper or 'LOI' in upper or 'LỖI' in upper or 'FAIL' in upper:
            return {'success': False, 'error': f'EasyInvoice: {text}'}
        number_match = re.search(r'\b(\d{1,8})\b', text)
        if number_match:
            return {
                'success': True,
                'invoice_no': number_match.group(1),
                'raw': text,
            }
        return {'success': False, 'error': f'EasyInvoice: {text[:500]}'}

    def issue(self, sale_data, items, loai_hdon=1, replace_unpublished=False):
        if not self.username or not self.password or not self.tax_code:
            return {
                'success': False,
                'error': 'EasyInvoice: thiếu username, password hoặc mã số thuế trong Cài đặt HĐĐT.',
            }
        if not self.partner_key:
            return {
                'success': False,
                'error': (
                    'EasyInvoice: thiếu Partner Key (App Secret / Api Key) để ký xác thực. '
                    'Lấy từ Softdreams hoặc đặt EASYINVOICE_PARTNER_KEY trong .env.'
                ),
            }

        loai = normalize_loai_hdon(loai_hdon, default=1)
        is_draft = loai == 0
        ikey = str(sale_data.get('sale_no') or sale_data.get('id') or uuid.uuid4().hex[:16])
        if replace_unpublished and sale_data.get('invoice_id'):
            ikey = str(sale_data.get('invoice_id'))

        seller = {
            'name': self.config.get('business_name') or '',
            'tax_code': self.tax_code,
            'address': self.config.get('business_address') or '',
        }
        if not seller['name']:
            try:
                import sqlite3
                from db_utils import get_db_connection
                conn = get_db_connection()
                conn.row_factory = sqlite3.Row
                seller = get_seller_from_db(conn)
                conn.close()
            except Exception as exc:
                logger.warning('EasyInvoice: không đọc được seller từ DB: %s', exc)

        xml_data = self._build_invoice_xml(sale_data, items, ikey, seller)
        url = self._resolve_import_url()
        headers = self._build_auth_headers()
        payload = {
            'XmlData': xml_data,
            'Pattern': self.pattern,
            'Serial': self.serial,
            'Account': self.account,
            'ACpass': self.account_pass,
            'Username': self.username,
            'Password': self.password,
            'TaxCode': self.tax_code,
            'Ikey': ikey,
            'Convert': 0 if is_draft else 1,
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=45)
            parsed = self._parse_response(resp)
            if not parsed.get('success'):
                if resp.status_code >= 400 and 'EasyInvoice:' in parsed.get('error', ''):
                    parsed['error'] = f"{parsed['error']} (HTTP {resp.status_code})"
                return parsed

            parsed['is_draft'] = is_draft
            parsed.setdefault('invoice_id', ikey)
            if is_draft:
                parsed['invoice_no'] = '0'
            elif not parsed.get('invoice_no'):
                parsed['invoice_no'] = parsed.get('invoice_id') or ikey
            return parsed
        except requests.RequestException as exc:
            logger.exception('EasyInvoice issue: %s', exc)
            return {'success': False, 'error': f'EasyInvoice: lỗi kết nối — {exc}'}

    def sign_draft(self, invoice_id):
        if not invoice_id:
            return {'success': False, 'error': 'EasyInvoice: thiếu mã hóa đơn nháp (Ikey).'}
        publish_url = self._resolve_import_url().replace('importInvoice', 'publishInvoice')
        headers = self._build_auth_headers()
        payload = {
            'Ikey': str(invoice_id),
            'Pattern': self.pattern,
            'Serial': self.serial,
            'TaxCode': self.tax_code,
        }
        try:
            resp = requests.post(publish_url, json=payload, headers=headers, timeout=45)
            parsed = self._parse_response(resp)
            if parsed.get('success'):
                parsed['is_draft'] = False
            return parsed
        except requests.RequestException as exc:
            return {
                'success': False,
                'error': (
                    f'EasyInvoice: không gọi được publishInvoice ({exc}). '
                    'Phát hành nháp qua portal EasyInvoice hoặc gửi tài liệu publish API từ Softdreams.'
                ),
            }


class VNPTInvoiceAdapter(BaseEInvoiceAdapter):
    """
    VNPT Invoice — SOAP PublishService.asmx (TT78 / QĐ 1510).

    Tài liệu: V5 Webservice Update QD1510 (PublishService, PortalService, BusinessService).

    Phân biệt hai khái niệm «loại»:
    - SME ``loai_hdon`` (Mắt Bão): 0 = nháp xem trước, 1 = phát hành chính thức.
    - VNPT tham số ``type`` (getHashInvWithToken, AdjustReplaceInvWithToken, …):
      0 = hóa đơn gốc/chính thức, 1 = hóa đơn thay thế — **không có mã type cho nháp**.

    Nháp trên VNPT = ``ImportInvByPattern`` (chưa cấp số, portal hiển thị số 0).
    Chính thức = ``ImportAndPublishInv`` hoặc ``ImportInvByPattern`` + ``PublishInvFkey``.
    Thay thế (type=1) = ``BusinessService.replaceInv`` (HSM) hoặc ``AdjustReplaceInvWithToken`` (USB).
    """
    supports_draft = True

    # VNPT getHashInvWithToken / ký USB — KHÁC loai_hdon SME (Mắt Bão)
    VNPT_TYPE_OFFICIAL = 0
    VNPT_TYPE_REPLACEMENT = 1

    DEFAULT_PUBLISH_URL = 'https://tt78api.vnpt-invoice.com.vn/PublishService.asmx'
    SOAP_NS = 'http://tempuri.org/'
    ERROR_HINTS = {
        'ERR:1': (
            'Sai Account hoặc ACPass (tài khoản nhân viên phát hành trên portal VNPT). '
            'Username/Mật khẩu ServiceRole không thay thế được Account/ACPass.'
        ),
        'ERR:3': 'Dữ liệu XML hóa đơn không đúng quy định TT78.',
        'ERR:5': 'Không phát hành được hóa đơn (lỗi xử lý phía VNPT).',
        'ERR:6': (
            'Không đủ số hóa đơn cho lô phát hành (theo tài liệu VNPT ImportAndPublishInv/PublishInvFkey). '
            'Chỉ HĐ chính thức mới cấp số 1, 2, … trong gói/dải đăng ký; nháp luôn hiển thị số 0 '
            'và không tiêu số dải. Nếu portal demo đã phát hành đến số 15 thì lần cấp số 16 trả ERR:6.'
        ),
        'ERR:7': 'Username ServiceRole không khớp đơn vị trên VNPT.',
        'ERR:2': 'Không tồn tại hóa đơn gốc cần thay thế trên VNPT (kiểm tra Fkey/số HĐ).',
        'ERR:8': 'Hóa đơn gốc đã được thay thế — không thể thay thế lại.',
        'ERR:9': 'Trạng thái hóa đơn gốc không được phép thay thế.',
        'ERR:13': (
            'Fkey (mã liên kết HĐ) đã tồn tại trên VNPT — SME sẽ đồng bộ nháp theo Fkey SME{id} '
            'và cập nhật lại XML (MST, người mua, …).'
        ),
        'ERR:15': (
            'Không phát hành chính thức được cho Fkey này — thường đi kèm ERR:6 khi dải số đã hết '
            'hoặc nháp không đủ điều kiện cấp số.'
        ),
        'ERR:20': (
            'Mẫu số (pattern) hoặc ký hiệu (serial) không khớp dải đang sử dụng trên portal. '
            'VD demo GTGT: pattern 1/001, serial C26TAA.'
        ),
        'ERR:1504': (
            'Tên/trường định danh không hợp lệ theo TT78 — thường do Mã ĐVQHNS (MDVQHNSach) '
            'không đúng 7 chữ số, hoặc tên người mua/sản phẩm chứa ký tự không được phép.'
        ),
    }

    def __init__(self, config):
        super().__init__(config)
        self.provider_key = 'vnpt'
        self.tax_code = (self.config.get('tax_code') or '').strip()
        self.service_user = (self.config.get('username') or '').strip()
        self.service_pass = (self.config.get('password') or '').strip()
        self.account = (self.config.get('api_key') or '').strip()
        self.account_pass = (
            self.config.get('app_secret')
            or self.config.get('client_secret')
            or ''
        ).strip()
        self.pattern = (self.config.get('invoice_type') or '1/001').strip()
        self.serial = (self.config.get('invoice_series') or 'C26TAA').strip()
        self.serial_cert = (self.config.get('serial_number') or '').strip()
        self.publish_url = self._resolve_publish_url()

    def _resolve_publish_url(self):
        raw = (
            self.config.get('api_url')
            or os.environ.get('VNPT_API_URL')
            or self.DEFAULT_PUBLISH_URL
        ).strip().rstrip('/')
        if raw.lower().endswith('.asmx'):
            return raw
        if 'publishservice' in raw.lower():
            return raw if raw.lower().endswith('.asmx') else f'{raw}/PublishService.asmx'
        if raw.startswith('http'):
            return f'{raw.rstrip("/")}/PublishService.asmx'
        return self.DEFAULT_PUBLISH_URL

    def _portal_base_url(self):
        pub = self.publish_url
        if '/PublishService.asmx' in pub:
            return pub.replace('/PublishService.asmx', '')
        return pub.rsplit('/', 1)[0]

    def _portal_service_url(self):
        return f'{self._portal_base_url()}/PortalService.asmx'

    def _business_service_url(self):
        return f'{self._portal_base_url()}/BusinessService.asmx'

    def _amount_in_words(self, amount):
        try:
            from helpers import so_thanh_chu
            return so_thanh_chu(int(round(float(amount))))
        except Exception:
            return str(int(round(float(amount))))

    def internal_draft_download_url(self, fkey, file_type='pdf'):
        """URL proxy nội bộ — tải PDF/XML hóa đơn nháp theo Fkey qua PortalService."""
        from urllib.parse import quote
        fkey = str(fkey or '').strip()
        if not fkey:
            return ''
        ft = 'xml' if str(file_type or '').lower() == 'xml' else 'pdf'
        return f'/api/vnpt/download-file?fkey={quote(fkey)}&type={ft}&draft=1'

    def _portal_soap_download(self, method, params):
        envelope_parts = []
        for key, value in params.items():
            envelope_parts.append(f'<{key}>{xml_escape(str(value))}</{key}>')
        body = ''.join(envelope_parts)
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            '<soap:Body>'
            f'<{method} xmlns="{self.SOAP_NS}">{body}</{method}>'
            '</soap:Body></soap:Envelope>'
        )
        resp = requests.post(
            self._portal_service_url(),
            data=envelope.encode('utf-8'),
            headers={
                'Content-Type': 'text/xml; charset=utf-8',
                'SOAPAction': f'{self.SOAP_NS}{method}',
            },
            timeout=60,
        )
        if resp.status_code != 200:
            return {'success': False, 'error': f'VNPT Portal HTTP {resp.status_code}'}
        result_text = self._extract_soap_result(resp.text or '', method)
        code = self._vnpt_error_code(result_text)
        if code:
            return {'success': False, 'error': self._vnpt_error_message(result_text)}
        data = self._decode_portal_file_payload(result_text)
        if not data:
            return {'success': False, 'error': 'VNPT: không nhận được nội dung file.'}
        return {'success': True, 'data': data}

    def download_invoice_by_fkey(self, fkey, file_type='pdf'):
        """Tải PDF/XML hóa đơn nháp (hoặc theo Fkey) qua PortalService."""
        fkey = str(fkey or '').strip()
        if not fkey:
            return {'success': False, 'error': 'VNPT: thiếu Fkey hóa đơn.'}
        if not self.service_user or not self.service_pass:
            return {'success': False, 'error': 'VNPT: thiếu username/password ServiceRole.'}
        ft = str(file_type or 'pdf').lower()
        if ft == 'xml':
            method = 'downloadInvFkeyNoPay'
        else:
            method = 'downloadInvPDFFkeyNoPay'
        return self._portal_soap_download(method, {
            'fkey': fkey,
            'userName': self.service_user,
            'userPass': self.service_pass,
        })

    @staticmethod
    def _to_vnpt_date(iso_date):
        text = str(iso_date or '').strip()[:10]
        if len(text) == 10 and text[4] == '-':
            y, m, d = text.split('-')
            return f'{d}/{m}/{y}'
        return text

    @staticmethod
    def _from_vnpt_date(vnpt_date):
        text = str(vnpt_date or '').strip()
        if not text:
            return ''
        if len(text) >= 10 and text[2] == '/':
            d, m, y = text[:10].split('/')
            return f'{y}-{m}-{d}'
        return text[:10]

    def invoice_file_token(self, pattern, serial, invoice_no):
        inv_no = str(invoice_no or '').strip()
        if not inv_no or inv_no in ('0', '00000000'):
            return ''
        pat = (pattern or self.pattern or '').strip()
        ser = (serial or self.serial or '').strip()
        if not pat or not ser:
            return ''
        return f'{pat};{ser};{inv_no}'

    def internal_download_url(self, pattern, serial, invoice_no, file_type='pdf'):
        """URL proxy nội bộ SME — VNPT Portal yêu cầu SOAP, không tải trực tiếp bằng GET."""
        from urllib.parse import quote
        token = self.invoice_file_token(pattern, serial, invoice_no)
        if not token:
            return ''
        ft = 'xml' if str(file_type or '').lower() == 'xml' else 'pdf'
        return f'/api/vnpt/download-file?token={quote(token)}&type={ft}'

    def pdf_url_for_invoice(self, pattern, serial, invoice_no):
        return self.internal_download_url(pattern, serial, invoice_no, 'pdf')

    def xml_url_for_invoice(self, pattern, serial, invoice_no):
        return self.internal_download_url(pattern, serial, invoice_no, 'xml')

    @staticmethod
    def _decode_portal_file_payload(text):
        raw = (text or '').strip()
        if not raw:
            return b''
        # Chỉ coi là lỗi VNPT khi phản hồi ngắn (tránh false-positive trong payload base64 dài).
        if len(raw) < 128:
            upper = raw.upper()
            if upper.startswith('ERR') or upper.startswith('OK:ERR') or 'ERR:' in upper:
                return b''
        if raw[:4] == '%PDF':
            return raw.encode('utf-8')
        if raw.lstrip()[:5] == '<?xml':
            return raw.encode('utf-8')
        try:
            data = base64.b64decode(raw, validate=False)
        except Exception:
            return raw.encode('utf-8')
        if data[:4] == b'%PDF' or data.lstrip()[:5] == b'<?xml':
            return data
        try:
            data2 = base64.b64decode(data, validate=False)
            if data2[:4] == b'%PDF' or data2.lstrip()[:5] == b'<?xml':
                return data2
        except Exception:
            pass
        return data

    def download_invoice_file(self, token, file_type='pdf'):
        """Tải PDF/XML qua PortalService SOAP (ServiceRole user/pass)."""
        token = str(token or '').strip()
        if not token:
            return {'success': False, 'error': 'VNPT: thiếu token hóa đơn.'}
        if not self.service_user or not self.service_pass:
            return {'success': False, 'error': 'VNPT: thiếu username/password ServiceRole.'}
        ft = str(file_type or 'pdf').lower()
        if ft == 'xml':
            method = 'downloadInvNoPay'
            params = {
                'invToken': token,
                'userName': self.service_user,
                'userPass': self.service_pass,
            }
        else:
            method = 'downloadInvPDFNoPay'
            params = {
                'token': token,
                'userName': self.service_user,
                'userPass': self.service_pass,
            }
        return self._portal_soap_download(method, params)

    def _portal_soap_call_raw(self, method, params):
        parts = []
        for key, value in params.items():
            if value is None:
                continue
            parts.append(f'<{key}>{xml_escape(str(value))}</{key}>')
        body = ''.join(parts)
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            '<soap:Body>'
            f'<{method} xmlns="{self.SOAP_NS}">{body}</{method}>'
            '</soap:Body></soap:Envelope>'
        )
        resp = requests.post(
            self._portal_service_url(),
            data=envelope.encode('utf-8'),
            headers={
                'Content-Type': 'text/xml; charset=utf-8',
                'SOAPAction': f'{self.SOAP_NS}{method}',
            },
            timeout=60,
        )
        if resp.status_code != 200:
            return {
                'success': False,
                'error': f'VNPT Portal HTTP {resp.status_code}',
            }
        result_text = self._extract_soap_result(resp.text or '', method)
        code = self._vnpt_error_code(result_text)
        if code:
            return {'success': False, 'error': self._vnpt_error_message(result_text), 'code': code}
        raw = (result_text or '').strip()
        if raw.upper().startswith('OK'):
            raw = raw.split(':', 1)[1].strip() if ':' in raw else raw[2:].strip()
        return {'success': True, 'raw_xml': raw, 'raw': result_text}

    def _first_xml_text(self, node, *names):
        if node is None:
            return ''
        wanted = {n.lower() for n in names}
        for child in node.iter():
            tag = child.tag.split('}')[-1].lower()
            if tag in wanted and (child.text or '').strip():
                return child.text.strip()
        return ''

    def _parse_portal_invoice_list_xml(self, xml_text):
        text = (xml_text or '').strip()
        if not text or text.upper().startswith('ERR'):
            return []
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return []

        rows = []
        invoice_nodes = []
        for elem in root.iter():
            tag = elem.tag.split('}')[-1].lower()
            if tag in ('inv', 'invoice', 'hdon', 'item', 'row'):
                invoice_nodes.append(elem)
        if not invoice_nodes and root.tag.split('}')[-1].lower() not in ('invoices', 'dshdon', 'data'):
            invoice_nodes = [root]

        for node in invoice_nodes:
            fkey = self._first_xml_text(
                node, 'key', 'fkey', 'fKey', 'Fkey', 'MaTraCuu', 'matracuu',
            )
            pattern = self._first_xml_text(node, 'pattern', 'mau', 'khmsHDOn', 'KHMSHDon')
            serial = self._first_xml_text(node, 'serial', 'khhdon', 'KHHDon')
            inv_no = self._first_xml_text(
                node, 'invNumber', 'invNum', 'shdon', 'SHDon', 'so', 'So',
            )
            inv_date = self._from_vnpt_date(self._first_xml_text(
                node, 'issueDate', 'ngaylap', 'nLap', 'NLap', 'ArisingDate',
            ))
            amount_raw = self._first_xml_text(
                node, 'amount', 'total', 'tgTTTBSo', 'TgTTTBSo', 'tongtien', 'TongTien',
            )
            try:
                amount = float(str(amount_raw or '0').replace(',', '') or 0)
            except (TypeError, ValueError):
                amount = 0.0
            status_raw = self._first_xml_text(node, 'status', 'tthai', 'TThai', 'tenTThaiHDon')
            status_code = ''
            if str(status_raw or '').strip().isdigit():
                status_code = str(status_raw).strip()
            buyer = self._first_xml_text(
                node, 'buyer', 'customer', 'nMua_Ten', 'NMua_Ten', 'TenKhachHang',
            )
            if not fkey and not inv_no:
                continue
            inv_no_norm = str(inv_no or '0').strip()
            is_draft = inv_no_norm in ('0', '00000000', '')
            is_replaced = status_code == '3'
            sale_no = ''
            if fkey.startswith('SME') and fkey[3:].isdigit():
                sale_no = fkey
            rows.append({
                'sale_no': sale_no,
                'sale_id': int(fkey[3:]) if fkey.startswith('SME') and fkey[3:].isdigit() else None,
                'invoice_no': '0' if is_draft else inv_no_norm,
                'invoice_date': inv_date,
                'company': buyer or '',
                'customer': buyer or '',
                'amount': amount,
                'fkey': fkey,
                'invoice_id': fkey or inv_no_norm,
                'pattern': pattern or self.pattern,
                'serial': serial or self.serial,
                'pdf_url': self.internal_draft_download_url(fkey, 'pdf') if is_draft else self.pdf_url_for_invoice(
                    pattern or self.pattern, serial or self.serial, inv_no_norm,
                ),
                'xml_url': self.internal_draft_download_url(fkey, 'xml') if is_draft else self.xml_url_for_invoice(
                    pattern or self.pattern, serial or self.serial, inv_no_norm,
                ),
                'ten_LoaiHDon': (
                    'Hóa đơn nháp' if is_draft
                    else ('Hóa đơn bị thay thế' if is_replaced else '')
                ),
                'tax_authority_status': (
                    status_raw or ('Hóa đơn nháp' if is_draft else 'Đã phát hành')
                ),
                'is_draft': is_draft,
                'is_replaced': is_replaced,
                'provider': 'vnpt',
            })
        return rows

    def list_invoices(self, from_date, to_date):
        """Lấy danh sách HĐ từ PortalService VNPT (best-effort)."""
        if not self.service_user or not self.service_pass:
            return {
                'success': False,
                'error': 'VNPT: thiếu username/password ServiceRole.',
                'data': [],
            }
        from_vnpt = self._to_vnpt_date(from_date)
        to_vnpt = self._to_vnpt_date(to_date)
        base_params = {
            'userName': self.service_user,
            'userPass': self.service_pass,
        }
        attempts = [
            ('SearchInv', {
                **base_params,
                'cusCode': self.tax_code,
                'pattern': self.pattern,
                'serial': self.serial,
                'fromDate': from_vnpt,
                'toDate': to_vnpt,
                'invNumber': '',
                'invStatus': -1,
                'page': 1,
                'cussignStatus': -1,
                'payment': -1,
            }),
            ('GetInvViewByDatePaging', {
                **base_params,
                'pattern': self.pattern,
                'serial': self.serial,
                'fromDate': from_vnpt,
                'toDate': to_vnpt,
                'pageIndex': 1,
                'pageSize': 200,
            }),
            ('GetInvViewByDate', {
                **base_params,
                'pattern': self.pattern,
                'serial': self.serial,
                'fromDate': from_vnpt,
                'toDate': to_vnpt,
            }),
            ('listInvByCus', {
                **base_params,
                'cusCode': self.tax_code,
                'fromDate': from_vnpt,
                'toDate': to_vnpt,
            }),
        ]
        warnings = []
        for method, params in attempts:
            parsed = self._portal_soap_call_raw(method, params)
            if not parsed.get('success'):
                warnings.append(parsed.get('error') or method)
                continue
            rows = self._parse_portal_invoice_list_xml(parsed.get('raw_xml') or '')
            if rows:
                return {
                    'success': True,
                    'data': rows,
                    'source': method,
                    'message': f'Đồng bộ {len(rows)} hóa đơn từ VNPT ({method}).',
                }
            warnings.append(f'{method}: không có dữ liệu')
        return {
            'success': True,
            'data': [],
            'warning': (
                'Không lấy được danh sách từ portal VNPT trong khoảng ngày này. '
                'Hiển thị hóa đơn đã lưu trong SME.'
            ),
            'details': warnings[:3],
        }

    def _load_seller(self):
        seller = {
            'name': self.config.get('business_name') or '',
            'tax_code': self.tax_code,
            'address': self.config.get('business_address') or '',
        }
        if seller['name'] and seller['address']:
            return seller
        try:
            import sqlite3
            from db_utils import get_db_connection
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            seller = get_seller_from_db(conn)
            conn.close()
        except Exception as exc:
            logger.warning('VNPT: không đọc được seller từ DB: %s', exc)
        return seller

    @staticmethod
    def _set_xml_text(parent, tag, value):
        text = '' if value is None else str(value).strip()
        if not text:
            return None
        elem = ET.SubElement(parent, tag)
        elem.text = text
        return elem

    @staticmethod
    def _fmt_tt78_number(value, decimals=2):
        num = round(float(value or 0), decimals)
        if decimals <= 0 or num == int(num):
            return str(int(num))
        formatted = f'{num:.{decimals}f}'
        return formatted.rstrip('0').rstrip('.')

    def _tsuat_label(self, tax_pct):
        pct = float(tax_pct or 0)
        if pct <= 0:
            return '0'
        if pct == int(pct):
            return str(int(pct))
        return self._fmt_tt78_number(pct, 2)

    @staticmethod
    def _normalize_invoice_date(value):
        text = str(value or '').strip()
        if not text:
            return datetime.now().strftime('%Y-%m-%d')
        if 'T' in text:
            text = text.split('T', 1)[0]
        return text[:10]

    def _build_ttchung(self, ttchung, replacement_info=None):
        """
        TTChung theo schema VNPT ImportInvByPattern (DSHDon).
        Pattern/serial truyền qua tham số SOAP — không đặt KHMSHDon/KHHDon trong TTChung.
        """
        ET.SubElement(ttchung, 'HTTToan').text = 'TM/CK'
        if replacement_info:
            ET.SubElement(ttchung, 'TCHDon').text = '1'
        ET.SubElement(ttchung, 'DVTTe').text = 'VND'
        ET.SubElement(ttchung, 'TGia').text = '1'

    def _build_tt78_xml(self, sale_data, items, fkey, seller, replacement_info=None):
        dshdon = ET.Element('DSHDon')
        hdon = ET.SubElement(dshdon, 'HDon')
        ET.SubElement(hdon, 'key').text = fkey

        dlhdon = ET.SubElement(hdon, 'DLHDon')
        ttchung = ET.SubElement(dlhdon, 'TTChung')
        self._build_ttchung(ttchung, replacement_info=replacement_info)

        ndhdon = ET.SubElement(dlhdon, 'NDHDon')

        nban = ET.SubElement(ndhdon, 'NBan')
        ET.SubElement(nban, 'Ten').text = str(seller.get('name') or 'DOANH NGHIỆP')
        ET.SubElement(nban, 'MST').text = str(seller.get('tax_code') or self.tax_code)
        ET.SubElement(nban, 'DChi').text = str(seller.get('address') or '')

        buyer = resolve_vnpt_buyer_fields(sale_data)
        nmua = ET.SubElement(ndhdon, 'NMua')
        # VNPT bắt buộc NMua/Ten — bán lẻ dùng HVTNMHang khi không có tên đơn vị.
        nmua_ten = buyer['unit_name'] or buyer['buyer_full_name']
        ET.SubElement(nmua, 'Ten').text = nmua_ten
        self._set_xml_text(nmua, 'MST', buyer.get('tax_code'))
        self._set_xml_text(nmua, 'MDVQHNSach', buyer.get('budget_unit_code'))
        self._set_xml_text(nmua, 'SHChieu', buyer.get('passport_no'))
        self._set_xml_text(nmua, 'DChi', buyer.get('unit_address'))
        self._set_xml_text(
            nmua,
            'MKHang',
            str(sale_data.get('customer_code') or '').strip(),
        )
        self._set_xml_text(
            nmua,
            'SDThoai',
            str(sale_data.get('phone') or sale_data.get('customer_phone') or '').strip(),
        )
        email = str(sale_data.get('email') or sale_data.get('customer_email') or '').strip()
        if email and '@' in email:
            self._set_xml_text(nmua, 'DCTDTu', email)
        ET.SubElement(nmua, 'HVTNMHang').text = buyer['buyer_full_name']

        dshhdvu = ET.SubElement(ndhdon, 'DSHHDVu')
        tax_buckets = {}
        subtotal = 0.0
        tax_total = 0.0

        for idx, item in enumerate(items, start=1):
            qty = float(item.get('quantity') or 0)
            price = float(item.get('price') or 0)
            tax_pct = float(item.get('tax_pct') or 0)
            discount_pct = float(item.get('discount_pct') or 0)
            line = round(qty * price, 2)
            discount_amt = round(line * discount_pct / 100.0, 2)
            tax_val = round(line * tax_pct / 100.0, 2)
            line_total = round(line + tax_val, 2)
            subtotal += line
            tax_total += tax_val
            tsuat = self._tsuat_label(tax_pct)
            bucket = tax_buckets.setdefault(tsuat, {'thtien': 0.0, 'tthue': 0.0})
            bucket['thtien'] += line
            bucket['tthue'] += tax_val

            hhdvu = ET.SubElement(dshhdvu, 'HHDVu')
            ET.SubElement(hhdvu, 'TChat').text = '1'
            ET.SubElement(hhdvu, 'STT').text = str(idx)
            self._set_xml_text(hhdvu, 'MHHDVu', str(item.get('product_code') or '').strip())
            ET.SubElement(hhdvu, 'THHDVu').text = str(
                item.get('name') or item.get('product_name') or ''
            )
            ET.SubElement(hhdvu, 'DVTinh').text = str(item.get('unit') or 'Cái')
            ET.SubElement(hhdvu, 'SLuong').text = self._fmt_tt78_number(qty, 4)
            ET.SubElement(hhdvu, 'DGia').text = self._fmt_tt78_number(price, 2)
            if discount_pct:
                ET.SubElement(hhdvu, 'TLCKhau').text = self._fmt_tt78_number(discount_pct, 4)
                ET.SubElement(hhdvu, 'STCKhau').text = self._fmt_tt78_number(discount_amt, 2)
            ET.SubElement(hhdvu, 'ThTien').text = self._fmt_tt78_number(line, 2)
            ET.SubElement(hhdvu, 'TSuat').text = tsuat
            ET.SubElement(hhdvu, 'TThue').text = self._fmt_tt78_number(tax_val, 2)
            ET.SubElement(hhdvu, 'TSThue').text = self._fmt_tt78_number(line_total, 2)

        grand = round(subtotal + tax_total, 2)
        ttoan = ET.SubElement(ndhdon, 'TToan')
        thttlt = ET.SubElement(ttoan, 'THTTLTSuat')
        for tsuat, bucket in sorted(tax_buckets.items()):
            ltsuat = ET.SubElement(thttlt, 'LTSuat')
            ET.SubElement(ltsuat, 'TSuat').text = tsuat
            ET.SubElement(ltsuat, 'ThTien').text = self._fmt_tt78_number(bucket['thtien'], 2)
            ET.SubElement(ltsuat, 'TThue').text = self._fmt_tt78_number(bucket['tthue'], 2)

        ET.SubElement(ttoan, 'TgTCThue').text = self._fmt_tt78_number(subtotal, 2)
        ET.SubElement(ttoan, 'TgTThue').text = self._fmt_tt78_number(tax_total, 2)
        ET.SubElement(ttoan, 'TTCKTMai').text = '0'
        ET.SubElement(ttoan, 'TgTTTBSo').text = self._fmt_tt78_number(grand, 2)
        ET.SubElement(ttoan, 'TgTTTBChu').text = self._amount_in_words(grand)

        return ET.tostring(dshdon, encoding='unicode')

    def _soap_call(self, method, params, fkey=None, service_url=None):
        parts = []
        for key, value in params.items():
            text = '' if value is None else str(value)
            parts.append(f'<{key}>{xml_escape(text)}</{key}>')
        body = '\n'.join(parts)
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            '<soap:Body>'
            f'<{method} xmlns="{self.SOAP_NS}">{body}</{method}>'
            '</soap:Body></soap:Envelope>'
        )
        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': f'{self.SOAP_NS}{method}',
        }
        resp = requests.post(
            service_url or self.publish_url,
            data=envelope.encode('utf-8'),
            headers=headers,
            timeout=60,
        )
        if resp.status_code != 200:
            return {
                'success': False,
                'error': f'VNPT HTTP {resp.status_code}: {(resp.text or "")[:400]}',
            }
        result_text = self._extract_soap_result(resp.text or '', method)
        return self._parse_vnpt_result(result_text, fkey=fkey)

    def _business_soap_call(self, method, params, fkey=None):
        return self._soap_call(
            method,
            params,
            fkey=fkey,
            service_url=self._business_service_url(),
        )

    def _extract_soap_result(self, xml_text, method):
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return (xml_text or '').strip()
        suffix = f'{method}Result'
        for elem in root.iter():
            if elem.tag.endswith(suffix) and elem.text:
                return elem.text.strip()
        return (xml_text or '').strip()

    def _vnpt_error_codes(self, raw):
        return [
            f'ERR:{match}'
            for match in re.findall(r'ERR\s*:\s*(\d+)', str(raw or ''), flags=re.I)
        ]

    def _vnpt_error_code(self, raw):
        codes = self._vnpt_error_codes(raw)
        return codes[0] if codes else None

    def _parse_vnpt_batch_segments(self, raw):
        text = str(raw or '').strip()
        if '#||' not in text:
            return [text] if text else []
        return [seg.strip() for seg in text.split('#||') if seg.strip()]

    @classmethod
    def _resolve_issue_mode(cls, loai_hdon):
        """
        Map SME loai_hdon → luồng SOAP VNPT (không map sang tham số type VNPT).

        Returns:
            (is_import_only, soap_method, is_draft_portal)
        """
        if normalize_loai_hdon(loai_hdon, default=1) == 0:
            return True, 'ImportInvByPattern', True
        return False, 'ImportAndPublishInv', False

    @classmethod
    def _vnpt_token_type(cls, issue_kind='official'):
        """Tham số ``type`` khi gọi getHashInvWithToken / publishInvWithToken."""
        if str(issue_kind or '').lower() in ('replace', 'replacement', 'thay_the', '1'):
            return cls.VNPT_TYPE_REPLACEMENT
        return cls.VNPT_TYPE_OFFICIAL

    def _format_err6_publish(self, raw_error, fkey=None, soap_method=None):
        segments = self._parse_vnpt_batch_segments(raw_error)
        parts = [self._vnpt_error_message(seg) for seg in segments if 'ERR' in seg.upper()]
        msg = ' | '.join(parts) if parts else self._vnpt_error_message(raw_error)
        extra = (
            f'Nháp (ImportInvByPattern) không lấy số trong dải {self.pattern}/{self.serial} — '
            'chỉ hiển thị số 0. ERR:6 xảy ra khi cấp số chính thức (ImportAndPublishInv hoặc PublishInvFkey). '
            'Portal demo thường chỉ có gói ~15 số (1→15); đã hết thì không cấp thêm số 16. '
            'Đăng ký thêm dải trên portal VNPT hoặc đổi ký hiệu trong Cài đặt HĐĐT.'
        )
        if soap_method:
            extra += f' API SME vừa gọi: {soap_method}.'
        if fkey:
            extra += f' Fkey: {fkey}.'
        return f'{msg} {extra}'

    def _vnpt_error_message(self, raw):
        text = (raw or '').strip()
        if not text:
            return 'VNPT: phản hồi rỗng'
        if '#||' in text:
            segments = self._parse_vnpt_batch_segments(text)
            parsed = []
            for seg in segments:
                if not seg.upper().startswith('ERR'):
                    continue
                code = self._vnpt_error_code(seg)
                if code and code in self.ERROR_HINTS:
                    detail = seg.split('#', 1)[1] if '#' in seg else ''
                    suffix = f' ({detail})' if detail else ''
                    parsed.append(f'{code}{suffix} — {self.ERROR_HINTS[code]}')
                else:
                    parsed.append(seg)
            if parsed:
                return 'VNPT: ' + ' | '.join(parsed)
        if '|' in text and not text.upper().startswith('ERR'):
            detail = text.split('|', 1)[-1].strip()
            if detail:
                return f'VNPT: {detail}'
        primary = self._vnpt_error_code(text) or text.split('#', 1)[0].strip()
        if primary in self.ERROR_HINTS:
            return f'VNPT: {primary} — {self.ERROR_HINTS[primary]}'
        code = self._vnpt_error_code(text)
        if code and code in self.ERROR_HINTS:
            return f'VNPT: {text} — {self.ERROR_HINTS[code]}'
        return f'VNPT: {text}'

    def _parse_vnpt_result(self, text, fkey=None):
        raw = (text or '').strip()
        if not raw:
            return {'success': False, 'error': 'VNPT: phản hồi rỗng'}
        upper = raw.upper()
        if upper.startswith('ERR') or 'ERR:' in upper:
            return {'success': False, 'error': self._vnpt_error_message(raw)}
        if not upper.startswith('OK'):
            return {'success': False, 'error': self._vnpt_error_message(raw[:500])}

        parsed = {'success': True, 'raw': raw, 'invoice_id': fkey}
        if ':' not in raw:
            return parsed

        tail = raw.split(':', 1)[1].strip()
        if ';' in tail and '-' in tail:
            pattern, rest = tail.split(';', 1)
            serial_part, keys_part = rest.split('-', 1)
            parsed['pattern'] = pattern.strip()
            parsed['serial'] = serial_part.strip()
            for pair in keys_part.split(','):
                pair = pair.strip().replace(' ', '')
                if not pair or '_' not in pair:
                    continue
                key, inv_no = pair.rsplit('_', 1)
                if fkey is None or key == fkey:
                    parsed['invoice_no'] = inv_no
                    parsed['invoice_id'] = key
                    break
            if not parsed.get('invoice_no') and '_' in keys_part:
                parsed['invoice_no'] = keys_part.rsplit('_', 1)[-1].strip()
        elif ';' in tail:
            chunks = tail.split(';')
            if len(chunks) >= 3:
                parsed['pattern'] = chunks[0].strip()
                parsed['serial'] = chunks[1].strip()
                parsed['invoice_no'] = chunks[2].strip()
        if fkey and not parsed.get('invoice_no'):
            match = re.search(rf'{re.escape(str(fkey))}_(\d+)', raw, flags=re.I)
            if match:
                parsed['invoice_no'] = match.group(1)
                parsed['invoice_id'] = str(fkey)
        return parsed

    def _base_params(self, xml_data):
        return {
            'Account': self.account,
            'ACpass': self.account_pass,
            'xmlInvData': xml_data,
            'username': self.service_user,
            'password': self.service_pass,
            'pattern': self.pattern,
            'serial': self.serial,
            'convert': 0,
        }

    def _test_publish_account(self):
        """Xác thực Account/ACPass — GetCertInfo không kiểm tra được bước này."""
        if not self.account or not self.account_pass:
            return {
                'ok': False,
                'error': (
                    'VNPT: thiếu Account (Api Key) hoặc ACPass (App Secret) — '
                    'tài khoản nhân viên phát hành trên portal, khác ServiceRole.'
                ),
            }
        parsed = self._soap_call('ImportInvByPattern', {
            'Account': self.account,
            'ACpass': self.account_pass,
            'xmlInvData': '<DSHDon></DSHDon>',
            'username': self.service_user,
            'password': self.service_pass,
            'pattern': self.pattern,
            'serial': self.serial,
            'convert': 0,
        })
        if parsed.get('success'):
            return {'ok': True}
        code = self._vnpt_error_code(parsed.get('error'))
        if code == 'ERR:1':
            return {
                'ok': False,
                'error': self._vnpt_error_message('ERR:1'),
            }
        if code == 'ERR:20':
            return {
                'ok': False,
                'error': self._vnpt_error_message('ERR:20'),
            }
        # Lỗi XML/schema → Account/ACPass đã qua bước xác thực.
        return {'ok': True, 'note': 'Account/ACPass hợp lệ'}

    def test_connection(self):
        if not self.service_user or not self.service_pass:
            return {
                'success': False,
                'error': 'VNPT: thiếu username/password ServiceRole.',
            }
        if not self.account or not self.account_pass:
            return {
                'success': False,
                'error': (
                    'VNPT: thiếu Account (Api Key) và ACPass (App Secret). '
                    'Demo có thể dùng Account vnpthcmcadmin_demo / Admin@1234.'
                ),
            }
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            '<soap:Body>'
            f'<GetCertInfo xmlns="{self.SOAP_NS}">'
            f'<userName>{xml_escape(self.service_user)}</userName>'
            f'<password>{xml_escape(self.service_pass)}</password>'
            '</GetCertInfo>'
            '</soap:Body></soap:Envelope>'
        )
        try:
            resp = requests.post(
                self.publish_url,
                data=envelope.encode('utf-8'),
                headers={
                    'Content-Type': 'text/xml; charset=utf-8',
                    'SOAPAction': f'{self.SOAP_NS}GetCertInfo',
                },
                timeout=30,
            )
            if resp.status_code != 200:
                return {
                    'success': False,
                    'error': f'VNPT HTTP {resp.status_code}: {(resp.text or "")[:300]}',
                }
            result_text = self._extract_soap_result(resp.text or '', 'GetCertInfo')
            upper = (result_text or '').strip().upper()
            if not result_text or upper.startswith('ERR') or 'ERR:' in upper:
                err = result_text or 'Không xác thực được tài khoản VNPT.'
                return {'success': False, 'error': self._vnpt_error_message(err)}

            account_check = self._test_publish_account()
            if not account_check.get('ok'):
                return {'success': False, 'error': account_check.get('error')}

            acct_note = account_check.get('note') or 'Account/ACPass hợp lệ'
            if result_text.strip().startswith('<'):
                return {
                    'success': True,
                    'message': (
                        'Kết nối VNPT OK — ServiceRole + Account/ACPass hợp lệ, '
                        'đã lấy thông tin chứng thư số.'
                    ),
                }
            if upper.startswith('OK'):
                return {
                    'success': True,
                    'message': f'Kết nối VNPT OK — ServiceRole hợp lệ; {acct_note}.',
                }
            return {
                'success': True,
                'message': f'Kết nối VNPT OK — ServiceRole hợp lệ; {acct_note}.',
            }
        except requests.RequestException as exc:
            return {'success': False, 'error': f'VNPT: không kết nối được PublishService — {exc}'}

    def _build_replacement_fkey(self, sale_data, old_fkey):
        sale_id = sale_data.get('id')
        base = f'SME{sale_id}' if sale_id else str(old_fkey or 'REP').strip()
        stamp = datetime.now().strftime('%Y%m%d%H%M%S')
        return f'{base}_TT{stamp}'

    def _build_fkey(self, sale_data, replace_unpublished=False):
        if replace_unpublished and sale_data.get('invoice_id'):
            return str(sale_data.get('invoice_id'))
        sale_id = sale_data.get('id')
        if sale_id:
            return f'SME{sale_id}'
        sale_no = sale_data.get('sale_no')
        if sale_no:
            return str(sale_no)
        return uuid.uuid4().hex[:16]

    def _candidate_fkeys(self, sale_data, primary_fkey):
        keys = []
        sale_id = sale_data.get('id')
        if sale_id:
            keys.append(f'SME{sale_id}')
        for val in (primary_fkey, sale_data.get('invoice_id')):
            text = str(val or '').strip()
            if not text or text in keys:
                continue
            upper = text.upper()
            if upper.startswith('DH') or text.startswith('ĐH'):
                continue
            keys.append(text)
        return keys

    def _reimport_draft_xml(self, sale_data, items, fkey):
        seller = self._load_seller()
        payload = dict(sale_data or {})
        payload['invoice_id'] = fkey
        xml_data = self._build_tt78_xml(payload, items, fkey, seller)
        return self._soap_call(
            'ImportInvByPattern',
            self._base_params(xml_data),
            fkey=fkey,
        )

    def _finalize_issue_result(self, parsed, *, fkey, is_draft, sale_data):
        parsed['is_draft'] = is_draft
        if not parsed.get('invoice_id'):
            parsed['invoice_id'] = fkey
        parsed['invoice_date'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        parsed['total_amount'] = float(sale_data.get('total_amount') or 0)
        if is_draft:
            parsed['invoice_no'] = '0'
            parsed['pdf_url'] = self.internal_draft_download_url(fkey, 'pdf')
            parsed['xml_url'] = self.internal_draft_download_url(fkey, 'xml')
            parsed['tax_authority_status'] = parsed.get('tax_authority_status') or 'Hóa đơn nháp'
        elif not parsed.get('invoice_no'):
            parsed['invoice_no'] = parsed.get('invoice_id') or fkey
        if not is_draft and parsed.get('invoice_no') and parsed.get('invoice_no') != '0':
            pat = parsed.get('pattern') or self.pattern
            ser = parsed.get('serial') or self.serial
            inv = parsed['invoice_no']
            parsed['pdf_url'] = self.internal_download_url(pat, ser, inv, 'pdf')
            parsed['xml_url'] = self.internal_download_url(pat, ser, inv, 'xml')
            parsed['tax_authority_status'] = 'Chờ phản hồi CQT'
        return parsed

    def _recover_duplicate_fkey(self, sale_data, items, fkeys, is_draft):
        """ERR:13 — HĐ đã có trên VNPT: đồng bộ Fkey SME{id} và cập nhật XML nháp."""
        last_error = None
        for fkey in self._candidate_fkeys(sale_data, fkeys[0] if fkeys else ''):
            if is_draft:
                if items:
                    reimport = self._reimport_draft_xml(sale_data, items, fkey)
                    err_code = self._vnpt_error_code(reimport.get('error'))
                    if not reimport.get('success') and err_code not in ('ERR:13', None):
                        continue
                return self._finalize_issue_result(
                    {
                        'success': True,
                        'invoice_id': fkey,
                        'recovered_from_duplicate': True,
                        'tax_authority_status': 'Hóa đơn nháp (đã có trên VNPT)',
                    },
                    fkey=fkey,
                    is_draft=True,
                    sale_data=sale_data,
                )
            pub = self._soap_call('PublishInvFkey', {
                'Account': self.account,
                'ACpass': self.account_pass,
                'lsFkey': fkey,
                'username': self.service_user,
                'password': self.service_pass,
                'pattern': self.pattern,
                'serial': self.serial,
            }, fkey=fkey)
            if pub.get('success'):
                pub['invoice_id'] = fkey
                pub['recovered_from_duplicate'] = True
                return self._finalize_issue_result(
                    pub, fkey=fkey, is_draft=False, sale_data=sale_data,
                )
            last_error = pub.get('error')
            if self._vnpt_error_code(last_error) == 'ERR:6':
                return {
                    'success': False,
                    'error': self._format_err6_publish(
                        last_error, fkey, soap_method='PublishInvFkey',
                    ),
                }
        fkey_hint = ', '.join(self._candidate_fkeys(sale_data, fkeys[0] if fkeys else ''))
        if last_error:
            return {
                'success': False,
                'error': self._vnpt_error_message(last_error),
            }
        return {
            'success': False,
            'error': (
                f'{self._vnpt_error_message("ERR:13")} '
                f'Fkey đã thử: {fkey_hint or "(trống)"}. '
                'Kiểm tra hóa đơn trên portal VNPT hoặc xóa nháp trùng Fkey.'
            ),
        }

    def issue(self, sale_data, items, loai_hdon=1, replace_unpublished=False):
        if not self.service_user or not self.service_pass:
            return {
                'success': False,
                'error': 'VNPT: thiếu username/password ServiceRole trong Cài đặt HĐĐT.',
            }
        if not self.account or not self.account_pass:
            return {
                'success': False,
                'error': (
                    'VNPT: thiếu tài khoản phát hành — nhập Account vào Api Key '
                    'và ACPass vào App Secret.'
                ),
            }
        if not self.pattern or not self.serial:
            return {
                'success': False,
                'error': 'VNPT: thiếu Mẫu số (invoice_type) hoặc Ký hiệu (invoice_series).',
            }

        fkey = self._build_fkey(sale_data, replace_unpublished=replace_unpublished)

        seller = self._load_seller()
        xml_data = self._build_tt78_xml(sale_data, items, fkey, seller)
        _, method, is_draft = self._resolve_issue_mode(loai_hdon)

        try:
            parsed = self._soap_call(method, self._base_params(xml_data), fkey=fkey)
            if not parsed.get('success'):
                err_code = self._vnpt_error_code(parsed.get('error'))
                if err_code == 'ERR:13':
                    return self._recover_duplicate_fkey(
                        sale_data,
                        items,
                        self._candidate_fkeys(sale_data, fkey),
                        is_draft,
                    )
                if err_code == 'ERR:6' and not is_draft:
                    return {
                        'success': False,
                        'error': (
                            f'{self._format_err6_publish(parsed.get("error"), fkey, soap_method=method)} '
                            'Trên trang bán hàng chọn «Nháp (xem trước)» — API nháp là ImportInvByPattern, '
                            'không gọi cấp số chính thức.'
                        ),
                    }
                if err_code == 'ERR:6' and is_draft:
                    return {
                        'success': False,
                        'error': (
                            f'{self._vnpt_error_message(parsed.get("error"))} '
                            f'Đã gọi {method} (nháp — không cấp số dải). '
                            'Nếu thông báo giống lỗi cấp số chính thức, kiểm tra đã chọn «Nháp (xem trước)» trên trang bán hàng. '
                            'Thử Fkey mới (đơn mới) hoặc xóa nháp trùng Fkey trên portal VNPT.'
                        ),
                    }
                if err_code == 'ERR:15' and not is_draft:
                    return {
                        'success': False,
                        'error': self._format_err6_publish(
                            parsed.get('error'), fkey, soap_method=method,
                        ),
                    }
                return parsed

            return self._finalize_issue_result(
                parsed, fkey=fkey, is_draft=is_draft, sale_data=sale_data,
            )
        except requests.RequestException as exc:
            logger.exception('VNPT issue: %s', exc)
            return {'success': False, 'error': f'VNPT: lỗi kết nối — {exc}'}

    def issue_replacement(self, sale_data, items, replacement_info):
        """
        Hóa đơn thay thế VNPT (TT78) — BusinessService.replaceInv, type=1.

        ``replacement_info`` cần: old_fkey (hoặc MSHDonDCLQuan), KHMSHDCLQuan, KHHDCLQuan,
        SHDCLQuan, NLHDCLQuan của hóa đơn gốc.
        """
        if not self.service_user or not self.service_pass:
            return {
                'success': False,
                'error': 'VNPT: thiếu username/password ServiceRole trong Cài đặt HĐĐT.',
            }
        if not self.account or not self.account_pass:
            return {
                'success': False,
                'error': (
                    'VNPT: thiếu tài khoản phát hành — nhập Account vào Api Key '
                    'và ACPass vào App Secret.'
                ),
            }
        if not self.pattern or not self.serial:
            return {
                'success': False,
                'error': 'VNPT: thiếu Mẫu số (invoice_type) hoặc Ký hiệu (invoice_series).',
            }

        info = dict(replacement_info or {})
        old_fkey = str(
            info.get('old_fkey')
            or info.get('MSHDonDCLQuan')
            or info.get('fkey')
            or sale_data.get('invoice_id')
            or ''
        ).strip()
        if not old_fkey:
            return {
                'success': False,
                'error': 'VNPT: thiếu Fkey hóa đơn gốc cần thay thế.',
            }

        inv_no_old = info.get('SHDCLQuan') or info.get('invoice_no')
        if not inv_no_old or str(inv_no_old).strip() in ('0', '00000000'):
            return {
                'success': False,
                'error': (
                    'VNPT: hóa đơn gốc chưa có số chính thức — chỉ thay thế được HĐ đã phát hành.'
                ),
            }

        new_fkey = self._build_replacement_fkey(sale_data, old_fkey)
        seller = self._load_seller()
        xml_data = self._build_tt78_xml(
            sale_data,
            items,
            new_fkey,
            seller,
            replacement_info=info,
        )

        try:
            parsed = self._business_soap_call(
                'replaceInv',
                {
                    'Account': self.account,
                    'ACpass': self.account_pass,
                    'xmlInvData': xml_data,
                    'username': self.service_user,
                    'pass': self.service_pass,
                    'fkey': old_fkey,
                    'convert': 0,
                },
                fkey=new_fkey,
            )
            if not parsed.get('success'):
                err_code = self._vnpt_error_code(parsed.get('error'))
                if err_code == 'ERR:6':
                    return {
                        'success': False,
                        'error': self._format_err6_publish(
                            parsed.get('error'),
                            old_fkey,
                            soap_method='replaceInv',
                        ),
                    }
                return parsed

            result = self._finalize_issue_result(
                parsed,
                fkey=new_fkey,
                is_draft=False,
                sale_data=sale_data,
            )
            result['replaced_fkey'] = old_fkey
            result['ten_LoaiHDon'] = 'Hóa đơn thay thế'
            return result
        except requests.RequestException as exc:
            logger.exception('VNPT issue_replacement: %s', exc)
            return {'success': False, 'error': f'VNPT: lỗi kết nối BusinessService — {exc}'}

    def publish_draft(self, sale_data, items):
        """
        Phát hành HĐ chính thức từ nháp VNPT:
        1) ImportInvByPattern — cập nhật XML (MST, người mua, …) theo Fkey cũ
        2) PublishInvFkey — ký và cấp số (không dùng ImportAndPublishInv)
        """
        invoice_id = str(sale_data.get('invoice_id') or '').strip()
        if not invoice_id:
            return {'success': False, 'error': 'VNPT: thiếu Fkey hóa đơn nháp.'}
        fkey = invoice_id.replace(',', '_')
        payload = dict(sale_data or {})
        payload['invoice_id'] = fkey
        seller = self._load_seller()
        xml_data = self._build_tt78_xml(payload, items, fkey, seller)

        reimport = self._soap_call(
            'ImportInvByPattern',
            self._base_params(xml_data),
            fkey=fkey,
        )
        if not reimport.get('success'):
            err_code = self._vnpt_error_code(reimport.get('error'))
            if err_code != 'ERR:13':
                return reimport

        pub = self._soap_call('PublishInvFkey', {
            'Account': self.account,
            'ACpass': self.account_pass,
            'lsFkey': fkey,
            'username': self.service_user,
            'password': self.service_pass,
            'pattern': self.pattern,
            'serial': self.serial,
        }, fkey=fkey)
        if not pub.get('success'):
            pub_code = self._vnpt_error_code(pub.get('error'))
            if pub_code in ('ERR:6', 'ERR:15'):
                alt = self._soap_call(
                    'ImportAndPublishInv',
                    self._base_params(xml_data),
                    fkey=fkey,
                )
                if alt.get('success'):
                    return self._finalize_issue_result(
                        alt, fkey=fkey, is_draft=False, sale_data=sale_data,
                    )
                return {
                    'success': False,
                    'error': self._format_err6_publish(
                        pub.get('error') or alt.get('error'),
                        fkey,
                        soap_method='PublishInvFkey → ImportAndPublishInv',
                    ),
                }
            return pub
        return self._finalize_issue_result(
            pub, fkey=fkey, is_draft=False, sale_data=sale_data,
        )

    def sign_draft(self, invoice_id, sale_data=None, items=None):
        if not invoice_id:
            return {'success': False, 'error': 'VNPT: thiếu Fkey hóa đơn nháp.'}
        if sale_data and items:
            payload = dict(sale_data)
            payload['invoice_id'] = str(invoice_id).replace(',', '_')
            return self.publish_draft(payload, items)
        fkey = str(invoice_id).replace(',', '_')
        try:
            parsed = self._soap_call('PublishInvFkey', {
                'Account': self.account,
                'ACpass': self.account_pass,
                'lsFkey': fkey,
                'username': self.service_user,
                'password': self.service_pass,
                'pattern': self.pattern,
                'serial': self.serial,
            })
            if parsed.get('success'):
                sale_data = sale_data or {}
                if not parsed.get('invoice_no') and '_' in fkey:
                    parsed['invoice_no'] = fkey.rsplit('_', 1)[-1]
                return self._finalize_issue_result(
                    parsed, fkey=fkey, is_draft=False, sale_data=sale_data,
                )
            return parsed
        except requests.RequestException as exc:
            return {'success': False, 'error': f'VNPT PublishInvFkey: {exc}'}


class MobifoneInvoiceAdapter(BaseEInvoiceAdapter):
    """
    M-Invoice / MobiFone Invoice — REST API v4.7 (TT78/ND70).

    Tài liệu: wiki.minvoice.com.vn, API Mobifone Invoice 4.7
    Luồng: Login → SaveListHoadon78 → (SignInvoiceCertFile68 → SendInvoiceToCQT68)
    """
    supports_draft = True

    DEFAULT_BASE_URL = 'https://hoadon.minvoice.com.vn'
    SERIES_REF_ID = 'RF00059'

    def __init__(self, config):
        super().__init__(config)
        self.provider_key = 'mobifone'
        self.tax_code = (self.config.get('tax_code') or '').strip()
        self.username = (self.config.get('username') or '').strip()
        self.password = (self.config.get('password') or '').strip()
        self.inv_series = (self.config.get('invoice_series') or '').strip()
        self.cctbao_id = (self.config.get('minvoice_cctbao_id') or '').strip()
        self.has_code = str(self.config.get('minvoice_has_code', '1')).lower() not in ('0', 'false', 'no')
        self.type_cmd = '200' if self.has_code else '203'
        self.base_url = self._resolve_base_url()
        self.token = None
        self.ma_dvcs = (self.config.get('api_key') or '').strip()

    def _resolve_base_url(self):
        raw = (self.config.get('api_url') or '').strip().rstrip('/')
        if not raw:
            env_url = (os.environ.get('MOBIFONE_API_URL') or '').strip().rstrip('/')
            if env_url and 'minvoice.com.vn' in env_url.lower():
                raw = env_url
        if not raw:
            raw = self.DEFAULT_BASE_URL
        if '/api/' in raw.lower():
            raw = raw.split('/api/')[0].rstrip('/')
        if raw.startswith('http'):
            return raw
        mst = re.sub(r'[^0-9A-Za-z\-]', '', self.tax_code)
        if mst:
            return f'https://{mst}.minvoice.com.vn'
        return self.DEFAULT_BASE_URL

    def _login(self):
        if self.token and self.ma_dvcs:
            return True
        if not self.username or not self.password or not self.tax_code:
            return False
        url = f'{self.base_url}/api/Account/Login'
        payload = {
            'username': self.username,
            'password': self.password,
            'tax_code': self.tax_code,
        }
        try:
            resp = requests.post(url, json=payload, timeout=25)
            data = resp.json() if resp.content else {}
            if resp.status_code != 200 or data.get('error'):
                logger.error('M-Invoice login: %s', data.get('error') or resp.text[:300])
                return False
            self.token = data.get('token')
            self.ma_dvcs = data.get('ma_dvcs') or self.ma_dvcs
            return bool(self.token and self.ma_dvcs)
        except Exception as exc:
            logger.exception('M-Invoice login: %s', exc)
            return False

    def _headers(self):
        return {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.token};{self.ma_dvcs}',
        }

    def _resolve_cctbao_id(self):
        if self.cctbao_id:
            return self.cctbao_id
        url = (
            f'{self.base_url}/api/System/GetDataReferencesByRefId'
            f'?refId={self.SERIES_REF_ID}&tax_code={self.tax_code}'
        )
        try:
            resp = requests.get(url, headers=self._headers(), timeout=25)
            rows = resp.json() if resp.content else []
            if not isinstance(rows, list):
                return None
            series_key = (self.inv_series or '').strip().upper()
            for row in rows:
                kh = str(row.get('khhdon') or row.get('value') or '').upper()
                if series_key and kh == series_key:
                    return row.get('qlkhsdung_id') or row.get('id')
            if len(rows) == 1:
                return rows[0].get('qlkhsdung_id') or rows[0].get('id')
        except Exception as exc:
            logger.warning('M-Invoice GetDataReferences: %s', exc)
        return None

    def _tsuat_code(self, tax_pct):
        pct = float(tax_pct or 0)
        if pct <= 0:
            return '0'
        if pct == 5:
            return '5'
        if pct == 8:
            return '8'
        if pct == 10:
            return '10'
        return str(int(pct))

    def _build_invoice_body(self, sale_data, items, cctbao_id):
        details = []
        subtotal = 0.0
        tax_total = 0.0
        for idx, item in enumerate(items, start=1):
            qty = float(item.get('quantity') or 0)
            price = float(item.get('price') or 0)
            tax_pct = float(item.get('tax_pct') or 0)
            line = round(qty * price, 2)
            tax_val = round(line * tax_pct / 100.0, 2)
            subtotal += line
            tax_total += tax_val
            details.append({
                'stt': idx,
                'ma': str(item.get('product_code') or ''),
                'ten': str(item.get('name') or item.get('product_name') or ''),
                'dvtinh': str(item.get('unit') or 'Cái'),
                'sluong': qty,
                'dgia': price,
                'thtien': line,
                'tlckhau': float(item.get('discount_pct') or 0),
                'stckhau': round(line * float(item.get('discount_pct') or 0) / 100.0, 2),
                'tsuat': self._tsuat_code(tax_pct),
                'tthue': tax_val,
                'tgtien': round(line + tax_val, 2),
                'kmai': 1,
            })

        grand = round(subtotal + tax_total, 2)
        today = datetime.now().strftime('%Y-%m-%d')
        buyer_name = normalize_retail_buyer_name(sale_data.get('customer_name'))
        company = str(sale_data.get('company_name') or buyer_name)

        return {
            'editmode': 1,
            'data': [{
                'cctbao_id': cctbao_id,
                'nlap': today,
                'sdhang': str(sale_data.get('sale_no') or ''),
                'dvtte': 'VND',
                'tgia': 1,
                'htttoan': str(sale_data.get('payment_method') or 'Tiền mặt/Chuyển khoản'),
                'mnmua': str(sale_data.get('customer_code') or ''),
                'mst': str(sale_data.get('tax_code') or ''),
                'tnmua': buyer_name,
                'ten': company,
                'email': str(sale_data.get('email') or ''),
                'dchi': str(sale_data.get('address') or ''),
                'tgtcthue': round(subtotal, 2),
                'tgtthue': round(tax_total, 2),
                'tgtttbso': grand,
                'tgtttbso_last': grand,
                'mdvi': self.ma_dvcs,
                'tthdon': 0,
                'is_hdcma': 1 if self.has_code else 0,
                'details': [{'data': details}],
            }],
        }

    def _parse_save_response(self, resp):
        try:
            body = resp.json()
        except ValueError:
            return {'success': False, 'error': f'M-Invoice: phản hồi không hợp lệ — {resp.text[:300]}'}
        if isinstance(body, dict) and body.get('error'):
            return {'success': False, 'error': f"M-Invoice: {body.get('error')}"}
        if isinstance(body, dict) and body.get('Message'):
            return {'success': False, 'error': f"M-Invoice: {body.get('Message')}"}
        rows = body if isinstance(body, list) else [body]
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get('ok', '')).lower() == 'true' and isinstance(row.get('data'), dict):
                return {'success': True, 'row': row['data']}
            if row.get('data') and isinstance(row['data'], dict):
                return {'success': True, 'row': row['data']}
        return {'success': False, 'error': f'M-Invoice: tạo HĐ thất bại — {str(body)[:400]}'}

    def _sign_and_send(self, hdon_id):
        cer_serial = (self.config.get('serial_number') or '').strip()
        sign_url = f'{self.base_url}/api/Invoice68/SignInvoiceCertFile68'
        sign_payload = {
            'branch_code': self.ma_dvcs,
            'username': self.username,
            'lsthdon_id': [str(hdon_id)],
            'cer_serial': cer_serial,
            'type_cmd': self.type_cmd,
        }
        sign_resp = requests.post(sign_url, json=sign_payload, headers=self._headers(), timeout=45)
        if sign_resp.status_code != 200:
            return {'success': False, 'error': f'M-Invoice ký HĐ HTTP {sign_resp.status_code}: {sign_resp.text[:300]}'}

        send_url = f'{self.base_url}/api/Invoice68/SendInvoiceToCQT68'
        send_payload = {'invs': [str(hdon_id)], 'type_cmd': self.type_cmd}
        send_resp = requests.post(send_url, json=send_payload, headers=self._headers(), timeout=45)
        if send_resp.status_code != 200:
            return {'success': False, 'error': f'M-Invoice gửi CQT HTTP {send_resp.status_code}: {send_resp.text[:300]}'}
        try:
            send_body = send_resp.json()
        except ValueError:
            send_body = {}
        if isinstance(send_body, dict) and send_body.get('error'):
            return {'success': False, 'error': f"M-Invoice gửi CQT: {send_body.get('error')}"}
        return {'success': True}

    def issue(self, sale_data, items, loai_hdon=1, replace_unpublished=False):
        if not self._login():
            return {
                'success': False,
                'error': (
                    'M-Invoice: không đăng nhập được — kiểm tra URL, username, password, MST. '
                    'Test: https://hoadon.minvoice.com.vn · Chính thức: https://{MST}.minvoice.com.vn'
                ),
            }

        cctbao_id = self._resolve_cctbao_id()
        if not cctbao_id:
            return {
                'success': False,
                'error': (
                    'M-Invoice: không xác định được cctbao_id (dải ký hiệu). '
                    'Nhập Ký hiệu HĐ khớp portal hoặc ID dải (qlkhsdung_id) trong Cài đặt.'
                ),
            }

        url = f'{self.base_url}/api/Invoice68/SaveListHoadon78'
        payload = self._build_invoice_body(sale_data, items, cctbao_id)
        try:
            resp = requests.post(url, json=payload, headers=self._headers(), timeout=45)
            created = self._parse_save_response(resp)
            if not created.get('success'):
                return created
            row = created['row']
            hdon_id = row.get('hdon_id') or row.get('id')
            inv_no = row.get('shdon') or row.get('khieu')
            sbmat = row.get('sbmat')

            if normalize_loai_hdon(loai_hdon) == 0:
                return {
                    'success': True,
                    'is_draft': True,
                    'invoice_id': hdon_id,
                    'invoice_no': '0',
                    'transaction_id': sbmat,
                }

            signed = self._sign_and_send(hdon_id)
            if not signed.get('success'):
                return signed

            pdf_url = f'{self.base_url}/api/Invoice68/inHoadon?id={hdon_id}&type=PDF&inchuyendoi=false'
            return {
                'success': True,
                'is_draft': False,
                'invoice_id': hdon_id,
                'invoice_no': str(inv_no or hdon_id),
                'pdf_url': pdf_url,
                'transaction_id': sbmat,
                'tax_authority_status': row.get('tthai') or 'Chờ phản hồi CQT',
            }
        except requests.RequestException as exc:
            logger.exception('M-Invoice issue: %s', exc)
            return {'success': False, 'error': f'M-Invoice: {exc}'}
