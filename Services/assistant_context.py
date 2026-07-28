"""Ngữ cảnh sâu theo màn hình, loại hình HKD/DN và quyền người dùng."""
from __future__ import annotations

from typing import Any

# Gợi ý chi tiết theo endpoint Flask
PAGE_CONTEXT: dict[str, dict[str, Any]] = {
    'sale': {
        'label': 'Trang Bán Hàng (POS)',
        'hint': (
            'Người dùng đang bán hàng. Nhắc bật Xuất HĐĐT tự động, quét mã vạch, F1 đơn tạm, '
            'thanh toán tiền mặt/chuyển khoản VietQR.'
        ),
        'steps': ['Thêm sản phẩm → Chọn KH → Thanh toán → In/HĐĐT'],
    },
    'store_setup': {
        'label': 'Thiết Lập Cửa Hàng',
        'hint': 'Điền thông tin HKD/DN, MST, ngân hàng VietQR trước khi bán hàng và nhập quỹ.',
        'steps': ['Hệ Thống → Thiết Lập → Lưu thông tin cửa hàng & TK ngân hàng'],
    },
    'thiet_lap': {
        'label': 'Thiết Lập Cửa Hàng',
        'hint': 'Tương tự store_setup — thiết lập tenant.',
    },
    'settings_page': {
        'label': 'Cài Đặt hệ thống',
        'hint': 'Cấu hình HĐĐT (MISA/Viettel/VNPT), Sepay/Casso, chữ ký số.',
    },
    'inward_invoice': {
        'label': 'Hóa Đơn Mua Hàng',
        'hint': 'Lập phiếu nhập hoặc hạch toán từ HĐ đầu vào. Chú ý LOẠI HÀNG và đơn vị lẻ/sỉ.',
    },
    'DanhSachPhieuNhapKho': {
        'label': 'Danh Sách Phiếu Nhập Kho',
        'hint': 'Sửa phiếu nhập, in mã vạch, kiểm tra thanh toán NCC.',
    },
    'import_list': {
        'label': 'Mua Hàng / Nhập kho',
        'hint': 'Nhập tồn cũ Excel hoặc phiếu nhập thủ công.',
    },
    'inventory_check': {
        'label': 'Nhập Tồn Cũ & Kiểm Kê',
        'hint': 'Tải mẫu Excel → điền tồn → upload xác nhận.',
    },
    'HKD_dashboard': {
        'label': 'Dashboard Kế Toán HKD',
        'hint': 'Tổng quan DT/CP/LN, biểu đồ, menu sổ kế toán TT88.',
    },
    'SoQuyTienMat': {
        'label': 'Sổ Quỹ Tiền Mặt',
        'hint': 'Nộp quỹ ban đầu và các lần nộp tiền mặt sau.',
    },
    'SoTienGuiNganHang': {
        'label': 'Sổ Tiền Gửi Ngân Hàng',
        'hint': 'Nộp số dư TK ngân hàng ban đầu và các giao dịch NH.',
    },
    'SoCongNoPhaiThu': {
        'label': 'Công Nợ Phải Thu',
        'hint': 'Thu nợ khách — chọn KH → Thu Tiền từng đơn.',
    },
    'SoCongNoPhaiTra': {
        'label': 'Công Nợ Phải Trả NCC',
        'hint': 'Trả nợ nhà cung cấp cho phiếu nhập chưa thanh toán.',
    },
    'DanhSachPhieuChi': {
        'label': 'Phiếu Chi / Chi phí phát sinh',
        'hint': 'Lập Phiếu Chi – Chi Phát Sinh cho chi phí không có HĐĐT.',
    },
    'LapBangLuong': {
        'label': 'Lập Bảng Lương',
        'hint': 'Cuối tháng: Tải dữ liệu chấm công → Chốt bảng lương.',
    },
    'employees': {
        'label': 'Danh Sách Nhân Viên',
        'hint': 'Thêm/sửa NV trước khi lập bảng lương.',
    },
    'TSCD': {
        'label': 'Tài Sản Cố Định',
        'hint': 'Đưa TSCĐ vào sử dụng sau nhập kho — khấu hao tự động.',
    },
    'CCDC': {
        'label': 'Công Cụ Dụng Cụ',
        'hint': 'CCDC dưới 30 triệu — phân bổ chi phí định kỳ.',
    },
    'rental_service': {
        'label': 'Quản Lý Phòng Trọ',
        'hint': 'Thu tiền trước Trả Phòng; cập nhật chỉ số điện cuối tháng.',
    },
    'cap_nhat_kien_thuc_page': {
        'label': 'Cập Nhật Kiến Thức',
        'hint': 'Tin pháp luật TCT/BTC tự đồng bộ; lọc theo HKD/DN.',
    },
    'huong_dan_su_dung': {
        'label': 'Hướng Dẫn Sử Dụng',
        'hint': 'Tài liệu đầy đủ 3 phân hệ: Bán hàng, Kế toán, Phòng trọ.',
    },
    'order': {
        'label': 'Quản Lý Đơn Hàng',
        'hint': 'Mở đơn tạm F1, in bill, xuất HĐ batch.',
    },
    'products': {
        'label': 'Danh Mục Sản Phẩm',
        'hint': 'Sửa giá, in mã vạch, quản lý tồn theo sản phẩm.',
    },
}

