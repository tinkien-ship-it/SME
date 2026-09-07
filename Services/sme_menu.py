"""Cấu hình menu trung tâm Kế Toán SME (TT58 / TT99) — tách biệt HKD (hộ kinh doanh)."""

SME_HUB_TITLE = 'Kế toán doanh nghiệp'

_R58 = ('SME_MICRO_TT58',)

SME_MENU_SECTIONS = (
    {'id': 'operations', 'label': 'NGHIỆP VỤ KINH DOANH'},
    {'id': 'accounting', 'label': 'KẾ TOÁN & TÀI CHÍNH'},
    {'id': 'tax', 'label': 'THUẾ & NSNN'},
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
        'description': 'Điểm bán hàng, gửi đại lý, đơn hàng, hóa đơn bán, trả hàng',
        'items': (
            {'endpoint': 'sale', 'label': 'Điểm bán hàng', 'icon': 'fas fa-cash-register'},
            {'endpoint': 'SME_consignment', 'label': 'Hàng gửi đi bán (TK 157)', 'icon': 'fas fa-truck'},
            {'endpoint': 'SME_sale_export', 'label': 'Xuất kho ra cảng', 'icon': 'fas fa-warehouse'},
            {'endpoint': 'SME_sale_export_list', 'label': 'Danh sách xuất khẩu', 'icon': 'fas fa-list'},
            {'endpoint': 'SME_customs_declarations', 'label': 'Tờ khai hải quan điện tử', 'icon': 'fas fa-passport'},
            {'endpoint': 'SME_order', 'label': 'Danh sách đơn hàng', 'icon': 'fas fa-list'},
            {'endpoint': 'SME_sale_details', 'label': 'Chi tiết hàng bán', 'icon': 'fas fa-receipt'},
            {'endpoint': 'SME_bank_transactions', 'label': 'Giao dịch ngân hàng', 'icon': 'fas fa-university'},
            {'endpoint': 'SME_outward_invoice', 'label': 'Hóa đơn bán hàng', 'icon': 'fas fa-file-export'},
            {'endpoint': 'SME_form_01_bh', 'label': 'Đại lý và ký gửi — mẫu 01-BH', 'icon': 'fas fa-file-signature'},
            {'endpoint': 'SME_form_02_bh', 'label': 'Thẻ quầy hàng — mẫu 02-BH', 'icon': 'fas fa-id-card'},
            {'endpoint': 'SME_SoCongNoPhaiThu', 'label': 'Sổ Công Nợ Phải Thu', 'icon': 'fas fa-user-clock'},
            {'endpoint': 'SME_return_sale', 'label': 'Khách trả hàng', 'icon': 'fas fa-rotate-left'},
        ),
    },
    {
        'id': 'purchasing', 'section': 'operations', 'label': 'Mua hàng',
        'icon': 'fas fa-bag-shopping', 'color': 'warning', 'endpoint': 'SME_purchasing',
        'description': 'Đơn mua, hóa đơn đầu vào, nhập kho, L/C, hải quan và trả nhà cung cấp',
        'items': (
            {'endpoint': 'SME_purchase_order_create', 'label': 'Lập đơn mua hàng', 'icon': 'fas fa-circle-plus'},
            {'endpoint': 'SME_purchase_order_list', 'label': 'Danh sách đơn mua', 'icon': 'fas fa-clipboard-list'},
            {'endpoint': 'SME_inward_invoice', 'label': 'Hóa đơn mua hàng', 'icon': 'fas fa-file-import'},
            {'endpoint': 'SME_landed_cost', 'label': 'Phân bổ chi phí mua hàng', 'icon': 'fas fa-share-alt'},
            {'endpoint': 'SME_import', 'label': 'Lập phiếu nhập kho', 'icon': 'fas fa-box-open'},
            {'endpoint': 'SME_DanhSachPhieuNhapKho', 'label': 'Danh sách phiếu nhập', 'icon': 'fas fa-list-check'},
            {'endpoint': 'SME_DanhSachPhieuNhapKho_VT', 'label': 'Phiếu nhập kho — mẫu 01-VT', 'icon': 'fas fa-file-import'},
            {'endpoint': 'SME_customs_declarations', 'label': 'Tờ khai hải quan điện tử', 'icon': 'fas fa-passport'},
            {'endpoint': 'SME_letter_of_credit', 'label': 'Mở L/C nhập khẩu', 'icon': 'fas fa-file-signature'},
            {'endpoint': 'SME_purchase_listing', 'label': 'Bảng kê mua hàng — mẫu 06-VT', 'icon': 'fas fa-list-ol'},
            {'endpoint': 'SME_purchase_02_tndn', 'label': 'Thu mua không HĐ — mẫu 02/TNDN', 'icon': 'fas fa-file-invoice'},
            {'endpoint': 'SME_import_details', 'label': 'Chi tiết mua hàng', 'icon': 'fas fa-list'},
            {'endpoint': 'SME_SoCongNoPhaiTra', 'label': 'Sổ Công Nợ Phải Trả', 'icon': 'fas fa-file-invoice-dollar'},
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
            {'endpoint': 'SME_material_alloc', 'label': 'Xuất dùng nội bộ (Nợ 642/641/627)', 'icon': 'fas fa-box-open'},
            {'endpoint': 'SME_inventory_lots', 'label': 'Theo dõi lô hàng', 'icon': 'fas fa-layer-group'},
            {'endpoint': 'SME_branches', 'label': 'Danh mục kho và chi nhánh', 'icon': 'fas fa-warehouse'},
        ),
    },
    {
        'id': 'crm', 'section': 'operations', 'label': 'CRM & chăm sóc KH',
        'icon': 'fas fa-handshake', 'color': 'primary', 'endpoint': 'crm_dashboard',
        'description': 'Lead, pipeline, báo giá, nhật ký chăm sóc và hồ sơ khách hàng 360°',
        'items': (
            {'endpoint': 'crm_dashboard', 'label': 'Tổng quan CRM', 'icon': 'fas fa-gauge'},
            {'endpoint': 'crm_leads_page', 'label': 'Leads (KH Tiềm Năng)', 'icon': 'fas fa-user-plus'},
            {'endpoint': 'crm_inbound_page', 'label': 'Hub Inbound (Kênh Marketing)', 'icon': 'fas fa-plug'},
            {'endpoint': 'crm_pipeline_page', 'label': 'Pipeline (Quy Trình Bán Hàng)', 'icon': 'fas fa-filter'},
            {'endpoint': 'crm_quotes_page', 'label': 'Báo giá CRM', 'icon': 'fas fa-file-invoice-dollar'},
            {'endpoint': 'crm_contracts_page', 'label': 'Hợp đồng', 'icon': 'fas fa-file-contract'},
            {'endpoint': 'crm_campaigns_page', 'label': 'Chiến dịch Marketing', 'icon': 'fas fa-bullhorn'},
            {'endpoint': 'crm_tickets_page', 'label': 'Phiếu hỗ trợ / Helpdesk', 'icon': 'fas fa-headset'},
            {'endpoint': 'crm_loyalty_page', 'label': 'Loyalty/CSAT (Trung Thành & Hài Lòng)', 'icon': 'fas fa-gift'},
            {'endpoint': 'crm_settings_page', 'label': 'Cấu hình CRM', 'icon': 'fas fa-cog'},
            {'endpoint': 'crm_customers_page', 'label': 'Danh Mục Khách Hàng', 'icon': 'fas fa-users'},
        ),
    },
    {
        'id': 'catalog', 'section': 'operations', 'label': 'Danh mục',
        'icon': 'fas fa-box', 'color': 'info', 'endpoint': 'products',
        'description': 'Sản phẩm, nhà cung cấp và khách hàng',
        'items': (
            {'endpoint': 'products', 'label': 'Danh mục sản phẩm', 'icon': 'fas fa-box'},
            {'endpoint': 'product_aliases', 'label': 'Tên hàng đã liên kết', 'icon': 'fas fa-link'},
            {'endpoint': 'suppliers_page', 'label': 'Nhà cung cấp', 'icon': 'fas fa-truck'},
            {'endpoint': 'customers_page', 'label': 'Khách hàng', 'icon': 'fas fa-users'},
            {'endpoint': 'crm_dashboard', 'label': 'CRM & chăm sóc KH', 'icon': 'fas fa-handshake'},
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
            {'endpoint': 'SME_consignment', 'label': 'Hàng gửi đi bán (TK 157)', 'icon': 'fas fa-truck'},
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
        'description': 'MRP kế hoạch NVL, MES điều hành xưởng, giá thành TT99',
        'items': (
            {
                'endpoint': 'SME_mrp',
                'label': 'MRP — Kế hoạch nhu cầu NVL',
                'icon': 'fas fa-project-diagram',
                'access': 'mrp',
            },
            {
                'endpoint': 'SME_mes',
                'label': 'MES — Điều hành sản xuất',
                'icon': 'fas fa-tablet-alt',
                'access': 'mes',
            },
            {'endpoint': 'SME_production', 'label': 'Lệnh sản xuất & BOM', 'icon': 'fas fa-industry'},
            {'endpoint': 'SME_period_cost_allocation', 'label': 'Giá thành 3 phương án (ĐM & chốt kỳ)', 'icon': 'fas fa-balance-scale'},
            {'endpoint': 'SME_service_costing', 'label': 'Giá vốn dịch vụ', 'icon': 'fas fa-handshake'},
            {'endpoint': 'SME_deferred_revenue', 'label': 'Gói DV trả trước (3387)', 'icon': 'fas fa-calendar-alt'},
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
            {'endpoint': 'SME_accruals', 'label': 'Chi phí phải trả 335 / DT chưa TH 3387', 'icon': 'fas fa-file-invoice'},
        ),
    },
    {
        'id': 'assets', 'section': 'accounting', 'label': 'Tài sản cố định và công cụ dụng cụ',
        'icon': 'fas fa-building', 'color': 'danger', 'endpoint': 'SME_TSCD',
        'description': 'Theo dõi tài sản cố định và công cụ dụng cụ',
        'items': (
            {'endpoint': 'SME_TSCD', 'label': 'Tổng quan tài sản cố định', 'icon': 'fas fa-chart-pie'},
            {'endpoint': 'SME_fixed_assets', 'label': 'Quản lý và khấu hao TSCĐ', 'icon': 'fas fa-building'},
            {'endpoint': 'SME_investment_properties', 'label': 'Bất động sản đầu tư', 'icon': 'fas fa-city'},
            {'endpoint': 'SME_CCDC', 'label': 'Tổng quan công cụ dụng cụ', 'icon': 'fas fa-chart-pie'},
            {'endpoint': 'SME_tools', 'label': 'Danh mục công cụ dụng cụ', 'icon': 'fas fa-screwdriver-wrench'},
            {'endpoint': 'SME_prepaid', 'label': 'Chi phí trả trước (TK 242)', 'icon': 'fas fa-calendar-check'},
        ),
    },
    {
        'id': 'hr', 'section': 'accounting', 'label': 'Nhân sự và tiền lương',
        'icon': 'fas fa-user-tie', 'color': 'secondary', 'endpoint': 'SME_dashboard_HRSalary',
        'description': 'Hồ sơ nhân viên, chấm công, lập lương và phân bổ chi phí nhân công',
        'items': (
            {'endpoint': 'SME_employees', 'label': 'Danh sách nhân viên', 'icon': 'fas fa-users'},
            {'endpoint': 'SME_attendance', 'label': 'Bảng chấm công', 'icon': 'fas fa-fingerprint'},
            {'endpoint': 'SME_hrm_contracts', 'label': 'Hợp đồng lao động (HĐLĐ)', 'icon': 'fas fa-file-signature'},
            {'endpoint': 'SME_hrm_shifts', 'label': 'Ca làm việc & tăng ca', 'icon': 'fas fa-clock'},
            {'endpoint': 'SME_hrm_formulas', 'label': 'Công thức lương động', 'icon': 'fas fa-flask'},
            {'endpoint': 'SME_hrm_compliance', 'label': 'Cảnh báo tuân thủ LĐ', 'icon': 'fas fa-exclamation-triangle'},
            {'endpoint': 'hrm_ess_portal', 'label': 'Cổng nhân viên (ESS)', 'icon': 'fas fa-mobile-alt'},
            {'endpoint': 'SME_kpi_settings', 'label': 'Thiết lập KPI phòng ban & nhân sự', 'icon': 'fas fa-bullseye'},
            {'endpoint': 'SME_salary_create', 'label': 'Lập bảng lương — mẫu 01-LĐTL', 'icon': 'fas fa-calculator'},
            {'endpoint': 'SME_PhaiTraCongNhanVien', 'label': 'Công nợ phải trả nhân viên', 'icon': 'fas fa-hand-holding-usd'},
            {'endpoint': 'SME_labor_sheets', 'label': 'Thưởng, ngoài giờ và thuê ngoài — mẫu 02 đến 04', 'icon': 'fas fa-gift'},
            {'endpoint': 'SME_labor_contracts', 'label': 'Hợp đồng giao khoán — mẫu 05 và 06-LĐTL', 'icon': 'fas fa-file-contract'},
            {'endpoint': 'SME_payroll_allocation', 'label': 'Phân bổ lương — mẫu 08-LĐTL', 'icon': 'fas fa-table'},
        ),
    },
    {
        'id': 'tax_nsnn', 'section': 'tax', 'label': 'Thuế & NSNN',
        'icon': 'fas fa-landmark', 'color': 'danger', 'endpoint': 'SME_tax_nsnn',
        'description': 'Thuế phải nộp, tờ khai và các khoản nộp ngân sách nhà nước (bảo hiểm…)',
        'items': (
            {'endpoint': 'SME_tax_nsnn', 'label': 'Theo dõi thuế và NSNN (133 / 333)', 'icon': 'fas fa-landmark'},
            {'endpoint': 'SME_vat_declaration', 'label': 'Tờ khai thuế giá trị gia tăng', 'icon': 'fas fa-file-invoice'},
            {'endpoint': 'SME_cit', 'label': 'Thuế thu nhập doanh nghiệp tạm nộp', 'icon': 'fas fa-percent'},
            {'endpoint': 'SME_cit_declaration', 'label': 'Quyết toán thuế thu nhập doanh nghiệp', 'icon': 'fas fa-file-code'},
            {'endpoint': 'SME_pit_declaration', 'label': 'Thuế thu nhập cá nhân khấu trừ từ lương', 'icon': 'fas fa-user-shield'},
            {'endpoint': 'SME_insurance_pay', 'label': 'Nộp bảo hiểm xã hội — mẫu 07-LĐTL', 'icon': 'fas fa-shield-alt'},
        ),
    },
    {
        'id': 'reports', 'section': 'reports', 'label': 'Báo cáo tài chính',
        'icon': 'fas fa-chart-line', 'color': 'primary', 'endpoint': 'SME_BCTC',
        'description': 'BCTC theo chế độ kế toán (TT58 DNSN / TT99 DN)',
        'items': (
            {
                'endpoint': 'SME_BCTC_reports', 'label': 'Bộ báo cáo tài chính',
                'icon': 'fas fa-file-excel', 'requires_bctc': True,
            },
            {
                'endpoint': 'SME_dnsn_books', 'label': 'Sổ & biểu mẫu DNSN (TT58)',
                'icon': 'fas fa-book', 'regimes': _R58,
            },
            {'endpoint': 'SME_mgmt_report', 'label': 'Báo cáo quản trị', 'icon': 'fas fa-chart-column'},
            {'endpoint': 'SME_revenue_report', 'label': 'Báo cáo doanh thu điểm bán hàng', 'icon': 'fas fa-chart-bar'},
            {'endpoint': 'SME_profit_report', 'label': 'Báo cáo lợi nhuận điểm bán hàng', 'icon': 'fas fa-coins'},
            {
                'endpoint': 'SME_fx_revaluation', 'label': 'Đánh giá lại tỷ giá',
                'icon': 'fas fa-dollar-sign',
            },
            {
                'endpoint': 'SME_capital', 'label': 'Góp vốn và cổ tức',
                'icon': 'fas fa-piggy-bank',
            },
        ),
    },
    {
        'id': 'utilities', 'section': 'reports', 'label': 'Tiện ích',
        'icon': 'fas fa-cubes', 'color': 'secondary', 'endpoint': 'SME_utilities',
        'description': 'Công cụ hỗ trợ và thiết lập hệ thống',
        'items': (
            {'endpoint': 'keto_pos_intro', 'label': 'Giới thiệu KETO POS', 'icon': 'fas fa-bullhorn'},
            {'endpoint': 'huong_dan_su_dung', 'label': 'Hướng dẫn sử dụng', 'icon': 'fas fa-circle-question'},
            {'endpoint': 'SME_cap_nhat_kien_thuc', 'label': 'Cập nhật kiến thức', 'icon': 'fas fa-newspaper'},
            {'endpoint': 'SME_audit_log', 'label': 'Nhật ký truy cập', 'icon': 'fas fa-clock-rotate-left'},
            {'endpoint': 'store_setup_page', 'label': 'Thiết lập hệ thống', 'icon': 'fas fa-sliders'},
        ),
    },
)

