"""Dịch vụ danh mục tài khoản SME — seed, tra cứu, tạo TK con linh hoạt."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Any

from Services.sme.coa_seed_tt99 import SEED_VERSION, iter_seed_accounts
from Services.sme.schema import ensure_sme_coa_schema

_CODE_RE = re.compile(r'^\d{3,12}$')


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ensure_sme_coa_ready(
    conn: sqlite3.Connection,
    *,
    force_reseed: bool = False,
    commit: bool = True,
) -> dict[str, Any]:
    """Đảm bảo schema + seed TT99/recommended đã có trên DB tenant."""
    ensure_sme_coa_schema(conn, commit=commit)
    c = conn.cursor()
    row = c.execute(
        "SELECT value FROM sme_coa_seed_meta WHERE key = 'seed_version'"
    ).fetchone()
    current = row[0] if row else None
    count = c.execute("SELECT COUNT(*) FROM sme_chart_of_accounts").fetchone()[0]

    if not force_reseed and current == SEED_VERSION and count > 0:
        return {'seeded': False, 'seed_version': current, 'count': count}

    if force_reseed:
        # Chỉ xóa tài khoản hệ thống/recommended chưa phát sinh custom chồng; giữ custom của DN
        c.execute(
            """
            DELETE FROM sme_chart_of_accounts
            WHERE is_custom = 0
              AND code NOT IN (
                  SELECT parent_code FROM sme_chart_of_accounts
                  WHERE is_custom = 1 AND parent_code IS NOT NULL
              )
            """
        )

    inserted = 0
    updated = 0
    for acc in iter_seed_accounts():
        exists = c.execute(
            "SELECT code, is_custom FROM sme_chart_of_accounts WHERE code = ?",
            (acc['code'],),
        ).fetchone()
        if exists:
            if exists[1]:
                continue  # không ghi đè TK do người dùng tạo
            c.execute(
                """
                UPDATE sme_chart_of_accounts SET
                    name = ?, parent_code = ?, level = ?, account_class = ?,
                    normal_balance = ?, is_postable = ?, is_system = ?,
                    is_recommended = ?, legal_source = ?, bctc_line_code = ?,
                    track_customer = ?, track_supplier = ?, track_employee = ?,
                    track_bank = ?, track_currency = ?, track_warehouse = ?,
                    track_product = ?, track_project = ?, track_department = ?,
                    sort_order = ?, description = ?, updated_at = ?
                WHERE code = ? AND is_custom = 0
                """,
                (
                    acc['name'], acc['parent_code'], acc['level'], acc['account_class'],
                    acc['normal_balance'], acc['is_postable'], acc['is_system'],
                    acc['is_recommended'], acc['legal_source'], acc['bctc_line_code'],
                    acc['track_customer'], acc['track_supplier'], acc['track_employee'],
                    acc['track_bank'], acc['track_currency'], acc['track_warehouse'],
                    acc['track_product'], acc['track_project'], acc['track_department'],
                    acc['sort_order'], acc['description'], _now(), acc['code'],
                ),
            )
            updated += 1
        else:
            c.execute(
                """
                INSERT INTO sme_chart_of_accounts (
                    code, name, parent_code, level, account_class, normal_balance,
                    is_postable, is_system, is_recommended, is_custom, is_active,
                    legal_source, bctc_line_code,
                    track_customer, track_supplier, track_employee, track_bank,
                    track_currency, track_warehouse, track_product, track_project,
                    track_department, sort_order, description, created_at, updated_at
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,0,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    acc['code'], acc['name'], acc['parent_code'], acc['level'],
                    acc['account_class'], acc['normal_balance'], acc['is_postable'],
                    acc['is_system'], acc['is_recommended'], acc['legal_source'],
                    acc['bctc_line_code'],
                    acc['track_customer'], acc['track_supplier'], acc['track_employee'],
                    acc['track_bank'], acc['track_currency'], acc['track_warehouse'],
                    acc['track_product'], acc['track_project'], acc['track_department'],
                    acc['sort_order'], acc['description'], _now(), _now(),
                ),
            )
            inserted += 1

    _refresh_postable_flags(c)
    c.execute(
        """
        INSERT INTO sme_coa_seed_meta(key, value, updated_at) VALUES ('seed_version', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (SEED_VERSION, _now()),
    )
    if commit:
        conn.commit()
    count = c.execute("SELECT COUNT(*) FROM sme_chart_of_accounts").fetchone()[0]
    return {
        'seeded': True,
        'seed_version': SEED_VERSION,
        'inserted': inserted,
        'updated': updated,
        'count': count,
    }


def _refresh_postable_flags(c: sqlite3.Cursor) -> None:
    """TK có con đang active → không postable; lá → postable (trừ khi người dùng tắt)."""
    c.execute(
        """
        UPDATE sme_chart_of_accounts
        SET is_postable = CASE
            WHEN code IN (
                SELECT DISTINCT parent_code FROM sme_chart_of_accounts
                WHERE parent_code IS NOT NULL AND is_active = 1
            ) THEN 0
            ELSE 1
        END,
        updated_at = ?
        WHERE is_custom = 0 OR is_custom = 1
        """,
        (_now(),),
    )


def _row_to_dict(row: sqlite3.Row | tuple, keys: list[str] | None = None) -> dict:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if keys:
        return dict(zip(keys, row))
    return {}


def list_accounts(
    conn: sqlite3.Connection,
    *,
    active_only: bool = True,
    parent_code: str | None = None,
    q: str | None = None,
    postable_only: bool = False,
    levels: list[int] | tuple[int, ...] | None = None,
) -> list[dict]:
    ensure_sme_coa_ready(conn)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    sql = "SELECT * FROM sme_chart_of_accounts WHERE 1=1"
    params: list[Any] = []
    if active_only:
        sql += " AND is_active = 1"
    if parent_code is not None:
        if parent_code == '':
            sql += " AND parent_code IS NULL"
        else:
            sql += " AND parent_code = ?"
            params.append(parent_code)
    if postable_only:
        sql += " AND is_postable = 1"
    if levels:
        placeholders = ','.join('?' for _ in levels)
        sql += f" AND level IN ({placeholders})"
        params.extend(int(lv) for lv in levels)
    if q:
        sql += " AND (code LIKE ? OR name LIKE ?)"
        like = f"%{q.strip()}%"
        params.extend([like, like])
    sql += " ORDER BY sort_order, code"
    return [dict(r) for r in c.execute(sql, params).fetchall()]


def list_children(conn: sqlite3.Connection, parent_code: str, *, active_only: bool = True) -> list[dict]:
    return list_accounts(conn, active_only=active_only, parent_code=parent_code)


def list_counterpart_accounts(
    conn: sqlite3.Connection,
    *,
    active_only: bool = True,
) -> list[dict]:
    """TK đối ứng phiếu thu/chi: cấp 2–4 + cấp 1 không có tài khoản con."""
    rows = list_accounts(conn, active_only=active_only)
    parents_with_kids = {
        r.get('parent_code') for r in rows if r.get('parent_code')
    }
    out: list[dict] = []
    for r in rows:
        lv = int(r.get('level') or 1)
        code = r.get('code')
        if 2 <= lv <= 4:
            out.append(r)
        elif lv == 1 and code not in parents_with_kids:
            out.append(r)
    return out


def get_account(
    conn: sqlite3.Connection,
    code: str,
    *,
    commit: bool = True,
) -> dict | None:
    ensure_sme_coa_ready(conn, commit=commit)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM sme_chart_of_accounts WHERE code = ?", (code,)
    ).fetchone()
    return dict(row) if row else None


def suggest_next_child_code(conn: sqlite3.Connection, parent_code: str) -> str:
    """Gợi ý mã con tiếp theo: 112 → 1121/1124…; 1121 → 112101, 112102…

    Với 1121/1122 lần đầu (chưa có con): gợi ý XX02 vì XX01 dành cho TK mặc định
    (tự tạo khi mở thêm tài khoản + chuyển số liệu cũ).
    """
    from Services.sme.bank_accounts import BANK_DETAIL_SPLIT, count_active_children

    ensure_sme_coa_ready(conn)
    parent = get_account(conn, parent_code)
    if not parent:
        raise ValueError(f'Không tìm thấy tài khoản cha {parent_code}')

    children = conn.execute(
        "SELECT code FROM sme_chart_of_accounts WHERE parent_code = ? ORDER BY code",
        (parent_code,),
    ).fetchall()
    existing = {r[0] for r in children}

    # Lần đầu tách NH: dành XX01 cho mặc định → gợi ý tài khoản mới = XX02
    if parent_code in BANK_DETAIL_SPLIT and count_active_children(conn, parent_code) == 0:
        reserved = BANK_DETAIL_SPLIT[parent_code]['default_code']
        cand = f'{parent_code}02'
        if cand not in existing and cand != reserved:
            return cand

    # Quy ước:
    # - Cha 3 số → con 4 số (thêm 1 chữ số 1..9 rồi 0..)
    # - Cha 4 số → con 6 số (thêm 01, 02…)
    # - Cha >=5 → thêm 2 chữ số
    if len(parent_code) == 3:
        for i in range(1, 100):
            if i <= 9:
                cand = f"{parent_code}{i}"
            else:
                cand = f"{parent_code}{i}"
            if cand not in existing and _CODE_RE.match(cand):
                return cand
    elif len(parent_code) == 4:
        for i in range(1, 100):
            cand = f"{parent_code}{i:02d}"
            if cand not in existing:
                return cand
    else:
        for i in range(1, 1000):
            width = 2 if len(parent_code) <= 6 else 2
            cand = f"{parent_code}{i:0{width}d}"
            if cand not in existing:
                return cand
    raise ValueError('Đã hết khoảng mã con khả dụng cho tài khoản này')


def _insert_child_account_row(
    conn: sqlite3.Connection,
    *,
    parent: dict,
    code: str,
    name: str,
    custom_reason: str = '',
    tracks: dict[str, int] | None = None,
    bctc_line_code: str | None = None,
    description: str = '',
) -> dict:
    """Chèn một TK con; đánh dấu cha không postable. Không commit."""
    parent_code = parent['code']
    if len(code) <= 3:
        level = 1
    elif len(code) == 4:
        level = 2
    elif len(code) == 5:
        level = 3
    else:
        level = max(4, parent['level'] + 1)

    track_fields = {
        'track_customer': int(parent.get('track_customer') or 0),
        'track_supplier': int(parent.get('track_supplier') or 0),
        'track_employee': int(parent.get('track_employee') or 0),
        'track_bank': int(parent.get('track_bank') or 0),
        'track_currency': int(parent.get('track_currency') or 0),
        'track_warehouse': int(parent.get('track_warehouse') or 0),
        'track_product': int(parent.get('track_product') or 0),
        'track_project': int(parent.get('track_project') or 0),
        'track_department': int(parent.get('track_department') or 0),
    }
    if tracks:
        for k, v in tracks.items():
            key = k if k.startswith('track_') else f'track_{k}'
            if key in track_fields:
                track_fields[key] = 1 if v else 0

    c = conn.cursor()
    c.execute(
        "UPDATE sme_chart_of_accounts SET is_postable = 0, updated_at = ? WHERE code = ?",
        (_now(), parent_code),
    )
    c.execute(
        """
        INSERT INTO sme_chart_of_accounts (
            code, name, parent_code, level, account_class, normal_balance,
            is_postable, is_system, is_recommended, is_custom, is_active,
            legal_source, bctc_line_code,
            track_customer, track_supplier, track_employee, track_bank,
            track_currency, track_warehouse, track_product, track_project,
            track_department, sort_order, description, custom_reason,
            created_at, updated_at
        ) VALUES (
            ?,?,?,?,?,?,1,0,0,1,1,'custom',?,
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            code, name, parent_code, level,
            parent['account_class'], parent['normal_balance'],
            bctc_line_code or parent.get('bctc_line_code'),
            track_fields['track_customer'], track_fields['track_supplier'],
            track_fields['track_employee'], track_fields['track_bank'],
            track_fields['track_currency'], track_fields['track_warehouse'],
            track_fields['track_product'], track_fields['track_project'],
            track_fields['track_department'],
            int(code.ljust(8, '0')[:8]),
            description or f'Tài khoản con của {parent_code}',
            (custom_reason or '').strip() or 'Mở theo nhu cầu quản lý (Điều 11 TT99)',
            _now(), _now(),
        ),
    )
    created = get_account(conn, code, commit=False)
    assert created
    return created


