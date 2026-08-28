# -*- coding: utf-8 -*-
"""Kiểm tra port dev server — tránh chạy trùng app.py."""
from __future__ import annotations

import socket


def dev_server_port_taken(port: int = 5000) -> bool:
    """True nếu đã có server lắng nghe (thường là app.py đang chạy)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(('127.0.0.1', int(port))) == 0
    except OSError:
        return False
