"""Cấu hình menu POS & Kế Toán HKD — nhóm + phân hệ sidebar."""
from Services.tenant_profile import HKD_MENU_FEATURE_MAP, tenant_has_feature

HUB_TITLE = 'POS và Kế Toán HKD'

HKD_DEFAULT_ROLES = (
    'accountant', 'manager', 'manager*', 'managerFB',
    'admin', 'admin*', 'adminFB', 'master',
)

SME_ACCOUNTING_ROLES = (
    'accountantSME', 'managerSME', 'adminSME',
)

# Tiêu đề phân hệ trên sidebar (không phải menu con)
MENU_SECTIONS = (
    {'id': 'sales', 'label': 'PHÂN HỆ BÁN HÀNG'},
    {'id': 'accounting', 'label': 'PHÂN HỆ KẾ TOÁN'},
    {'id': 'hr', 'label': 'NHÂN SỰ & TIỀN LƯƠNG'},
    {'id': 'general', 'label': 'TỔNG HỢP'},
)

# Mỗi nhóm: id, label, icon, color, description, section, items[]
# _menu_style: 'direct' = một liên kết cấp 1 (không mở submenu)
POS_HKD_MENU = [
    {
        'id': 'pos_ban_hang',
        'section': 'sales',
        'label': 'Bán Hàng',
        'icon': 'fas fa-shopping-cart',
        'color': 'success',
        'description': 'POS, đơn hàng, hóa đơn bán và trả hàng',
        'items': [
            {
                'endpoint': 'sale',
                'label': 'Bán Hàng',
                'icon': 'fas fa-cash-register text-success',
                'perm': 'view_sale',
                'roles': ('staff', 'manager', 'admin', 'master'),
            },
            {
                'endpoint': 'order',
                'label': 'Quản Lý Đơn Hàng',
                'icon': 'fas fa-file-invoice text-primary',
                'perm': 'view_order',
                'roles': ('staff', 'accountant', 'manager', 'managerFB', 'admin', 'master', 'admin*', 'adminFB'),
            },
            {
                'endpoint': 'bank_transactions_page',
                'label': 'Giao Dịch Ngân Hàng',
                'icon': 'fas fa-university text-info',
                'perm': 'view_order',
                'roles': ('staff', 'accountant', 'manager', 'managerFB', 'admin', 'master', 'admin*', 'adminFB'),
            },
            {
                'endpoint': 'outward_invoice',
                'label': 'Hóa Đơn Bán Hàng',
                'icon': 'fas fa-file-export text-warning',
                'perm': 'view_outward_invoice',
                'roles': ('accountant', 'manager', 'managerFB', 'admin', 'adminFB', 'master', 'staff'),
            },
            {
                'endpoint': 'return_sale_page',
                'label': 'Khách Trả Hàng',
                'icon': 'fas fa-undo text-warning',
                'perm': 'view_return_sale',
                'roles': ('staff', 'accountant', 'manager', 'admin', 'master'),
            },
        ],
    },
    {
        'id': 'pos_nhap_kho',
        'section': 'sales',
        'label': 'Mua Hàng',
        'icon': 'fas fa-truck-loading',
        'color': 'primary',
        'description': 'Phiếu nhập, hóa đơn mua và trả hàng nhập',
        'items': [
            {
                'endpoint': 'import_stock',
                'label': 'Tạo Phiếu Nhập',
                'icon': 'fas fa-plus-circle text-primary',
                'perm': 'create_import',
                'roles': ('accountant', 'manager', 'admin', 'master'),
                'hide_metric': True,
            },
            {
                'endpoint': 'importFB_stock',
                'label': 'Tạo Phiếu Nhập F&B',
                'icon': 'fas fa-plus-circle text-primary',
                'perm': 'create_import',
                'roles': ('accountantFB', 'managerFB', 'admin', 'master', 'adminFB'),
                'hide_metric': True,
            },
            {
                'endpoint': 'import_list',
                'label': 'Danh Sách Phiếu Nhập',
                'icon': 'fas fa-list-ul text-secondary',
                'perm': 'view_import_list',
                'roles': ('accountant', 'manager', 'managerFB', 'admin', 'adminFB', 'master'),
            },
            {
                'endpoint': 'import_details_page',
                'label': 'Chi Tiết Mua Hàng',
                'icon': 'fas fa-list text-primary',
                'perm': 'view_import_list',
                'roles': ('accountant', 'manager', 'manager*', 'managerFB', 'admin', 'admin*', 'adminFB', 'master'),
            },
            {
                'endpoint': 'inward_invoice',
                'label': 'Hóa Đơn Mua Hàng',
                'icon': 'fas fa-file-import text-primary',
                'perm': 'view_inward_invoice',
                'roles': ('accountant', 'manager', 'managerFB', 'admin', 'adminFB', 'master'),
            },
            {
                'endpoint': 'return_import_page',
                'label': 'Trả Hàng Nhập',
                'icon': 'fas fa-undo text-danger',
                'perm': 'view_return_import',
                'roles': ('accountant', 'manager', 'admin', 'master'),
            },
            {
                'endpoint': 'inventory_check',
                'label': 'Nhập Tồn Cũ & Kiểm Kê',
                'icon': 'fas fa-clipboard-check text-secondary',
                'perm': 'view_inventory_check',
                'roles': ('accountant', 'manager', 'managerFB', 'admin', 'adminFB', 'master'),
                'hide_metric': True,
            },
        ],
    },
    {
        'id': 'pos_ton_kho',
        'section': 'sales',
        'label': 'Tồn Kho',
        'icon': 'fas fa-warehouse',
        'color': 'warning',
        'description': 'Tồn kho tổng hợp',
        'items': [
            {
                'endpoint': 'inventory',
                'label': 'Tồn Kho',
                'icon': 'fas fa-warehouse text-warning',
                'perm': 'view_inventory',
                'roles': ('accountant', 'manager', 'managerFB', 'admin', 'adminFB', 'master'),
            },
            {
                'endpoint': 'production_page',
                'label': 'Sản Xuất & Tính Giá Thành',
                'icon': 'fas fa-industry text-success',
                'perm': 'view_inventory',
                'roles': ('accountant', 'manager', 'admin', 'master'),
            },
        ],
    },
    {
        'id': 'pos_bao_cao',
        'section': 'sales',
        'label': 'Báo Cáo',
        'icon': 'fas fa-chart-bar',
        'color': 'secondary',
        'description': 'Doanh thu, lợi nhuận và báo cáo chi tiết mua/bán/tồn',
        'items': [
            {
                'endpoint': 'reports',
                'label': 'Báo Cáo Doanh Thu',
                'icon': 'fas fa-chart-bar text-success',
                'perm': 'view_revenue_report',
                'roles': ('accountant', 'manager', 'manager*', 'managerFB', 'admin', 'admin*', 'adminFB', 'master'),
            },
            {
                'endpoint': 'profit',
                'label': 'Báo Cáo Lợi Nhuận',
                'icon': 'fas fa-coins text-success',
                'perm': 'view_profit',
                'roles': ('accountant', 'manager', 'manager*', 'managerFB', 'admin', 'admin*', 'adminFB', 'master'),
            },
            {
                'endpoint': 'inventory_detail',
                'label': 'Tồn Kho Chi Tiết',
                'icon': 'fas fa-boxes-stacked text-warning',
                'perm': 'view_inventory_detail',
                'roles': ('accountant', 'manager', 'manager*', 'managerFB', 'admin', 'admin*', 'adminFB', 'master'),
            },
            {
                'endpoint': 'sale_details_page',
                'label': 'Chi Tiết Bán Hàng',
                'icon': 'fas fa-list text-success',
                'perm': 'view_revenue_report',
                'roles': ('accountant', 'manager', 'manager*', 'managerFB', 'admin', 'admin*', 'adminFB', 'master'),
            },
            {
                'endpoint': 'SoChiTietHangHoa',
                'label': 'Sổ Chi Tiết Hàng Hóa',
                'icon': 'fas fa-warehouse text-primary',
                'perm': 'view_so_hang_hoa',
            },
        ],
    },
    {
        'id': 'pos_danh_muc',
        'section': 'sales',
        'label': 'Danh Mục',
        'icon': 'fas fa-box',
        'color': 'info',
        'description': 'Sản phẩm, nhà cung cấp và khách hàng',
        'items': [
            {
                'endpoint': 'products',
                'label': 'Danh Mục Sản Phẩm',
                'icon': 'fas fa-box text-info',
                'perm': 'view_products',
                'roles': ('accountant', 'manager', 'managerFB', 'admin', 'adminFB', 'master'),
            },
            {
                'endpoint': 'suppliers_page',
                'label': 'Nhà Cung Cấp',
                'icon': 'fas fa-truck text-muted',
                'perm': 'view_suppliers',
                'roles': ('accountant', 'manager', 'managerFB', 'admin', 'adminFB', 'master'),
            },
            {
                'endpoint': 'customers_page',
                'label': 'Khách Hàng',
                'icon': 'fas fa-users text-info',
                'perm': 'view_customers',
                'roles': ('accountant', 'manager', 'managerFB', 'admin', 'adminFB', 'master', 'staff'),
            },
        ],
    },
    {
        'id': 'hkd_chung_tu',
        'section': 'accounting',
        'label': 'Chứng Từ Kế Toán',
        'icon': 'fas fa-file-alt',
        'color': 'danger',
        'description': 'Phiếu thu, chi, nhập xuất kho và bảng lương 05-LĐTL',
        'items': [
            {'endpoint': 'DanhSachPhieuThu', 'label': 'Phiếu Thu (01-TT)', 'icon': 'fas fa-receipt text-success', 'perm': 'view_phieu_thu'},
            {'endpoint': 'DanhSachPhieuChi', 'label': 'Phiếu Chi (02-TT)', 'icon': 'fas fa-money-bill-wave text-danger', 'perm': 'view_phieu_chi'},
            {'endpoint': 'DanhSachPhieuNhapKho', 'label': 'Phiếu Nhập Kho (03-VT)', 'icon': 'fas fa-truck-loading text-primary', 'perm': 'view_phieu_nhap_kho'},
            {'endpoint': 'DanhSachPhieuXuatKho', 'label': 'Phiếu Xuất Kho (04-VT)', 'icon': 'fas fa-box-open text-warning', 'perm': 'view_phieu_xuat_kho'},
            {
                'endpoint': 'production_page',
                'label': 'Sản Xuất & Tính Giá Thành',
                'icon': 'fas fa-industry text-success',
                'perm': 'view_inventory',
                'roles': ('accountant', 'manager', 'admin', 'master'),
            },
            {
                'endpoint': 'DanhSachBangLuong_05LDTL',
                'label': 'Bảng Thanh Toán Lương (05-LĐTL)',
                'icon': 'fas fa-file-invoice-dollar text-info',
                'perm': 'view_bang_luong',
            },
        ],
    },
    {
        'id': 'hkd_so_sach',
        'section': 'accounting',
        'label': 'Sổ Sách Kế Toán',
        'icon': 'fas fa-book',
        'color': 'primary',
        'description': 'Tổng hợp đầy đủ các sổ kế toán HKD',
        'items': [
            {'endpoint': 'SoChiTietDoanhThu', 'label': 'Sổ Doanh Thu (S1a)', 'icon': 'fas fa-chart-line text-success', 'perm': 'view_so_doanh_thu_s1a'},
            {'endpoint': 'SoChiTietDoanhThu_S2a', 'label': 'Sổ Doanh Thu (S2a)', 'icon': 'fas fa-chart-line text-success', 'perm': 'view_so_doanh_thu_s2a'},
            {'endpoint': 'SoChiTietDoanhThu_S2b', 'label': 'Sổ Doanh Thu (S2b)', 'icon': 'fas fa-chart-line text-success', 'perm': 'view_so_doanh_thu_s2b'},
            {'endpoint': 'SoChiTietDoanhThu_ChiPhi_S2c', 'label': 'Sổ DT & Chi Phí (S2c)', 'icon': 'fas fa-chart-line text-success', 'perm': 'view_so_doanh_thu_chi_phi', 'roles': ('accountant', 'manager', 'admin', 'master')},
            {'endpoint': 'SoQuyTienMat', 'label': 'Sổ Quỹ Tiền Mặt (S2e)', 'icon': 'fas fa-wallet text-success', 'perm': 'view_so_quy_tien_mat'},
            {'endpoint': 'SoTienGuiNganHang', 'label': 'Sổ Tiền Gửi NH (S2e)', 'icon': 'fas fa-building-columns text-success', 'perm': 'view_so_tien_gui_ngan_hang'},
            {'endpoint': 'SoChiPhiSXKD', 'label': 'Sổ Chi Phí SXKD (S3)', 'icon': 'fas fa-industry text-warning', 'perm': 'view_so_chi_phi'},
            {'endpoint': 'SoTheoDoiThueKhac', 'label': 'Sổ Thuế Khác (S3a)', 'icon': 'fas fa-file-invoice text-success', 'perm': 'view_so_thue_khac'},
            {'endpoint': 'SoTheoDoiNSNN', 'label': 'Sổ NSNN (S4)', 'icon': 'fas fa-landmark text-secondary', 'perm': 'view_so_nsnn'},
            {'endpoint': 'SoTheoDoiTienLuong', 'label': 'Theo Dõi Lương (S5)', 'icon': 'fas fa-file-invoice-dollar text-info', 'perm': 'view_so_luong'},
            {'endpoint': 'SoCongNoPhaiThu', 'label': 'Công Nợ Phải Thu (SP1)', 'icon': 'fas fa-user-plus text-primary', 'perm': 'view_so_cong_no_phai_thu'},
            {'endpoint': 'SoCongNoPhaiTra', 'label': 'Công Nợ Phải Trả NCC (SP2)', 'icon': 'fas fa-truck text-danger', 'perm': 'view_so_cong_no_phai_tra'},
            {'endpoint': 'SoCongNoPhaiTraNhanVien', 'label': 'Công Nợ Phải Trả Nhân Viên', 'icon': 'fas fa-user-minus text-danger', 'perm': 'view_so_cong_no_phai_tra', 'roles': ('accountant', 'manager', 'admin', 'master')},
            {'endpoint': 'SoCongNoBaoHiem', 'label': 'Công Nợ BHXH/BHYT/BHTN', 'icon': 'fas fa-shield-heart text-info', 'perm': 'view_so_cong_no_phai_tra', 'roles': ('accountant', 'manager', 'admin', 'master')},
            {'endpoint': 'TaiSanCoDinh', 'label': 'TSCĐ & Khấu Hao (SP3)', 'icon': 'fas fa-building text-danger', 'perm': 'view_tai_san_co_dinh'},
            {'endpoint': 'CongCuDungCu', 'label': 'Công Cụ Dụng Cụ', 'icon': 'fas fa-screwdriver-wrench text-secondary', 'perm': 'view_tai_san_co_dinh'},
            {'endpoint': 'SoTheoDoiKhoanVay', 'label': 'Theo Dõi Khoản Vay (SP4)', 'icon': 'fas fa-hand-holding-dollar text-danger', 'perm': 'view_khoan_vay'},
            {'endpoint': 'SoChiTietHangHoa', 'label': 'Sổ Chi Tiết Hàng Hóa', 'icon': 'fas fa-warehouse text-primary', 'perm': 'view_so_hang_hoa'},
        ],
    },
    {
        'id': 'hkd_so_quy',
        'section': 'accounting',
        'label': 'Tiền Mặt & Tiền Gửi NH',
        'icon': 'fas fa-wallet',
        'color': 'success',
        'description': 'Quỹ tiền mặt và tiền gửi ngân hàng',
        'items': [
            {'endpoint': 'SoQuyTienMat', 'label': 'Sổ Quỹ Tiền Mặt (S2e)', 'icon': 'fas fa-wallet text-success', 'perm': 'view_so_quy_tien_mat'},
            {'endpoint': 'SoTienGuiNganHang', 'label': 'Sổ Tiền Gửi NH (S2e)', 'icon': 'fas fa-building-columns text-success', 'perm': 'view_so_tien_gui_ngan_hang'},
        ],
    },
    {
        'id': 'hkd_cong_no',
        'section': 'accounting',
        'label': 'Công Nợ',
        'icon': 'fas fa-balance-scale',
        'color': 'danger',
        'description': 'Công nợ phải thu, phải trả NCC, nhân viên, bảo hiểm và thuế NSNN',
        'items': [
            {'endpoint': 'SoCongNoPhaiThu', 'label': 'Công Nợ Phải Thu (SP1)', 'icon': 'fas fa-user-plus text-primary', 'perm': 'view_so_cong_no_phai_thu'},
            {'endpoint': 'SoCongNoPhaiTra', 'label': 'Công Nợ Phải Trả NCC (SP2)', 'icon': 'fas fa-truck text-danger', 'perm': 'view_so_cong_no_phai_tra'},
            {'endpoint': 'SoCongNoPhaiTraNhanVien', 'label': 'Công Nợ Phải Trả Nhân Viên', 'icon': 'fas fa-user-minus text-danger', 'perm': 'view_so_cong_no_phai_tra', 'roles': ('accountant', 'manager', 'admin', 'master')},
            {'endpoint': 'SoCongNoBaoHiem', 'label': 'Công Nợ BHXH/BHYT/BHTN', 'icon': 'fas fa-shield-heart text-info', 'perm': 'view_so_cong_no_phai_tra', 'roles': ('accountant', 'manager', 'admin', 'master')},
            {'endpoint': 'SoCongNoThueNSNN', 'label': 'Công Nợ Thuế & NSNN', 'icon': 'fas fa-landmark text-danger', 'perm': 'view_so_nsnn', 'roles': ('accountant', 'manager', 'admin', 'master')},
        ],
    },
    {
        'id': 'hkd_tscd_ccdc',
        'section': 'accounting',
        'label': 'TSCĐ & CCDC',
        'icon': 'fas fa-building',
        'color': 'secondary',
        'description': 'Tài sản cố định và công cụ dụng cụ',
        'items': [
            {'endpoint': 'TaiSanCoDinh', 'label': 'TSCĐ & Khấu Hao (SP3)', 'icon': 'fas fa-building text-danger', 'perm': 'view_tai_san_co_dinh'},
            {'endpoint': 'CongCuDungCu', 'label': 'Công Cụ Dụng Cụ', 'icon': 'fas fa-screwdriver-wrench text-secondary', 'perm': 'view_tai_san_co_dinh'},
        ],
    },
    {
        'id': 'hkd_thue_nsnn',
        'section': 'accounting',
        'label': 'Thuế & NSNN',
        'icon': 'fas fa-landmark',
        'color': 'secondary',
        'description': 'Kê khai thuế và sổ theo dõi NSNN',
        'items': [
            {
                'endpoint': 'tax_report',
                'label': 'Kê Khai Thuế',
                'icon': 'fas fa-file-invoice-dollar text-danger',
                'perm': 'view_tax_report',
                'roles': ('accountant', 'manager', 'manager*', 'managerFB', 'admin', 'admin*', 'adminFB', 'master'),
            },
            {'endpoint': 'SoTheoDoiThueKhac', 'label': 'Sổ Thuế Khác (S3a)', 'icon': 'fas fa-file-invoice text-success', 'perm': 'view_so_thue_khac'},
            {'endpoint': 'SoTheoDoiNSNN', 'label': 'Sổ NSNN (S4)', 'icon': 'fas fa-landmark text-secondary', 'perm': 'view_so_nsnn'},
        ],
    },
    {
        'id': 'hkd_nhan_su',
        'section': 'hr',
        'label': 'Nhân Sự & Lương',
        'icon': 'fas fa-user-tie',
        'color': 'info',
        'description': 'Hồ sơ nhân viên, chấm công và lập bảng lương',
        'items': [
            {
                'endpoint': 'employees_page',
                'label': 'Danh Sách Nhân Viên',
                'icon': 'fas fa-users text-info',
                'perm': 'view_employees',
                'roles': ('accountant', 'manager', 'admin', 'master'),
            },
            {
                'endpoint': 'attendance_page',
                'label': 'Bảng Chấm Công',
                'icon': 'fas fa-fingerprint text-primary',
                'perm': 'view_employees',
                'roles': ('accountant', 'manager', 'admin', 'master'),
            },
            {
                'endpoint': 'salary_create',
                'label': 'Lập Bảng Lương',
                'icon': 'fas fa-calculator text-primary',
                'perm': 'view_bang_luong',
                'hide_metric': True,
            },
            {
                'endpoint': 'DanhSachBangLuong_05LDTL',
                'label': 'Danh Sách Bảng Lương (05-LĐTL)',
                'icon': 'fas fa-file-invoice-dollar text-info',
                'perm': 'view_bang_luong',
            },
            {
                'endpoint': 'SoTheoDoiTienLuong',
                'label': 'Theo Dõi Lương (S5)',
                'icon': 'fas fa-file-invoice-dollar text-info',
                'perm': 'view_so_luong',
            },
            {
                'endpoint': 'SoCongNoPhaiTraNhanVien',
                'label': 'Công Nợ Phải Trả Nhân Viên',
                'icon': 'fas fa-user-minus text-danger',
                'perm': 'view_so_cong_no_phai_tra',
                'roles': ('accountant', 'manager', 'admin', 'master'),
            },
            {
                'endpoint': 'SoCongNoBaoHiem',
                'label': 'Công Nợ BHXH/BHYT/BHTN',
                'icon': 'fas fa-shield-heart text-info',
                'perm': 'view_so_cong_no_phai_tra',
                'roles': ('accountant', 'manager', 'admin', 'master'),
            },
        ],
    },
    {
        'id': 'huong_dan_hub',
        'section': 'general',
        'label': 'Hướng Dẫn Sử Dụng',
        'icon': 'fas fa-question-circle',
        'color': 'info',
        '_menu_style': 'direct',
        'description': 'Hướng dẫn thao tác phần mềm',
        'items': [{
            'endpoint': 'huong_dan_su_dung',
            'label': 'Hướng Dẫn Sử Dụng',
            'icon': 'fas fa-question-circle text-info',
            'public': True,
        }],
    },
    {
        'id': 'cap_nhat_kien_thuc',
        'section': 'general',
        'label': 'Cập Nhật Kiến Thức',
        'icon': 'fas fa-newspaper',
        'color': 'primary',
        '_menu_style': 'direct',
        'description': 'Chính sách pháp luật và cập nhật cho HKD/DN',
        'items': [{
            'endpoint': 'cap_nhat_kien_thuc_page',
            'label': 'Cập Nhật Kiến Thức',
            'icon': 'fas fa-newspaper text-primary',
            'public': True,
        }],
    },
    {
        'id': 'he_thong',
        'section': 'general',
        'label': 'Hệ Thống',
        'icon': 'fas fa-cog',
        'color': 'dark',
        'description': 'Thiết lập và nhật ký hệ thống',
        'items': [
            {
                'endpoint': 'store_setup_page',
                'label': 'Thiết Lập',
                'icon': 'fas fa-sliders-h text-dark',
                'roles': ('admin', 'admin*', 'adminFB', 'master'),
                'hide_metric': True,
            },
            {
                'endpoint': 'audit_log_page',
                'label': 'Nhật Ký Truy Cập',
                'icon': 'fas fa-clipboard-list text-secondary',
                'perm': 'view_audit_log',
                'roles': ('manager*', 'managerFB', 'manager', 'admin', 'admin*', 'adminFB', 'master'),
                'hide_metric': True,
            },
        ],
    },
]

