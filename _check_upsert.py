# -*- coding: utf-8 -*-
from pathlib import Path
p = Path(r"C:\SME\templates\KeToanSME\import_sme.html")
t = p.read_text(encoding="utf-8")
needle = "const upsertRes = await fetch('/api/products/upsert'"
i = t.find(needle)
print("found", i)
out = Path(r"C:\SME\_snip.txt")
out.write_text(t[i:i+1200] if i >= 0 else "NOT FOUND", encoding="utf-8")
print("wrote snip")
# also check LINE_TYPE_LABEL
print("LABEL def", "LINE_TYPE_LABEL =" in t)
print("LABEL usage count", t.count("LINE_TYPE_LABEL"))