def _normalize_menu_regime(regime: str | None) -> str:
    r = (regime or '').strip().upper()
    if 'TT58' in r or 'MICRO' in r:
        return 'SME_MICRO_TT58'
    if 'TT99' in r or r.startswith('SME'):
        return 'SME_TT99'
    return r or 'SME_TT99'


def _endpoint_registered(endpoint: str | None) -> bool:
    """Ẩn mục menu nếu route chưa có trên app đang chạy (tránh BuildError)."""
    if not endpoint:
        return True
    try:
        from flask import current_app, has_app_context
        if not has_app_context():
            return True
        return endpoint in current_app.view_functions
    except Exception:
        return True


def _item_allowed(
    item: dict,
    regime: str,
    *,
    show_bctc: bool = True,
    user_role: str | None = None,
    permissions=None,
) -> bool:
    allowed = item.get('regimes')
    if allowed and regime not in allowed:
        return False
    if item.get('requires_bctc') and not show_bctc:
        return False
    if not _endpoint_registered(item.get('endpoint')):
        return False
    access = item.get('access')
    if access == 'mrp':
        from Services.sme_roles import can_access_mrp
        if not can_access_mrp(user_role, permissions):
            return False
    elif access == 'mes':
        from Services.sme_roles import can_access_mes
        if not can_access_mes(user_role, permissions):
            return False
    return True