HKD_MENU = POS_HKD_MENU

HUB_EXTRA_ENDPOINTS = frozenset({
    'LapBangLuong',
    'salary_create',
    'DanhSachPhieuXuatKhoTheoDonBan',
    'hkd_accounting',
    'HKD_hub_group',
    'huong_dan_su_dung',
    'cap_nhat_kien_thuc_page',
    'audit_log_page',
    'store_setup_page',
    'settings_page',
    'SoCongNoPhaiTraNhanVien',
    'SoCongNoBaoHiem',
    'SoCongNoThueNSNN',
})

HUB_ENDPOINTS = frozenset(
    item['endpoint']
    for group in POS_HKD_MENU
    for item in group['items']
) | HUB_EXTRA_ENDPOINTS | frozenset({'HKD_dashboard'})

HKD_EXTRA_ENDPOINTS = HUB_EXTRA_ENDPOINTS
HKD_ENDPOINTS = HUB_ENDPOINTS


def _user_perms(user):
    perms = user.get('permissions') or []
    if isinstance(perms, str):
        return [p.strip() for p in perms.split(',') if p.strip()]
    return list(perms)


def _is_sme_accounting_user(user) -> bool:
    """User thuộc nhóm role Kế toán SME (không dùng hub HKD)."""
    if not user:
        return False
    role = str(user.get('role') or '').strip()
    if role in SME_ACCOUNTING_ROLES:
        return True
    perms = _user_perms(user)
    return any(p in perms for p in ('SME_dashboard', 'view_sme_accounting'))


