# -*- coding: utf-8 -*-
import re, sys
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = Path(r'C:\SME')

# Void functions in services
print('=== VOID FUNCTIONS IN Services/sme ===')
for p in sorted((ROOT/'Services'/'sme').glob('*.py')):
    t = p.read_text(encoding='utf-8', errors='replace')
    for m in re.finditer(r'^def (void_\w+)', t, re.M):
        print(f'  {p.name}: {m.group(1)}')

# Forms without void - check API void routes
print('\n=== API VOID ROUTES ===')
for rf in sorted((ROOT/'routes').glob('ketoan_sme*.py')):
    t = rf.read_text(encoding='utf-8', errors='replace')
    for m in re.finditer(r"@app\.route\(['\"]([^'\"]*void[^'\"]*)['\"]", t):
        print(f'  {rf.name}: {m.group(1)}')

# Print routes
print('\n=== PRINT (/in) ROUTES ===')
for rf in sorted((ROOT/'routes').glob('ketoan_sme*.py')):
    t = rf.read_text(encoding='utf-8', errors='replace')
    for m in re.finditer(r"@app\.route\(['\"]([^'\"]*/in[^'\"]*)['\"]", t):
        print(f'  {rf.name}: {m.group(1)}')

# Check cash_count_fx for print/void
print('\n=== cash_count_fx / 08b ===')
for p in list((ROOT/'routes').glob('ketoan_sme*.py')) + list((ROOT/'Services'/'sme').glob('*.py')) + list((ROOT/'templates'/'KeToanSME').glob('*fx*')):
    t = p.read_text(encoding='utf-8', errors='replace')
    if 'cash_count_fx' in t or '08b' in t or 'fx_count' in t.lower():
        for i, line in enumerate(t.splitlines(), 1):
            if any(k in line.lower() for k in ('cash_count_fx', '08b', 'void', 'print', '/in')):
                if 'cash' in line.lower() or '08b' in line or 'fx' in line.lower():
                    print(f'  {p.name}:{i}: {line.strip()[:140]}')

# CCDC page APIs used
print('\n=== CCDC dashboard fetch ===')
t = (ROOT/'templates'/'KeToanSME'/'dashboard_CCDC.html').read_text(encoding='utf-8')
for m in re.finditer(r"fetch\(['\"]([^'\"]+)", t):
    print(' ', m.group(1))
# TSCD dashboard
print('\n=== TSCD dashboard fetch ===')
t = (ROOT/'templates'/'KeToanSME'/'dashboard_TSCD.html').read_text(encoding='utf-8')
for m in re.finditer(r"fetch\(['\"]([^'\"]+)|url_for\(['\"]([^'\"]+)", t):
    print(' ', m.group(1) or m.group(2))

# Check require_sme_regime on menu page routes
print('\n=== MENU ENDPOINTS WITHOUT require_sme_regime nearby ===')
menu = (ROOT/'Services'/'sme_menu.py').read_text(encoding='utf-8')
eps = sorted(set(re.findall(r"['\"]endpoint['\"]\s*:\s*['\"]([^'\"]+)['\"]", menu)))
# build map def -> has decorator
for rf in (ROOT/'routes').glob('ketoan_sme*.py'):
    t = rf.read_text(encoding='utf-8', errors='replace')
    for ep in eps:
        if f'def {ep}(' in t:
            # get 300 chars before def
            idx = t.find(f'def {ep}(')
            pre = t[max(0, idx-250):idx]
            has = 'require_sme_regime' in pre
            if not has and ep.startswith('SME_'):
                print(f'  NO_REGIME: {ep} in {rf.name}')

# Multi-branch mentions
print('\n=== BRANCH / CHI NHANH ===')
for folder in ['routes', 'Services/sme', 'templates/KeToanSME']:
    for p in (ROOT/folder).rglob('*'):
        if p.suffix not in ('.py', '.html'): continue
        t = p.read_text(encoding='utf-8', errors='replace')
        if re.search(r'chi_nhanh|branch_id|multi.?branch|đơn vị phụ thuộc|chi nhánh', t, re.I):
            hits = len(re.findall(r'chi_nhanh|branch_id|multi.?branch|chi nhánh', t, re.I))
            print(f'  {p.relative_to(ROOT)}: {hits} hits')

# Templates that are very small (possible shells)
print('\n=== SMALL KeToanSME TEMPLATES (<40 lines) ===')
for p in sorted((ROOT/'templates'/'KeToanSME').glob('*.html')):
    lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
    if len(lines) < 40 and not p.name.startswith('_'):
        print(f'  {p.name}: {len(lines)} lines')

# Check if employees/attendance templates are HKD
print('\n=== employees_page / attendance_page targets ===')
for rf in (ROOT/'routes').glob('*.py'):
    t = rf.read_text(encoding='utf-8', errors='replace')
    for name in ('employees_page', 'attendance_page', 'bank_transactions_page', 'import_details_page', 'inventory_check'):
        if f'def {name}(' in t:
            idx = t.find(f'def {name}(')
            chunk = t[idx:idx+400]
            m = re.search(r"render_template\(\s*['\"]([^'\"]+)", chunk)
            print(f'  {name} -> {m.group(1) if m else "?"} ({rf.name})')

# Check void gaps by looking for create without void in same module
print('\n=== CREATE WITHOUT VOID IN SAME MODULE ===')
modules = {
    'cash_extras.py': ['create_temp_receipt', 'create_gold_sheet', 'list_cash_listings'],
    'stock_inspection.py': ['create_stock_inspection'],
    'material_remaining.py': ['create_material_remaining'],
    'fa_lifecycle.py': ['create_fa_handover', 'create_fa_upgrade', 'create_fa_revaluation', 'create_fa_inventory', 'void_disposal'],
    'loans_deposits.py': ['create', 'void'],
    'inventory_ops.py': ['create_stock_transfer', 'void_stock_count', 'void_material_allocation'],
    'cit.py': ['void'],
    'fx_revaluation.py': ['void'],
}
for mod, keys in modules.items():
    p = ROOT/'Services'/'sme'/mod
    if not p.exists():
        print(f'  MISSING FILE {mod}')
        continue
    t = p.read_text(encoding='utf-8', errors='replace')
    creates = re.findall(r'^def (create_\w+|post_\w+|run_\w+|save_\w+)', t, re.M)
    voids = re.findall(r'^def (void_\w+|cancel_\w+|reverse_\w+)', t, re.M)
    print(f'  {mod}: creates={creates} voids={voids}')

# SME MICRO differences
print('\n=== MICRO / TT58 SPECIFIC ===')
for p in list((ROOT/'Services'/'sme').glob('*.py')) + [ROOT/'Services'/'sme_menu.py']:
    t = p.read_text(encoding='utf-8', errors='replace')
    if 'MICRO' in t or 'TT58' in t or 'sme_tt58' in t:
        for i, line in enumerate(t.splitlines(), 1):
            if any(k in line for k in ('MICRO', 'TT58', 'sme_tt58', 'tt58')):
                print(f'  {p.name}:{i}: {line.strip()[:130]}')

print('\nDONE')
