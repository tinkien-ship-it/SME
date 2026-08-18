"""Chỉ tiêu BCTC doanh nghiệp siêu nhỏ — TT58/2026/TT-BTC (B01-DNSN / B02-DNSN).

Map số liệu từ `bctc_line_code` trên COA TT99 (dùng chung sổ kép) sang mã DNSN rút gọn.
"""
from __future__ import annotations

# coa_lines: danh sách mã chỉ tiêu TT99 (bctc_line_code) cộng vào chỉ tiêu DNSN
# coa_contra_lines: trừ đi (dự phòng / giảm trừ / hao mòn)

B01_DNSN_BALANCE_SHEET: list[dict] = [
    {'code': 'A', 'name': 'TÀI SẢN', 'kind': 'header', 'level': 0},
    {
        'code': '110', 'name': '1. Tiền', 'kind': 'leaf', 'level': 1,
        'sign_role': 'asset', 'coa_lines': ['111'],
    },
    {
        'code': '120', 'name': '2. Các khoản nợ phải thu', 'kind': 'leaf', 'level': 1,
        'sign_role': 'asset',
        'coa_lines': ['131', '132', '133', '136'],
        'coa_contra_lines': ['137'],
        'coa_contra_role': 'contra_asset',
    },
    {
        'code': '130', 'name': '3. Hàng tồn kho', 'kind': 'leaf', 'level': 1,
        'sign_role': 'asset', 'coa_lines': ['141'],
    },
    {
        'code': '140', 'name': '4. Tài sản cố định', 'kind': 'leaf', 'level': 1,
        'sign_role': 'asset',
        'coa_lines': ['221', '227', '230'],
        'coa_contra_lines': ['223'],
        'coa_contra_role': 'contra_asset',
    },
    {
        'code': '150', 'name': '5. Tài sản khác', 'kind': 'leaf', 'level': 1,
        'sign_role': 'asset',
        'coa_lines': ['121', '123', '155', '242', '251', '252', '253', '262'],
    },
    {
        'code': '200', 'name': 'TỔNG CỘNG TÀI SẢN (200=110+120+130+140+150)',
        'kind': 'calc', 'level': 0,
        'formula': '110 + 120 + 130 + 140 + 150', 'bold': True, 'highlight': True,
    },

    {'code': 'B', 'name': 'NGUỒN VỐN', 'kind': 'header', 'level': 0},
    {'code': 'I', 'name': 'I. Nợ phải trả', 'kind': 'header', 'level': 0},
    {
        'code': '310', 'name': '1. Các khoản nợ phải trả', 'kind': 'leaf', 'level': 1,
        'sign_role': 'liability',
        'coa_lines': [
            '311', '314', '315', '316', '318', '319', '320', '322',
            '337', '338', '341', '342',
        ],
    },
    {
        'code': '320', 'name': '2. Thuế và các khoản phải nộp Nhà nước', 'kind': 'leaf', 'level': 1,
        'sign_role': 'liability', 'coa_lines': ['313'],
    },
    {
        'code': '300', 'name': 'Tổng nợ phải trả (300=310+320)', 'kind': 'calc', 'level': 0,
        'formula': '310 + 320', 'bold': True,
    },

    {'code': 'II', 'name': 'II. Vốn chủ sở hữu', 'kind': 'header', 'level': 0},
    {
        'code': '410', 'name': '1. Vốn đầu tư của chủ sở hữu', 'kind': 'leaf', 'level': 1,
        'sign_role': 'equity', 'coa_lines': ['411'],
    },
    {
        'code': '420', 'name': '2. Lợi nhuận sau thuế chưa phân phối', 'kind': 'leaf', 'level': 1,
        'sign_role': 'equity', 'coa_lines': ['421'],
    },
    {
        'code': '430', 'name': '3. Các quỹ thuộc vốn chủ sở hữu', 'kind': 'leaf', 'level': 1,
        'sign_role': 'equity',
        'coa_lines': ['412', '413', '418', '422'],
        'coa_contra_lines': ['419'],
        'coa_contra_role': 'asset',
    },
    {
        'code': '400', 'name': 'Tổng vốn chủ sở hữu (400=410+420+430)', 'kind': 'calc', 'level': 0,
        'formula': '410 + 420 + 430', 'bold': True,
    },
    {
        'code': '500', 'name': 'TỔNG CỘNG NGUỒN VỐN (500=300+400)', 'kind': 'calc', 'level': 0,
        'formula': '300 + 400', 'bold': True, 'highlight': True,
    },
]

B02_DNSN_INCOME_STATEMENT: list[dict] = [
    {
        'code': '01', 'name': '1. Doanh thu và thu nhập thuần', 'kind': 'leaf', 'level': 1,
        'sign_role': 'revenue',
        'coa_lines': ['01', '21', '31'],
        'coa_contra_lines': ['02'],
        'coa_contra_role': 'expense',
    },
    {
        'code': '02', 'name': '2. Các khoản chi phí', 'kind': 'leaf', 'level': 1,
        'sign_role': 'expense',
        'coa_lines': ['11', '22', '25', '26', '32'],
    },
    {
        'code': '03', 'name': '3. Lợi nhuận kế toán trước thuế TNDN {(03)=(01)-(02)}',
        'kind': 'calc', 'level': 0, 'formula': '01 - 02', 'bold': True, 'highlight': True,
    },
    {
        'code': '10', 'name': '4. Chi phí thuế TNDN', 'kind': 'leaf', 'level': 1,
        'sign_role': 'expense', 'coa_lines': ['51'],
    },
    {
        'code': '20', 'name': '5. Lợi nhuận sau thuế TNDN {(20)=(03)-(10)}',
        'kind': 'calc', 'level': 0, 'formula': '03 - 10', 'bold': True, 'highlight': True,
    },
]