def _tenant_is_sme(tenant_profile) -> bool:
    if not tenant_profile:
        return False
    from Services.tenant_profile import is_sme_regime
    return is_sme_regime(tenant_profile.get('accounting_regime'))


def user_can_access_item(user, item, tenant_profile=None):
    if not user:
        return False
    if tenant_profile is not None:
        from Services.tenant_profile import is_master_session
        if not is_master_session():
            # Tenant SME: ẩn toàn bộ mục menu POS & HKD
            if _tenant_is_sme(tenant_profile):
                return False
            if not tenant_profile.get('regime_active', True):
                ep = item.get('endpoint')
                if ep not in ('HKD_dashboard', 'huong_dan_su_dung'):
                    return False
            feature = item.get('feature') or HKD_MENU_FEATURE_MAP.get(item.get('endpoint'))
            if feature and not tenant_has_feature(tenant_profile, feature):
                return False
    # User role SME: không vào hub HKD dù tenant HKD (trừ master)
    role = user.get('role') or ''
    if role in SME_ACCOUNTING_ROLES and role != 'master':
        return False
    if item.get('public'):
        return True
    perms = _user_perms(user)
    perm = item.get('perm')
    roles = item.get('roles')
    if roles:
        if role in roles:
            return True
        if perm and perm in perms:
            return True
        return False
    if role in HKD_DEFAULT_ROLES:
        return True
    if perm and perm in perms:
        return True
    return False


