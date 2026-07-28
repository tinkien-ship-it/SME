import re
import sys
from pathlib import Path

import requests
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'}

url = 'https://news.google.com/rss/search?q=site:gdt.gov.vn+ho+kinh+doanh&hl=vi&gl=VN&ceid=VN:vi'
r = requests.get(url, timeout=25, headers=headers)
root = ET.fromstring(r.content)
lines = []
for item in list(root.iter('item'))[:5]:
    title = item.findtext('title') or ''
    link = item.findtext('link') or ''
    desc = item.findtext('description') or ''
    src = re.search(r'href="([^"]+)"', desc)
    lines.append(f'TITLE: {title}\nLINK: {link}\nSRC: {src.group(1) if src else None}\n---\n')

(ROOT / 'scripts' / '_rss_out.txt').write_text(''.join(lines), encoding='utf-8')
