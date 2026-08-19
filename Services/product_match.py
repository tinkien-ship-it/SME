"""Khớp tên HĐ nhà cung cấp với SKU đã có trong kho.

Một product_id = một tồn / một giá vốn. Tên trên hóa đơn NCC là bí danh (alias).
Không tự gộp khi điểm thấp — chỉ gợi ý để người dùng xác nhận liên kết.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

_VI_FOLD = str.maketrans(
    'àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ'
    'ÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ',
    'aaaaaaaaaaaaaaaaaeeeeeeeeeeeiiiiiooooooooooooooooouuuuuuuuuuuyyyyyd'
    'AAAAAAAAAAAAAAAAAEEEEEEEEEEEIIIIIOOOOOOOOOOOOOOOOOUUUUUUUUUUUYYYYYD',
)

_STOPWORDS = frozenset({
    'cai', 'chiec', 'chiec', 'hang', 'loai', 'san', 'pham', 'sp',
    'moi', 'chinh', 'hang', 'cua', 'cho', 'voi', 'va', 'the', 'and',
    'bo', 'hop', 'thung', 'loc', 'vi', 'go', 'mau', 'size',
    'bong', 'den', 'bongden',
})

_MODEL_RE = re.compile(r'(?i)(?<![a-z0-9])([a-z]{0,8}\d[\w\-\./]{0,14}|\d{4,10})(?![a-z0-9])')
_YEAR_RE = re.compile(r'^(19|20)\d{2}$')
_SERVICE_TYPES = frozenset({'service', 'services', 'dich_vu', 'dichvu'})

AUTO_SCORE = 90
SUGGEST_SCORE = 55
HIGH_SCORE = 70


def fold_name(value: str | None) -> str:
    text = str(value or '').translate(_VI_FOLD).casefold()
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def extract_models(value: str | None) -> set[str]:
    raw = str(value or '')
    folded = fold_name(raw)
    found: set[str] = set()
    for m in _MODEL_RE.finditer(folded.replace(' ', ' ')):
        token = re.sub(r'[^a-z0-9]', '', m.group(1).casefold())
        if len(token) < 2 or _YEAR_RE.match(token):
            continue
        if token.isdigit() and len(token) < 4:
            continue
        found.add(token)
    return found


def tokenize(value: str | None) -> set[str]:
    folded = fold_name(value)
    tokens = set()
    for w in folded.split():
        if len(w) < 2 or w in _STOPWORDS:
            continue
        tokens.add(w)
    tokens |= extract_models(value)
    return tokens


def ensure_product_aliases_schema(conn: sqlite3.Connection, *, commit: bool = False) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            invoice_name TEXT NOT NULL,
            supplier_id INTEGER,
            supplier_sku TEXT,
            barcode TEXT,
            normalized_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cols = {r[1] for r in conn.execute('PRAGMA table_info(product_aliases)').fetchall()}
    for col, typ in (
        ('supplier_sku', 'TEXT'),
        ('barcode', 'TEXT'),
        ('normalized_name', 'TEXT'),
        ('created_at', 'TEXT'),
    ):
        if col not in cols:
            try:
                conn.execute(f'ALTER TABLE product_aliases ADD COLUMN {col} {typ}')
            except sqlite3.OperationalError:
                pass
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_product_aliases_norm ON product_aliases(normalized_name)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_product_aliases_product ON product_aliases(product_id)'
    )
    try:
        conn.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_product_aliases_key '
            'ON product_aliases(supplier_id, invoice_name)'
        )
    except sqlite3.OperationalError:
        pass
    if commit:
        conn.commit()


def save_product_alias(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    invoice_name: str,
    supplier_id: int | None = None,
    barcode: str | None = None,
    supplier_sku: str | None = None,
    commit: bool = False,
) -> None:
    ensure_product_aliases_schema(conn, commit=False)
    pid = int(product_id or 0)
    name = (invoice_name or '').strip()
    if pid <= 0 or not name:
        return
    sid = int(supplier_id) if supplier_id else None
    norm = fold_name(name)
    bc = (barcode or '').strip() or None
    sku = (supplier_sku or '').strip() or None
    row = conn.execute(
        """
        SELECT id FROM product_aliases
        WHERE invoice_name = ? AND COALESCE(supplier_id, 0) = ?
        """,
        (name, int(sid or 0)),
    ).fetchone()
    if row:
        conn.execute(
            """
            UPDATE product_aliases
            SET product_id = ?, barcode = COALESCE(?, barcode),
                supplier_sku = COALESCE(?, supplier_sku),
                normalized_name = ?
            WHERE id = ?
            """,
            (pid, bc, sku, norm, row[0]),
        )
    else:
        conn.execute(
            """
            INSERT INTO product_aliases
                (product_id, invoice_name, supplier_id, supplier_sku, barcode, normalized_name)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (pid, name, sid, sku, bc, norm),
        )
    if commit:
        conn.commit()


