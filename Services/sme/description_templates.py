"""Mẫu diễn giải kế toán SME — template theo nghiệp vụ, override theo tenant/chứng từ."""
from __future__ import annotations
import re
from datetime import datetime
from typing import Any

_ALLOWED_SCOPES = frozenset({"header", "debit_line", "credit_line", "receipt", "debt"})
_TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# Tên nghiệp vụ chỉ dùng cho giao diện/cấu hình. Mã business_type trong DB
# được giữ nguyên để không ảnh hưởng posting rules, worker và dữ liệu lịch sử.
BUSINESS_TYPE_LABELS = {
    "THU_CONG": "Bút toán thủ công",
    "SALE_REVENUE": "Ghi nhận doanh thu bán hàng",
    "SALE_COGS": "Ghi nhận giá vốn hàng bán",
    "BAN_HANG_TM": "Bán hàng thu tiền mặt",
    "BAN_HANG_CK": "Bán hàng thu qua ngân hàng",
    "BAN_HANG_CONG_NO": "Bán hàng chưa thu tiền",
    "GIA_VON_BAN_HANG": "Ghi nhận giá vốn hàng bán",
    "GIAM_TRU_DOANH_THU": "Ghi nhận khoản giảm trừ doanh thu",
    "TRA_HANG_BAN": "Hàng bán bị trả lại",
    "THU_TIEN": "Thu tiền",
    "CHI_TIEN": "Chi tiền",
    "THANH_TOAN_NCC_NK": "Thanh toán công nợ nhà cung cấp",
    "NOP_NGAN_HANG": "Nộp tiền vào ngân hàng",
    "TAT_TOAN_113": "Tất toán tiền đang chuyển",
    "TAT_TOAN_LC": "Tất toán thư tín dụng",
    "NHAP_KHO_HANG_HOA": "Nhập kho hàng hóa",
    "NHAP_KHO_NVL": "Nhập kho nguyên vật liệu",
    "SAN_XUAT_TP": "Nhập kho thành phẩm sản xuất",
    "MUA_DICH_VU": "Mua dịch vụ",
    "MUA_CCDC": "Mua công cụ, dụng cụ",
    "MUA_TSCD": "Mua tài sản cố định",
    "KHAU_HAO_TSCD": "Khấu hao tài sản cố định",
    "PHAN_BO_CCDC": "Phân bổ công cụ, dụng cụ",
    "PHAN_BO_CPTT": "Phân bổ chi phí trả trước",
    "TRICH_LUONG": "Trích tiền lương phải trả",
    "TRICH_KPCD": "Trích kinh phí công đoàn",
    "TRICH_DU_PHONG": "Trích lập dự phòng",
    "THUE_TAI_CHINH": "Thuê tài chính",
    "PHAN_PHOI_LN": "Phân phối lợi nhuận",
    "MUA_BDSDT": "Mua bất động sản đầu tư",
    "BDSDT_LEASE_DIRECT": "Cho thuê bất động sản đầu tư từng kỳ",
    "BDSDT_LEASE_PREPAID": "Thu trước tiền thuê bất động sản đầu tư",
    "BDSDT_REVENUE_RECOGNITION": "Phân bổ doanh thu cho thuê bất động sản đầu tư",
    "BDSDT_DEPRECIATION": "Khấu hao bất động sản đầu tư",
    "BDSDT_IMPAIRMENT": "Ghi nhận suy giảm giá trị bất động sản đầu tư",
    "BDSDT_SALE": "Bán bất động sản đầu tư",
    "BDSDT_TRANSFER_TO_FA": "Chuyển bất động sản đầu tư sang tài sản cố định",
    "BDSDT_TRANSFER_TO_INVENTORY": "Chuyển bất động sản đầu tư sang hàng tồn kho",
}

ACCOUNTING_TEMPLATE_SCOPES = ("header", "debit_line", "credit_line")
SCOPE_LABELS = {
    "header": "Diễn giải chung",
    "debit_line": "Diễn giải dòng Nợ",
    "credit_line": "Diễn giải dòng Có",
    "receipt": "Diễn giải Phiếu Thu",
    "debt": "Diễn giải công nợ",
}


def business_type_label(business_type: str | None) -> str:
    bt = str(business_type or "").strip().upper()
    if not bt:
        return "Nghiệp vụ kế toán"
    return BUSINESS_TYPE_LABELS.get(bt) or bt.replace("_", " ").title()


