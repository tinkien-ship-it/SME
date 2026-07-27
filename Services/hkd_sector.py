"""Phân nhóm ngành nghề HKD NN1–NN4 (alias legacy G1–G4) theo product_type."""

# Mã hiển thị cho người dùng VN
NN_SECTORS = ('NN1', 'NN2', 'NN3', 'NN4')
HKD_SECTORS = NN_SECTORS  # alias tương thích import cũ

LEGACY_G_TO_NN = {'G1': 'NN1', 'G2': 'NN2', 'G3': 'NN3', 'G4': 'NN4'}
NN_TO_LEGACY_G = {v: k for k, v in LEGACY_G_TO_NN.items()}

NN_SECTOR_OPTIONS = [
    {
        'code': 'NN1',
        'title': 'Phân phối, cung cấp hàng hóa',
        'vat_rate': '1%',
        'pit_rate': '0,5%',
        'examples': 'Bán lẻ, buôn bán hàng hóa, phân phối sản phẩm.',
    },
    {
        'code': 'NN2',
        'title': 'Dịch vụ, xây dựng không bao thầu nguyên vật liệu',
        'vat_rate': '5%',
        'pit_rate': '2%',
        'examples': 'Ăn uống, lưu trú, sửa chữa, tư vấn, xây dựng thuần nhân công.',
    },
    {
        'code': 'NN3',
        'title': 'Sản xuất, vận tải, thầu xây dựng có cung cấp vật tư',
        'vat_rate': '3%',
        'pit_rate': '1,5%',
        'examples': 'Gia công sản xuất, vận tải hàng hóa, thi công có vật tư.',
    },
    {
        'code': 'NN4',
        'title': 'Hoạt động kinh doanh khác',
        'vat_rate': '2%',
        'pit_rate': '1%',
        'examples': 'Các hoạt động không thuộc NN1, NN2, NN3.',
    },
]
HKD_SECTOR_OPTIONS = NN_SECTOR_OPTIONS

NN_SECTOR_LEGAL_INTRO = (
    'Phân nhóm ngành nghề NN1–NN4 theo tờ khai thuế HKD mẫu 01/CNKD '
    '(Thông tư 50/2026; hướng dẫn tại Thông tư 152/2025). '
    'Hộ kinh doanh đa ngành có thể chọn nhiều NN. '
    'Tỷ lệ GTGT/TNCN theo phương pháp trực tiếp áp dụng cho nhóm doanh thu DT2.'
)
HKD_SECTOR_LEGAL_INTRO = NN_SECTOR_LEGAL_INTRO


def normalize_nn_code(code, default='NN1'):
    """Chuẩn hóa NNx hoặc legacy Gx → NNx."""
    raw = (code or '').strip().upper()
    if raw in NN_SECTORS:
        return raw
    if raw in LEGACY_G_TO_NN:
        return LEGACY_G_TO_NN[raw]
    if raw.startswith('G') and raw[1:] in ('1', '2', '3', '4'):
        return LEGACY_G_TO_NN.get(raw, default)
    return default


def nn_to_storage_code(nn_code):
    """Mã lưu DB cột hkd_sector_code — giữ Gx để tương thích dữ liệu cũ."""
    return NN_TO_LEGACY_G.get(normalize_nn_code(nn_code), 'G1')


def storage_code_to_nn(stored):
    """Đọc Gx/NNx từ DB → NNx."""
    return normalize_nn_code(stored)


def nn_to_totals_key(nn_code):
    """NN1 → g1 (khóa nội bộ calc_sector_taxes / HTKK)."""
    nn = normalize_nn_code(nn_code)
    return f'g{nn[2:]}'


def normalize_enabled_nn_sectors(raw, default=None):
    """Parse list / CSV → sorted unique NN codes."""
    items = []
    if raw is None:
        items = list(default or ['NN1'])
    elif isinstance(raw, str):
        items = [p.strip() for p in raw.replace(';', ',').split(',') if p.strip()]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = list(default or ['NN1'])
    normalized = sorted({normalize_nn_code(x) for x in items if x})
    return normalized or list(default or ['NN1'])


