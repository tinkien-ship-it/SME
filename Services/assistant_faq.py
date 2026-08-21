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
            'Vào **Hệ Thống → Thiết Lập** (`/thiet-lap`): điền thông tin doanh nghiệp hoặc hộ kinh doanh, MST, địa chỉ và '
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
        'keywords': [
            'phòng trọ', 'cho thuê', 'lưu trú', 'thu tiền phòng', 'chỉ số điện',
            'khách sạn', 'phòng cho thuê', 'quản lý khách sạn', 'quản lý phòng',
            'thuê phòng', 'dv thuê phòng',
        ],
        'pages': ['rental_service'],
        'title': 'Quản lý khách sạn / phòng cho thuê',
        'answer': (
            'Menu **Quản Lý DV Thuê Phòng** (khách sạn, phòng trọ, phòng cho thuê):\n'
            '1) Tải mẫu Excel nhập số phòng & giá.\n'
            '2) Trên **Sơ đồ phòng**: điền thông tin người thuê, chỉ số điện bàn giao.\n'
            '3) **Thu Tiền** trước, sau đó mới **Trả Phòng**.\n'
            '4) Cuối tháng cập nhật chỉ số điện mới rồi in phiếu thanh toán.'
        ),
    },
    {
        'id': 'accounting_auto',
        'keywords': ['kế toán', 'sổ sách', 'chứng từ', 'hạch toán', 'phiếu chi', 'tt 88', 'thông tư 88'],
        'pages': ['HKD_dashboard', 'DanhSachPhieuChi'],
        'title': 'Kế toán HKD',
        'answer': (
            'Hầu hết chứng từ và sổ kế toán (TT 88) được lập **tự động** từ bán hàng, nhập kho, lương, TSCĐ. '
            'Sản xuất thành phẩm: **Chứng Từ Kế Toán → Tính Giá Thành (Thành Phẩm)**. '
            'Chi phí không có HĐĐT: **Chứng Từ Kế Toán → Phiếu Chi → Lập Phiếu Chi – Chi Phát Sinh**.'
        ),
    },
    {
        'id': 'fb_service',
        'keywords': [
            'f&b', 'fb', 'nhà hàng', 'quán ăn', 'quán cafe', 'quán cà phê', 'cà phê', 'cafe',
            'trà sữa', 'trà sửa', 'quán trà sữa', 'dịch vụ ăn uống', 'ăn uống', 'ẩm thực',
            'quản lý quán ăn', 'quản lý nhà hàng', 'quản lý cà phê', 'quản lý trà sữa',
            'dịch vụ ẩm thực',
        ],
        'pages': ['F_and_B_service', 'import'],
        'title': 'F&B nhà hàng / quán ăn / cà phê / trà sữa',
        'answer': (
            'Menu **F&B** dành cho **nhà hàng, quán ăn, quán cà phê, trà sữa**:\n'
            '1) Tạo **khu vực & bàn** (Excel hoặc cấu hình sơ đồ quán).\n'
            '2) Tạo **thực đơn** (Excel hoặc Thêm Món Thủ Công) — mã món hệ thống tự cấp.\n'
            '3) Nhập kho **Hàng Dùng Ngay** / **Nguyên Vật Liệu** từ Hóa Đơn Mua Hàng hoặc XML.\n'
            '4) Khách chọn bàn → gọi món → theo dõi phục vụ → **Thanh Toán**.\n'
            '5) Có thể lập **Định mức** từng món hoặc **Kiểm Kê NVL** cuối ngày rồi **Chốt doanh thu**.\n'
            'Chi tiết: tab **Dịch Vụ Ẩm Thực (F&B)** trong **Hướng Dẫn Sử Dụng**.'
        ),
        'follow_ups': [
            'Tạo danh sách bàn thế nào?',
            'Tạo menu món ăn thế nào?',
            'Lập phiếu nhập kho F&B?',
            'Thanh toán order bàn thế nào?',
        ],
    },
    {
        'id': 'fb_tables',
        'keywords': [
            'tạo bàn', 'danh sách bàn', 'khu vực bàn', 'sơ đồ quán', 'nhập khu vực',
            'số bàn', 'cấu hình sơ đồ', 'thêm bàn', 'bàn ăn',
        ],
        'pages': ['F_and_B_service'],
        'title': 'Tạo khu vực & danh sách bàn F&B',
        'answer': (
            'Trên màn hình F&B:\n'
            '1) Nhấn **Nhập Khu Vực & Số Bàn** → **Tải Mẫu**.\n'
            '2) Mở Excel, điền **khu vực** và **số bàn** → lưu.\n'
            '3) **Nhập file Excel** → **Xác nhận thêm bàn**.\n'
            'Hoặc tạo thủ công trên **Cấu Hình Sơ Đồ Quán** (các ô sẵn trên giao diện).'
        ),
        'follow_ups': [
            'Tạo menu món ăn thế nào?',
            'Khi khách chọn bàn làm sao?',
            'Theo dõi món bếp thế nào?',
        ],
    },
    {
        'id': 'fb_menu',
        'keywords': [
            'tạo menu', 'nhập menu', 'thực đơn', 'thêm món', 'món thủ công', 'nhập menu excel',
            'tải menu', 'mã món', 'hình món', 'quản lý menu', 'lưu thực đơn',
        ],
        'pages': ['F_and_B_service'],
        'title': 'Tạo thực đơn F&B',
        'answer': (
            '**Cách 1 — Excel:** **Nhập menu Excel** → **Tải Menu** → điền món theo hướng dẫn → lưu → '
            '**Nhập Menu** chọn file. Vào **Quản Lý Menu & Định Mức** → **Sửa** để thêm hình.\n'
            '**Cách 2 — Thủ công:** **Thêm Món Thủ Công** → nhập thông tin + chọn ảnh → **Lưu Thực Đơn**.\n'
            '**Lưu ý:** mã món do hệ thống tự tạo, không cần nhập.'
        ),
        'follow_ups': [
            'Định mức nguyên liệu món ăn?',
            'Hàng dùng ngay hiện lên menu thế nào?',
            'Khi khách chọn bàn làm sao?',
        ],
    },
    {
        'id': 'fb_order',
        'keywords': [
            'gọi món', 'chọn bàn', 'order bàn', 'theo dõi phục vụ', 'món đã gọi',
            'nhân viên bếp', 'phục vụ bàn', 'order', 'đặt món', 'theo dõi món bếp',
            'khi khách chọn bàn', 'chọn được bàn',
        ],
        'pages': ['F_and_B_service'],
        'title': 'Gọi món & theo dõi phục vụ',
        'answer': (
            'Khi khách có bàn: nhấn **số bàn** → chọn món khách gọi → chờ tính tiền.\n'
            'Sau khi chọn món, hệ thống hiện danh sách món đã gọi để Quản lý kiểm soát; '
            'nhân viên Bếp / phục vụ biết món nào cần làm trước.'
        ),
        'follow_ups': [
            'Thanh toán order bàn thế nào?',
            'Định mức nguyên liệu món ăn?',
            'Kiểm kê NVL cuối ngày?',
        ],
    },
    {
        'id': 'fb_recipe',
        'keywords': [
            'định mức', 'định mức nvl', 'định mức món', 'nguyên liệu món', 'công thức món',
            'nguyên vật liệu', 'nvl món ăn', 'định mức nguyên liệu',
        ],
        'pages': ['F_and_B_service'],
        'title': 'Định mức nguyên liệu món ăn',
        'answer': (
            'Vào **Quản Lý Menu & Định Mức** → chọn món → **Định Mức** → chọn nguyên liệu '
            'và số lượng cho 1 phần món.\n'
            'Không bắt buộc: chưa có định mức vẫn bán được. Phương án nhanh — cuối ngày vào '
            '**Kiểm Kê NVL** nhập tồn còn lại → **Chốt doanh thu** để hệ thống trừ NVL đã dùng.\n'
            'NVL **không nhập giá bán**; chi phí tính theo giá vốn khi có định mức hoặc kiểm kê.'
        ),
        'follow_ups': [
            'Kiểm kê NVL cuối ngày?',
            'Lập phiếu nhập kho F&B?',
            'Hàng dùng ngay và nguyên vật liệu khác nhau?',
        ],
    },
    {
        'id': 'fb_inventory_check',
        'keywords': [
            'kiểm kê nvl', 'kiểm kê nguyên liệu', 'chốt doanh thu', 'chốt cuối ngày',
            'trừ tồn nvl', 'cuối ngày f&b',
        ],
        'pages': ['F_and_B_service'],
        'title': 'Kiểm kê NVL cuối ngày',
        'answer': (
            'Dùng khi món **chưa** lập định mức (hoặc muốn chốt nhanh):\n'
            '1) Vào **Kiểm Kê NVL** cuối ngày.\n'
            '2) Nhập số nguyên vật liệu **còn lại** thực tế.\n'
            '3) Nhấn **Chốt doanh thu** — hệ thống tự trừ tồn kho phần đã dùng trong ngày.'
        ),
        'follow_ups': [
            'Định mức nguyên liệu món ăn?',
            'Thanh toán order bàn thế nào?',
            'Lập phiếu nhập kho F&B?',
        ],
    },
    {
        'id': 'fb_import',
        'keywords': [
            'phiếu nhập f&b', 'nhập kho f&b', 'hàng dùng ngay', 'nguyên vật liệu nhập kho',
            'lập phiếu nhập', 'tạo phiếu nhập f&b', 'nhập nvl', 'bia nước ngọt',
            'đơn vị sỉ', 'đơn vị lẻ', 'tỷ lệ thùng', 'cột kho f&b',
        ],
        'pages': ['F_and_B_service', 'import', 'inward_invoice'],
        'title': 'Lập phiếu nhập kho F&B',
        'answer': (
            '**Hóa Đơn Mua Hàng → Lập Phiếu Nhập** (số phiếu tự sinh, chi tiết HĐ tự điền).\n'
            '- **Hàng Dùng Ngay** (bia, nước suối…): tự hiện trên menu sau nhập; trừ kho khi thanh toán; '
            '**Sửa** món để thêm ảnh.\n'
            '- **Nguyên Vật Liệu** (thịt, rau, gia vị…): chỉ nhập kho, không hiện menu; trừ kho qua '
            '**định mức** hoặc **kiểm kê cuối ngày**.\n'
            'Thiết lập ĐV lẻ / ĐV sỉ / **Tỷ lệ**, nhập giá bán lẻ–sỉ; nhiều kho → chọn cột **KHO**. '
            'NVL không nhập giá bán.'
        ),
        'follow_ups': [
            'Nhập kho từ file XML thế nào?',
            'Hàng dùng ngay và nguyên vật liệu khác nhau?',
            'Sửa phiếu nhập kho sai?',
        ],
    },
    {
        'id': 'fb_import_xml',
        'keywords': [
            'nhập xml f&b', 'xml hóa đơn', 'tự động lập', 'không có hóa đơn mua',
            'nhập từ xml', 'hđđt xml', 'tạo phiếu nhập từ xml',
        ],
        'pages': ['F_and_B_service', 'import'],
        'title': 'Nhập kho F&B từ file XML',
        'answer': (
            'Khi HKD không lấy được danh sách HĐ mua về phần mềm:\n'
            '1) Tải file **XML** NCC gửi về máy.\n'
            '2) **Mua Hàng → Tạo Phiếu Nhập F&B** → mục **NHẬP TỪ XML (HĐĐT)**.\n'
            '3) **Chọn file** → **TỰ ĐỘNG LẬP**.\n'
            '4) Kiểm tra **Loại Hàng**, **Kho** và thông tin khác → **Lưu Phiếu Nhập**.'
        ),
        'follow_ups': [
            'Lập phiếu nhập kho F&B?',
            'Hàng dùng ngay và nguyên vật liệu khác nhau?',
            'Sửa phiếu nhập kho sai?',
        ],
    },
    {
        'id': 'fb_line_types',
        'keywords': [
            'hàng dùng ngay và nguyên vật liệu', 'loại hàng f&b', 'ready made',
            'khác nhau hàng dùng ngay', 'không hiện trên menu',
            'hàng dùng ngay hiện lên menu', 'hiện lên menu',
        ],
        'pages': ['F_and_B_service', 'import'],
        'title': 'Hàng dùng ngay vs nguyên vật liệu',
        'answer': (
            '**Hàng Dùng Ngay** (bia, nước ngọt, đóng gói…): sau nhập kho **tự hiện trên menu** '
            '(có mã món, giá bán); trừ tồn khi thanh toán khách; thêm ảnh bằng nút **Sửa**.\n'
            '**Nguyên Vật Liệu** (thịt, cá, rau, gia vị…): **chỉ nhập kho**, không hiện menu; '
            'trừ tồn khi có **định mức** hoặc qua **Kiểm Kê NVL** cuối ngày. Không nhập giá bán NVL.'
        ),
        'follow_ups': [
            'Định mức nguyên liệu món ăn?',
            'Kiểm kê NVL cuối ngày?',
            'Lập phiếu nhập kho F&B?',
        ],
    },
    {
        'id': 'fb_edit_import',
        'keywords': [
            'sửa phiếu nhập', 'sửa phiếu nhập kho', 'phiếu nhập sai', 'danh sách phiếu nhập',
        ],
        'pages': ['DanhSachPhieuNhapKho', 'import', 'F_and_B_service'],
        'title': 'Sửa phiếu nhập kho',
        'answer': (
            'Vào **Danh Sách Phiếu Nhập** → chọn phiếu cần sửa → nhấn **Sửa** → chỉnh đúng → **Lưu**. '
            'Hệ thống cập nhật chứng từ / tồn kho liên quan.'
        ),
        'follow_ups': [
            'Lập phiếu nhập kho F&B?',
            'Nhập kho từ file XML thế nào?',
            'Thanh toán order bàn thế nào?',
        ],
    },
    {
        'id': 'fb_payment',
        'keywords': [
            'thanh toán bàn', 'thanh toán f&b', 'hoàn tất thanh toán', 'xuất hóa đơn bàn',
            'tính tiền bàn', 'thanh toán order', 'hóa đơn bàn ăn',
        ],
        'pages': ['F_and_B_service'],
        'title': 'Thanh toán order bàn F&B',
        'answer': (
            'Nhấn bàn cần thanh toán → **Thanh Toán**.\n'
            'Bảng **Thanh Toán & Hóa Đơn**: nhập thông tin khách + email (nếu gửi HĐ); '
            'không lấy HĐ thì để mặc định.\n'
            'Chọn **Phương Thức Thanh Toán**, bật **Xuất hóa đơn điện tử** nếu cần → '
            '**HOÀN TẤT THANH TOÁN**.'
        ),
        'follow_ups': [
            'Theo dõi món bếp thế nào?',
            'Kiểm kê NVL cuối ngày?',
            'Quản lý quán ăn, Cà Phê, Trà sửa, Nhà hàng?',
        ],
    },
    {
        'id': 'production_costing',
        'keywords': [
            'giá thành', 'tính giá thành', 'kế toán tính giá thành', 'thành phẩm', 'phiếu sản xuất', 'sản xuất',
            'bom', 'định mức bom', 'vật tư sản xuất', 'xuất vật tư', 'nhập thành phẩm',
            'chế biến thành phẩm',
        ],
        'pages': ['production_page', 'production_print'],
        'title': 'Tính giá thành thành phẩm',
        'answer': (
            'Menu **Chứng Từ Kế Toán → Tính Giá Thành (Thành Phẩm)**:\n'
            '1) Tab **Định mức BOM**: chọn **thành phẩm** (mã dạng **TP001**…), '
            'thêm **vật tư** (mã **VT…**) và số lượng định mức cho **1 đơn vị thành phẩm** → **Lưu định mức**.\n'
            '2) Tab **Phiếu sản xuất**: chọn thành phẩm, **Số lượng hoàn thành**, (tuỳ chọn) **Nhân công** / '
            '**Chi phí khác** → **Tính vật tư & giá thành** → **Hoàn thành phiếu SX**.\n'
            'Hệ thống **xuất kho vật tư** theo giá vốn bình quân và **nhập kho thành phẩm**. '
            '**Giá thành / ĐV** = (tiền vật tư + nhân công + chi phí khác) ÷ số lượng hoàn thành. '
            'Có **In phiếu** hoặc **Hủy phiếu** nếu sai.'
        ),
    },
    {
        'id': 'production_bom',
        'keywords': [
            'định mức bom', 'công thức sản xuất', 'định mức vật tư', 'bom thành phẩm',
            '1 đơn vị thành phẩm',
        ],
        'pages': ['production_page'],
        'title': 'Định mức BOM vật tư',
        'answer': (
            'Trên **Tính Giá Thành → Định mức BOM**: chọn **thành phẩm**, thêm từng dòng **vật tư** '
            'với số lượng dùng cho **1 đơn vị thành phẩm**. Danh sách chọn chỉ hiện vật tư mã **VT…**. '
            'Mỗi thành phẩm có **một** định mức — lưu xong mới lập **Phiếu sản xuất**. '
            'Khi tính giá thành, hệ thống nhân định mức × số lượng hoàn thành '
            '(có thể sửa **SL thực tế** trước khi **Hoàn thành phiếu SX**).'
        ),
    },
    {
        'id': 'production_codes',
        'keywords': [
            'mã thành phẩm', 'tp001', 'tp00101', 'barcode thành phẩm', 'mã vt',
            'tạo thành phẩm', 'mã vạch thành phẩm', 'thêm thành phẩm',
        ],
        'pages': ['production_page', 'products'],
        'title': 'Mã thành phẩm & mã vạch',
        'answer': (
            'Trên **Danh Mục Sản Phẩm** (tab **Thành phẩm**) hoặc nút **+** cạnh chọn thành phẩm '
            'trong **Tính Giá Thành**: hệ thống tự cấp mã **TP001**, **TP002**…; '
            'mã vạch lẻ **TP00101**, mã vạch sỉ (nếu có đơn vị 2) **TP00102**. '
            'Nhập tên thành phẩm, đơn vị, giá bán rồi lưu. '
            'Vật tư dùng cho sản xuất có mã **VT…** (tạo khi nhập kho loại **Vật Tư**).'
        ),
    },
    {
        'id': 'production_cancel',
        'keywords': [
            'hủy phiếu sản xuất', 'hủy sx', 'hủy giá thành', 'đảo kho sản xuất',
            'sai phiếu sản xuất',
        ],
        'pages': ['production_page'],
        'title': 'Hủy phiếu sản xuất',
        'answer': (
            'Trong danh sách phiếu: chọn phiếu trạng thái **Hoàn thành** → mở chi tiết → **Hủy phiếu**. '
            'Hệ thống **nhập lại vật tư** và **xuất lại thành phẩm** đã nhập trước đó; phiếu chuyển sang **Đã hủy** '
            '(không xóa lịch sử). Chỉ hủy được khi tồn thành phẩm còn đủ.'
        ),
    },
    {
        'id': 'knowledge',
        'keywords': ['pháp luật', 'thuế', 'cập nhật kiến thức', 'bản tin', 'tct', 'bộ tài chính'],
        'pages': ['cap_nhat_kien_thuc_page', 'SME_cap_nhat_kien_thuc'],
        'title': 'Cập nhật pháp luật & thuế',
        'answer': (
            'HKD: **Cập Nhật Kiến Thức** (`/cap-nhat-kien-thuc`) — tin hộ kinh doanh. '
            'SME: **Tiện ích → Cập nhật kiến thức** (`/SME_cap-nhat-kien-thuc`) — chỉ thông tư, '
            'nghị định, thuế DN. Tin từ Tổng cục Thuế và Bộ Tài Chính tự cập nhật. '
            'Nhấn tiêu đề có biểu tượng ↗ để xem văn bản gốc.'
        ),
    },
    {
        'id': 'help_full',
        'keywords': ['hướng dẫn', 'hd sd', 'cách dùng', 'tutorial', 'ultraview', 'từ xa'],
        'pages': ['huong_dan_su_dung'],
        'title': 'Hướng dẫn đầy đủ',
        'answer': (
            'Mở **Hướng Dẫn Sử Dụng** (Tiện ích trong menu SME hoặc menu Kế toán HKD). '
            'Tab **Kế Toán SME** → chọn nhánh **SME TT58 (DNSN)** hoặc **SME TT99** đúng chế độ tenant; '
            'tab **Kế Toán HKD** cho hộ kinh doanh TT88; thêm tab Bán hàng, F&B, Phòng trọ. '
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
    # —— FAQ Kế toán SME ——
    {
        'id': 'sme_overview',
        'keywords': [
            'kế toán sme', 'kế toán doanh nghiệp', 'tt99', 'tt58', 'sổ kép',
            'menu sme', 'hub sme',
        ],
        'pages': ['SME_dashboard', 'huong_dan_su_dung', 'SME_cap_nhat_kien_thuc'],
        'title': 'Kế toán doanh nghiệp (SME) là gì?',
        'answer': (
            'Menu **Kế toán doanh nghiệp** dùng sổ kép theo **Thông tư 99** hoặc **Thông tư 58**. '
            'Chọn **chi nhánh** trên thanh trên (Tất cả = hợp nhất). '
            'Hướng dẫn: tab **Kế Toán SME** → chọn **TT58** hoặc **TT99**. '
            'Cập nhật pháp luật / thuế DN: **Tiện ích → Cập nhật kiến thức** '
            '(`/SME_cap-nhat-kien-thuc`).'
        ),
    },
    {
        'id': 'sme_tt58_start',
        'keywords': [
            'tt58', 'dnsn', 'siêu nhỏ', 'trường hợp thuế', 'sổ dnsn',
            'bắt đầu tt58', 'hướng dẫn tt58', 'article5', 's1-dnsn',
        ],
        'pages': ['SME_dashboard', 'SME_dnsn_books', 'huong_dan_su_dung'],
        'title': 'Bắt đầu Kế toán SME TT58 (DNSN)',
        'answer': (
            '1) Thiết lập DN + ngân hàng + HĐĐT. 2) **Chọn Trường hợp thuế 1–4** và tỷ lệ % ngành. '
            '3) Vốn góp phiếu thu 1111/1121·4111. 4) Hóa đơn mua → Lập phiếu nhập. 5) Bán POS. '
            '6) In đủ sổ tại **Sổ & biểu mẫu DNSN** (framework bắt buộc khi CQT kiểm tra). '
            'TT58 được dùng **đầy đủ nghiệp vụ** như TT99 (L/C, ngoại tệ, vay, mẫu chứng từ…). '
            'Chi tiết: Hướng dẫn → tab Kế Toán SME → **SME TT58**.'
        ),
        'follow_ups': [
            'Bốn trường hợp thuế TT58?',
            'Thu công nợ khách hàng SME?',
            'Khác nhau TT58 và TT99?',
        ],
    },
    {
        'id': 'sme_tt99_start',
        'keywords': [
            'tt99', 'thông tư 99', 'bắt đầu tt99', 'hướng dẫn tt99',
            'l/c', 'ngoại tệ sme', 'b01', 'bctc sme',
        ],
        'pages': ['SME_dashboard', 'SME_BCTC_reports', 'huong_dan_su_dung'],
        'title': 'Bắt đầu Kế toán SME TT99',
        'answer': (
            '1) Thiết lập DN + HĐĐT. 2) Chi nhánh/kho. 3) Vốn góp phiếu thu. '
            '4) HĐ mua → PN / phân bổ. 5) Bán POS (VAT hóa đơn 33311). '
            '6) Cuối kỳ Kết chuyển khóa sổ; xem **BCTC B01–B09**. '
            'Có thêm L/C, ngoại tệ, vay, mẫu 03–09-TT / biên bản TSCĐ. '
            'Chi tiết: Hướng dẫn → tab Kế Toán SME → **SME TT99**.'
        ),
        'follow_ups': [
            'Xuất khẩu SME?',
            'Mở L/C nhập khẩu?',
            'Khác nhau TT58 và TT99?',
        ],
    },
    {
        'id': 'sme_tt58_vs_tt99',
        'keywords': [
            'khác nhau tt58 và tt99', 'tt58 hay tt99', 'phân biệt tt58',
            'dnsn hay tt99', 'chế độ nào',
        ],
        'pages': ['huong_dan_su_dung', 'SME_dashboard'],
        'title': 'Khác nhau TT58 và TT99',
        'answer': (
            '**TT58 (siêu nhỏ):** chọn 4 trường hợp thuế; **bắt buộc sổ DNSN** (framework CQT); '
            'thuế % DT có thể tự ghi 811/821; BCTC theo TH2/TH4. '
            'Được dùng **cùng đầy đủ nghiệp vụ** mua/bán/kế toán như TT99 (L/C, FX, vay, mẫu chứng từ…). '
            '**TT99:** BCTC B01–B09 luôn có; không có menu sổ DNSN.'
        ),
    },
    {
        'id': 'sme_cash_receipt',
        'keywords': [
            'lập phiếu thu', 'phiếu thu sme', 'vốn góp', '4111', '1111',
            'nộp quỹ sme', 'sổ quỹ tiền mặt sme', 'góp vốn',
        ],
        'pages': ['SME_SoQuyTienMat', 'SME_DanhSachPhieuThu', 'SME_dashboard_debt'],
        'title': 'Lập phiếu thu trên Sổ Quỹ Tiền Mặt (SME)',
        'answer': (
            '**Tiền Và Công Nợ → Sổ Quỹ Tiền Mặt → Lập phiếu thu**. '
            'Mặc định Nợ **1111**, Có **4111** Vốn góp chủ sở hữu (đổi Có khi thu nợ 131 hoặc tạm ứng 141). '
            'Danh sách đầy đủ: **Chứng từ kế toán → Phiếu thu — mẫu 01-TT**.'
        ),
    },
    {
        'id': 'sme_bank_receipt',
        'keywords': [
            'phiếu thu ngân hàng', '1121', 'sổ tiền gửi sme', 'nộp ngân hàng sme',
        ],
        'pages': ['SME_SoTienGuiNganHang'],
        'title': 'Lập phiếu thu trên Sổ Tiền Gửi Ngân Hàng (SME)',
        'answer': (
            '**Tiền Và Công Nợ → Sổ Tiền Gửi Ngân Hàng → Lập phiếu thu**. '
            'Mặc định Nợ **1121**, Có **4111**. Sau khi lưu, sổ tự tải lại số dư.'
        ),
    },
    {
        'id': 'sme_period_close',
        'keywords': [
            'khóa sổ', 'kết chuyển', 'cuối năm', 'tự động hóa kỳ', 'mở khóa sổ',
            'kết chuyển doanh thu', '4212', '911',
        ],
        'pages': ['SME_auto_posting'],
        'title': 'Kết chuyển, khóa sổ và cuối năm',
        'answer': (
            '**Sổ sách kế toán → Kết chuyển, khóa sổ và cuối năm**: chạy kỳ gồm khấu hao TSCĐ → '
            'phân bổ CCDC → kết chuyển doanh thu/chi phí → 911 → 4212 → quyết toán thuế GTGT → khóa sổ. '
            'Cuối năm kết chuyển 4212 → 4211. Kỳ đã khóa thì mở khóa tại cùng trang nếu cần sửa.'
        ),
    },
    {
        'id': 'sme_branch',
        'keywords': [
            'chi nhánh', 'tất cả chi nhánh', 'kho_001', 'đơn vị', 'trụ sở hq',
        ],
        'pages': ['SME_branches', 'SME_dashboard'],
        'title': 'Chi nhánh và kho SME',
        'answer': (
            'Chọn chi nhánh trên thanh trên hub SME. **Tất cả chi nhánh** = xem hợp nhất; '
            'một chi nhánh = ghi sổ / lọc vận hành. Trụ sở mã **HQ**. '
            'Gắn kho tại **Danh mục kho và chi nhánh** (thường KHO_001…).'
        ),
    },
    {
        'id': 'sme_fa',
        'keywords': [
            'tscđ sme', 'tài sản cố định sme', 'khấu hao sme', 'đưa tscđ vào sử dụng sme',
        ],
        'pages': ['SME_fixed_assets', 'SME_TSCD', 'SME_fa_docs'],
        'title': 'Tài sản cố định SME',
        'answer': (
            'Nhập TSCĐ qua **Lập phiếu nhập kho** (loại tài sản cố định) → **Danh mục tài sản cố định** '
            '→ thiết lập số tháng khấu hao và ngày bắt đầu. Chạy khấu hao kỳ tại '
            '**Kết chuyển, khóa sổ và cuối năm**. Biên bản mẫu 01–06 tại Chứng từ kế toán.'
        ),
    },
    {
        'id': 'sme_ccdc',
        'keywords': [
            'ccdc sme', 'công cụ dụng cụ sme', 'phân bổ ccdc',
        ],
        'pages': ['SME_tools', 'SME_CCDC'],
        'title': 'Công cụ dụng cụ SME',
        'answer': (
            'Nhập CCDC qua phiếu nhập → **Danh mục công cụ dụng cụ** → thiết lập phân bổ hoặc đưa vào sử dụng. '
            'Phân bổ theo kỳ khi chạy **Kết chuyển, khóa sổ và cuối năm**.'
        ),
    },
    {
        'id': 'sme_debt',
        'keywords': [
            'công nợ sme', 'phải thu sme', 'phải trả sme', 'thu nợ sme', 'trả nợ ncc sme',
        ],
        'pages': [
            'SME_SoCongNoPhaiThu', 'SME_SoCongNoPhaiTra',
            'SME_PhaiThuCongNhanVien', 'SME_PhaiTraCongNhanVien', 'SME_dashboard_debt',
        ],
        'title': 'Công nợ SME',
        'answer': (
            'Nhóm **Tiền Và Công Nợ**: Sổ phải thu (131), phải trả (331), phải thu/trả nhân viên (141/334). '
            'Thu nợ / trả nợ lập **phiếu thu** hoặc **phiếu chi** gắn đúng đối tượng.'
        ),
    },
    {
        'id': 'sme_journal',
        'keywords': [
            'nhật ký bút toán', 'sổ cái sme', 'cân đối phát sinh', 'đảo bút toán', 'bt000',
        ],
        'pages': ['SME_journal', 'SME_general_ledger', 'SME_chart_of_accounts'],
        'title': 'Nhật ký bút toán và sổ cái',
        'answer': (
            '**Sổ sách kế toán → Nhật ký bút toán**: lọc chi nhánh / loại chứng từ / ngày; đảo bút toán nếu kỳ chưa khóa. '
            '**Sổ cái và cân đối phát sinh** để đối chiếu số dư. Số bút toán dạng BT0000001.'
        ),
    },
    {
        'id': 'sme_bctc',
        'keywords': [
            'bctc', 'báo cáo tài chính sme', 'b01', 'b02', 'tờ khai gtgt sme', 'thuế tndn',
        ],
        'pages': ['SME_BCTC_reports', 'SME_vat_declaration', 'SME_tax_nsnn', 'SME_cit'],
        'title': 'Báo cáo tài chính và thuế SME',
        'answer': (
            '**Báo cáo tài chính → Bộ báo cáo tài chính** (B01–B09). '
            '**Tờ khai thuế giá trị gia tăng**; **Thuế và ngân sách nhà nước** (133/333); '
            'thuế TNDN tạm nộp / quyết toán; TNCN khấu trừ từ lương.'
        ),
    },
    {
        'id': 'sme_production',
        'keywords': [
            'sản xuất sme', 'giá thành sme', '621', '622', '627',
        ],
        'pages': ['SME_production', 'SME_costing'],
        'title': 'Sản xuất và giá thành SME',
        'answer': (
            '**Sản xuất và giá thành → Sản xuất**: lập lệnh (xuất NVL, CPSX vào 154) → trên mỗi lệnh bấm '
            '**Nhập kho thành phẩm** để nhập theo đợt hoặc đủ lệnh (lúc này mới Nợ 155 / Có 154). '
            '**Kế toán giá thành** tập hợp 621/622/627.'
        ),
    },
    {
        'id': 'sme_vs_hkd',
        'keywords': [
            'khác hkd', 'phân biệt hkd', 'hkd hay sme', 'doanh nghiệp hay hộ',
        ],
        'pages': [],
        'title': 'Phân biệt HKD và SME',
        'answer': (
            '**HKD** (TT88): menu kế toán hộ kinh doanh, phiếu quỹ kiểu Nộp Quỹ. '
            '**SME** (TT99/TT58): menu **Kế toán doanh nghiệp**, sổ kép, phiếu thu 01-TT / phiếu chi 02-TT, '
            'Lập phiếu thu trên sổ quỹ/ngân hàng với TK 1111/1121 và 4111. Không dùng chung thao tác hai chế độ.'
        ),
    },
]

PAGE_SUGGESTIONS: dict[str, list[str]] = {
    'sale': ['Làm sao bán hàng và xuất HĐĐT?', 'Khách trả hàng thế nào?', 'Đơn tạm (F1) dùng ra sao?'],
    'store_setup': ['Thiết lập cửa hàng ở đâu?', 'Nhập quỹ tiền mặt ban đầu?', 'Cấu hình VietQR?'],
    'thiet_lap': ['Thiết lập cửa hàng ở đâu?', 'Nhập quỹ tiền mặt ban đầu?'],
    'settings_page': ['Cấu hình hóa đơn điện tử?', 'Kết nối ngân hàng Sepay/Casso?'],
    'inward_invoice': [
        'Lập phiếu nhập từ hóa đơn mua?',
        'Lập phiếu nhập kho F&B?',
        'Hạch toán dịch vụ mua ngay?',
    ],
    'DanhSachPhieuNhapKho': [
        'In mã vạch sản phẩm?',
        'Sửa phiếu nhập kho sai?',
        'Lập phiếu nhập kho F&B?',
    ],
    'HKD_dashboard': [
        'Kế toán tính giá thành?',
        'Quản lý quán ăn, Cà Phê, Trà sửa, Nhà hàng?',
        'Quản lý khách sạn, phòng cho thuê?',
        'Tạo menu món ăn thế nào?',
    ],
    'SoCongNoPhaiThu': ['Thu nợ khách hàng?', 'Xem công nợ phải thu?'],
    'SoCongNoPhaiTra': ['Trả nợ nhà cung cấp?'],
    'rental_service': [
        'Quản lý khách sạn, phòng cho thuê?',
        'Quy trình cho thuê phòng?',
        'Thu tiền phòng trọ?',
    ],
    'LapBangLuong': ['Lập bảng lương cuối tháng?', 'Chấm công nhân viên?'],
    'production_page': [
        'Kế toán tính giá thành?',
        'Lập định mức BOM thế nào?',
        'Mã thành phẩm và mã vạch?',
        'Hủy phiếu sản xuất ra sao?',
    ],
    'production_print': ['In phiếu sản xuất?', 'Giá thành gồm những gì?'],
    'F_and_B_service': [
        'Quản lý quán ăn, Cà Phê, Trà sửa, Nhà hàng?',
        'Tạo danh sách bàn thế nào?',
        'Tạo menu món ăn thế nào?',
        'Lập phiếu nhập kho F&B?',
    ],
    'import': [
        'Lập phiếu nhập kho F&B?',
        'Nhập kho từ file XML thế nào?',
        'Hàng dùng ngay và nguyên vật liệu khác nhau?',
        'Sửa phiếu nhập kho sai?',
    ],
    'cap_nhat_kien_thuc_page': [
        'Tin pháp luật hộ kinh doanh mới?',
        'Tra cứu thông tư thuế HKD?',
        'Đồng bộ tin Tổng cục Thuế?',
    ],
    'SME_cap_nhat_kien_thuc': [
        'Tin pháp luật doanh nghiệp mới?',
        'Tra cứu thông tư thuế TT99?',
        'Đồng bộ tin Tổng cục Thuế?',
    ],
    'SME_dashboard': [
        'Kế toán doanh nghiệp SME bắt đầu thế nào?',
        'Chọn chi nhánh ra sao?',
        'Lập phiếu thu vốn góp?',
    ],
    'SME_SoQuyTienMat': [
        'Lập phiếu thu trên sổ quỹ thế nào?',
        'Tài khoản Có mặc định là gì?',
        'Vốn góp chủ sở hữu 4111?',
    ],
    'SME_SoTienGuiNganHang': [
        'Lập phiếu thu ngân hàng SME?',
        'Nợ 1121 Có 4111 nghĩa là gì?',
    ],
    'SME_SoCongNoPhaiThu': ['Thu nợ khách trên SME?', 'Xem sổ phải thu 131?'],
    'SME_SoCongNoPhaiTra': ['Trả nợ nhà cung cấp SME?'],
    'SME_dashboard_debt': [
        'Tiền Và Công Nợ gồm những sổ nào?',
        'Lập phiếu thu vốn góp?',
    ],
    'SME_auto_posting': [
        'Kết chuyển khóa sổ cuối kỳ thế nào?',
        'Mở khóa sổ khi cần sửa?',
        'Cuối năm 4212 → 4211?',
    ],
    'SME_journal': ['Xem nhật ký bút toán?', 'Đảo bút toán khi nào?'],
    'SME_general_ledger': ['Xem sổ cái và cân đối phát sinh?'],
    'SME_fixed_assets': ['Nhập TSCĐ và thiết lập khấu hao?', 'Chạy khấu hao kỳ ở đâu?'],
    'SME_tools': ['Nhập CCDC và phân bổ?', 'Danh mục CCDC trống?'],
    'SME_production': ['Sản xuất và giá thành SME?', 'Định mức và phiếu sản xuất?'],
    'SME_BCTC_reports': ['Xem bộ báo cáo tài chính B01–B09?'],
    'SME_vat_declaration': ['Lập tờ khai thuế GTGT?'],
    'huong_dan_su_dung': [
        'Tab Kế Toán SME ở đâu?',
        'Bắt đầu Kế toán SME TT58 (DNSN)?',
        'Bắt đầu Kế toán SME TT99?',
        'Khác nhau TT58 và TT99?',
        'Khác nhau HKD và SME?',
    ],
    '_default': [
        'Kế toán tính giá thành?',
        'Quản lý quán ăn, Cà Phê, Trà sửa, Nhà hàng?',
        'Quản lý khách sạn, phòng cho thuê?',
        'Tạo menu món ăn thế nào?',
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


def get_follow_ups(
    faq_id: str | None = None,
    *,
    page: str | None = None,
    exclude_question: str | None = None,
) -> list[str]:
    """Gợi ý câu hỏi tiếp theo sau khi đã trả lời một FAQ."""
    follow: list[str] = []
    if faq_id:
        for entry in STATIC_FAQ:
            if entry.get('id') == faq_id:
                follow = list(entry.get('follow_ups') or [])
                break
    if not follow:
        follow = get_suggestions(page)
    excl = (exclude_question or '').strip().lower()
    out: list[str] = []
    for q in follow:
        if excl and q.strip().lower() == excl:
            continue
        if q not in out:
            out.append(q)
        if len(out) >= 4:
            break
    return out


def should_escalate(message: str) -> bool:
    n = _normalize(message)
    return any(kw in n for kw in ESCALATION_KEYWORDS)