def create_child_account(
    conn: sqlite3.Connection,
    *,
    parent_code: str,
    code: str | None = None,
    name: str,
    custom_reason: str = '',
    tracks: dict[str, int] | None = None,
    bctc_line_code: str | None = None,
    description: str = '',
) -> dict:
    """
    Tạo tài khoản con linh hoạt (Điều 11 TT99).
    - Kế thừa account_class, normal_balance, tracking từ cha (có thể override tracks).
    - Cha sẽ chuyển thành không postable.
    - Với 1121/1122 lần đầu: tự tạo XX01 (mặc định), chuyển số liệu cũ, TK mới = XX02.
    """
    from Services.sme.bank_accounts import (
        BANK_DETAIL_SPLIT,
        ensure_bank_default_child_on_first_split,
    )

    ensure_sme_coa_ready(conn)
    name = (name or '').strip()
    if not name:
        raise ValueError('Tên tài khoản không được trống')

    parent = get_account(conn, parent_code)
    if not parent:
        raise ValueError(f'Không tìm thấy tài khoản cha {parent_code}')
    if not parent.get('is_active'):
        raise ValueError('Tài khoản cha đã ngừng sử dụng')

    automation = None
    split_cfg = BANK_DETAIL_SPLIT.get(parent_code)

    def _insert_default(**kwargs):
        return _insert_child_account_row(
            conn,
            parent=parent,
            code=kwargs['code'],
            name=kwargs['name'],
            custom_reason=kwargs.get('custom_reason') or '',
            description=kwargs.get('description') or '',
        )

    if split_cfg:
        automation = ensure_bank_default_child_on_first_split(
            conn, parent_code, insert_child_fn=_insert_default,
        )

    if not code:
        code = suggest_next_child_code(conn, parent_code)
    code = str(code).strip()
    if not _CODE_RE.match(code):
        raise ValueError('Mã tài khoản chỉ gồm chữ số, độ dài 3–12')
    if not code.startswith(parent_code):
        raise ValueError(f'Mã con phải bắt đầu bằng mã cha ({parent_code})')
    if code == parent_code:
        raise ValueError('Mã con phải khác mã cha')

    # Không cho ghi đè mã mặc định vừa tạo / dành riêng
    if split_cfg and code == split_cfg['default_code']:
        code = suggest_next_child_code(conn, parent_code)
        if code == split_cfg['default_code']:
            raise ValueError(
                f'Mã {split_cfg["default_code"]} dành cho tài khoản mặc định. '
                f'Hãy dùng mã khác (ví dụ {parent_code}02).'
            )

    if get_account(conn, code, commit=False):
        raise ValueError(f'Mã {code} đã tồn tại')

    created = _insert_child_account_row(
        conn,
        parent=parent,
        code=code,
        name=name,
        custom_reason=custom_reason,
        tracks=tracks,
        bctc_line_code=bctc_line_code,
        description=description,
    )
    conn.commit()
    created = get_account(conn, code, commit=False) or created
    if automation:
        created = dict(created)
        created['automation'] = automation
        created['effective_code'] = code
        if automation.get('created_default'):
            created['automation_message'] = (
                f"Đã tự tạo {automation['default_code']} làm TK mặc định, "
                f"chuyển số liệu từ {parent_code} → {automation['default_code']}. "
                f"Tài khoản mới của bạn: {code}."
            )
    return created


