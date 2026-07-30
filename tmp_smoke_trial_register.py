# -*- coding: utf-8 -*-
"""Smoke: dang ky dung thu qua OAuth redirect khong con bao 'Thieu ma xac thuc Google'."""
import os
import sys

os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
sys.stdout.reconfigure(encoding='utf-8')

from datetime import datetime, timedelta

import app as app_module

flask_app = app_module.app
flask_app.config['TESTING'] = True

client = flask_app.test_client()

# 1. Khong co credential + khong co session -> loi ro rang, co retry_url
res = client.post('/api/trial/register', json={'oauth_register': True, 'phone': '0900000001', 'business_name': 'Test'})
data = res.get_json()
print('no-session ->', res.status_code, data.get('error'), data.get('retry_url'))
assert res.status_code == 400
assert 'het han' in data['error'].lower().replace('ế', 'e').replace('ạ', 'a') or 'hết hạn' in data['error']
assert data.get('retry_url')

# 2. Session het han (>60 phut) -> van bao het han, khong roi vao verify credential rong
with client.session_transaction() as sess:
    sess['trial_google'] = {
        'email': 'stale@gmail.com',
        'verified_at': (datetime.now() - timedelta(hours=3)).isoformat(timespec='seconds'),
    }
res = client.post('/api/trial/register', json={'oauth_register': True, 'phone': '0900000002', 'business_name': 'Test'})
data = res.get_json()
print('stale-session ->', res.status_code, data.get('error'))
assert 'Google' in data['error'] and 'credential' not in data['error']

# 3. Session hop le -> di tiep den buoc validate SDT (khong con loi xac thuc Google)
with client.session_transaction() as sess:
    sess['trial_google'] = {
        'email': 'newuser@gmail.com',
        'name': 'New User',
        'verified_at': datetime.now().isoformat(timespec='seconds'),
    }
res = client.post('/api/trial/register', json={'oauth_register': True, 'phone': 'abc', 'business_name': 'Test'})
data = res.get_json()
print('valid-session, bad phone ->', res.status_code, data.get('error'))
assert 'dien thoai' in data['error'].lower() or 'điện thoại' in data['error']

# 4. Trang login khong con "an" session trial_google
with client.session_transaction() as sess:
    sess['trial_google'] = {
        'email': 'keepme@gmail.com',
        'verified_at': datetime.now().isoformat(timespec='seconds'),
    }
res = client.get('/login?trial_google=1')
print('GET /login?trial_google=1 ->', res.status_code)
assert res.status_code == 200
with client.session_transaction() as sess:
    kept = (sess.get('trial_google') or {}).get('email')
print('session sau khi render login ->', kept)
assert kept == 'keepme@gmail.com'

print('OK smoke trial register')
