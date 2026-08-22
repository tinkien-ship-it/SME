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
        'label': 'Cập Nhật Kiến Thức (HKD)',
        'hint': 'Tin pháp luật HKD từ TCT/BTC; lọc theo hộ kinh doanh.',
    },
    'SME_cap_nhat_kien_thuc': {
        'label': 'Cập Nhật Kiến Thức DN',
        'hint': 'Chỉ thông tư, nghị định, thuế doanh nghiệp (TT99/TT58).',
    },
    'huong_dan_su_dung': {
        'label': 'Hướng Dẫn Sử Dụng',
        'hint': (
            'Tab: Bán hàng | Kế Toán HKD | Kế Toán SME (nhánh TT58 DNSN / TT99) | F&B | Phòng trọ. '
            'Doanh nghiệp SME mở tab Kế Toán SME rồi chọn đúng chế độ tenant.'
        ),
    },
    'order': {
        'label': 'Quản Lý Đơn Hàng',
        'hint': 'Mở đơn tạm F1, in bill, xuất HĐ batch.',
    },
    'products': {
        'label': 'Danh Mục Sản Phẩm',
        'hint': (
            'Sửa giá, in mã vạch, quản lý tồn. Tab Thành phẩm: mã TP001…, mã vạch TP00101/TP00102. '
            'Vật tư sản xuất thường mã VT… (từ phiếu nhập loại Vật Tư).'
        ),
    },
    'production_page': {
        'label': 'Tính Giá Thành (Thành Phẩm)',
        'hint': (
            'Định mức BOM (thành phẩm + vật tư VT) → phiếu sản xuất → xuất vật tư (giá vốn bình quân) '
            '+ nhập thành phẩm. Giá thành/ĐV = (vật tư + nhân công + chi phí khác) ÷ số lượng hoàn thành. '
            'Nút +: thêm thành phẩm (mã TP001…).'
        ),
        'steps': [
            'Định mức BOM',
            'Chọn thành phẩm + số lượng (+ nhân công/chi phí khác)',
            'Tính vật tư & giá thành',
            'Hoàn thành phiếu SX',
            'In phiếu / Hủy phiếu nếu cần',
        ],
    },
    'production_print': {
        'label': 'In phiếu sản xuất',
        'hint': 'Phiếu in gồm vật tư xuất, nhân công, chi phí khác và giá thành đơn vị thành phẩm.',
    },
    'F_and_B_service': {
        'label': 'F&B — Nhà hàng / quán ăn / cà phê / trà sữa',
        'hint': (
            'Dịch vụ ẩm thực: tạo khu vực & bàn → tạo thực đơn → nhập kho '
            '(Hàng Dùng Ngay / Nguyên Vật Liệu) → gọi món theo bàn → thanh toán. '
            'Có thể lập Định mức NVL từng món, hoặc Kiểm Kê NVL cuối ngày rồi Chốt doanh thu. '
            'Xem tab Dịch Vụ Ẩm Thực (F&B) trong Hướng Dẫn Sử Dụng.'
        ),
        'steps': [
            'Tạo khu vực & bàn',
            'Tạo thực đơn (+ hình món)',
            'Nhập kho NVL / hàng dùng ngay',
            'Gọi món theo bàn → theo dõi phục vụ',
            'Thanh toán / xuất HĐĐT',
            'Định mức hoặc Kiểm kê NVL cuối ngày',
        ],
    },
    # —— Kế toán SME (TT99 / TT58) ——
    'SME_dashboard': {
        'label': 'Hub Kế toán doanh nghiệp',
        'hint': 'Menu trung tâm SME. Chọn chi nhánh trên thanh trên (Tất cả = hợp nhất).',
    },
    'SME_SoQuyTienMat': {
        'label': 'Sổ Quỹ Tiền Mặt (SME)',
        'hint': (
            'Sổ kép TK 111. Nút Lập phiếu thu: mặc định Nợ 1111 · Có 4111 Vốn góp chủ sở hữu. '
            'Đổi tài khoản Có khi thu nợ khách (131) hoặc tạm ứng (141).'
        ),
        'steps': ['Chọn chi nhánh', 'Lập phiếu thu / lọc kỳ', 'Đối chiếu nhật ký bút toán'],
    },
    'SME_SoTienGuiNganHang': {
        'label': 'Sổ Tiền Gửi Ngân Hàng (SME)',
        'hint': 'Sổ kép TK 112. Lập phiếu thu mặc định Nợ 1121 · Có 4111.',
    },
    'SME_SoCongNoPhaiThu': {
        'label': 'Sổ Công Nợ Phải Thu (SME)',
        'hint': 'Theo dõi phải thu khách (131). Thu nợ bằng phiếu thu gắn đúng đối tượng.',
    },
    'SME_SoCongNoPhaiTra': {
        'label': 'Sổ Công Nợ Phải Trả (SME)',
        'hint': 'Theo dõi phải trả NCC (331). Trả nợ bằng phiếu chi hoặc chức năng trả trên sổ.',
    },
    'SME_PhaiThuCongNhanVien': {
        'label': 'Sổ Phải Thu Nhân Viên (SME)',
        'hint': 'Tạm ứng / phải thu nhân viên (141).',
    },
    'SME_PhaiTraCongNhanVien': {
        'label': 'Công nợ phải trả nhân viên (SME)',
        'hint': 'Trả lương cả kỳ (1 phiếu chi) hoặc trả lẻ NV — Nợ 3341 / Có 1111|1121.',
    },
    'SME_dashboard_debt': {
        'label': 'Tiền Và Công Nợ (SME)',
        'hint': 'Nhóm sổ quỹ, ngân hàng, công nợ phải thu/trả và nhân viên.',
    },
    'SME_journal': {
        'label': 'Nhật ký bút toán (SME)',
        'hint': 'Xem/lọc/đảo bút toán sổ kép. Kỳ đã khóa thì không ghi mới.',
    },
    'SME_general_ledger': {
        'label': 'Sổ cái và cân đối phát sinh (SME)',
        'hint': 'Đối chiếu số dư tài khoản theo kỳ và chi nhánh.',
    },
    'SME_auto_posting': {
        'label': 'Kết chuyển, khóa sổ và cuối năm',
        'hint': (
            'Chạy kỳ: khấu hao TSCĐ → phân bổ CCDC → kết chuyển DT/CP → 911 → 4212 → '
            'quyết toán thuế GTGT → khóa sổ. Cuối năm: 4212 → 4211.'
        ),
        'steps': ['Chọn kỳ', 'Chạy tự động hóa', 'Kiểm tra nhật ký', 'Khóa sổ'],
    },
    'SME_fixed_assets': {
        'label': 'Danh mục tài sản cố định (SME)',
        'hint': 'Nhập TSCĐ qua phiếu nhập → thiết lập khấu hao → chạy kỳ tại Kết chuyển khóa sổ.',
    },
    'SME_TSCD': {
        'label': 'Tổng quan tài sản cố định (SME)',
        'hint': 'Dashboard TSCĐ — trạng thái Trong kho / Đang sử dụng / Đã thanh lý.',
    },
    'SME_tools': {
        'label': 'Danh mục công cụ dụng cụ (SME)',
        'hint': 'Nhập CCDC qua phiếu nhập → thiết lập phân bổ hoặc đưa vào sử dụng.',
    },
    'SME_CCDC': {
        'label': 'Tổng quan công cụ dụng cụ (SME)',
        'hint': 'Dashboard CCDC và phân bổ theo kỳ.',
    },
    'SME_import': {
        'label': 'Lập phiếu nhập kho (SME)',
        'hint': 'Chọn loại hàng: hàng hóa, NVL, TSCĐ, CCDC. Gắn đúng kho/chi nhánh.',
    },
    'SME_inward_invoice': {
        'label': 'Hóa đơn mua hàng (SME)',
        'hint': 'Từ HĐ mua → lập phiếu nhập hoặc hạch toán dịch vụ.',
    },
    'SME_production': {
        'label': 'Sản xuất (SME)',
        'hint': (
            'Lập lệnh: xuất NVL + CPSX vào 154. '
            'Trên mỗi lệnh: nút Nhập kho thành phẩm (theo đợt hoặc đủ) → mới Nợ 155 / Có 154.'
        ),
    },
    'SME_costing': {
        'label': 'Kế toán giá thành (SME)',
        'hint': 'Tập hợp chi phí 621/622/627 và lệnh sản xuất.',
    },
    'SME_BCTC_reports': {
        'label': 'Bộ báo cáo tài chính (SME)',
        'hint': 'Báo cáo B01–B09 theo kỳ; xuất Excel khi cần.',
    },
    'SME_vat_declaration': {
        'label': 'Tờ khai thuế GTGT (SME)',
        'hint': 'Lập tờ khai thuế giá trị gia tăng theo kỳ kê khai của doanh nghiệp.',
    },
    'SME_tax_nsnn': {
        'label': 'Thuế và ngân sách nhà nước (SME)',
        'hint': 'Theo dõi số dư TK 133 / 333 từ sổ kép.',
    },
    'SME_salary_create': {
        'label': 'Lập bảng lương (SME)',
        'hint': 'Mẫu 01-LĐTL — tải chấm công → chốt bảng lương → trả qua sổ phải trả NV.',
    },
    'SME_branches': {
        'label': 'Chi nhánh và kho (SME)',
        'hint': 'Danh mục chi nhánh/đơn vị và gắn kho (KHO_001…). HQ = trụ sở chính.',
    },
    'SME_DanhSachPhieuThu': {
        'label': 'Phiếu thu — mẫu 01-TT',
        'hint': 'Danh sách phiếu thu SME; có thể lập nhanh từ sổ quỹ / sổ ngân hàng.',
    },
    'SME_DanhSachPhieuChi': {
        'label': 'Phiếu chi — mẫu 02-TT',
        'hint': 'Chi tiền mặt hoặc chuyển khoản; Nợ chi phí/công nợ · Có 1111/1121.',
    },
}