def user_can_access_hkd(user, perm='view_accounting', tenant_profile=None):
    return user_can_access_item(
        user, {'perm': perm, 'roles': HKD_DEFAULT_ROLES}, tenant_profile,
    )


def user_can_access_hub(user, tenant_profile=None):
    """Navbar «POS và Kế Toán HKD» — ẩn với tenant SME và user role SME."""
    if _tenant_is_sme(tenant_profile):
        return False
    if _is_sme_accounting_user(user):
        return False
    for group in POS_HKD_MENU:
        for item in group['items']:
            if user_can_access_item(user, item, tenant_profile):
                return True
    return False


def user_can_see_sme_nav(user, tenant_profile=None) -> bool:
    """Navbar «Kế Toán SME»."""
    if not user:
        return False
    role = str(user.get('role') or '').strip()
    if role == 'master' or role in SME_ACCOUNTING_ROLES:
        return True
    perms = _user_perms(user)
    if 'SME_dashboard' in perms or 'view_sme_accounting' in perms:
        return True
    # Tenant SME: hiện menu cho admin/manager vận hành DN
    if _tenant_is_sme(tenant_profile) and role in (
        'admin', 'adminSME', 'manager', 'managerSME', 'accountant', 'accountantSME',
    ):
        return True
    return False


def _filter_group_items(user, group, tenant_profile=None):
    items = [item for item in group['items'] if user_can_access_item(user, item, tenant_profile)]
    if not items:
        return None
    return {**group, 'items': items}


