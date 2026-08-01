"""Cấu hình menu trung tâm Kế Toán SME (TT58 / TT99) — tách biệt HKD (hộ kinh doanh)."""

SME_HUB_TITLE = 'Kế Toán SME'

SME_MENU_SECTIONS = (
    {'id': 'operations', 'label': 'NGHIỆP VỤ KINH DOANH'},
    {'id': 'accounting', 'label': 'KẾ TOÁN & TÀI CHÍNH'},
    {'id': 'reports', 'label': 'BÁO CÁO & HỆ THỐNG'},
)

SME_MENU_GROUPS = (
    {
        'id': 'overview', 'section': 'operations', 'label': 'Tổng quan',
        'icon': 'fas fa-chart-pie', 'color': 'primary', 'endpoint': 'SME_dashboard',
        'description': 'Trung tâm chỉ số tài chính doanh nghiệp (TT58/TT99)',
        'items': (),
    },
    {
        'id': 'sales', 'section': 'operations', 'label': 'Bán hàng',
        'icon': 'fas fa-cart-shopping', 'color': 'success', 'endpoint': 'SME_dashboard_sale',
        'description': 'Điểm bán hàng, đơn hàng, hóa đơn bán, trả hàng và báo cáo đại lý',
        'items': (
            {'endpoint': 'sale', 'label': 'Điểm Bán Hàng', 'icon': 'fas fa-cash-register'},
            {'endpoint': 'order', 'label': 'Danh sách đơn hàng', 'icon': 'fas fa-list'},
            {'endpoint': 'SME_sale_details', 'label': 'Chi tiết hàng bán', 'icon': 'fas fa-receipt'},
            {'endpoint': 'SME_bank_transactions', 'label': 'Giao dịch ngân hàng', 'icon': 'fas fa-university'},
            {'endpoint': 'outward_invoice', 'label': 'Hóa đơn bán hàng', 'icon': 'fas fa-file-export'},
            {'endpoint': 'return_sale_page', 'label': 'Khách trả hàng', 'icon': 'fas fa-rotate-left'},
            {'endpoint': 'SME_form_01_bh', 'label': '01-BH Đại lý / ký gửi', 'icon': 'fas fa-file-signature'},
            {'endpoint': 'SME_form_02_bh', 'label': '02-BH Thẻ quầy hàng', 'icon': 'fas fa-id-card'},
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
            {'endpoint': 'SME_DanhSachPhieuNhapKho', 'label': 'Danh sách phiếu nhập', 'icon': 'fas fa-list-check'},
            {'endpoint': 'SME_import_details', 'label': 'Chi tiết mua hàng', 'icon': 'fas fa-list'},
            {'endpoint': 'SME_return_supplier', 'label': 'Trả nhà cung cấp', 'icon': 'fas fa-rotate-left'},
            {'endpoint': 'SME_SoCongNoPhaiTra', 'label': 'Công nợ phải trả', 'icon': 'fas fa-hand-holding-dollar'},
        ),
    },
    {
        'id': 'warehouse', 'section': 'operations', 'label': 'Tồn Kho',
        'icon': 'fas fa-warehouse', 'color': 'warning', 'endpoint': 'SME_dashboard_warehouse',
        'description': 'Tồn kho, phiếu nhập/xuất 01-VT/02-VT, kiểm kê',
        'items': (
            {'endpoint': 'inventory', 'label': 'Tồn Kho', 'icon': 'fas fa-boxes-stacked'},
            {'endpoint': 'inventory_detail', 'label': 'Báo Cáo Tồn Kho', 'icon': 'fas fa-chart-column'},
            {'endpoint': 'SME_DanhSachPhieuNhapKho_VT', 'label': 'Phiếu nhập kho (01-VT)', 'icon': 'fas fa-file-import'},
            {'endpoint': 'SME_DanhSachPhieuXuatKho_VT', 'label': 'Phiếu xuất kho (02-VT)', 'icon': 'fas fa-file-export'},
            {'endpoint': 'SME_inventory_check', 'label': 'Nhập tồn cũ & kiểm kê', 'icon': 'fas fa-clipboard-check'},
            {'endpoint': 'SME_production', 'label': 'Sản xuất & giá thành', 'icon': 'fas fa-industry'},
            {'endpoint': 'SME_costing', 'label': 'Kế toán giá thành', 'icon': 'fas fa-calculator'},
        ),
    },
    {
        'id': 'catalog', 'section': 'operations', 'label': 'Danh Mục',
        'icon': 'fas fa-box', 'color': 'info', 'endpoint': 'products',
        'description': 'Sản phẩm, nhà cung cấp và khách hàng',
        'items': (
            {'endpoint': 'products', 'label': 'Danh Mục Sản Phẩm', 'icon': 'fas fa-box'},
            {'endpoint': 'suppliers_page', 'label': 'Nhà Cung Cấp', 'icon': 'fas fa-truck'},
            {'endpoint': 'customers_page', 'label': 'Khách Hàng', 'icon': 'fas fa-users'},
        ),
    },
    {
        'id': 'cash_debt', 'section': 'accounting', 'label': 'Tiền & công nợ',
        'icon': 'fas fa-wallet', 'color': 'info', 'endpoint': 'SME_dashboard_debt',
        'description': 'Phiếu thu/chi 01-02 TT, quỹ và công nợ',
        'items': (
            {'endpoint': 'SME_DanhSachPhieuThu', 'label': 'Phiếu thu (01-TT)', 'icon': 'fas fa-receipt'},
            {'endpoint': 'SME_DanhSachPhieuChi', 'label': 'Phiếu chi (02-TT)', 'icon': 'fas fa-money-bill-wave'},
            {'endpoint': 'SME_SoQuyTienMat', 'label': 'Sổ quỹ tiền mặt', 'icon': 'fas fa-money-bill-wave'},
            {'endpoint': 'SME_SoTienGuiNganHang', 'label': 'Tiền gửi ngân hàng', 'icon': 'fas fa-building-columns'},
            {'endpoint': 'SME_SoCongNoPhaiThu', 'label': 'Công nợ phải thu', 'icon': 'fas fa-user-clock'},
            {'endpoint': 'SME_SoCongNoPhaiTra', 'label': 'Công nợ phải trả', 'icon': 'fas fa-file-invoice-dollar'},
        ),
    },
    {
        'id': 'books', 'section': 'accounting', 'label': 'Sổ kế toán',
        'icon': 'fas fa-book', 'color': 'primary', 'endpoint': 'SME_SoSachKeToan',
        'description': 'Hệ thống tài khoản, nhật ký, sổ cái và bút toán tự động (TT58/TT99)',
        'items': (
            {'endpoint': 'SME_chart_of_accounts', 'label': 'Danh mục tài khoản', 'icon': 'fas fa-sitemap'},
            {'endpoint': 'SME_journal', 'label': 'Nhật ký bút toán', 'icon': 'fas fa-book-open'},
            {'endpoint': 'SME_general_ledger', 'label': 'Sổ cái / CĐPS', 'icon': 'fas fa-scale-balanced'},
            {'endpoint': 'SME_auto_posting', 'label': 'Tự động kết chuyển', 'icon': 'fas fa-robot'},
            {'endpoint': 'SME_costing', 'label': 'Kế toán giá thành', 'icon': 'fas fa-calculator'},
        ),
    },
    {
        'id': 'assets', 'section': 'accounting', 'label': 'Tài sản & CCDC',
        'icon': 'fas fa-building', 'color': 'danger', 'endpoint': 'SME_TSCD',
        'description': 'Theo dõi tài sản cố định và công cụ dụng cụ',
        'items': (
            {'endpoint': 'SME_TSCD', 'label': 'Tài sản cố định', 'icon': 'fas fa-building'},
            {'endpoint': 'SME_CCDC', 'label': 'Công cụ dụng cụ', 'icon': 'fas fa-screwdriver-wrench'},
        ),
    },
    {
        'id': 'hr', 'section': 'accounting', 'label': 'Nhân sự & tiền lương',
        'icon': 'fas fa-user-tie', 'color': 'secondary', 'endpoint': 'SME_dashboard_HRSalary',
        'description': 'Hồ sơ NV, chấm công, lập lương và công nợ nhân viên',
        'items': (
            {'endpoint': 'SME_employees', 'label': 'Danh sách nhân viên', 'icon': 'fas fa-users'},
            {'endpoint': 'SME_attendance', 'label': 'Bảng chấm công', 'icon': 'fas fa-fingerprint'},
            {'endpoint': 'SME_salary_create', 'label': 'Lập bảng lương', 'icon': 'fas fa-calculator'},
            {'endpoint': 'SME_PhaiThuCongNhanVien', 'label': 'Phải thu nhân viên', 'icon': 'fas fa-user-plus'},
            {'endpoint': 'SME_PhaiTraCongNhanVien', 'label': 'Phải trả nhân viên', 'icon': 'fas fa-user-minus'},
        ),
    },
    {
        'id': 'reports', 'section': 'reports', 'label': 'Báo cáo tài chính',
        'icon': 'fas fa-chart-line', 'color': 'primary', 'endpoint': 'SME_BCTC',
        'description': 'BCTC, báo cáo quản trị, doanh thu/lợi nhuận POS và thuế',
        'items': (
            {'endpoint': 'SME_BCTC_reports', 'label': 'Bộ báo cáo tài chính', 'icon': 'fas fa-file-excel'},
            {'endpoint': 'SME_mgmt_report', 'label': 'Báo cáo quản trị', 'icon': 'fas fa-chart-column'},
            {'endpoint': 'SME_revenue_report', 'label': 'Báo cáo doanh thu (POS)', 'icon': 'fas fa-chart-bar'},
            {'endpoint': 'SME_profit_report', 'label': 'Báo cáo lợi nhuận (POS)', 'icon': 'fas fa-coins'},
            {'endpoint': 'SME_tax_nsnn', 'label': 'Thuế & NSNN', 'icon': 'fas fa-landmark'},
            {'endpoint': 'SME_vat_declaration', 'label': 'Tờ khai GTGT', 'icon': 'fas fa-file-invoice'},
        ),
    },
    {
        'id': 'utilities', 'section': 'reports', 'label': 'Tiện ích',
        'icon': 'fas fa-cubes', 'color': 'secondary', 'endpoint': 'SME_utilities',
        'description': 'Công cụ hỗ trợ và thiết lập hệ thống',
        'items': (
            {'endpoint': 'huong_dan_su_dung', 'label': 'Hướng dẫn sử dụng', 'icon': 'fas fa-circle-question'},
            {'endpoint': 'SME_audit_log', 'label': 'Nhật ký truy cập', 'icon': 'fas fa-clock-rotate-left'},
            {'endpoint': 'settings_page', 'label': 'Thiết lập hệ thống', 'icon': 'fas fa-sliders'},
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
        ('SME_purchase_order_create', 'Lập đơn mua hàng', 'fas fa-cart-plus'),
        ('SME_vat_declaration', 'Lập tờ khai GTGT', 'fas fa-file-invoice'),
        ('SME_BCTC_reports', 'Xem báo cáo tài chính', 'fas fa-chart-line'),
    )
    return [{'endpoint': ep, 'label': label, 'icon': icon} for ep, label, icon in endpoints]


def get_sme_featured_links():
    endpoints = (
        ('SME_chart_of_accounts', 'Hệ thống tài khoản', 'fas fa-sitemap'),
        ('SME_general_ledger', 'Sổ cái / Cân đối phát sinh', 'fas fa-scale-balanced'),
        ('SME_dashboard_debt', 'Theo dõi công nợ', 'fas fa-hand-holding-dollar'),
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
