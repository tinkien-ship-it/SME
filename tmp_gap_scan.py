# -*- coding: utf-8 -*-
"""Read-only gap scan for SME menu/routes/templates/TT99."""
import re
import ast
from pathlib import Path

ROOT = Path(r'C:\SME')

# 1) Menu endpoints
menu_src = (ROOT / 'Services' / 'sme_menu.py').read_text(encoding='utf-8')
menu_eps = re.findall(r"['\"]endpoint['\"]\s*:\s*['\"]([^'\"]+)['\"]", menu_src)
quick = re.findall(r"\('([^']+)',\s*'[^']+',\s*'[^']+'\)", menu_src)
all_menu = sorted(set(menu_eps + quick))

# 2) Collect def names and endpoint= from routes
route_defs = set()
endpoint_kw = set()
route_files = list((ROOT / 'routes').glob('*.py'))
for rf in route_files:
    text = rf.read_text(encoding='utf-8', errors='replace')
    for m in re.finditer(r'@app\.route\([^\n]+\)\s*\n\s*def\s+(\w+)\s*\(', text):
        route_defs.add(m.group(1))
    for m in re.finditer(r"endpoint\s*=\s*['\"](\w+)['\"]", text):
        endpoint_kw.add(m.group(1))
    for m in re.finditer(r'def\s+(\w+)\s*\(', text):
        # only top-level-ish; keep all for matching
        if m.group(1).startswith('SME_') or m.group(1) in all_menu:
            route_defs.add(m.group(1))

# Broader: any def name matching menu
all_defs = set()
for rf in route_files:
    text = rf.read_text(encoding='utf-8', errors='replace')
    all_defs |= set(re.findall(r'def\s+(\w+)\s*\(', text))

missing = []
found = []
for ep in all_menu:
    if ep in all_defs or ep in endpoint_kw:
        found.append(ep)
    else:
        missing.append(ep)

print('=== MENU ENDPOINTS (%d unique) ===' % len(all_menu))
for ep in all_menu:
    status = 'OK' if ep not in missing else 'MISSING'
    print(f'  [{status}] {ep}')

print('\n=== MISSING ROUTES ===')
for ep in missing:
    print(' ', ep)

# 3) Map SME_ routes to templates
print('\n=== SME PAGE ROUTES -> TEMPLATES ===')
for rf in sorted((ROOT / 'routes').glob('ketoan_sme*.py')):
    text = rf.read_text(encoding='utf-8', errors='replace')
    # find def SME_* with nearby render_template
    for m in re.finditer(
        r"def\s+(SME_\w+)\s*\([^)]*\):\s*(?:\n(?:[ \t].*)?)*?render_template\(\s*['\"]([^'\"]+)['\"]",
        text,
        re.M,
    ):
        print(f'  {m.group(1)} -> {m.group(2)}  ({rf.name})')

# Also collect all render_template KeToanSME
print('\n=== ALL KeToanSME render_template ===')
for rf in route_files:
    text = rf.read_text(encoding='utf-8', errors='replace')
    for m in re.finditer(r"render_template\(\s*['\"](KeToanSME/[^'\"]+)['\"]", text):
        print(f'  {rf.name}: {m.group(1)}')

# 4) TT99 form codes in codebase
print('\n=== TT99 FORM CODE MENTIONS ===')
codes = [
    '01-LĐTL','02-LĐTL','03-LĐTL','04-LĐTL','05-LĐTL','06-LĐTL','07-LĐTL','08-LĐTL',
    '01-VT','02-VT','03-VT','04-VT','05-VT','06-VT','07-VT',
    '01-BH','02-BH',
    '01-TT','02-TT','03-TT','04-TT','05-TT','06-TT','07-TT','08a-TT','08b-TT','08-TT','09-TT',
    '01-TSCĐ','02-TSCĐ','03-TSCĐ','04-TSCĐ','05-TSCĐ','06-TSCĐ',
    '01-TSCD','02-TSCD','03-TSCD','04-TSCD','05-TSCD','06-TSCD',
]
for code in codes:
    hits = []
    for p in list((ROOT/'routes').glob('ketoan_sme*.py')) + list((ROOT/'Services'/'sme').glob('*.py')) + list((ROOT/'templates'/'KeToanSME').glob('*.html')) + [ROOT/'Services'/'sme_menu.py']:
        t = p.read_text(encoding='utf-8', errors='replace')
        if code in t or code.replace('Đ','D') in t:
            hits.append(p.name)
    if hits:
        print(f'  {code}: {sorted(set(hits))[:12]}')
    else:
        print(f'  {code}: NONE')

# 5) void/print coverage for form-related APIs
print('\n=== VOID / PRINT ROUTES (SME) ===')
for rf in sorted((ROOT/'routes').glob('ketoan_sme*.py')):
    text = rf.read_text(encoding='utf-8', errors='replace')
    for m in re.finditer(r"@app\.route\(['\"]([^'\"]+)['\"]", text):
        path = m.group(1)
        if any(x in path.lower() for x in ('void', '/in', 'print', 'export')):
            # get following def
            idx = m.end()
            dm = re.search(r'def\s+(\w+)', text[idx:idx+200])
            print(f'  {rf.name}: {path} -> {dm.group(1) if dm else "?"}')

# 6) HKD refs in KeToanSME templates
print('\n=== HKD REFS IN KeToanSME TEMPLATES ===')
for p in (ROOT/'templates'/'KeToanSME').rglob('*.html'):
    t = p.read_text(encoding='utf-8', errors='replace')
    for i, line in enumerate(t.splitlines(), 1):
        if re.search(r'hkd|KeToanHKD|/ketoan_hkd|url_for\(\s*[\'\"](?!SME_)', line, re.I):
            if 'url_for' in line or 'href=' in line or 'fetch(' in line or 'hkd' in line.lower() or 'KeToanHKD' in line:
                # filter noise
                if any(k in line for k in ('hkd', 'HKD', 'KeToanHKD', 'ketoan_hkd', 'url_for')):
                    print(f'  {p.name}:{i}: {line.strip()[:160]}')

print('\nDONE')