def _overview_group(user, tenant_profile=None):
    if not user_can_access_item(user, {'endpoint': 'HKD_dashboard', 'public': True}, tenant_profile):
        return None
    return {
        'id': 'overview',
        'label': 'Tổng quan',
        'icon': 'fas fa-chart-pie',
        'color': 'primary',
        'description': 'Dashboard tổng hợp',
        'items': [{
            'endpoint': 'HKD_dashboard',
            'label': 'Dashboard',
            'icon': 'fas fa-chart-pie text-primary',
            'public': True,
        }],
    }


def get_hkd_menu_groups(user, tenant_profile=None):
    """Menu sidebar — có tiêu đề phân hệ."""
    result = []
    overview = _overview_group(user, tenant_profile)
    if overview:
        result.append(overview)

    for section in MENU_SECTIONS:
        section_groups = []
        for group in POS_HKD_MENU:
            if group.get('section') != section['id']:
                continue
            filtered = _filter_group_items(user, group, tenant_profile)
            if filtered:
                section_groups.append(filtered)
        if not section_groups:
            continue
        result.append({
            'id': f'section_{section["id"]}',
            '_type': 'section_header',
            'label': section['label'],
        })
        result.extend(section_groups)
    return result


def get_hub_group_cards(user, tenant_profile=None):
    """Các nhóm hiển thị trên dashboard chính."""
    cards = []
    for group in POS_HKD_MENU:
        filtered = _filter_group_items(user, group, tenant_profile)
        if filtered:
            cards.append(filtered)
    return cards