def deactivate_account(conn: sqlite3.Connection, code: str) -> dict:
    """Ngừng sử dụng TK (không xóa). Không cho tắt nếu còn con active."""
    ensure_sme_coa_ready(conn)
    acc = get_account(conn, code)
    if not acc:
        raise ValueError(f'Không tìm thấy tài khoản {code}')
    kids = conn.execute(
        "SELECT COUNT(*) FROM sme_chart_of_accounts WHERE parent_code = ? AND is_active = 1",
        (code,),
    ).fetchone()[0]
    if kids:
        raise ValueError('Còn tài khoản con đang hoạt động — hãy ngừng sử dụng các TK con trước')
    conn.execute(
        "UPDATE sme_chart_of_accounts SET is_active = 0, is_postable = 0, updated_at = ? WHERE code = ?",
        (_now(), code),
    )
    # Nếu cha không còn con active nào khác và cha từng là lá → có thể postable lại
    parent = acc.get('parent_code')
    if parent:
        sibs = conn.execute(
            "SELECT COUNT(*) FROM sme_chart_of_accounts WHERE parent_code = ? AND is_active = 1",
            (parent,),
        ).fetchone()[0]
        if sibs == 0:
            conn.execute(
                "UPDATE sme_chart_of_accounts SET is_postable = 1, updated_at = ? WHERE code = ?",
                (_now(), parent),
            )
    conn.commit()
    return get_account(conn, code) or acc


