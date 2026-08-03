"""Cấu hình menu trung tâm Kế Toán SME (TT58 / TT99) — tách biệt HKD (hộ kinh doanh)."""

SME_HUB_TITLE = 'Kế toán doanh nghiệp'

SME_MENU_SECTIONS = (
    {'id': 'operations', 'label': 'NGHIỆP VỤ KINH DOANH'},
    {'id': 'accounting', 'label': 'KẾ TOÁN & TÀI CHÍNH'},
    {'id': 'reports', 'label': 'BÁO CÁO & HỆ THỐNG'},
)

SME_MENU_GROUPS = (
    {
        'id': 'overview', 'section': 'operations', 'label': 'Tổng quan',
        'icon': 'fas fa-chart-pie', 'color': 'primary', 'endpoint': 'SME_dashboard',
        'description': 'Trung tâm chỉ số tài chính doanh nghiệp (Thông tư 58 / Thông tư 99)',
        'items': (),
    },
    {
        'id': 'sales', 'section': 'operations', 'label': 'Bán hàng',
        'icon': 'fas fa-cart-shopping', 'color': 'success', 'endpoint': 'SME_dashboard_sale',
        'description': 'Điểm bán hàng, đơn hàng, hóa đơn bán, trả hàng',
        'items': (
            {'endpoint': 'sale', 'label': 'Điểm bán hàng', 'icon': 'fas fa-cash-register'},
            {'endpoint': 'SME_sale_export', 'label': 'Bán hàng xuất khẩu', 'icon': 'fas fa-plane-departure'},
            {'endpoint': 'SME_sale_export_list', 'label': 'Danh sách xuất khẩu', 'icon': 'fas fa-list'},
            {'endpoint': 'SME_customs_declarations', 'label': 'Tờ khai hải quan điện tử', 'icon': 'fas fa-passport'},
            {'endpoint': 'SME_order', 'label': 'Danh sách đơn hàng', 'icon': 'fas fa-list'},
            {'endpoint': 'SME_sale_details', 'label': 'Chi tiết hàng bán', 'icon': 'fas fa-receipt'},
            {'endpoint': 'SME_bank_transactions', 'label': 'Giao dịch ngân hàng', 'icon': 'fas fa-university'},
            {'endpoint': 'SME_outward_invoice', 'label': 'Hóa đơn bán hàng', 'icon': 'fas fa-file-export'},
            {'endpoint': 'SME_return_sale', 'label': 'Khách trả hàng', 'icon': 'fas fa-rotate-left'},
        ),
    },
    {
        'id': 'purchasing', 'section': 'operations', 'label': 'Mua hàng',
        'icon': 'fas fa-bag-shopping', 'color': 'warning', 'endpoint': 'SME_purchasing',
        'description': 'Đơn mua, hóa đơn đầu vào, nhập kho và trả nhà cung cấp',
        'items': (
            {'endpoint': 'SME_purchase_order_create', 'label': 'Lập đơn mua hàng', 'icon': 'fas fa-circle-plus'},
            {'endpoint': 'SME_purchase_order_list', 'label': 'Danh sách đơn mua', 'icon': 'fas fa-clipboard-list'},
            {'endpoint': 'SME_inward_invoice', 'label': 'Hóa đơn mua hàng', 'icon': 'fas fa-file-import'},
            {'endpoint': 'SME_landed_cost', 'label': 'Phân bổ chi phí mua hàng', 'icon': 'fas fa-share-alt'},
            {'endpoint': 'SME_import', 'label': 'Lập phiếu nhập kho', 'icon': 'fas fa-box-open'},
            {'endpoint': 'SME_customs_declarations', 'label': 'Tờ khai hải quan điện tử', 'icon': 'fas fa-passport'},
            {'endpoint': 'SME_letter_of_credit', 'label': 'Mở L/C nhập khẩu', 'icon': 'fas fa-file-signature'},
            {'endpoint': 'SME_DanhSachPhieuNhapKho', 'label': 'Danh sách phiếu nhập', 'icon': 'fas fa-list-check'},
            {'endpoint': 'SME_import_details', 'label': 'Chi tiết mua hàng', 'icon': 'fas fa-list'},
            {'endpoint': 'SME_return_supplier', 'label': 'Trả nhà cung cấp', 'icon': 'fas fa-rotate-left'},
        ),
    },
    {
        'id': 'warehouse', 'section': 'operations', 'label': 'Tồn kho',
        'icon': 'fas fa-warehouse', 'color': 'warning', 'endpoint': 'SME_dashboard_warehouse',
        'description': 'Theo dõi tồn kho và báo cáo vật tư hàng hóa',
        'items': (
            {'endpoint': 'inventory', 'label': 'Tồn kho', 'icon': 'fas fa-boxes-stacked'},
            {'endpoint': 'inventory_detail', 'label': 'Báo cáo tồn kho', 'icon': 'fas fa-chart-column'},
            {'endpoint': 'SME_branches', 'label': 'Danh mục kho và chi nhánh', 'icon': 'fas fa-warehouse'},
        ),
    },
    {
        'id': 'catalog', 'section': 'operations', 'label': 'Danh mục',
        'icon': 'fas fa-box', 'color': 'info', 'endpoint': 'products',
        'description': 'Sản phẩm, nhà cung cấp và khách hàng',
        'items': (
            {'endpoint': 'products', 'label': 'Danh mục sản phẩm', 'icon': 'fas fa-box'},
            {'endpoint': 'suppliers_page', 'label': 'Nhà cung cấp', 'icon': 'fas fa-truck'},
            {'endpoint': 'customers_page', 'label': 'Khách hàng', 'icon': 'fas fa-users'},
        ),
    },
    {
        'id': 'vouchers', 'section': 'accounting', 'label': 'Chứng từ kế toán',
        'icon': 'fas fa-file-invoice', 'color': 'info', 'endpoint': 'SME_chung_tu',
        'description': 'Chứng từ thu chi, kho, tài sản cố định, bán hàng đại lý và đối chiếu',
        'items': (
            {'endpoint': 'SME_DanhSachPhieuThu', 'label': 'Phiếu thu — mẫu 01-TT', 'icon': 'fas fa-receipt'},
            {'endpoint': 'SME_DanhSachPhieuChi', 'label': 'Phiếu chi — mẫu 02-TT', 'icon': 'fas fa-money-bill-wave'},
            {'endpoint': 'SME_fx_cash', 'label': 'Ngoại tệ — nhật ký và số dư', 'icon': 'fas fa-coins'},
            {'endpoint': 'SME_advances', 'label': 'Tạm ứng và thanh toán — mẫu 03 đến 05-TT', 'icon': 'fas fa-hand-holding-dollar'},
            {'endpoint': 'SME_temp_receipts', 'label': 'Biên lai thu tiền — mẫu 06-TT', 'icon': 'fas fa-file-invoice'},
            {'endpoint': 'SME_gold_sheet', 'label': 'Bảng kê vàng — mẫu 07-TT', 'icon': 'fas fa-coins'},
            {'endpoint': 'SME_cash_count', 'label': 'Kiểm kê quỹ tiền mặt — mẫu 08a-TT', 'icon': 'fas fa-coins'},
            {'endpoint': 'SME_cash_count_fx', 'label': 'Kiểm kê quỹ ngoại tệ — mẫu 08b-TT', 'icon': 'fas fa-coins'},
            {'endpoint': 'SME_payment_listing', 'label': 'Bảng kê chi tiền — mẫu 09-TT', 'icon': 'fas fa-list'},
            {'endpoint': 'SME_DanhSachPhieuNhapKho_VT', 'label': 'Phiếu nhập kho — mẫu 01-VT', 'icon': 'fas fa-file-import'},
            {'endpoint': 'SME_DanhSachPhieuXuatKho_VT', 'label': 'Phiếu xuất kho — mẫu 02-VT', 'icon': 'fas fa-file-export'},
            {'endpoint': 'SME_stock_inspection', 'label': 'Kiểm nghiệm vật tư — mẫu 03-VT', 'icon': 'fas fa-flask'},
            {'endpoint': 'SME_stock_count', 'label': 'Kiểm kê kho — mẫu 05-VT', 'icon': 'fas fa-clipboard-check'},
            {'endpoint': 'SME_purchase_listing', 'label': 'Bảng kê mua hàng — mẫu 06-VT', 'icon': 'fas fa-list-ol'},
            {'endpoint': 'SME_stock_transfer', 'label': 'Chuyển kho liên chi nhánh', 'icon': 'fas fa-right-left'},
            {'endpoint': 'SME_form_01_bh', 'label': 'Đại lý và ký gửi — mẫu 01-BH', 'icon': 'fas fa-file-signature'},
            {'endpoint': 'SME_form_02_bh', 'label': 'Thẻ quầy hàng — mẫu 02-BH', 'icon': 'fas fa-id-card'},
            {'endpoint': 'SME_fa_docs', 'label': 'Biên bản tài sản cố định — mẫu 01/03/04/05', 'icon': 'fas fa-file-signature'},
            {'endpoint': 'SME_fa_disposal', 'label': 'Thanh lý tài sản cố định — mẫu 02', 'icon': 'fas fa-trash-can'},
            {'endpoint': 'SME_fa_depreciation_table', 'label': 'Bảng khấu hao tài sản cố định — mẫu 06', 'icon': 'fas fa-table'},
            {'endpoint': 'SME_bank_reconcile', 'label': 'Đối chiếu ngân hàng', 'icon': 'fas fa-scale-balanced'},
            {'endpoint': 'SME_loans', 'label': 'Vay và trả nợ', 'icon': 'fas fa-landmark'},
            {'endpoint': 'SME_deposits', 'label': 'Ký quỹ và ký cược', 'icon': 'fas fa-file-contract'},
            {'endpoint': 'SME_letter_of_credit', 'label': 'Thư tín dụng (L/C)', 'icon': 'fas fa-file-signature'},
        ),
    },
    {
        'id': 'books', 'section': 'accounting', 'label': 'Sổ sách kế toán',
        'icon': 'fas fa-book', 'color': 'primary', 'endpoint': 'SME_SoSachKeToan',
        'description': 'Hệ thống tài khoản, nhật ký, sổ quỹ, sổ cái và công nợ',
        'items': (
            {'endpoint': 'SME_chart_of_accounts', 'label': 'Danh mục tài khoản', 'icon': 'fas fa-sitemap'},
            {'endpoint': 'SME_branches', 'label': 'Chi nhánh và đơn vị', 'icon': 'fas fa-code-branch'},
            {'endpoint': 'SME_journal', 'label': 'Nhật ký bút toán', 'icon': 'fas fa-book-open'},
            {'endpoint': 'SME_general_ledger', 'label': 'Sổ cái và cân đối phát sinh', 'icon': 'fas fa-scale-balanced'},
            {'endpoint': 'SME_SoQuyTienMat', 'label': 'Sổ Quỹ Tiền Mặt', 'icon': 'fas fa-money-bill-wave'},
            {'endpoint': 'SME_SoTienGuiNganHang', 'label': 'Sổ Tiền Gửi Ngân Hàng', 'icon': 'fas fa-building-columns'},
            {'endpoint': 'SME_fx_cash', 'label': 'Sổ ngoại tệ (1112 / 1122)', 'icon': 'fas fa-coins'},
            {'endpoint': 'SME_SoCongNoPhaiThu', 'label': 'Sổ Công Nợ Phải Thu', 'icon': 'fas fa-user-clock'},
            {'endpoint': 'SME_SoCongNoPhaiTra', 'label': 'Sổ Công Nợ Phải Trả', 'icon': 'fas fa-file-invoice-dollar'},
            {'endpoint': 'SME_PhaiThuCongNhanVien', 'label': 'Sổ Phải Thu Nhân Viên', 'icon': 'fas fa-user-plus'},
            {'endpoint': 'SME_PhaiTraCongNhanVien', 'label': 'Công nợ phải trả nhân viên', 'icon': 'fas fa-user-minus'},
            {'endpoint': 'SME_auto_posting', 'label': 'Kết chuyển, khóa sổ và cuối năm', 'icon': 'fas fa-robot'},
        ),
    },
    {
        'id': 'production', 'section': 'accounting', 'label': 'Sản xuất và giá thành',
        'icon': 'fas fa-industry', 'color': 'danger', 'endpoint': 'SME_san_xuat_gia_thanh',
        'description': 'Lệnh sản xuất, phân bổ nguyên vật liệu và tính giá thành',
        'items': (
            {'endpoint': 'SME_production', 'label': 'Sản xuất', 'icon': 'fas fa-industry'},
            {'endpoint': 'SME_costing', 'label': 'Kế toán giá thành', 'icon': 'fas fa-calculator'},
            {'endpoint': 'SME_material_remaining', 'label': 'Vật tư còn lại cuối kỳ — mẫu 04-VT', 'icon': 'fas fa-clipboard-list'},
            {'endpoint': 'SME_material_alloc', 'label': 'Phân bổ nguyên vật liệu — mẫu 07-VT', 'icon': 'fas fa-share'},
        ),
    },
    {
        'id': 'cash_debt', 'section': 'accounting', 'label': 'Tiền Và Công Nợ',
        'icon': 'fas fa-wallet', 'color': 'info', 'endpoint': 'SME_dashboard_debt',
        'description': 'Tổng quan tiền mặt, phải thu, phải trả và vốn lưu động',
        'items': (
            {'endpoint': 'SME_SoQuyTienMat', 'label': 'Sổ Quỹ Tiền Mặt', 'icon': 'fas fa-money-bill-wave'},
            {'endpoint': 'SME_SoTienGuiNganHang', 'label': 'Sổ Tiền Gửi Ngân Hàng', 'icon': 'fas fa-building-columns'},
            {'endpoint': 'SME_fx_cash', 'label': 'Sổ ngoại tệ (1112 / 1122)', 'icon': 'fas fa-coins'},
            {'endpoint': 'SME_SoCongNoPhaiThu', 'label': 'Sổ Công Nợ Phải Thu', 'icon': 'fas fa-user-clock'},
            {'endpoint': 'SME_SoCongNoPhaiTra', 'label': 'Sổ Công Nợ Phải Trả', 'icon': 'fas fa-file-invoice-dollar'},
            {'endpoint': 'SME_PhaiThuCongNhanVien', 'label': 'Sổ Phải Thu Nhân Viên', 'icon': 'fas fa-user-plus'},
            {'endpoint': 'SME_PhaiTraCongNhanVien', 'label': 'Công nợ phải trả nhân viên', 'icon': 'fas fa-user-minus'},
        ),
    },
    {
        'id': 'assets', 'section': 'accounting', 'label': 'Tài sản cố định và công cụ dụng cụ',
        'icon': 'fas fa-building', 'color': 'danger', 'endpoint': 'SME_TSCD',
        'description': 'Theo dõi tài sản cố định và công cụ dụng cụ',
        'items': (
            {'endpoint': 'SME_TSCD', 'label': 'Tổng quan tài sản cố định', 'icon': 'fas fa-chart-pie'},
            {'endpoint': 'SME_fixed_assets', 'label': 'Danh mục tài sản cố định', 'icon': 'fas fa-building'},
            {'endpoint': 'SME_CCDC', 'label': 'Tổng quan công cụ dụng cụ', 'icon': 'fas fa-chart-pie'},
            {'endpoint': 'SME_tools', 'label': 'Danh mục công cụ dụng cụ', 'icon': 'fas fa-screwdriver-wrench'},
        ),
    },
    {
        'id': 'hr', 'section': 'accounting', 'label': 'Nhân sự và tiền lương',
        'icon': 'fas fa-user-tie', 'color': 'secondary', 'endpoint': 'SME_dashboard_HRSalary',
        'description': 'Hồ sơ nhân viên, chấm công, lập lương và phân bổ chi phí nhân công',
        'items': (
            {'endpoint': 'SME_employees', 'label': 'Danh sách nhân viên', 'icon': 'fas fa-users'},
            {'endpoint': 'SME_attendance', 'label': 'Bảng chấm công', 'icon': 'fas fa-fingerprint'},
            {'endpoint': 'SME_salary_create', 'label': 'Lập bảng lương — mẫu 01-LĐTL', 'icon': 'fas fa-calculator'},
            {'endpoint': 'SME_PhaiTraCongNhanVien', 'label': 'Công nợ phải trả nhân viên', 'icon': 'fas fa-hand-holding-usd'},
            {'endpoint': 'SME_labor_sheets', 'label': 'Thưởng, ngoài giờ và thuê ngoài — mẫu 02 đến 04', 'icon': 'fas fa-gift'},
            {'endpoint': 'SME_labor_contracts', 'label': 'Hợp đồng giao khoán — mẫu 05 và 06-LĐTL', 'icon': 'fas fa-file-contract'},
            {'endpoint': 'SME_insurance_pay', 'label': 'Nộp bảo hiểm xã hội — mẫu 07-LĐTL', 'icon': 'fas fa-shield-alt'},
            {'endpoint': 'SME_payroll_allocation', 'label': 'Phân bổ lương — mẫu 08-LĐTL', 'icon': 'fas fa-table'},
        ),
    },
    {
        'id': 'reports', 'section': 'reports', 'label': 'Báo cáo tài chính',
        'icon': 'fas fa-chart-line', 'color': 'primary', 'endpoint': 'SME_BCTC',
        'description': 'Báo cáo tài chính, báo cáo quản trị, doanh thu lợi nhuận và thuế',
        'items': (
            {'endpoint': 'SME_BCTC_reports', 'label': 'Bộ báo cáo tài chính', 'icon': 'fas fa-file-excel'},
            {'endpoint': 'SME_mgmt_report', 'label': 'Báo cáo quản trị', 'icon': 'fas fa-chart-column'},
            {'endpoint': 'SME_revenue_report', 'label': 'Báo cáo doanh thu điểm bán hàng', 'icon': 'fas fa-chart-bar'},
            {'endpoint': 'SME_profit_report', 'label': 'Báo cáo lợi nhuận điểm bán hàng', 'icon': 'fas fa-coins'},
            {'endpoint': 'SME_tax_nsnn', 'label': 'Thuế và ngân sách nhà nước', 'icon': 'fas fa-landmark'},
            {'endpoint': 'SME_vat_declaration', 'label': 'Tờ khai thuế giá trị gia tăng', 'icon': 'fas fa-file-invoice'},
            {'endpoint': 'SME_cit', 'label': 'Thuế thu nhập doanh nghiệp tạm nộp', 'icon': 'fas fa-percent'},
            {'endpoint': 'SME_cit_declaration', 'label': 'Quyết toán thuế thu nhập doanh nghiệp', 'icon': 'fas fa-file-code'},
            {'endpoint': 'SME_pit_declaration', 'label': 'Thuế thu nhập cá nhân khấu trừ từ lương', 'icon': 'fas fa-user-shield'},
            {'endpoint': 'SME_fx_revaluation', 'label': 'Đánh giá lại tỷ giá', 'icon': 'fas fa-dollar-sign'},
            {'endpoint': 'SME_capital', 'label': 'Góp vốn và cổ tức', 'icon': 'fas fa-piggy-bank'},
        ),
    },
    {
        'id': 'utilities', 'section': 'reports', 'label': 'Tiện ích',
        'icon': 'fas fa-cubes', 'color': 'secondary', 'endpoint': 'SME_utilities',
        'description': 'Công cụ hỗ trợ và thiết lập hệ thống',
        'items': (
            {'endpoint': 'huong_dan_su_dung', 'label': 'Hướng dẫn sử dụng', 'icon': 'fas fa-circle-question'},
            {'endpoint': 'SME_audit_log', 'label': 'Nhật ký truy cập', 'icon': 'fas fa-clock-rotate-left'},
            {'endpoint': 'store_setup_page', 'label': 'Thiết lập hệ thống', 'icon': 'fas fa-sliders'},
        ),
    },
)