SYSTEM_DEFAULTS = {
    ("BDSDT_LEASE_DIRECT", "header"): "Ghi nhận doanh thu cho thuê {property_name} kỳ {period_key}",
    ("BDSDT_LEASE_PREPAID", "header"): "Thu trước tiền thuê {property_name} từ {period_from} đến {period_to}, HĐ {invoice_no}",
    ("BDSDT_REVENUE_RECOGNITION", "header"): "Phân bổ doanh thu cho thuê {property_name} kỳ {period_key}",
    ("BDSDT_DEPRECIATION", "header"): "Trích khấu hao BĐSĐT {property_name} kỳ {period_key}",
    ("BDSDT_IMPAIRMENT", "header"): "Ghi nhận suy giảm giá trị BĐSĐT {property_name}",
    ("BDSDT_SALE", "header"): "Ghi giảm BĐSĐT {property_name} khi bán",
    ("BDSDT_TRANSFER_TO_FA", "header"): "Chuyển BĐSĐT {property_name} sang tài sản cố định",
    ("BDSDT_TRANSFER_TO_INVENTORY", "header"): "Chuyển BĐSĐT {property_name} sang hàng tồn kho",
    ("SALE_REVENUE", "header"): "Ghi nhận doanh thu bán hàng theo chứng từ {document_no}",
    ("SALE_COGS", "header"): "Ghi nhận giá vốn bán hàng theo chứng từ {document_no}",
    ("MUA_BDSDT", "header"): "Ghi nhận mua bất động sản đầu tư theo chứng từ {document_no}",
}

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def ensure_description_template_schema(conn, *, commit: bool = False) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sme_accounting_description_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            business_type TEXT NOT NULL,
            template_scope TEXT NOT NULL DEFAULT 'header',
            template_text TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            is_system_default INTEGER NOT NULL DEFAULT 0,
            updated_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(business_type, template_scope)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sme_accounting_description_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL,
            line_id INTEGER,
            old_description TEXT,
            new_description TEXT,
            reason TEXT,
            updated_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sme_desc_tpl_business ON sme_accounting_description_templates(business_type, template_scope)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sme_desc_audit_entry ON sme_accounting_description_audit(entry_id, created_at)")
    for (business_type, scope), text in SYSTEM_DEFAULTS.items():
        conn.execute("""
            INSERT INTO sme_accounting_description_templates
                (business_type, template_scope, template_text, active, is_system_default, created_at, updated_at)
            VALUES (?, ?, ?, 1, 1, ?, ?)
            ON CONFLICT(business_type, template_scope) DO NOTHING
        """, (business_type, scope, text, _now(), _now()))
    if commit:
        conn.commit()

def list_templates(conn) -> list[dict[str, Any]]:
    """Danh mục mẫu diễn giải cho toàn bộ nghiệp vụ kế toán đang có.

    Nguồn business_type:
    - catalog hệ thống;
    - bút toán đã phát sinh;
    - posting rules đang cấu hình;
    - các template tenant đã lưu.

    Với nghiệp vụ chưa có template, trả một dòng rỗng cho từng scope
    header/debit_line/credit_line để người dùng có thể thiết lập ngay.
    """
    ensure_description_template_schema(conn, commit=False)

    business_types = set(BUSINESS_TYPE_LABELS)
    try:
        business_types.update(
            str(r[0] or "").strip().upper()
            for r in conn.execute(
                "SELECT DISTINCT business_type FROM sme_journal_entries "
                "WHERE COALESCE(TRIM(business_type),'') <> ''"
            ).fetchall()
        )
    except Exception:
        pass
    try:
        business_types.update(
            str(r[0] or "").strip().upper()
            for r in conn.execute(
                "SELECT DISTINCT business_type FROM sme_posting_rules "
                "WHERE COALESCE(TRIM(business_type),'') <> ''"
            ).fetchall()
        )
    except Exception:
        pass

    rows = conn.execute("""
        SELECT id, business_type, template_scope, template_text, active,
               is_system_default, updated_by, updated_at
        FROM sme_accounting_description_templates
        ORDER BY business_type, template_scope
    """).fetchall()

    saved: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        item = dict(r) if hasattr(r, "keys") else {
            "id": r[0], "business_type": r[1], "template_scope": r[2],
            "template_text": r[3], "active": r[4], "is_system_default": r[5],
            "updated_by": r[6], "updated_at": r[7],
        }
        bt = str(item.get("business_type") or "").strip().upper()
        scope = str(item.get("template_scope") or "header").strip().lower()
        if not bt:
            continue
        business_types.add(bt)
        saved[(bt, scope)] = item

    out: list[dict[str, Any]] = []
    for bt in sorted(business_types, key=lambda x: (business_type_label(x), x)):
        for scope in ACCOUNTING_TEMPLATE_SCOPES:
            key = (bt, scope)
            item = dict(saved.get(key) or {})
            system_text = SYSTEM_DEFAULTS.get(key)
            item.update({
                "business_type": bt,
                "business_label": business_type_label(bt),
                "template_scope": scope,
                "scope_label": SCOPE_LABELS.get(scope, scope),
                "template_text": item.get("template_text") or system_text or "",
                "active": int(item.get("active", 1 if system_text else 0)),
                "is_system_default": int(item.get("is_system_default", 1 if system_text else 0)),
                "system_default_text": system_text,
                "has_system_default": key in SYSTEM_DEFAULTS,
                "configured": bool(item.get("id") or system_text),
            })
            out.append(item)
    return out