def get_sme_menu_groups(
    accounting_regime: str | None = None,
    *,
    user_role: str | None = None,
    permissions=None,
):
    """Trả menu đã xen tiêu đề phân khu; lọc item theo chế độ TT58/TT99 + PP thuế + quyền MRP/MES."""
    regime = _normalize_menu_regime(accounting_regime)
    if accounting_regime is None:
        try:
            from flask import has_request_context
            if has_request_context():
                from Services.tenant_profile import get_current_tenant_profile
                regime = _normalize_menu_regime(
                    (get_current_tenant_profile() or {}).get('accounting_regime')
                )
        except Exception:
            pass

    if user_role is None:
        try:
            from Services.sme_roles import current_session_role
            user_role = current_session_role()
        except Exception:
            user_role = None
    if permissions is None:
        try:
            from flask import has_request_context, session
            if has_request_context():
                permissions = (session.get('user') or {}).get('permissions')
        except Exception:
            permissions = None

    show_bctc = True
    if regime == 'SME_MICRO_TT58':
        try:
            from db_utils import get_db_connection
            from Services.sme.regime_profile import get_ledger_profile
            conn = get_db_connection()
            try:
                show_bctc = bool(get_ledger_profile(conn).get('show_bctc', True))
            finally:
                conn.close()
        except Exception:
            show_bctc = True

    result = []
    for section in SME_MENU_SECTIONS:
        groups = []
        for group in SME_MENU_GROUPS:
            if group['section'] != section['id']:
                continue
            g = dict(group)
            items = tuple(
                i for i in (g.get('items') or ())
                if _item_allowed(
                    i, regime, show_bctc=show_bctc,
                    user_role=user_role, permissions=permissions,
                )
            )
            # Ẩn nhóm nếu endpoint hub chưa đăng ký
            if g.get('endpoint') and not _endpoint_registered(g.get('endpoint')):
                if not items:
                    continue
            g['items'] = items
            g_regimes = g.get('regimes')
            if g_regimes and regime not in g_regimes:
                continue
            # Ẩn cả nhóm BCTC nếu không còn item nào (TT58 PP1/PP3)
            if g.get('id') == 'reports' and not items:
                continue
            groups.append(g)
        if groups:
            result.append({**section, '_type': 'section_header'})
            result.extend(groups)
    return result