REGIME_HINTS = {
    'HKD': 'Loại hình: Hộ kinh doanh — sổ kế toán TT 88/2021, thuế HKD.',
    'DN': 'Loại hình: Doanh nghiệp — TT99/TT58, VAS, thuế DN.',
    'SME': 'Loại hình: Doanh nghiệp SME.',
}

ROLE_HINTS = {
    'master': 'Quyền Master — quản trị toàn hệ thống.',
    'admin': 'Quyền Admin — cấu hình cửa hàng, duyệt tin.',
    'manager': 'Quyền Quản lý — báo cáo, duyệt một số nghiệp vụ.',
    'staff': 'Quyền Nhân viên — chủ yếu bán hàng.',
}


def build_context_prompt(ctx: dict[str, Any] | None) -> str:
    if not ctx:
        return ''
    parts: list[str] = []
    page = (ctx.get('page') or '').strip()
    path = (ctx.get('path') or '').strip()
    page_title = (ctx.get('page_title') or '').strip()
    form_id = (ctx.get('form_id') or '').strip()
    screen_hint = (ctx.get('screen_hint') or '').strip()
    regime = (ctx.get('regime') or '').strip().upper()
    role = (ctx.get('role') or '').strip()

    if page_title:
        parts.append(f'Tiêu đề trang: {page_title}')
    if path:
        parts.append(f'URL: {path}')
    if page and page in PAGE_CONTEXT:
        pc = PAGE_CONTEXT[page]
        parts.append(f'Màn hình: {pc.get("label", page)}')
        if pc.get('hint'):
            parts.append(f'Gợi ý màn hình: {pc["hint"]}')
        if pc.get('steps'):
            parts.append('Bước thường gặp: ' + ' → '.join(pc['steps']))
    elif page:
        parts.append(f'Endpoint: {page}')

    if regime and regime in REGIME_HINTS:
        parts.append(REGIME_HINTS[regime])
    elif regime:
        parts.append(f'Chế độ kế toán: {regime}')

    role_key = role.rstrip('*').rstrip('FB').rstrip('SME') if role else ''
    if role in ROLE_HINTS:
        parts.append(ROLE_HINTS[role])
    elif role_key in ROLE_HINTS:
        parts.append(ROLE_HINTS[role_key])
    elif role:
        parts.append(f'Vai trò: {role}')

    if form_id:
        parts.append(f'Form đang mở: #{form_id}')
    if screen_hint:
        parts.append(f'Ghi chú màn hình: {screen_hint}')

    tenant = (ctx.get('tenant_id') or '').strip()
    if tenant:
        parts.append(f'Tenant: {tenant}')

    return '\n'.join(parts)


def rag_section_for_regime(regime: str | None) -> str | None:
    r = (regime or '').upper()
    if r == 'HKD':
        return None
    if r in ('DN', 'SME'):
        return 'Kế toán'
    return None
