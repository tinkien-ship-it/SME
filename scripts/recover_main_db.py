#!/usr/bin/env python3
"""Cứu dữ liệu từ SQLite bị "database disk image is malformed".

Dùng khi sqlite3 CLI không có lệnh .recover (thiếu sqlite_dbpage).
Script mở file hỏng ở chế độ read-only, dựng lại schema sang file mới rồi
copy dữ liệu từng bảng — bảng/dòng nào đọc được thì giữ, hỏng thì bỏ qua.

    python scripts/recover_main_db.py                       # database.db -> database_recovered.db
    python scripts/recover_main_db.py <nguon.db> <dich.db>
"""
import os
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# Bảng quan trọng nhất — cứu trước để chắc chắn có dữ liệu đăng nhập
PRIORITY_TABLES = (
    'tenants',
    'user_tenant_mapping',
    'settings',
    'users',
    'login_history',
    'audit_log',
)


def _open_ro(path):
    uri = 'file:{}?mode=ro'.format(path.replace('?', '%3f').replace('#', '%23'))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    # TEXT hỏng encoding vẫn đọc được thay vì raise UnicodeDecodeError
    conn.text_factory = lambda raw: raw.decode('utf-8', 'replace')
    return conn


def _schema_items(src):
    """Trả (tables, others) từ sqlite_master; chịu được lỗi đọc."""
    tables, others = [], []
    try:
        rows = src.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        print('  ! Không đọc được sqlite_master: {}'.format(exc))
        return tables, others
    for row in rows:
        if row['type'] == 'table':
            tables.append((row['name'], row['sql']))
        else:
            others.append((row['type'], row['name'], row['sql']))
    return tables, others


def _copy_table(src, dst, name):
    """Copy dữ liệu 1 bảng, bỏ qua dòng lỗi. Trả (số dòng copy, số dòng lỗi)."""
    try:
        cols = [r[1] for r in src.execute('PRAGMA table_info("{}")'.format(name))]
    except sqlite3.DatabaseError as exc:
        print('  ! {}: không đọc được cấu trúc ({})'.format(name, exc))
        return 0, -1
    if not cols:
        return 0, 0

    placeholders = ','.join('?' * len(cols))
    col_list = ','.join('"{}"'.format(c) for c in cols)
    insert = 'INSERT OR IGNORE INTO "{}" ({}) VALUES ({})'.format(name, col_list, placeholders)

    copied = failed = 0
    last_error = None
    try:
        cursor = src.execute('SELECT {} FROM "{}"'.format(col_list, name))
    except sqlite3.DatabaseError as exc:
        print('  ! {}: không đọc được dữ liệu ({})'.format(name, exc))
        return 0, -1
    while True:
        try:
            row = cursor.fetchone()
        except sqlite3.DatabaseError as exc:
            last_error = exc
            failed += 1
            break
        if row is None:
            break
        try:
            dst.execute(insert, tuple(row))
            copied += 1
        except sqlite3.DatabaseError as exc:
            last_error = exc
            failed += 1
    if failed and last_error is not None:
        print('  ! {}: {}'.format(name, last_error))
    return copied, failed


def recover(src_path, dst_path):
    if not os.path.exists(src_path):
        print('Không thấy file nguồn: {}'.format(src_path))
        return 1
    if os.path.exists(dst_path):
        print('File đích đã tồn tại, hãy xóa hoặc đổi tên: {}'.format(dst_path))
        return 1

    print('Nguồn : {}'.format(src_path))
    print('Đích  : {}'.format(dst_path))

    src = _open_ro(src_path)
    dst = sqlite3.connect(dst_path)
    dst.execute('PRAGMA journal_mode=DELETE')

    tables, others = _schema_items(src)
    if not tables:
        print('Không dựng lại được bảng nào — file hỏng quá nặng, hãy dùng bản backup.')
        src.close()
        dst.close()
        return 2

    print('\n--- Dựng schema ({} bảng) ---'.format(len(tables)))
    created = []
    for name, sql in tables:
        try:
            dst.execute(sql)
            created.append(name)
        except sqlite3.DatabaseError as exc:
            print('  ! CREATE {} lỗi: {}'.format(name, exc))
    dst.commit()

    order = [t for t in PRIORITY_TABLES if t in created]
    order += [t for t in created if t not in order]

    print('\n--- Copy dữ liệu ---')
    total_rows = broken = 0
    for name in order:
        copied, failed = _copy_table(src, dst, name)
        total_rows += copied
        flag = ''
        if failed:
            broken += 1
            flag = '  (mất dữ liệu)' if failed > 0 else '  (bỏ qua)'
        star = '*' if name in PRIORITY_TABLES else ' '
        print('  {} {:<34} {:>7} dòng{}'.format(star, name, copied, flag))
        dst.commit()

    print('\n--- Dựng index / view / trigger ---')
    for kind, name, sql in others:
        try:
            dst.execute(sql)
        except sqlite3.DatabaseError as exc:
            print('  ! {} {} lỗi: {}'.format(kind, name, exc))
    dst.commit()

    check = dst.execute('PRAGMA integrity_check').fetchone()[0]
    print('\nintegrity_check file mới: {}'.format(check))
    print('Tổng số dòng cứu được  : {}'.format(total_rows))
    if broken:
        print('Số bảng bị mất dữ liệu : {}'.format(broken))

    print('\n--- Bảng quan trọng ---')
    for name in PRIORITY_TABLES:
        if name not in created:
            print('  {:<22} KHÔNG CÓ trong file hỏng'.format(name))
            continue
        try:
            count = dst.execute('SELECT COUNT(*) FROM "{}"'.format(name)).fetchone()[0]
        except sqlite3.DatabaseError:
            count = '?'
        print('  {:<22} {} dòng'.format(name, count))

    src.close()
    dst.close()
    return 0 if check == 'ok' else 3


if __name__ == '__main__':
    source = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'database.db')
    target = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, 'database_recovered.db')
    sys.exit(recover(source, target))