def _current_menu_flags():
    regime = _normalize_menu_regime(None)
    show_bctc = True
    try:
        from flask import has_request_context
        if has_request_context():
            from Services.tenant_profile import get_current_tenant_profile
            regime = _normalize_menu_regime(
                (get_current_tenant_profile() or {}).get('accounting_regime')
            )
    except Exception:
        pass
    if regime == 'SME_MICRO_TT58':
        try:
            from db_utils import get_db_connection
            from Services.sme.regime_profile import get_ledger_profile
            conn = get_db_connection()
            try:
                show_bctc = bool(get_ledger_profile(conn).get('show_bctc', True))
            finally:
                conn.close()
        except Exception:
            show_bctc = True
    return regime, show_bctc


def get_sme_quick_links():
    regime, show_bctc = _current_menu_flags()
    endpoints = [
        ('SME_journal', 'Ghi sổ bút toán', 'fas fa-pen-to-square'),
        ('SME_chung_tu', 'Chứng từ kế toán', 'fas fa-file-invoice'),
        ('SME_purchase_order_create', 'Lập đơn mua hàng', 'fas fa-cart-plus'),
    ]
    if show_bctc:
        endpoints.append(('SME_BCTC_reports', 'Xem báo cáo tài chính', 'fas fa-chart-line'))
    elif regime == 'SME_MICRO_TT58':
        endpoints.append(('SME_dnsn_books', 'Sổ & biểu mẫu DNSN (TT58)', 'fas fa-book'))
    return [{'endpoint': ep, 'label': label, 'icon': icon} for ep, label, icon in endpoints]


