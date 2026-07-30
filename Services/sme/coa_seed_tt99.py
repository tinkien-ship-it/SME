"""
Seed hệ thống tài khoản SME theo TT99/2025 + tiểu khoản khuyến nghị thực tế.

legal_source:
  - TT99: tài khoản/phân cấp được hướng dẫn hoặc nêu rõ trong Phụ lục II
  - recommended: tiểu khoản thực tế phần mềm đề xuất (DN được mở theo Điều 11 TT99)
  - custom: do người dùng tạo sau này (không nằm trong seed)
"""
from __future__ import annotations

SEED_VERSION = 'tt99_v1_2026-07e'

# account_class: asset | liability | equity | revenue | expense | off_balance | result
# normal_balance: debit | credit
# track_* flags: yêu cầu chọn đối tượng khi hạch toán trên TK postable


def _a(
    code: str,
    name: str,
    *,
    parent: str | None = None,
    cls: str = 'asset',
    bal: str = 'debit',
    source: str = 'TT99',
    recommended: int = 0,
    postable: int | None = None,
    bctc: str | None = None,
    tracks: tuple[str, ...] = (),
    desc: str = '',
    sort: int | None = None,
) -> dict:
    level = len(code) - 2 if len(code) >= 3 else 1  # 111->1, 1111->2, 11111->3...
    # Better: 3 digits = L1, 4 = L2, 5 = L3, 6+ = L4
    if len(code) <= 3:
        level = 1
    elif len(code) == 4:
        level = 2
    elif len(code) == 5:
        level = 3
    else:
        level = 4
    if parent is None and level > 1:
        parent = code[:-1] if len(code) > 3 else None
        # for 1281 parent 128; for 13311 parent 1331
        if len(code) == 4:
            parent = code[:3]
        elif len(code) == 5:
            parent = code[:4]
        elif len(code) >= 6:
            parent = code[:5]
    if postable is None:
        postable = 1  # will be recalculated after seed: parents with children -> 0
    flags = {
        'track_customer': 0,
        'track_supplier': 0,
        'track_employee': 0,
        'track_bank': 0,
        'track_currency': 0,
        'track_warehouse': 0,
        'track_product': 0,
        'track_project': 0,
        'track_department': 0,
    }
    for t in tracks:
        key = f'track_{t}'
        if key in flags:
            flags[key] = 1
    return {
        'code': code,
        'name': name,
        'parent_code': parent,
        'level': level,
        'account_class': cls,
        'normal_balance': bal,
        'is_postable': postable,
        'is_system': 1,
        'is_recommended': recommended,
        'is_custom': 0,
        'is_active': 1,
        'legal_source': source,
        'bctc_line_code': bctc,
        'sort_order': sort if sort is not None else int(code.ljust(8, '0')[:8]),
        'description': desc,
        **flags,
    }


