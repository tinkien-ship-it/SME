from Services.hkd_menu import POS_HKD_MENU, MENU_SECTIONS

for s in MENU_SECTIONS:
    print(f"=== {s['label']} ({s['id']}) ===")
    for g in POS_HKD_MENU:
        if g.get('section') != s['id']:
            continue
        print(f"  [{g['id']}] {g['label']}")
        for it in g.get('items') or []:
            print(f"      - {it['label']}")