def sector_option_label(code):
    nn = normalize_nn_code(code)
    for opt in NN_SECTOR_OPTIONS:
        if opt['code'] == nn:
            return (
                f"{opt['code']} — {opt['title']} "
                f"(GTGT {opt['vat_rate']}, TNCN {opt['pit_rate']})"
            )
    return nn


def get_sector_ui_options():
    """Metadata NN1–NN4 cho form (tooltip + ghi chú pháp lý)."""
    row_map = {r[0]: r for r in HKD_XML_SECTOR_ROWS}
    options = []
    for opt in NN_SECTOR_OPTIONS:
        legacy_key = f'g{opt["code"][2:]}'
        xml = row_map.get(legacy_key)
        htkk_code = xml[1] if xml else ''
        htkk_letter = xml[3] if xml else ''
        options.append({
            'code': opt['code'],
            'legacy_code': NN_TO_LEGACY_G[opt['code']],
            'title': opt['title'],
            'label': sector_option_label(opt['code']),
            'vat_rate': opt['vat_rate'],
            'pit_rate': opt['pit_rate'],
            'examples': opt.get('examples', ''),
            'htkk_code': htkk_code,
            'htkk_letter': htkk_letter,
            'help_text': (
                f"{opt['code']} — {opt['title']}. "
                f"Tỷ lệ trên doanh thu: GTGT {opt['vat_rate']}, TNCN {opt['pit_rate']}. "
                f"{opt.get('examples', '')}"
            ),
            'tooltip': (
                f"{opt['code']}: {opt['title']} | GTGT {opt['vat_rate']}, TNCN {opt['pit_rate']} "
                f"| Mẫu 01/CNKD {htkk_letter or ''}"
            ).strip(),
        })
    return options


HKD_TAX_RATES = {
    'NN1': {'gtgt': 0.01, 'tncn': 0.005},
    'NN2': {'gtgt': 0.05, 'tncn': 0.02},
    'NN3': {'gtgt': 0.03, 'tncn': 0.015},
    'NN4': {'gtgt': 0.02, 'tncn': 0.01},
}
for _g, _nn in LEGACY_G_TO_NN.items():
    HKD_TAX_RATES[_g] = HKD_TAX_RATES[_nn]

STOCK_TRACKED_TYPES = frozenset({'goods', 'materials', 'finished_goods'})


def requires_stock_check(product_type):
    pt = (product_type or 'goods').strip().lower()
    if pt == 'service':
        return False
    if pt in STOCK_TRACKED_TYPES:
        return True
    return pt not in ('service',)


def resolve_item_hkd_sector(
    item_sector=None,
    product_sector=None,
    product_type=None,
    menu_product_type=None,
    business_line=None,
):
    """Xác định NN cho một dòng bán — trả về mã lưu DB (Gx)."""
    for code in (item_sector, product_sector):
        if code:
            return nn_to_storage_code(code)
    bl = (business_line or '').strip().lower()
    if bl in ('fb_service', 'rental_service') or 'service' in bl:
        return nn_to_storage_code('NN2')
    pt = (product_type or menu_product_type or 'goods').strip().lower()
    resolved = resolve_hkd_sector(pt)
    return nn_to_storage_code(resolved) if resolved else nn_to_storage_code('NN4')


HKD_TNCN_YTD_THRESHOLD = 1_000_000_000

HKD_XML_SECTOR_ROWS = (
    ('g1', '01', 'Phân phối, cung cấp hàng hóa', '(a)'),
    ('g2', '02', 'Dịch vụ, xây dựng không bao thầu nguyên vật liệu', '(b)'),
    ('g3', '03', 'Sản xuất, vận tải, thầu xây dựng có cung cấp vật tư', '(c)'),
    ('g4', '04', 'Hoạt động kinh doanh khác', '(d)'),
)

HKD_XML_GTGT_TIEU_MUC = (
    '1701',
    'Thuế giá trị gia tăng hàng sản xuất kinh doanh trong nước',
)
HKD_XML_TNCN_TIEU_MUC = (
    '1003',
    'Thuế thu nhập từ hoạt động sản xuất, kinh doanh của cá nhân',
)