def save_template(conn, *, business_type: str, template_scope: str,
                  template_text: str, active: bool = True,
                  updated_by: str | None = None) -> dict[str, Any]:
    business_type = str(business_type or "").strip().upper()
    scope = str(template_scope or "header").strip().lower()
    text = str(template_text or "").strip()
    if not business_type:
        raise ValueError("business_type không được để trống")
    if scope not in _ALLOWED_SCOPES:
        raise ValueError("template_scope không hợp lệ")
    if not text:
        raise ValueError("Mẫu diễn giải không được để trống")
    ensure_description_template_schema(conn, commit=False)
    conn.execute("""
        INSERT INTO sme_accounting_description_templates
            (business_type, template_scope, template_text, active, is_system_default,
             updated_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, 0, ?, ?, ?)
        ON CONFLICT(business_type, template_scope) DO UPDATE SET
            template_text=excluded.template_text,
            active=excluded.active,
            is_system_default=0,
            updated_by=excluded.updated_by,
            updated_at=excluded.updated_at
    """, (business_type, scope, text, int(bool(active)), updated_by, _now(), _now()))
    return {"business_type": business_type, "template_scope": scope,
            "template_text": text, "active": bool(active)}


def reset_template(conn, *, business_type: str, template_scope: str = "header",
                   updated_by: str | None = None) -> dict[str, Any]:
    business_type = str(business_type or "").strip().upper()
    scope = str(template_scope or "header").strip().lower()
    key = (business_type, scope)
    if key not in SYSTEM_DEFAULTS:
        raise ValueError("Nghiệp vụ này chưa có mẫu mặc định KETO")
    ensure_description_template_schema(conn, commit=False)
    text = SYSTEM_DEFAULTS[key]
    conn.execute("""
        INSERT INTO sme_accounting_description_templates
            (business_type, template_scope, template_text, active, is_system_default,
             updated_by, created_at, updated_at)
        VALUES (?, ?, ?, 1, 1, ?, ?, ?)
        ON CONFLICT(business_type, template_scope) DO UPDATE SET
            template_text=excluded.template_text,
            active=1,
            is_system_default=1,
            updated_by=excluded.updated_by,
            updated_at=excluded.updated_at
    """, (business_type, scope, text, updated_by, _now(), _now()))
    return {
        "business_type": business_type,
        "template_scope": scope,
        "template_text": text,
        "active": True,
        "is_system_default": True,
        "system_default_text": text,
        "has_system_default": True,
    }


def render_template_text(text: str, context: dict[str, Any] | None = None) -> str:
    ctx = {str(k): "" if v is None else str(v) for k, v in (context or {}).items()}
    return _TOKEN_RE.sub(lambda m: ctx.get(m.group(1), m.group(0)), str(text or "")).strip()

def render_description(conn, *, business_type: str | None, scope: str = "header",
                       context: dict[str, Any] | None = None,
                       fallback: str = "") -> str:
    bt = str(business_type or "").strip().upper()
    scope = str(scope or "header").strip().lower()
    if not bt:
        return fallback
    ensure_description_template_schema(conn, commit=False)
    row = conn.execute("""
        SELECT template_text FROM sme_accounting_description_templates
        WHERE business_type=? AND template_scope=? AND active=1 LIMIT 1
    """, (bt, scope)).fetchone()
    if not row:
        return fallback
    rendered = render_template_text(row[0], context)
    return rendered or fallback

def update_entry_descriptions(conn, *, entry_id: int, header_description: str | None = None,
                              line_descriptions: dict[int, str] | None = None,
                              reason: str = "Sửa diễn giải", updated_by: str | None = None) -> dict[str, Any]:
    """Chỉ sửa metadata diễn giải; không đổi tài khoản, số tiền, ngày hay số dư."""
    ensure_description_template_schema(conn, commit=False)
    row = conn.execute("SELECT id, entry_no, description FROM sme_journal_entries WHERE id=?", (entry_id,)).fetchone()
    if not row:
        raise ValueError(f"Không tìm thấy bút toán #{entry_id}")
    old_header = row[2] or ""
    changed = 0
    if header_description is not None:
        new_header = str(header_description).strip()
        if new_header != old_header:
            conn.execute("UPDATE sme_journal_entries SET description=?, updated_at=? WHERE id=?",
                         (new_header, _now(), entry_id))
            conn.execute("""INSERT INTO sme_accounting_description_audit
                (entry_id,line_id,old_description,new_description,reason,updated_by,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (entry_id, None, old_header, new_header, reason, updated_by, _now()))
            changed += 1
    for line_id, new_text in (line_descriptions or {}).items():
        ln = conn.execute("SELECT id, description FROM sme_journal_lines WHERE id=? AND entry_id=?",
                          (int(line_id), entry_id)).fetchone()
        if not ln:
            raise ValueError(f"Dòng bút toán #{line_id} không thuộc bút toán #{entry_id}")
        old = ln[1] or ""
        new = str(new_text or "").strip()
        if new != old:
            conn.execute("UPDATE sme_journal_lines SET description=? WHERE id=?", (new, int(line_id)))
            conn.execute("""INSERT INTO sme_accounting_description_audit
                (entry_id,line_id,old_description,new_description,reason,updated_by,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (entry_id, int(line_id), old, new, reason, updated_by, _now()))
            changed += 1
    return {"id": entry_id, "entry_no": row[1], "changed": changed}