def update_account_meta(
    conn: sqlite3.Connection,
    code: str,
    *,
    name: str | None = None,
    description: str | None = None,
    bctc_line_code: str | None = None,
    tracks: dict[str, int] | None = None,
) -> dict:
    """Sửa tên/mô tả/tracking — không đổi mã / bản chất Nợ-Có."""
    ensure_sme_coa_ready(conn)
    acc = get_account(conn, code)
    if not acc:
        raise ValueError(f'Không tìm thấy tài khoản {code}')
    if not acc.get('is_active'):
        raise ValueError('Tài khoản đã ngừng sử dụng')

    fields = []
    params: list[Any] = []
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError('Tên không được trống')
        # System legal L1: cho phép sửa tên hiển thị nhẹ nhưng giữ is_system
        fields.append('name = ?')
        params.append(name)
    if description is not None:
        fields.append('description = ?')
        params.append(description)
    if bctc_line_code is not None:
        fields.append('bctc_line_code = ?')
        params.append(bctc_line_code or None)
    if tracks:
        for k, v in tracks.items():
            key = k if k.startswith('track_') else f'track_{k}'
            if key.startswith('track_'):
                fields.append(f'{key} = ?')
                params.append(1 if v else 0)
    if not fields:
        return acc
    fields.append('updated_at = ?')
    params.append(_now())
    params.append(code)
    conn.execute(
        f"UPDATE sme_chart_of_accounts SET {', '.join(fields)} WHERE code = ?",
        params,
    )
    conn.commit()
    return get_account(conn, code) or acc


def account_tree(conn: sqlite3.Connection, *, active_only: bool = True) -> list[dict]:
    """Cây tài khoản phẳng kèm indent/has_children cho UI."""
    rows = list_accounts(conn, active_only=active_only)
    children_map: dict[str, list[str]] = {}
    for r in rows:
        p = r.get('parent_code')
        if p:
            children_map.setdefault(p, []).append(r['code'])
    out = []
    for r in rows:
        item = dict(r)
        item['has_children'] = r['code'] in children_map
        item['child_count'] = len(children_map.get(r['code'], []))
        out.append(item)
    return out
