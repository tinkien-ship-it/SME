"""FAQ tĩnh + động (master duyệt) cho trợ lý AI."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from unicodedata import normalize

from Services.assistant_store import bump_faq_hit, list_dynamic_faq

STATIC_FAQ: list[dict[str, Any]] = [
    {
        'id': 'setup_store',
        'keywords': ['thiết lập', 'cửa hàng', 'ngân hàng', 'vietqr', 'thông tin shop', 'store setup'],
        'pages': ['store_setup', 'settings_page', 'thiet_lap'],
        'title': 'Thiết lập cửa hàng & ngân hàng',
        'answer': (
            'Vào **Hệ Thống → Thiết Lập** (`/thiet-lap`): điền thông tin cửa hàng, MST, địa chỉ và '
            'tài khoản ngân hàng (phục vụ VietQR khi bán hàng). Lưu xong mới nên nhập quỹ tiền mặt và số dư ngân hàng.'
        ),
    },
    {
        'id': 'cash_fund',
        'keywords': ['quỹ tiền mặt', 'nộp quỹ', 'sổ quỹ', 'tiền mặt ban đầu', 'phiếu thu quỹ'],
        'pages': ['SoQuyTienMat', 'HKD_dashboard'],
        'title': 'Nhập quỹ tiền mặt',
        'answer': (
            'Menu **TIỀN MẶT & TIỀN GỬI NH → Sổ Quỹ Tiền Mặt** → **Nộp Quỹ Tiền Mặt**: chọn ngày bắt đầu dùng phần mềm, '
            'nhập số tiền → **Lưu Phiếu Thu**. Nếu chưa có quỹ, phiếu nhập kho không chọn được **Đã thanh toán**.'
        ),
    },
    {
        'id': 'bank_balance',
        'keywords': ['số dư ngân hàng', 'nộp tiền vào tài khoản', 'sổ tiền gửi', 'tiền gửi nh'],
        'pages': ['SoTienGuiNganHang'],
        'title': 'Nhập số dư tài khoản ngân hàng',
        'answer': (
            '**TIỀN MẶT & TIỀN GỬI NH → Sổ Tiền Gửi NH** → **Nộp Tiền Vào Tài Khoản**: nhập số dư tại ngày bắt đầu '
            'sử dụng phần mềm → **Lưu Phiếu Thu**. Nộp thêm tiền sau này làm tương tự.'
        ),
    },
    {
        'id': 'import_invoice',
        'keywords': ['nhập kho', 'hóa đơn mua', 'phiếu nhập', 'lập phiếu nhập', 'hóa đơn đầu vào'],
        'pages': ['inward_invoice', 'DanhSachPhieuNhapKho', 'import_list'],
        'title': 'Lập phiếu nhập kho từ hóa đơn mua',
        'answer': (
            '**Mua Hàng → Hóa Đơn Mua Hàng** → **Lập Phiếu Nhập**. Hệ thống tự điền chi tiết; bạn chỉ cần '
            'thiết lập đơn vị lẻ/sỉ, giá bán, kho và **LOẠI HÀNG** (hàng hóa, dịch vụ, CCDC, TSCĐ). '
            'Chọn phương thức thanh toán → **Lưu Phiếu Nhập**.'
        ),
    },
    {
        'id': 'import_stock',
        'keywords': ['tồn kho cũ', 'tồn ban đầu', 'kiểm kê', 'excel tồn', 'nhập tồn'],
        'pages': ['inventory_check', 'import_list'],
        'title': 'Nhập tồn kho ban đầu',
        'answer': (
            '**Mua Hàng → Nhập Tồn Cũ & Kiểm Kê** → **Tải Mẫu Excel** → điền hàng tồn trước khi dùng phần mềm → '
            '**Chọn file** → **Xác Nhận Nhập Tồn Kho Ban Đầu**.'
        ),
    },
    {
        'id': 'barcode',
        'keywords': ['mã vạch', 'in barcode', 'quét mã', 'barcode'],
        'pages': ['DanhSachPhieuNhapKho', 'products'],
        'title': 'In mã vạch',
        'answer': (
            '**Danh Sách Phiếu Nhập Kho** → **In Mã Vạch** hoặc **Danh Mục Sản Phẩm** → chọn hàng → '
            'nhập số lượng mã lẻ/sỉ → **Tạo & In Ngay**. Có thể quét bằng camera điện thoại hoặc máy quét.'
        ),
    },
    {
        'id': 'sale_basic',
        'keywords': ['bán hàng', 'pos', 'tính tiền', 'xuất hóa đơn', 'hóa đơn điện tử', 'hđđt'],
        'pages': ['sale', 'order'],
        'title': 'Bán hàng & hóa đơn điện tử',
        'answer': (
            'Trang **Bán Hàng**: bật **Tự Động Xuất Hóa Đơn Điện Tử** khi bán. '
            'Đơn tạm: **F1** hoặc lưu tại **Quản Lý Đơn Hàng**. '
            'Sửa HĐ đã xuất: **Hóa Đơn Bán Hàng → Xuất Hóa Đơn Thay Thế**.'
        ),
    },
    {
        'id': 'return_goods',
        'keywords': ['trả hàng', 'đổi trả', 'hủy đơn', 'khách trả'],
        'pages': ['sale', 'return'],
        'title': 'Khách trả hàng',
        'answer': (
            '**Chưa xuất HĐĐT:** **Khách Trả Hàng** → chọn đơn → xác nhận. '
            '**Đã xuất HĐĐT:** dùng **Xuất Hóa Đơn Thay Thế** — trả hết thì số lượng = 0, '
            'khách chọn **Bán cho người tiêu dùng** nếu cần.'
        ),
    },
    {
        'id': 'debt_customer',
        'keywords': ['công nợ phải thu', 'thu nợ', 'khách nợ', 'phiếu thu khách'],
        'pages': ['SoCongNoPhaiThu'],
        'title': 'Thu nợ khách hàng',
        'answer': (
            '**Công Nợ → Công Nợ Phải Thu** → chọn khách → **Thu Tiền** cho từng đơn → '
            'ngày, hình thức, số tiền → **LƯU PHIẾU THU**.'
        ),
    },
    {
        'id': 'debt_supplier',
        'keywords': ['công nợ phải trả', 'trả ncc', 'trả nhà cung cấp', 'phiếu chi ncc'],
        'pages': ['SoCongNoPhaiTra'],
        'title': 'Trả nợ nhà cung cấp',
        'answer': (
            '**Công Nợ → Công Nợ Phải Trả NCC** → chọn NCC → **Trả Tiền** → '
            'ngày ghi sổ, phương thức → **XÁC NHẬN CHI TIỀN**.'
        ),
    },
    {
        'id': 'payroll',
        'keywords': ['lương', 'bảng lương', 'chấm công', 'nhân viên', 'bhxh'],
        'pages': ['DanhSachBangLuong_05LDTL', 'LapBangLuong', 'employees', 'attendance'],
        'title': 'Nhân sự & bảng lương',
        'answer': (
            '**NHÂN SỰ & LƯƠNG → Danh Sách Nhân Viên** để thêm NV. Cuối tháng: **Lập Bảng Lương** → '
            '**Tải dữ liệu** → **Chốt Bảng Lương**. Trả lương tại **Công Nợ Phải Trả Nhân Viên**; '
            'nộp BHXH tại **Công Nợ BHXH/BHYT/BHTN**.'
        ),
    },
    {
        'id': 'fixed_assets',
        'keywords': ['tscd', 'ccdc', 'khấu hao', 'tài sản cố định', '30 triệu'],
        'pages': ['TSCD', 'CCDC'],
        'title': 'TSCĐ & CCDC',
        'answer': (
            'Sau nhập kho TSCĐ/CCDC: **TSCĐ & CCDC** → **ĐƯA TSCĐ/CCDC VÀO SỬ DỤNG** → '
            'ngày bắt đầu, số tháng khấu hao/phân bổ. Từ **30 triệu** trở lên là TSCĐ; dưới 30 triệu là CCDC.'
        ),
    },
    {
        'id': 'einvoice_config',
        'keywords': ['cấu hình hóa đơn', 'misa', 'viettel', 'vnpt', 'kết nối hđđt', 'einvoice'],
        'pages': ['settings_page', 'invoice_einvoice'],
        'title': 'Cấu hình hóa đơn điện tử',
        'answer': (
            '**Cài Đặt → Hóa đơn điện tử**: chọn nhà cung cấp (MISA, Viettel, VNPT…), nhập App ID, '
            'MST, user/pass theo hướng dẫn NCC. Kiểm tra kết nối trước khi bật tự động xuất HĐ trên trang bán hàng.'
        ),
    },
    {
        'id': 'rental',
        'keywords': ['phòng trọ', 'cho thuê', 'lưu trú', 'thu tiền phòng', 'chỉ số điện'],
        'pages': ['rental_service'],
        'title': 'Quản lý phòng trọ',
        'answer': (
            '**Quản Lý DV Thuê Phòng**: Bước 1 — tải mẫu Excel nhập số phòng & giá. '
            'Bước 2 — Sơ đồ phòng, điền thông tin người thuê, chỉ số điện bàn giao. '
            '**Thu Tiền** trước, sau đó mới **Trả Phòng**. Cuối tháng cập nhật chỉ số điện mới rồi in phiếu thanh toán.'
        ),
    },
    {
        'id': 'accounting_auto',
        'keywords': ['kế toán', 'sổ sách', 'chứng từ', 'hạch toán', 'phiếu chi', 'tt 88', 'thông tư 88'],
        'pages': ['HKD_dashboard', 'DanhSachPhieuChi'],
        'title': 'Kế toán HKD',
        'answer': (
            'Hầu hết chứng từ và sổ kế toán (TT 88) được lập **tự động** từ bán hàng, nhập kho, lương, TSCĐ. '
            'Chi phí không có HĐĐT: **Chứng Từ Kế Toán → Phiếu Chi → Lập Phiếu Chi – Chi Phát Sinh**.'
        ),
    },
    {
        'id': 'knowledge',
        'keywords': ['pháp luật', 'thuế', 'cập nhật kiến thức', 'bản tin', 'tct', 'bộ tài chính'],
        'pages': ['cap_nhat_kien_thuc_page'],
        'title': 'Cập nhật pháp luật & thuế',
        'answer': (
            'Menu **Cập Nhật Kiến Thức** (`/cap-nhat-kien-thuc`): tin từ Tổng cục Thuế và Bộ Tài Chính '
            'tự cập nhật hàng ngày. Nhấn tiêu đề có biểu tượng ↗ để xem văn bản gốc.'
        ),
    },
    {
        'id': 'help_full',
        'keywords': ['hướng dẫn', 'hd sd', 'cách dùng', 'tutorial', 'ultraview', 'từ xa'],
        'pages': [],
        'title': 'Hướng dẫn đầy đủ',
        'answer': (
            'Xem **Hướng Dẫn Sử Dụng** trong menu Kế Toán HKD. '
            'Cần hỗ trợ từ xa: cài **UltraViewer**, gửi mã kết nối qua Zalo **0908870287**.'
        ),
    },
    {
        'id': 'support_zalo',
        'keywords': ['liên hệ', 'hotline', 'zalo', 'hỗ trợ', 'tư vấn', '0908870287'],
        'pages': [],
        'title': 'Liên hệ hỗ trợ',
        'answer': (
            'Nhắn Zalo **0908870287** (Trung Tín — KETO POS) để được tư vấn và hỗ trợ trực tiếp. '
            'Giờ hành chính: phản hồi trong ngày; ngoài giờ có thể trả lời sáng hôm sau.'
        ),
    },
]

PAGE_SUGGESTIONS: dict[str, list[str]] = {
    'sale': ['Làm sao bán hàng và xuất HĐĐT?', 'Khách trả hàng thế nào?', 'Đơn tạm (F1) dùng ra sao?'],
    'store_setup': ['Thiết lập cửa hàng ở đâu?', 'Nhập quỹ tiền mặt ban đầu?', 'Cấu hình VietQR?'],
    'thiet_lap': ['Thiết lập cửa hàng ở đâu?', 'Nhập quỹ tiền mặt ban đầu?'],
    'settings_page': ['Cấu hình hóa đơn điện tử?', 'Kết nối ngân hàng Sepay/Casso?'],
    'inward_invoice': ['Lập phiếu nhập từ hóa đơn mua?', 'Hạch toán dịch vụ mua ngay?'],
    'DanhSachPhieuNhapKho': ['In mã vạch sản phẩm?', 'Sửa phiếu nhập kho sai?'],
    'HKD_dashboard': ['Sổ kế toán tự động thế nào?', 'Xem báo cáo lãi lỗ?'],
    'SoCongNoPhaiThu': ['Thu nợ khách hàng?', 'Xem công nợ phải thu?'],
    'SoCongNoPhaiTra': ['Trả nợ nhà cung cấp?'],
    'rental_service': ['Quy trình cho thuê phòng?', 'Thu tiền phòng trọ?'],
    'LapBangLuong': ['Lập bảng lương cuối tháng?', 'Chấm công nhân viên?'],
    'cap_nhat_kien_thuc_page': ['Tin pháp luật HKD mới?', 'Tra cứu thông tư thuế?'],
    '_default': [
        'Bắt đầu sử dụng phần mềm?',
        'Nhập kho và bán hàng?',
        'Liên hệ Zalo hỗ trợ?',
    ],
}

ESCALATION_KEYWORDS = [
    'gặp lỗi', 'bị lỗi', 'không được', 'không chạy', 'sai số', 'mất dữ liệu',
    'khiếu nại', 'hoàn tiền', 'gọi điện', 'nhân viên', 'người thật',
]

_dynamic_cache: list[dict] | None = None


@dataclass
class FaqMatch:
    entry: dict[str, Any]
    score: float


def _normalize(text: str) -> str:
    if not text:
        return ''
    t = normalize('NFD', text.lower())
    return ''.join(c for c in t if ord(c) < 768).strip()


def _tokenize(text: str) -> set[str]:
    norm = _normalize(text)
    return {w for w in re.split(r'[\s,./;:!?()\[\]"\']+', norm) if len(w) >= 2}


def _dynamic_entries() -> list[dict[str, Any]]:
    global _dynamic_cache
    try:
        rows = list_dynamic_faq(status='approved')
        entries = []
        for r in rows:
            kw = r.get('keywords') or '[]'
            pages = r.get('pages') or '[]'
            try:
                kw_list = json.loads(kw) if isinstance(kw, str) else kw
            except json.JSONDecodeError:
                kw_list = []
            try:
                pages_list = json.loads(pages) if isinstance(pages, str) else pages
            except json.JSONDecodeError:
                pages_list = []
            if not kw_list:
                kw_list = _tokenize(r.get('question') or '')
            entries.append({
                'id': f'dyn_{r["id"]}',
                'dyn_id': r['id'],
                'keywords': list(kw_list),
                'pages': list(pages_list),
                'title': (r.get('question') or '')[:80],
                'answer': r.get('answer') or '',
            })
        _dynamic_cache = entries
        return entries
    except Exception:
        return _dynamic_cache or []


def get_all_faq_entries() -> list[dict[str, Any]]:
    return STATIC_FAQ + _dynamic_entries()


def invalidate_dynamic_cache() -> None:
    global _dynamic_cache
    _dynamic_cache = None


def search_faq(query: str, *, page: str | None = None) -> FaqMatch | None:
    q_norm = _normalize(query)
    q_tokens = _tokenize(query)
    if not q_norm:
        return None

    best: FaqMatch | None = None
    for entry in get_all_faq_entries():
        score = 0.0
        title_norm = _normalize(entry.get('title') or '')
        if title_norm and title_norm in q_norm:
            score += 2.0
        for kw in entry.get('keywords') or []:
            kw_n = _normalize(str(kw))
            if kw_n in q_norm:
                score += 3.0 + len(kw_n) * 0.05
            kw_tokens = _tokenize(str(kw))
            overlap = len(q_tokens & kw_tokens)
            score += overlap * 1.2

        if page and page in (entry.get('pages') or []):
            score += 1.5

        if score > 0 and (best is None or score > best.score):
            best = FaqMatch(entry=entry, score=score)

    if best and best.score >= 1.5:
        dyn_id = best.entry.get('dyn_id')
        if dyn_id:
            try:
                bump_faq_hit(int(dyn_id))
            except Exception:
                pass
        return best
    return None


def get_suggestions(page: str | None = None) -> list[str]:
    if page and page in PAGE_SUGGESTIONS:
        return PAGE_SUGGESTIONS[page][:4]
    return PAGE_SUGGESTIONS['_default']


def should_escalate(message: str) -> bool:
    n = _normalize(message)
    return any(kw in n for kw in ESCALATION_KEYWORDS)
