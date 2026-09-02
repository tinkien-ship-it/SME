"""Map giá vốn → role kế toán ổn định (không hardcode leaf).

Mặc định ghi sổ cấp 4 số:
  6321 HH · 6322 TP · 6323 DV · 6328 hao hụt
DN có thể mở thêm 63211/63212… và đặt ★ mặc định — resolve tự theo.

Nghiệp vụ trả role_key; journal resolve sang leaf postable qua account_roles.
"""
from __future__ import annotations

CHANNEL_DOMESTIC = 'domestic'
CHANNEL_EXPORT = 'export'

COGS_SPOILAGE_ROLE = 'cogs.spoilage'
COGS_SPOILAGE_ACCOUNT = '6328'


def normalize_cogs_channel(channel: str | None) -> str:
    ch = (channel or CHANNEL_DOMESTIC).strip().lower()
    if ch in ('export', 'xk', 'xuat_khau', 'xuất khẩu'):
        return CHANNEL_EXPORT
    return CHANNEL_DOMESTIC


def cogs_accounts_for_line(
    product_type: str | None = None,
    move_type: str | None = None,
    *,
    channel: str | None = CHANNEL_DOMESTIC,
    line_type: str | None = None,
) -> tuple[str, str, str]:
    """Trả (role GV, role/mã kho đối ứng, nhãn).

    Hàng hóa → cogs.goods.* (mặc định resolve 6321).
    Thành phẩm / NVL chế biến → cogs.fg.* (6322).
    Dịch vụ → cogs.service.processing (6323); kho trống.
    """
    ch = normalize_cogs_channel(channel)
    pt = (product_type or line_type or 'goods').strip().lower()
    mt = (move_type or '').strip().upper()

    is_material = (
        mt == 'SALE_RECIPE'
        or pt in ('recipe', 'raw_materials', 'materials', 'material', 'nvl')
    )
    is_finished = pt in (
        'finished_goods', 'finished', 'thanh_pham', 'ready_made', 'thanhpham',
    )
    is_service = pt in ('service', 'services', 'dich_vu', 'dv')

    if is_service:
        return 'cogs.service.processing', '', 'dịch vụ'

    if ch == CHANNEL_EXPORT:
        if is_finished:
            return 'cogs.fg.export', 'inv.finished', 'GV TP xuất khẩu'
        if is_material:
            return 'cogs.fg.export', 'inv.materials', 'GV NVL xuất khẩu'
        return 'cogs.goods.export', 'inv.goods', 'GV HH xuất khẩu'

    if is_finished:
        return 'cogs.fg.domestic', 'inv.finished', 'GV TP nội địa'
    if is_material:
        return 'cogs.fg.domestic', 'inv.materials', 'GV NVL / chế biến nội địa'
    return 'cogs.goods.domestic', 'inv.goods', 'GV HH nội địa'


def inventory_tk_for_product_type(product_type: str | None = None) -> str:
    """Fallback cũ theo loại SP — báo cáo tồn kho ưu tiên TK sổ cái (xem inventory_account_for_product)."""
    pt = (product_type or 'goods').strip().lower()
    if pt in ('recipe', 'raw_materials', 'materials', 'material', 'nvl'):
        return '152'
    if pt in ('finished_goods', 'finished', 'thanh_pham', 'ready_made', 'thanhpham'):
        return '155'
    return '156'


def inventory_tk_label(tk: str | None) -> str:
    t = (tk or '').strip()
    return {
        '152': 'Nguyên vật liệu (152)',
        '155': 'Thành phẩm (155)',
        '156': 'Hàng hóa (156)',
    }.get(t, 'Hàng tồn kho')


def cogs_spoilage_account() -> str:
    """Hao hụt / mất mát — role (resolve → 6328 hoặc leaf DN chọn)."""
    return COGS_SPOILAGE_ROLE
