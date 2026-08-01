from pathlib import Path
import zipfile
from xml.etree import ElementTree as ET
import json

base = Path(r"c:\Laptop Dell's Files\Dell's Laptop\Mắt Bảo")
docx = base / "API-Proxy-HDDT-v1.0.1.docx"
out = Path(r"C:\SME\_matbao_doc.txt")

with zipfile.ZipFile(docx) as z:
    xml = z.read("word/document.xml")
root = ET.fromstring(xml)
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
paras = []
for p_el in root.iter(f"{W}p"):
    parts = []
    for t in p_el.iter(f"{W}t"):
        if t.text:
            parts.append(t.text)
    line = "".join(parts).strip()
    if line:
        paras.append(line)
out.write_text("\n".join(paras), encoding="utf-8")
print("docx paras", len(paras))

# Postman endpoints
pc = json.loads((base / "API HDDT - PROXY.postman_collection.json").read_text(encoding="utf-8"))
names = []
for item in pc.get("item", []):
    name = item.get("name")
    req = item.get("request") or {}
    method = req.get("method")
    url = req.get("url")
    if isinstance(url, dict):
        raw = url.get("raw", "")
    else:
        raw = str(url or "")
    names.append(f"{method} {name} -> {raw}")
Path(r"C:\SME\_matbao_endpoints.txt").write_text("\n".join(names), encoding="utf-8")
print("endpoints", len(names))
for n in names:
    print(n)
