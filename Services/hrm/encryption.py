# -*- coding: utf-8 -*-
"""Field-level encryption cho lương / STK (Fernet hoặc XOR fallback)."""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional

_ENV_KEY = 'SME_HRM_FIELD_KEY'


def _fernet():
    key = (os.environ.get(_ENV_KEY) or '').strip()
    if not key:
        # derive ổn định từ SECRET_KEY app nếu có
        secret = (os.environ.get('SECRET_KEY') or 'sme-hrm-dev-key').encode('utf-8')
        digest = hashlib.sha256(secret).digest()
        key = base64.urlsafe_b64encode(digest)
    else:
        # pad/hash to 32 bytes urlsafe
        raw = hashlib.sha256(key.encode('utf-8')).digest()
        key = base64.urlsafe_b64encode(raw)
    try:
        from cryptography.fernet import Fernet
        return Fernet(key)
    except Exception:
        return None


def encrypt_text(plain: str | None) -> str:
    if plain is None or plain == '':
        return ''
    text = str(plain)
    f = _fernet()
    if f:
        return 'enc:' + f.encrypt(text.encode('utf-8')).decode('ascii')
    # weak fallback (dev only)
    mixed = base64.b64encode(text.encode('utf-8')).decode('ascii')
    return 'b64:' + mixed


def decrypt_text(blob: str | None) -> str:
    if not blob:
        return ''
    s = str(blob)
    if s.startswith('enc:'):
        f = _fernet()
        if not f:
            return ''
        try:
            return f.decrypt(s[4:].encode('ascii')).decode('utf-8')
        except Exception:
            return ''
    if s.startswith('b64:'):
        try:
            return base64.b64decode(s[4:].encode('ascii')).decode('utf-8')
        except Exception:
            return ''
    return s


def mask_bank_account(acc: str | None) -> str:
    t = decrypt_text(acc) if acc and str(acc).startswith(('enc:', 'b64:')) else (acc or '')
    t = str(t)
    if len(t) <= 4:
        return '****'
    return '*' * (len(t) - 4) + t[-4:]


def store_employee_bank(conn, employee_id: int, account: str, *, commit: bool = True) -> None:
    from Services.hrm.schema import ensure_hrm_schema
    from db_utils import sqlite_commit
    ensure_hrm_schema(conn)
    enc = encrypt_text(account)
    conn.execute(
        'UPDATE employees SET bank_account_enc=?, bank_account=? WHERE id=?',
        (enc, mask_bank_account(account), int(employee_id)),
    )
    if commit:
        sqlite_commit(conn, label='hrm_bank_enc')