REGIME_HINTS = {
    'HKD': 'Loại hình: Hộ kinh doanh — sổ kế toán TT 88/2021, thuế HKD. Không hướng dẫn menu SME.',
    'DN': 'Loại hình: Doanh nghiệp — TT99/TT58, sổ kép. Dùng ngôn ngữ menu Kế toán doanh nghiệp.',
    'SME': (
        'Loại hình: Doanh nghiệp SME (TT99 hoặc TT58) — sổ kép. '
        'Menu: Kế toán doanh nghiệp. Mở Hướng dẫn → tab Kế Toán SME → chọn đúng TT58 hoặc TT99. '
        'Không nhầm với thao tác HKD (Nộp Quỹ kiểu TT88).'
    ),
    'SME_TT99': (
        'Doanh nghiệp Thông tư 99/2025/TT-BTC — sổ kép đầy đủ, BCTC B01–B09, L/C, ngoại tệ, vay. '
        'Hướng dẫn: tab Kế Toán SME → SME TT99. Không chỉ dẫn sổ DNSN (TT58).'
    ),
    'SME_MICRO_TT58': (
        'Doanh nghiệp siêu nhỏ Thông tư 58 — DNSN. Nghiệp vụ mua/bán/kế toán mở đầy đủ như TT99 '
        '(DN siêu nhỏ có thể phát sinh nghiệp vụ giống DN thường). '
        'Phải chọn Trường hợp thuế 1–4 và in đủ sổ DNSN (framework CQT). '
        'Hướng dẫn: tab Kế Toán SME → SME TT58.'
    ),
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
    elif regime and regime.startswith('SME'):
        parts.append(REGIME_HINTS['SME'])
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
    if 'TT58' in r or 'MICRO' in r:
        return 'Kế toán SME TT58'
    if 'TT99' in r:
        return 'Kế toán SME TT99'
    if r.startswith('SME') or r in ('DN',):
        return 'Kế toán SME'
    return None