# Catalog sổ / chứng từ DNSN (TT58) — dùng cho menu & trang danh mục
DNSN_BOOK_CATALOG: tuple[dict, ...] = (
    {
        'code': 'S1-DNSN', 'name': 'Sổ doanh thu bán hàng hóa, dịch vụ',
        'tax_method': 'khoan', 'group': 'revenue',
    },
    {
        'code': 'S2a-DNSN', 'name': 'Sổ doanh thu bán hàng hóa, dịch vụ',
        'tax_method': 'gtgt_kk', 'group': 'revenue',
    },
    {
        'code': 'S2b-DNSN', 'name': 'Sổ chi tiết doanh thu, chi phí',
        'tax_method': 'gtgt_kk', 'group': 'pnl',
    },
    {
        'code': 'S2c-DNSN', 'name': 'Sổ chi tiết vật liệu, dụng cụ, sản phẩm, hàng hóa',
        'tax_method': 'common', 'group': 'inventory',
    },
    {
        'code': 'S2d-DNSN', 'name': 'Sổ chi tiết tiền',
        'tax_method': 'common', 'group': 'cash',
    },
    {
        'code': 'S3a-DNSN', 'name': 'Sổ doanh thu bán hàng hóa, dịch vụ',
        'tax_method': 'gtgt_tt', 'group': 'revenue',
    },
    {
        'code': 'S3b-DNSN', 'name': 'Sổ theo dõi nghĩa vụ thuế GTGT',
        'tax_method': 'gtgt_tt', 'group': 'tax',
    },
    {
        'code': 'S4a-DNSN', 'name': 'Sổ chi tiết thanh toán công nợ',
        'tax_method': 'common', 'group': 'ar_ap',
    },
    {
        'code': 'S4b-DNSN', 'name': 'Sổ tài sản cố định',
        'tax_method': 'common', 'group': 'fixed_asset',
    },
    {
        'code': 'S4c-DNSN', 'name': 'Sổ theo dõi nghĩa vụ thuế khác',
        'tax_method': 'common', 'group': 'tax',
    },
    {
        'code': 'S4d-DNSN', 'name': 'Sổ theo dõi vốn chủ sở hữu',
        'tax_method': 'common', 'group': 'equity',
    },
)

DNSN_VOUCHER_FORMS: tuple[dict, ...] = (
    # Điều 9 TT58 — danh mục chứng từ doanh nghiệp siêu nhỏ có thể lựa chọn
    {'code': '01-TT', 'name': 'Phiếu thu', 'endpoint': 'SME_DanhSachPhieuThu'},
    {'code': '02-TT', 'name': 'Phiếu chi', 'endpoint': 'SME_DanhSachPhieuChi'},
    {'code': '01-VT', 'name': 'Phiếu nhập kho', 'endpoint': 'SME_DanhSachPhieuNhapKho'},
    {'code': '02-VT', 'name': 'Phiếu xuất kho', 'endpoint': 'SME_DanhSachPhieuXuatKho_VT'},
)

# Nhóm sổ theo phương pháp thuế (UI danh mục)
DNSN_BOOK_GROUPS: tuple[dict, ...] = (
    {
        'key': 'khoan',
        'title': 'Trường hợp 1 — GTGT % DT + TNDN % DT (S1-DNSN · không bắt buộc BCTC)',
        'codes': ('S1-DNSN',),
    },
    {
        'key': 'gtgt_kk',
        'title': 'Trường hợp 2 — GTGT % DT + TNDN trên thu nhập (S2a–S2d · bắt buộc BCTC 90 ngày)',
        'codes': ('S2a-DNSN', 'S2b-DNSN', 'S2c-DNSN', 'S2d-DNSN'),
    },
    {
        'key': 'gtgt_tt',
        'title': 'Trường hợp 3 — GTGT khấu trừ + TNDN % DT (S3a–S3b · không bắt buộc BCTC)',
        'codes': ('S3a-DNSN', 'S3b-DNSN'),
    },
    {
        'key': 'kk_tt_shared',
        'title': 'Trường hợp 4 — GTGT khấu trừ + TNDN trên thu nhập (S2b/S2c/S2d + S3b · bắt buộc BCTC 90 ngày)',
        'codes': ('S2b-DNSN', 'S2c-DNSN', 'S2d-DNSN', 'S3b-DNSN'),
    },
    {
        'key': 'common',
        'title': 'Sổ chi tiết tùy chọn (Điều 9)',
        'codes': ('S4a-DNSN', 'S4b-DNSN', 'S4c-DNSN', 'S4d-DNSN'),
    },
    {
        'key': 'bctc',
        'title': 'Báo cáo tài chính B01-DNSN / B02-DNSN (bắt buộc TH2 & TH4 — nộp trong 90 ngày)',
        'codes': (),
        'bctc': True,
    },
)