def tncn_taxable_revenue(ytd_before, period_total):
    ytd_before = float(ytd_before or 0)
    period_total = float(period_total or 0)
    ytd_end = ytd_before + period_total
    floor = max(HKD_TNCN_YTD_THRESHOLD, ytd_before)
    return max(0.0, ytd_end - floor)


def calc_sector_taxes(totals, ytd_before=None):
    """
    totals: dict g1..g4 hoặc nn1..nn4 (lowercase).
    """
    normalized = {}
    for k, v in (totals or {}).items():
        key = str(k or '').lower()
        if key.startswith('nn') and len(key) == 3:
            key = f'g{key[2]}'
        if key in ('g1', 'g2', 'g3', 'g4'):
            normalized[key] = normalized.get(key, 0.0) + float(v or 0)

    taxes = {}
    total_gtgt = total_tncn = 0.0
    period_total = sum(normalized.get(k, 0) for k in ('g1', 'g2', 'g3', 'g4'))

    if ytd_before is None:
        tncn_taxable_total = period_total
        ytd_end = period_total
        below_threshold = False
        ytd_before_val = None
    else:
        ytd_before_val = float(ytd_before)
        ytd_end = ytd_before_val + period_total
        tncn_taxable_total = tncn_taxable_revenue(ytd_before_val, period_total)
        below_threshold = ytd_end < HKD_TNCN_YTD_THRESHOLD

    for nn in NN_SECTORS:
        key = nn_to_totals_key(nn)
        amount = float(normalized.get(key, 0) or 0)
        rates = HKD_TAX_RATES[nn]
        gtgt = round(amount * rates['gtgt'])
        if period_total > 0 and tncn_taxable_total > 0:
            tncn_base = amount * (tncn_taxable_total / period_total)
        else:
            tncn_base = 0.0
        tncn = round(tncn_base * rates['tncn'])
        taxes[key] = {'gtgt': gtgt, 'tncn': tncn, 'dt_tncn': round(tncn_base), 'nn': nn}
        total_gtgt += gtgt
        total_tncn += tncn

    taxes['total_gtgt'] = total_gtgt
    taxes['total_tncn'] = total_tncn
    taxes['tncn_meta'] = {
        'threshold': HKD_TNCN_YTD_THRESHOLD,
        'ytd_before': round(ytd_before_val) if ytd_before_val is not None else None,
        'ytd_after': round(ytd_end) if ytd_before_val is not None else None,
        'period_total': round(period_total),
        'tncn_taxable_revenue': round(tncn_taxable_total),
        'below_threshold': below_threshold,
    }
    return taxes


SECTOR_BY_PRODUCT_TYPE = {
    'goods': 'NN1',
    'materials': 'NN3',
    'fixed_asset': 'NN4',
    'tools': 'NN4',
    'finished_goods': 'NN3',
    'ready_made': 'NN2',
    'service': 'NN2',
    'transport_service': 'NN3',
    'construction_with_materials': 'NN3',
    'construction_without_materials': 'NN2',
}


def resolve_hkd_sector(product_type, override=None):
    if override:
        return normalize_nn_code(override)
    pt = (product_type or '').strip().lower()
    if pt == 'raw_materials':
        return None
    return SECTOR_BY_PRODUCT_TYPE.get(pt, 'NN4')


def default_nn_sectors_for_business_line(business_line):
    bl = (business_line or 'pos').strip().lower()
    if bl in ('fb_service', 'rental_service'):
        return ['NN2']
    return ['NN1']


def suggest_nn_sectors_for_business_line(business_line):
    """Gợi ý khi đăng ký — dịch vụ ưu tiên NN2 nhưng vẫn có thể thêm NN khác."""
    bl = (business_line or 'pos').strip().lower()
    if bl == 'fb_service':
        return ['NN2', 'NN1']
    if bl == 'rental_service':
        return ['NN2']
    return ['NN1', 'NN2', 'NN3', 'NN4']
