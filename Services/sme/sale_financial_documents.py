"""Safety-net chứng từ tài chính phát sinh từ sale completed."""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Any
from db.dialect import table_exists
from db.schema_helpers import table_cols
from Services.sme.payment_method import normalize_sale_payment_method, payment_method_label
logger = logging.getLogger(__name__)

def _row_dict(row) -> dict:
    if row is None: return {}
    if isinstance(row, dict): return dict(row)
    if hasattr(row, "keys"): return {k: row[k] for k in row.keys()}
    return {}

def _first_existing(cols: set[str], *names: str) -> str | None:
    lower={str(c).lower():str(c) for c in cols}
    for name in names:
        if name.lower() in lower: return lower[name.lower()]
    return None

def _table_required_columns(conn, table: str) -> set[str]:
    try: rows=conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception: return set()
    required=set()
    for row in rows:
        if hasattr(row,"keys"):
            name=row["name"]; notnull=int(row["notnull"] or 0); default=row["dflt_value"]; pk=int(row["pk"] or 0)
        else:
            name=row[1]; notnull=int(row[3] or 0); default=row[4]; pk=int(row[5] or 0)
        if notnull and default is None and not pk: required.add(str(name))
    return required

def _existing_by_sale_id(conn, table: str, sale_id: int) -> bool:
    cols=set(table_cols(conn, table))
    ref_col=_first_existing(cols,"sale_id","ref_id","document_id","source_id","order_id")
    if not ref_col:
        raise RuntimeError(f"{table}: không có cột tham chiếu sale")
    return bool(conn.execute(f"SELECT 1 FROM {table} WHERE {ref_col} = ? LIMIT 1",(sale_id,)).fetchone())

def _insert_adaptive(conn, *, table: str, sale: dict, payment_code: str, created_by: str | None, doc_kind: str) -> int | None:
    cols=set(table_cols(conn, table))
    if not cols: raise RuntimeError(f"{table}: không đọc được schema")
    sale_id=int(sale["id"]); sale_date=str(sale.get("date") or sale.get("created_at") or datetime.now().strftime("%Y-%m-%d"))[:10]
    amount=float(sale.get("total_amount") or 0); sale_no=str(sale.get("sale_no") or sale_id)
    customer_name=str(sale.get("customer_name") or sale.get("company_name") or sale.get("customer") or "").strip(); customer_id=sale.get("customer_id")
    label=payment_method_label(payment_code)
    if doc_kind=="cash_receipt": doc_no=f"PT-{sale_id}"; reason=f"Thu tiền bán hàng {sale_no} - {payment_method_label(payment_code)}"
    elif doc_kind=="bank_receipt": doc_no=f"BC-{sale_id}"; reason=f"Thu tiền chuyển khoản bán hàng {sale_no}"
    elif doc_kind=="receivable": doc_no=f"CN-{sale_id}"; reason=f"Công nợ bán hàng {sale_no}"
    else: raise ValueError(f"doc_kind không hỗ trợ: {doc_kind}")
    semantic=[
        (("sale_id","ref_id","document_id","source_id","order_id"),sale_id),
        (("date","receipt_date","document_date","posting_date","debt_date"),sale_date),
        (("amount","total_amount","so_tien","debt_amount","value"),amount),
        (("receipt_no","voucher_no","document_no","debt_no","code","number"),doc_no),
        (("reason","description","content","note","dien_giai"),reason),
        (("customer_id","partner_id"),customer_id),
        (("customer_name","payer_name","partner_name","nguoi_nop"),customer_name),
        (("payment_method","payment_code","method"),payment_code),
        (("payment_method_name","payment_label"),label),
        (("sale_no","reference_no","reference_document"),sale_no),
        (("status",),"completed"), (("created_by","user_name","creator"),created_by),
        (("created_at",),datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    values={}
    for aliases,value in semantic:
        col=_first_existing(cols,*aliases)
        if col and col not in values and value is not None: values[col]=value
    missing=sorted(c for c in _table_required_columns(conn,table) if c not in values)
    if missing: raise RuntimeError(f"{table}: thiếu mapping required columns: {', '.join(missing)}")
    if not values: raise RuntimeError(f"{table}: không có cột tương thích để insert")
    cols_sql=", ".join(values); ph=", ".join("?" for _ in values)
    cur=conn.execute(f"INSERT INTO {table} ({cols_sql}) VALUES ({ph})",tuple(values.values()))
    return getattr(cur,"lastrowid",None)

def ensure_sale_financial_documents(conn, sale_id: int, *, created_by: str | None=None) -> dict:
    sale=_row_dict(conn.execute("SELECT * FROM sale WHERE id = ?",(sale_id,)).fetchone())
    if not sale: raise ValueError(f"Không tìm thấy sale #{sale_id}")
    if str(sale.get("status") or "").strip().lower()!="completed":
        return {"sale_id":sale_id,"created":[],"existing":[],"skipped":["sale_not_completed"],"errors":[]}
    payment_code=normalize_sale_payment_method(sale.get("payment_method"))
    created=[]; existing=[]; skipped=[]; errors=[]; targets=[]
    if payment_code in ("111", "112"):
        # Phiếu thu dùng chung cho tiền mặt và chuyển khoản.
        # payment_method/payment_method_name trên phiếu phân biệt nguồn thu.
        targets.append(("phieu_thu", "cash_receipt"))
    elif payment_code=="131":
        # Công nợ chưa phải khoản đã thu nên không tạo Phiếu thu.
        targets.append(("cong_no","receivable"))
    for table,kind in targets:
        try:
            if not table_exists(conn,table): errors.append(f"{table}: table_missing"); continue
            if _existing_by_sale_id(conn,table,sale_id): existing.append(table); continue
            _insert_adaptive(conn,table=table,sale=sale,payment_code=payment_code,created_by=created_by,doc_kind=kind); created.append(table)
        except Exception as exc:
            errors.append(f"{table}: {exc}")
            logger.warning("sale_document reconcile sale=%s table=%s: %s",sale_id,table,exc,exc_info=True)
    return {"sale_id":sale_id,"payment_method":payment_code,"created":created,"existing":existing,"skipped":skipped,"errors":errors}

def reconcile_completed_sale_documents(conn, *, batch_size: int=100, created_by: str="scheduler_reconcile") -> dict:
    try: batch_size=max(1,min(int(batch_size or 100),1000))
    except (TypeError,ValueError): batch_size=100
    if not table_exists(conn,"sale"):
        return {"scanned":0,"created":0,"existing":0,"errors":0,"details":[],"reason":"sale_table_missing"}
    rows=conn.execute("SELECT id FROM sale WHERE LOWER(TRIM(COALESCE(status, '')))='completed' ORDER BY id ASC LIMIT ?",(batch_size,)).fetchall()
    tc=te=terr=0; details=[]
    for row in rows:
        sid=int(row["id"] if hasattr(row,"keys") else row[0])
        result=ensure_sale_financial_documents(conn,sid,created_by=created_by)
        tc+=len(result.get("created") or []); te+=len(result.get("existing") or []); terr+=len(result.get("errors") or [])
        if result.get("created") or result.get("errors") or result.get("skipped"): details.append(result)
    conn.commit()
    return {"scanned":len(rows),"created":tc,"existing":te,"errors":terr,"details":details[:50]}
