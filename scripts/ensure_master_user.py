#!/usr/bin/env python3
"""Tạo / đặt lại tài khoản master trên database.db (main registry).

Master đăng nhập bằng bảng users trong database.db (không nằm trong tenants/*.db).
Khi main DB hỏng / cứu rỗng, user master mất → không đăng nhập được.

    python scripts/ensure_master_user.py
    python scripts/ensure_master_user.py --apply --password 'MatKhauMoi'

Biến môi trường: MASTER_USERNAME, MASTER_PASSWORD, MASTER_EMAIL, MASTER_FULL_NAME
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, '.env'))
except Exception:
    pass

from Services.master_account import (  # noqa: E402
    ensure_master,
    ensure_users_table,
    list_masters,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--main', default=os.path.join(ROOT, 'database.db'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--username', default=os.environ.get('MASTER_USERNAME', 'master'))
    parser.add_argument('--password', default=os.environ.get('MASTER_PASSWORD', ''))
    parser.add_argument('--email', default=os.environ.get('MASTER_EMAIL', ''))
    parser.add_argument('--full-name', default=os.environ.get('MASTER_FULL_NAME', 'Master'))
    parser.add_argument('--keep-2fa', action='store_true',
                        help='(tương thích cũ) giữ 2FA — giờ là mặc định')
    parser.add_argument('--disable-2fa', action='store_true',
                        help='Tắt 2FA tạm (khôi phục khẩn cấp)')
    parser.add_argument('--reset-totp', action='store_true',
                        help='Xóa secret Authenticator để lần đăng nhập sau hiện lại QR')
    args = parser.parse_args()

    if not os.path.exists(args.main):
        print('Không thấy %s' % args.main)
        return 1

    conn = sqlite3.connect(args.main)
    conn.row_factory = sqlite3.Row

    schema_changes = ensure_users_table(conn)
    if schema_changes:
        print('Schema users:', ', '.join(schema_changes))
        conn.commit()

    masters = list_masters(conn)
    print('Registry DB : %s' % args.main)
    print('Master hiện có: %d' % len(masters))
    for m in masters:
        print('  - id=%s username=%s email=%s 2fa=%s' % (
            m['id'], m['username'], m.get('email') or '-', m['is_2fa_enabled']))

    same = conn.execute(
        "SELECT id, role FROM users WHERE username = ?", (args.username,)
    ).fetchone()
    if same:
        print('Username %r: id=%s role=%s' % (args.username, same['id'], same['role']))
    else:
        print('Username %r: chưa có' % args.username)

    if not args.apply:
        print('\nChưa ghi gì. Chạy với --apply --password ... để tạo/reset.')
        conn.close()
        return 0

    if not args.password:
        print('Thiếu mật khẩu. Truyền --password hoặc set MASTER_PASSWORD trong .env')
        conn.close()
        return 2

    action = ensure_master(
        conn,
        username=args.username.strip(),
        password=args.password,
        email=(args.email or '').strip(),
        full_name=(args.full_name or 'Master').strip(),
        disable_2fa=bool(args.disable_2fa),
        force_password=True,
        reset_totp=bool(args.reset_totp),
    )
    conn.commit()
    if args.disable_2fa:
        tfa_msg = 'TẮT'
    elif args.reset_totp:
        tfa_msg = 'bật — sẽ hiện QR mới'
    else:
        tfa_msg = 'bật (giữ Authenticator nếu đã có)'
    print('\nKết quả: %s user %r (2FA %s)' % (action, args.username, tfa_msg))
    print('Đăng nhập tại /login với username=%s' % args.username)
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