# ---------------------------------------------------------------------------
# Cấp 1 + cấp pháp định / khuyến nghị
# ---------------------------------------------------------------------------
SEED_ACCOUNTS: list[dict] = [
    # --- Tiền ---
    _a('111', 'Tiền mặt', cls='asset', bal='debit', bctc='111', desc='Quỹ tiền mặt'),
    _a('1111', 'Tiền Việt Nam', parent='111', source='recommended', recommended=1, tracks=('currency',)),
    _a('1112', 'Ngoại tệ', parent='111', source='recommended', recommended=1, tracks=('currency',)),
    _a('1113', 'Vàng tiền tệ', parent='111', source='recommended', recommended=1, tracks=('currency',)),

    _a('112', 'Tiền gửi không kỳ hạn', cls='asset', bal='debit', bctc='111',
       desc='Tiền gửi không kỳ hạn tại ngân hàng/tổ chức được phép'),
    _a('1121', 'Tiền Việt Nam', parent='112', source='recommended', recommended=1, tracks=('bank', 'currency')),
    _a('1122', 'Ngoại tệ', parent='112', source='recommended', recommended=1, tracks=('bank', 'currency')),
    _a('1123', 'Vàng tiền tệ', parent='112', source='recommended', recommended=1, tracks=('bank', 'currency')),

    _a('113', 'Tiền đang chuyển', cls='asset', bal='debit', bctc='111'),
    _a('1131', 'Tiền Việt Nam', parent='113', source='recommended', recommended=1, tracks=('currency',)),
    _a('1132', 'Ngoại tệ', parent='113', source='recommended', recommended=1, tracks=('currency',)),

    # --- Đầu tư ngắn hạn ---
    _a('121', 'Chứng khoán kinh doanh', cls='asset', bal='debit', bctc='120'),
    _a('1211', 'Cổ phiếu', parent='121', source='recommended', recommended=1),
    _a('1212', 'Trái phiếu', parent='121', source='recommended', recommended=1),
    _a('1218', 'Chứng khoán khác', parent='121', source='recommended', recommended=1),

    _a('128', 'Đầu tư nắm giữ đến ngày đáo hạn', cls='asset', bal='debit', bctc='123'),
    _a('1281', 'Tiền gửi có kỳ hạn', parent='128'),
    _a('1282', 'Trái phiếu', parent='128'),
    _a('1283', 'Cho vay', parent='128', tracks=('customer',)),
    _a('1288', 'Đầu tư khác nắm giữ đến ngày đáo hạn', parent='128'),

    # --- Phải thu ---
    _a('131', 'Phải thu của khách hàng', cls='asset', bal='debit', bctc='131', tracks=('customer',)),
    _a('133', 'Thuế GTGT được khấu trừ', cls='asset', bal='debit', bctc='133'),
    _a('1331', 'Thuế GTGT được khấu trừ của hàng hóa, dịch vụ', parent='133', cls='asset', bal='debit'),
    _a('1332', 'Thuế GTGT được khấu trừ của TSCĐ', parent='133', cls='asset', bal='debit'),
    _a('13311', 'Thuế GTGT hàng hóa, dịch vụ trong nước', parent='1331', cls='asset', bal='debit', source='recommended', recommended=1),
    _a('13312', 'Thuế GTGT hàng nhập khẩu', parent='1331', cls='asset', bal='debit', source='recommended', recommended=1),

    _a('136', 'Phải thu nội bộ', cls='asset', bal='debit', bctc='132'),
    _a('1361', 'Vốn kinh doanh ở đơn vị trực thuộc', parent='136'),
    _a('1362', 'Phải thu nội bộ về chênh lệch tỷ giá', parent='136'),
    _a('1363', 'Phải thu nội bộ về chi phí đi vay đủ điều kiện được vốn hóa', parent='136'),
    _a('1368', 'Phải thu nội bộ khác', parent='136', source='recommended', recommended=1),

    _a('138', 'Phải thu khác', cls='asset', bal='debit', bctc='136'),
    _a('1381', 'Tài sản thiếu chờ xử lý', parent='138'),
    _a('1383', 'Thuế TTĐB của hàng nhập khẩu', parent='138'),
    _a('1388', 'Phải thu khác', parent='138', source='recommended', recommended=1, tracks=('customer',)),

    _a('141', 'Tạm ứng', cls='asset', bal='debit', bctc='136', tracks=('employee',)),

    # --- Hàng tồn kho ---
    _a('151', 'Hàng mua đang đi đường', cls='asset', bal='debit', bctc='141', tracks=('product', 'warehouse')),
    _a('152', 'Nguyên liệu, vật liệu', cls='asset', bal='debit', bctc='141', tracks=('product', 'warehouse')),
    _a('153', 'Công cụ, dụng cụ', cls='asset', bal='debit', bctc='141', tracks=('product', 'warehouse')),
    _a('154', 'Chi phí sản xuất, kinh doanh dở dang', cls='asset', bal='debit', bctc='141',
       tracks=('product', 'project', 'department')),
    _a('155', 'Sản phẩm', cls='asset', bal='debit', bctc='141', tracks=('product', 'warehouse')),
    _a('156', 'Hàng hóa', cls='asset', bal='debit', bctc='141', tracks=('product', 'warehouse')),
    _a('157', 'Hàng gửi đi bán', cls='asset', bal='debit', bctc='141', tracks=('product', 'customer')),
    _a('158', 'Hàng hóa kho bảo thuế', cls='asset', bal='debit', bctc='141', tracks=('product', 'warehouse')),

    # --- TSCĐ & đầu tư dài hạn ---
    _a('211', 'TSCĐ hữu hình', cls='asset', bal='debit', bctc='221'),
    _a('2111', 'Nhà cửa, vật kiến trúc', parent='211', source='recommended', recommended=1),
    _a('2112', 'Máy móc, thiết bị', parent='211', source='recommended', recommended=1),
    _a('2113', 'Phương tiện vận tải, truyền dẫn', parent='211', source='recommended', recommended=1),
    _a('2114', 'Thiết bị, dụng cụ quản lý', parent='211', source='recommended', recommended=1),
    _a('2115', 'Cây lâu năm, súc vật làm việc và cho sản phẩm', parent='211', source='recommended', recommended=1),
    _a('2118', 'TSCĐ hữu hình khác', parent='211', source='recommended', recommended=1),

    _a('212', 'TSCĐ thuê tài chính', cls='asset', bal='debit', bctc='221'),
    _a('213', 'TSCĐ vô hình', cls='asset', bal='debit', bctc='227'),
    _a('2131', 'Quyền sử dụng đất', parent='213', source='recommended', recommended=1),
    _a('2132', 'Quyền phát hành', parent='213', source='recommended', recommended=1),
    _a('2133', 'Bản quyền, bằng sáng chế', parent='213', source='recommended', recommended=1),
    _a('2134', 'Nhãn hiệu, tên thương mại', parent='213', source='recommended', recommended=1),
    _a('2135', 'Chương trình phần mềm', parent='213', source='recommended', recommended=1),
    _a('2136', 'Giấy phép và giấy phép nhượng quyền', parent='213', source='recommended', recommended=1),
    _a('2138', 'TSCĐ vô hình khác', parent='213', source='recommended', recommended=1),

    _a('214', 'Hao mòn TSCĐ', cls='asset', bal='credit', bctc='223'),
    _a('2141', 'Hao mòn TSCĐ hữu hình', parent='214', cls='asset', bal='credit'),
    _a('2142', 'Hao mòn TSCĐ thuê tài chính', parent='214', cls='asset', bal='credit'),
    _a('2143', 'Hao mòn TSCĐ vô hình', parent='214', cls='asset', bal='credit'),
    _a('2147', 'Hao mòn bất động sản đầu tư', parent='214', cls='asset', bal='credit', source='recommended', recommended=1),

    _a('217', 'Bất động sản đầu tư', cls='asset', bal='debit', bctc='230'),
    _a('221', 'Đầu tư vào công ty con', cls='asset', bal='debit', bctc='251'),
    _a('222', 'Đầu tư vào công ty liên doanh, liên kết', cls='asset', bal='debit', bctc='252'),
    _a('228', 'Đầu tư khác', cls='asset', bal='debit', bctc='253'),
    _a('2281', 'Đầu tư góp vốn vào đơn vị khác', parent='228'),
    _a('2288', 'Đầu tư khác', parent='228'),

    _a('229', 'Dự phòng tổn thất tài sản', cls='asset', bal='credit', bctc='137'),
    _a('2291', 'Dự phòng giảm giá chứng khoán kinh doanh', parent='229'),
    _a('2292', 'Dự phòng tổn thất đầu tư vào đơn vị khác', parent='229'),
    _a('2293', 'Dự phòng phải thu khó đòi', parent='229'),
    _a('2294', 'Dự phòng giảm giá hàng tồn kho', parent='229'),
    _a('2295', 'Dự phòng tổn thất tài sản khác', parent='229', source='recommended', recommended=1),

    _a('241', 'Xây dựng cơ bản dở dang', cls='asset', bal='debit', bctc='242', tracks=('project',)),
    _a('242', 'Chi phí trả trước', cls='asset', bal='debit', bctc='242'),
    _a('243', 'Tài sản thuế thu nhập hoãn lại', cls='asset', bal='debit', bctc='262'),
    _a('244', 'Cầm cố, thế chấp, ký quỹ, ký cược', cls='asset', bal='debit', bctc='155'),

    # --- Nợ phải trả ---
    _a('331', 'Phải trả cho người bán', cls='liability', bal='credit', bctc='311', tracks=('supplier',)),
    _a('333', 'Thuế và các khoản phải nộp Nhà nước', cls='liability', bal='credit', bctc='313'),
    _a('3331', 'Thuế GTGT phải nộp', parent='333', cls='liability', bal='credit'),
    _a('33311', 'Thuế GTGT đầu ra', parent='3331', cls='liability', bal='credit', source='recommended', recommended=1),
    _a('33312', 'Thuế GTGT hàng nhập khẩu', parent='3331', cls='liability', bal='credit', source='recommended', recommended=1),
    _a('3332', 'Thuế tiêu thụ đặc biệt', parent='333', cls='liability', bal='credit'),
    _a('3333', 'Thuế xuất, nhập khẩu', parent='333', cls='liability', bal='credit'),
    _a('3334', 'Thuế thu nhập doanh nghiệp', parent='333', cls='liability', bal='credit'),
    _a('3335', 'Thuế thu nhập cá nhân', parent='333', cls='liability', bal='credit'),
    _a('3336', 'Thuế tài nguyên', parent='333', cls='liability', bal='credit', source='recommended', recommended=1),
    _a('3337', 'Thuế nhà đất, tiền thuê đất', parent='333', cls='liability', bal='credit', source='recommended', recommended=1),
    _a('3338', 'Thuế bảo vệ môi trường và các loại thuế khác', parent='333', cls='liability', bal='credit', source='recommended', recommended=1),
    _a('3339', 'Phí, lệ phí và các khoản phải nộp khác', parent='333', cls='liability', bal='credit'),

    _a('334', 'Phải trả người lao động', cls='liability', bal='credit', bctc='314', tracks=('employee',)),
    _a('3341', 'Phải trả công nhân viên', parent='334', source='recommended', recommended=1, tracks=('employee',)),
    _a('3348', 'Phải trả người lao động khác', parent='334', source='recommended', recommended=1, tracks=('employee',)),

    _a('335', 'Chi phí phải trả', cls='liability', bal='credit', bctc='315'),
    _a('336', 'Phải trả nội bộ', cls='liability', bal='credit', bctc='316'),
    _a('337', 'Thanh toán theo tiến độ kế hoạch hợp đồng xây dựng', cls='liability', bal='credit', bctc='318'),
    _a('338', 'Phải trả, phải nộp khác', cls='liability', bal='credit', bctc='319'),
    _a('3381', 'Tài sản thừa chờ giải quyết', parent='338', source='recommended', recommended=1),
    _a('3382', 'Kinh phí công đoàn', parent='338', source='recommended', recommended=1),
    _a('3383', 'Bảo hiểm xã hội', parent='338', source='recommended', recommended=1),
    _a('3384', 'Bảo hiểm y tế', parent='338', source='recommended', recommended=1),
    _a('3385', 'Bảo hiểm thất nghiệp', parent='338', source='recommended', recommended=1),
    _a('3386', 'Nhận ký quỹ, ký cược ngắn hạn', parent='338', source='recommended', recommended=1),
    _a('3387', 'Doanh thu chưa thực hiện', parent='338', source='recommended', recommended=1),
    _a('3388', 'Phải trả, phải nộp khác', parent='338', source='recommended', recommended=1),

    _a('341', 'Vay và nợ thuê tài chính', cls='liability', bal='credit', bctc='320'),
    _a('3411', 'Các khoản đi vay', parent='341', source='recommended', recommended=1, tracks=('bank',)),
    _a('3412', 'Nợ thuê tài chính', parent='341', source='recommended', recommended=1),

    _a('343', 'Trái phiếu phát hành', cls='liability', bal='credit', bctc='338'),
    _a('344', 'Nhận ký quỹ, ký cược dài hạn', cls='liability', bal='credit', bctc='337'),
    _a('347', 'Thuế thu nhập hoãn lại phải trả', cls='liability', bal='credit', bctc='341'),
    _a('352', 'Dự phòng phải trả', cls='liability', bal='credit', bctc='342'),
    _a('353', 'Quỹ khen thưởng, phúc lợi', cls='liability', bal='credit', bctc='322'),

    # --- Vốn ---
    _a('411', 'Vốn đầu tư của chủ sở hữu', cls='equity', bal='credit', bctc='411'),
    _a('4111', 'Vốn góp của chủ sở hữu', parent='411'),
    _a('4112', 'Thặng dư vốn cổ phần', parent='411'),
    _a('4113', 'Quyền chọn chuyển đổi trái phiếu', parent='411', source='recommended', recommended=1),
    _a('4118', 'Vốn khác của chủ sở hữu', parent='411', source='recommended', recommended=1),

    _a('412', 'Chênh lệch đánh giá lại tài sản', cls='equity', bal='credit', bctc='412'),
    _a('413', 'Chênh lệch tỷ giá hối đoái', cls='equity', bal='credit', bctc='413'),
    _a('414', 'Quỹ đầu tư phát triển', cls='equity', bal='credit', bctc='418'),
    _a('417', 'Quỹ hỗ trợ sắp xếp doanh nghiệp', cls='equity', bal='credit', bctc='418'),
    _a('418', 'Các quỹ khác thuộc vốn chủ sở hữu', cls='equity', bal='credit', bctc='418'),
    _a('419', 'Cổ phiếu quỹ', cls='equity', bal='debit', bctc='419'),
    _a('421', 'Lợi nhuận sau thuế chưa phân phối', cls='equity', bal='credit', bctc='421'),
    _a('4211', 'LNST chưa phân phối năm trước', parent='421', cls='equity', bal='credit', source='recommended', recommended=1),
    _a('4212', 'LNST chưa phân phối năm nay', parent='421', cls='equity', bal='credit', source='recommended', recommended=1),
    _a('441', 'Nguồn vốn đầu tư xây dựng cơ bản', cls='equity', bal='credit', bctc='422'),

    # --- Doanh thu / giảm trừ ---
    _a('511', 'Doanh thu bán hàng và cung cấp dịch vụ', cls='revenue', bal='credit', bctc='01'),
    _a('5111', 'Doanh thu bán hàng hóa', parent='511', cls='revenue', bal='credit', source='recommended', recommended=1, tracks=('department',)),
    _a('5112', 'Doanh thu bán các thành phẩm', parent='511', cls='revenue', bal='credit', source='recommended', recommended=1, tracks=('department',)),
    _a('5113', 'Doanh thu cung cấp dịch vụ', parent='511', cls='revenue', bal='credit', source='recommended', recommended=1, tracks=('department',)),
    _a('5114', 'Doanh thu trợ cấp, trợ giá', parent='511', cls='revenue', bal='credit', source='recommended', recommended=1),
    _a('5117', 'Doanh thu kinh doanh bất động sản đầu tư', parent='511', cls='revenue', bal='credit', source='recommended', recommended=1),
    _a('5118', 'Doanh thu khác', parent='511', cls='revenue', bal='credit', source='recommended', recommended=1),

    _a('515', 'Doanh thu hoạt động tài chính', cls='revenue', bal='credit', bctc='21'),
    _a('521', 'Các khoản giảm trừ doanh thu', cls='revenue', bal='debit', bctc='02'),
    _a('5211', 'Chiết khấu thương mại', parent='521', cls='revenue', bal='debit', source='recommended', recommended=1),
    _a('5212', 'Hàng bán bị trả lại', parent='521', cls='revenue', bal='debit', source='recommended', recommended=1),
    _a('5213', 'Giảm giá hàng bán', parent='521', cls='revenue', bal='debit', source='recommended', recommended=1),

    # --- Chi phí ---
    _a('611', 'Mua hàng', cls='expense', bal='debit', desc='Dùng khi áp dụng PP KKTX hoặc theo dõi mua hàng'),
    _a('621', 'Chi phí nguyên liệu, vật liệu trực tiếp', cls='expense', bal='debit', tracks=('product', 'department')),
    _a('622', 'Chi phí nhân công trực tiếp', cls='expense', bal='debit', tracks=('department',)),
    _a('623', 'Chi phí sử dụng máy thi công', cls='expense', bal='debit', tracks=('project',)),
    _a('627', 'Chi phí sản xuất chung', cls='expense', bal='debit', tracks=('department',)),
    _a('631', 'Giá thành sản xuất', cls='expense', bal='debit', tracks=('product',)),
    _a('632', 'Giá vốn hàng bán', cls='expense', bal='debit', bctc='11'),
    _a('6321', 'Giá vốn hàng hóa', parent='632', cls='expense', bal='debit', source='recommended', recommended=1),
    _a('6322', 'Giá vốn thành phẩm', parent='632', cls='expense', bal='debit', source='recommended', recommended=1),
    _a('6323', 'Giá vốn dịch vụ', parent='632', cls='expense', bal='debit', source='recommended', recommended=1),
    _a('6327', 'Giá vốn kinh doanh BĐSĐT', parent='632', cls='expense', bal='debit', source='recommended', recommended=1),

    _a('635', 'Chi phí tài chính', cls='expense', bal='debit', bctc='22'),
    _a('641', 'Chi phí bán hàng', cls='expense', bal='debit', bctc='25', tracks=('department',)),
    _a('642', 'Chi phí quản lý doanh nghiệp', cls='expense', bal='debit', bctc='26', tracks=('department',)),
    _a('711', 'Thu nhập khác', cls='revenue', bal='credit', bctc='31'),
    _a('811', 'Chi phí khác', cls='expense', bal='debit', bctc='32'),
    _a('821', 'Chi phí thuế thu nhập doanh nghiệp', cls='expense', bal='debit', bctc='51'),
    _a('8211', 'Chi phí thuế TNDN hiện hành', parent='821', source='recommended', recommended=1),
    _a('8212', 'Chi phí thuế TNDN hoãn lại', parent='821', source='recommended', recommended=1),

    _a('911', 'Xác định kết quả kinh doanh', cls='result', bal='debit'),
]


def iter_seed_accounts():
    """Yield seed rows; recalculate is_postable; kế thừa bctc_line_code từ cha nếu thiếu."""
    by_code = {a['code']: dict(a) for a in SEED_ACCOUNTS}
    parents_with_kids = {a['parent_code'] for a in SEED_ACCOUNTS if a.get('parent_code')}

    def _resolve_bctc(code: str) -> str | None:
        seen = set()
        cur = code
        while cur and cur not in seen:
            seen.add(cur)
            row = by_code.get(cur)
            if not row:
                return None
            if row.get('bctc_line_code'):
                return row['bctc_line_code']
            cur = row.get('parent_code')
        return None

    for code, row in by_code.items():
        if code in parents_with_kids:
            row['is_postable'] = 0
        if not row.get('bctc_line_code'):
            inherited = _resolve_bctc(code)
            if inherited:
                row['bctc_line_code'] = inherited
        yield row