def get_sme_featured_links():
    regime, show_bctc = _current_menu_flags()
    endpoints = [
        ('SME_chart_of_accounts', 'Hệ thống tài khoản', 'fas fa-sitemap'),
        ('SME_general_ledger', 'Sổ cái / Cân đối phát sinh', 'fas fa-scale-balanced'),
        ('SME_san_xuat_gia_thanh', 'Sản xuất & giá thành', 'fas fa-industry'),
    ]
    if regime == 'SME_MICRO_TT58':
        endpoints.append(('SME_dnsn_books', 'Sổ & biểu mẫu DNSN (TT58)', 'fas fa-book'))
    if show_bctc:
        endpoints.append(('SME_BCTC', 'Trung tâm báo cáo', 'fas fa-chart-column'))
    return [{'endpoint': ep, 'label': label, 'icon': icon} for ep, label, icon in endpoints]


def is_sme_endpoint(endpoint):
    if not endpoint:
        return False
    if endpoint.startswith('SME_'):
        return True
    if endpoint.startswith('crm_'):
        return True
    return any(
        endpoint == item['endpoint']
        for group in SME_MENU_GROUPS
        for item in group['items']
    )


def get_sme_group_by_id(group_id, accounting_regime=None):
    """Nhóm menu đã lọc theo chế độ TT58/TT99 của tenant."""
    for group in get_sme_menu_groups(accounting_regime):
        if group.get('id') == group_id and group.get('_type') != 'section_header':
            return group
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