def get_sme_menu_groups():
    """Trả menu đã xen tiêu đề phân khu để render sidebar."""
    result = []
    for section in SME_MENU_SECTIONS:
        groups = [dict(group) for group in SME_MENU_GROUPS if group['section'] == section['id']]
        if groups:
            result.append({**section, '_type': 'section_header'})
            result.extend(groups)
    return result


def get_sme_quick_links():
    endpoints = (
        ('SME_journal', 'Ghi sổ bút toán', 'fas fa-pen-to-square'),
        ('SME_chung_tu', 'Chứng từ kế toán', 'fas fa-file-invoice'),
        ('SME_purchase_order_create', 'Lập đơn mua hàng', 'fas fa-cart-plus'),
        ('SME_BCTC_reports', 'Xem báo cáo tài chính', 'fas fa-chart-line'),
    )
    return [{'endpoint': ep, 'label': label, 'icon': icon} for ep, label, icon in endpoints]


def get_sme_featured_links():
    endpoints = (
        ('SME_chart_of_accounts', 'Hệ thống tài khoản', 'fas fa-sitemap'),
        ('SME_general_ledger', 'Sổ cái / Cân đối phát sinh', 'fas fa-scale-balanced'),
        ('SME_san_xuat_gia_thanh', 'Sản xuất & giá thành', 'fas fa-industry'),
        ('SME_BCTC', 'Trung tâm báo cáo', 'fas fa-chart-column'),
    )
    return [{'endpoint': ep, 'label': label, 'icon': icon} for ep, label, icon in endpoints]


def is_sme_endpoint(endpoint):
    if not endpoint:
        return False
    if endpoint.startswith('SME_'):
        return True
    return any(
        endpoint == item['endpoint']
        for group in SME_MENU_GROUPS
        for item in group['items']
    )


def get_sme_group_by_id(group_id):
    for group in SME_MENU_GROUPS:
        if group['id'] == group_id and group.get('_type') != 'section_header':
            return dict(group)
    return None


def resolve_sme_current_group(endpoint):
    if not endpoint:
        return None
    for group in SME_MENU_GROUPS:
        if group['endpoint'] == endpoint:
            return group['id']
        if any(item['endpoint'] == endpoint for item in group['items']):
            return group['id']
    return None