def get_hub_group_by_id(group_id, user, tenant_profile=None):
    """Một nhóm cho dashboard con."""
    for group in POS_HKD_MENU:
        if group['id'] == group_id:
            return _filter_group_items(user, group, tenant_profile)
    return None


def is_hub_endpoint(endpoint):
    if not endpoint:
        return False
    if endpoint in HUB_ENDPOINTS:
        return True
    if endpoint.startswith('print_') or endpoint.startswith('revenue/'):
        return True
    return False


def is_hkd_endpoint(endpoint):
    return is_hub_endpoint(endpoint)


HUB_DASHBOARD_FEATURED_ENDPOINTS = (
    'tax_report',
    'sale_details_page',
    'import_details_page',
    'inward_invoice',
    'outward_invoice',
    'huong_dan_su_dung',
    'audit_log_page',
)


def _find_menu_item(endpoint):
    for group in POS_HKD_MENU:
        for item in group['items']:
            if item['endpoint'] == endpoint:
                return item
    return None


def get_hub_dashboard_quick_links(user, tenant_profile=None):
    """Thao tác nhanh — POS + chứng từ HKD."""
    links = []

    for ep in ('import_stock', 'inward_invoice'):
        item = _find_menu_item(ep)
        if item and user_can_access_item(user, item, tenant_profile):
            links.append(dict(item))

    for group_id, limit in (
        ('pos_ban_hang', 2),
        ('hkd_nhan_su', 2),
        ('hkd_chung_tu', 2),
    ):
        group = get_hub_group_by_id(group_id, user, tenant_profile)
        if not group:
            continue
        items = group['items'] if limit is None else group['items'][:limit]
        links.extend(items)
    return links


def get_hub_dashboard_featured_links(user, tenant_profile=None):
    """Trang thường dùng trên dashboard."""
    links = []
    for endpoint in HUB_DASHBOARD_FEATURED_ENDPOINTS:
        item = _find_menu_item(endpoint)
        if item and user_can_access_item(user, item, tenant_profile):
            links.append(dict(item))
    return links


def get_hub_dashboard_soso_links(user, tenant_profile=None):
    """Sổ kế toán / báo cáo HKD thường mở."""
    links = []
    for group_id in ('pos_bao_cao', 'hkd_so_quy', 'hkd_cong_no'):
        group = get_hub_group_by_id(group_id, user, tenant_profile)
        if group:
            links.extend(group['items'][:2])
    return links