def list_product_aliases(
    conn: sqlite3.Connection,
    *,
    q: str = '',
    supplier_id: int | None = None,
    product_id: int | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    ensure_product_aliases_schema(conn, commit=False)
    pcols = _table_cols(conn, 'products')
    scols = _table_cols(conn, 'suppliers')
    code_expr = 'p.product_code' if 'product_code' in pcols else "''"
    p_join = 'LEFT JOIN products p ON p.id = a.product_id' if 'id' in pcols else ''
    s_join = 'LEFT JOIN suppliers s ON s.id = a.supplier_id' if 'id' in scols else ''
    s_name = 's.name' if 'name' in scols else "''"
    sql = f"""
        SELECT a.id, a.product_id, a.invoice_name, a.supplier_id,
               a.supplier_sku, a.barcode, a.normalized_name, a.created_at,
               COALESCE(p.name, '') AS product_name,
               COALESCE({code_expr}, '') AS product_code,
               COALESCE({s_name}, '') AS supplier_name
        FROM product_aliases a
        {p_join}
        {s_join}
        WHERE 1=1
    """
    params: list[Any] = []
    needle = (q or '').strip()
    if needle:
        like = f'%{needle}%'
        sql += """
            AND (
                a.invoice_name LIKE ?
                OR COALESCE(p.name, '') LIKE ?
                OR COALESCE({code}, '') LIKE ?
                OR COALESCE({sname}, '') LIKE ?
            )
        """.format(code=code_expr, sname=s_name)
        params.extend([like, like, like, like])
    if supplier_id:
        sql += ' AND a.supplier_id = ?'
        params.append(int(supplier_id))
    if product_id:
        sql += ' AND a.product_id = ?'
        params.append(int(product_id))
    sql += ' ORDER BY a.id DESC LIMIT ?'
    params.append(min(max(int(limit or 500), 1), 2000))
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(r) if isinstance(r, sqlite3.Row) else dict(zip(cols, r))
        out.append(d)
    return out


def update_product_alias(
    conn: sqlite3.Connection,
    alias_id: int,
    *,
    product_id: int | None = None,
    invoice_name: str | None = None,
    commit: bool = True,
) -> dict[str, Any] | None:
    ensure_product_aliases_schema(conn, commit=False)
    aid = int(alias_id or 0)
    if aid <= 0:
        raise ValueError('Thiếu liên kết')
    row = conn.execute('SELECT * FROM product_aliases WHERE id = ?', (aid,)).fetchone()
    if not row:
        return None
    sets: list[str] = []
    params: list[Any] = []
    if product_id is not None:
        pid = int(product_id or 0)
        if pid <= 0:
            raise ValueError('Sản phẩm không hợp lệ')
        exists = conn.execute('SELECT id FROM products WHERE id = ?', (pid,)).fetchone()
        if not exists:
            raise ValueError('Không tìm thấy hàng trong kho')
        sets.append('product_id = ?')
        params.append(pid)
    if invoice_name is not None:
        name = invoice_name.strip()
        if not name:
            raise ValueError('Tên trên hóa đơn trống')
        sets.append('invoice_name = ?')
        params.append(name)
        sets.append('normalized_name = ?')
        params.append(fold_name(name))
    if not sets:
        d = dict(row) if isinstance(row, sqlite3.Row) else None
        return d
    params.append(aid)
    try:
        conn.execute(f"UPDATE product_aliases SET {', '.join(sets)} WHERE id = ?", params)
    except sqlite3.IntegrityError as exc:
        raise ValueError('Tên trên hóa đơn này đã được liên kết rồi') from exc
    if commit:
        conn.commit()
    for item in list_product_aliases(conn, limit=2000):
        if int(item.get('id') or 0) == aid:
            return item
    return {'id': aid}


def delete_product_alias(conn: sqlite3.Connection, alias_id: int, *, commit: bool = True) -> bool:
    ensure_product_aliases_schema(conn, commit=False)
    aid = int(alias_id or 0)
    if aid <= 0:
        return False
    cur = conn.execute('DELETE FROM product_aliases WHERE id = ?', (aid,))
    if commit:
        conn.commit()
    return cur.rowcount > 0


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    except sqlite3.Error:
        return set()


def _row_dict(row) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if hasattr(row, 'keys'):
        return {k: row[k] for k in row.keys()}
    return {}


def _load_products(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    pcols = _table_cols(conn, 'products')
    icols = _table_cols(conn, 'inventory')
    if 'id' not in pcols or 'name' not in pcols:
        return []

    def pcol(name: str, default: str = "''"):
        return f'p.{name}' if name in pcols else f'{default} AS {name}'

    qty = 'COALESCE(i.quantity, 0) AS quantity' if icols else '0 AS quantity'
    cost = 'COALESCE(i.avg_cost, 0) AS avg_cost' if icols else '0 AS avg_cost'
    join_sql = 'LEFT JOIN inventory i ON i.product_id = p.id' if icols else ''
    sql = f"""
        SELECT p.id, p.name,
               {pcol('unit')}, {pcol('unit1')},
               {pcol('unit_ratio', '1')},
               {pcol('base_price', '0')}, {pcol('price', '0')},
               {pcol('product_code')}, {pcol('barcode')}, {pcol('barcode1')},
               {pcol('product_type', "'goods'")},
               {qty}, {cost}
        FROM products p
        {join_sql}
        LIMIT 8000
    """
    cur = conn.execute(sql)
    col_names = [d[0] for d in cur.description]
    rows = []
    for r in cur.fetchall():
        d = _row_dict(r) if isinstance(r, sqlite3.Row) or hasattr(r, 'keys') else dict(zip(col_names, r))
        pt = str(d.get('product_type') or 'goods').strip().lower()
        if pt in _SERVICE_TYPES:
            continue
        rows.append(d)
    return rows


def _load_aliases(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_product_aliases_schema(conn, commit=False)
    try:
        cur = conn.execute(
            """
            SELECT product_id, invoice_name, supplier_id, supplier_sku, barcode, normalized_name
            FROM product_aliases
            """
        )
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            if isinstance(r, sqlite3.Row):
                out.append(dict(r))
            else:
                out.append(dict(zip(cols, r)))
        return out
    except sqlite3.Error:
        return []


def _product_payload(p: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': p['id'],
        'name': p.get('name') or '',
        'unit': p.get('unit') or '',
        'unit1': p.get('unit1') or '',
        'unit_ratio': p.get('unit_ratio') or 1,
        'base_price': p.get('base_price') or 0,
        'price': p.get('price') or 0,
        'product_code': p.get('product_code') or '',
        'barcode': p.get('barcode') or '',
        'barcode1': p.get('barcode1') or '',
        'product_type': p.get('product_type') or 'goods',
        'quantity': p.get('quantity') or 0,
        'avg_cost': p.get('avg_cost') or 0,
    }


def _score_text(query: str, target: str, q_tokens: set[str], q_models: set[str]) -> tuple[int, list[str]]:
    """Trả (điểm 0-100, lý do)."""
    reasons: list[str] = []
    if not target:
        return 0, reasons
    qn = fold_name(query)
    tn = fold_name(target)
    if not qn or not tn:
        return 0, reasons
    if qn == tn:
        return 100, ['Trùng tên']
    if qn in tn or tn in qn:
        score = 88 if min(len(qn), len(tn)) >= 8 else 72
        reasons.append('Tên chứa nhau')
        return score, reasons

    t_models = extract_models(target)
    shared_models = q_models & t_models
    t_tokens = tokenize(target)
    shared = q_tokens & t_tokens
    score = 0
    if shared_models:
        score += min(40, 22 * len(shared_models))
        reasons.append('Model ' + ', '.join(sorted(shared_models)[:3]))
    if shared:
        union = q_tokens | t_tokens
        jacc = len(shared) / max(len(union), 1)
        score += int(round(50 * jacc))
        if len(shared) >= 2:
            score += 8
        reasons.append(f'{len(shared)} từ khóa')
    if score > 0 and (qn[:6] and tn.startswith(qn[:6])):
        score += 6
    return min(score, 89), reasons


def match_products(
    conn: sqlite3.Connection,
    *,
    invoice_name: str,
    supplier_id: int | None = None,
    barcode: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Trả danh sách ứng viên đã xếp hạng.

    auto_bind=True chỉ khi barcode / alias / tên / mã nội bộ khớp chắc.
    """
    ensure_product_aliases_schema(conn, commit=False)
    name = (invoice_name or '').strip()
    bc = (barcode or '').strip()
    if not name and not bc:
        return []

    products = _load_products(conn)
    by_id = {int(p['id']): p for p in products}
    aliases = _load_aliases(conn)
    alias_by_pid: dict[int, list[dict[str, Any]]] = {}
    for a in aliases:
        alias_by_pid.setdefault(int(a['product_id']), []).append(a)

    best: dict[int, dict[str, Any]] = {}

    def consider(pid: int, score: int, match_type: str, reasons: list[str], auto: bool) -> None:
        p = by_id.get(int(pid))
        if not p:
            return
        cur = best.get(int(pid))
        if cur and cur['score'] >= score:
            if reasons:
                merged = list(dict.fromkeys((cur.get('reasons') or []) + reasons))
                cur['reasons'] = merged[:4]
            return
        best[int(pid)] = {
            'score': int(score),
            'match_type': match_type,
            'reasons': reasons[:4],
            'auto_bind': bool(auto and score >= AUTO_SCORE),
            'product': _product_payload(p),
        }

    # 1) Barcode / GTIN
    if bc:
        bc_fold = fold_name(bc).replace(' ', '')
        for p in products:
            for field in ('barcode', 'barcode1', 'product_code'):
                val = str(p.get(field) or '').strip()
                if val and fold_name(val).replace(' ', '') == bc_fold:
                    consider(p['id'], 100, 'barcode', ['Mã vạch/mã hàng'], True)
                    break
        for a in aliases:
            aval = str(a.get('barcode') or '').strip()
            if aval and fold_name(aval).replace(' ', '') == bc_fold:
                consider(a['product_id'], 100, 'barcode', ['Mã vạch trên HĐ cũ'], True)

    if not name:
        ranked = sorted(best.values(), key=lambda x: -x['score'])
        return ranked[: max(1, min(int(limit or 5), 10))]

    q_fold = fold_name(name)
    q_tokens = tokenize(name)
    q_models = extract_models(name)
    sid = int(supplier_id) if supplier_id else 0

    # 2) Alias đúng NCC + tên HĐ
    for a in aliases:
        inv = (a.get('invoice_name') or '').strip()
        if fold_name(inv) != q_fold:
            continue
        a_sid = int(a.get('supplier_id') or 0)
        if a_sid == sid and sid:
            consider(a['product_id'], 100, 'alias', ['Tên HĐ đã liên kết NCC này'], True)
        else:
            consider(a['product_id'], 95, 'alias', ['Tên HĐ đã liên kết NCC khác'], True)

    # 3) Tên danh mục / mã nội bộ
    for p in products:
        code = str(p.get('product_code') or '').strip()
        reasons: list[str] = []
        score = 0
        auto = False
        mtype = 'keyword'
        if fold_name(p.get('name')) == q_fold:
            score, reasons, auto, mtype = 100, ['Trùng tên danh mục'], True, 'name'
        elif code and (fold_name(code) == q_fold or fold_name(code) in q_fold.split()):
            score, reasons, auto, mtype = 96, [f'Mã {code}'], True, 'code'
        else:
            s, r = _score_text(name, p.get('name') or '', q_tokens, q_models)
            score, reasons = s, r
            if code:
                cs, cr = _score_text(name, code, q_tokens, q_models)
                if cs > score:
                    score, reasons = cs, cr
            for a in alias_by_pid.get(int(p['id']), []):
                ascore, ar = _score_text(name, a.get('invoice_name') or '', q_tokens, q_models)
                if ascore > score:
                    score, reasons = ascore, (['Bí danh cũ'] + ar)[:4]
        if score >= SUGGEST_SCORE:
            consider(p['id'], score, mtype, reasons, auto)

    ranked = sorted(best.values(), key=lambda x: -x['score'])
    return ranked[: max(1, min(int(limit or 5), 10))]
