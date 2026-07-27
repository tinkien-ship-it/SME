from abc import ABC, abstractmethod
import logging

class InvoiceProvider(ABC):
    """Interface chung cho mọi nhà cung cấp HĐĐT"""

    def __init__(self, config):
        self.config = config

    @abstractmethod
    def get_token(self):
        """Lấy access token"""
        pass

    @abstractmethod
    def create_draft(self, invoice_data, items):
        """Tạo hóa đơn nháp, trả về invoice_code"""
        pass

    @abstractmethod
    def sign_invoice(self, invoice_code):
        """Ký điện tử"""
        pass

    @abstractmethod
    def issue_invoice(self, invoice_code):
        """Phát hành hóa đơn, trả về số HĐ, pdf_url, xml_url"""
        pass

    def issue_full(self, sale_id, customer_name, tax_code, address, items, total_amount):
        """Quy trình đầy đủ: tạo → ký → phát hành"""
        try:
            token = self.get_token()
            if not token:
                return {"success": False, "error": "Không lấy được token"}

            draft_res = self.create_draft({
                "sale_id": sale_id,
                "customer_name": customer_name,
                "tax_code": tax_code,
                "address": address,
                "total_amount": total_amount
            }, items)

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
        except Exception as e:
            logging.error(f"Lỗi xuất HĐĐT {self.__class__.__name__}: {e}")
            return {"success": False, "error": str(e)}