# -*- coding: utf-8 -*-
"""Tải lại CSS/JS vendor local (Bootstrap, jQuery, Flatpickr, Font Awesome)."""
from __future__ import annotations

import os
import urllib.request

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'vendor')

FILES = [
    ('https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css',
     'bootstrap/bootstrap.min.css'),
    ('https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js',
     'bootstrap/bootstrap.bundle.min.js'),
    ('https://code.jquery.com/jquery-3.6.0.min.js', 'jquery/jquery-3.6.0.min.js'),
    ('https://cdn.jsdelivr.net/npm/flatpickr/dist/flatpickr.min.css', 'flatpickr/flatpickr.min.css'),
    ('https://cdn.jsdelivr.net/npm/flatpickr/dist/themes/material_blue.css', 'flatpickr/material_blue.css'),
    ('https://cdn.jsdelivr.net/npm/flatpickr/dist/flatpickr.min.js', 'flatpickr/flatpickr.min.js'),
    ('https://cdn.jsdelivr.net/npm/flatpickr/dist/l10n/vn.js', 'flatpickr/vn.js'),
    ('https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css',
     'fontawesome/css/all.min.css'),
    ('https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/webfonts/fa-solid-900.woff2',
     'fontawesome/webfonts/fa-solid-900.woff2'),
    ('https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/webfonts/fa-regular-400.woff2',
     'fontawesome/webfonts/fa-regular-400.woff2'),
    ('https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/webfonts/fa-brands-400.woff2',
     'fontawesome/webfonts/fa-brands-400.woff2'),
    ('https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/webfonts/fa-v4compatibility.woff2',
     'fontawesome/webfonts/fa-v4compatibility.woff2'),
]


def main():
    for url, rel in FILES:
        dest = os.path.join(BASE, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        print('GET', url)
        urllib.request.urlretrieve(url, dest)
        print(' ->', dest, os.path.getsize(dest), 'bytes')


if __name__ == '__main__':
    main()
