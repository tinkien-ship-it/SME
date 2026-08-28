# -*- coding: utf-8 -*-
import re
import sys
from pathlib import Path

p = Path(sys.argv[1])
if p.suffix.lower() == '.docx':
    import zipfile
    import xml.etree.ElementTree as ET
    z = zipfile.ZipFile(p)
    root = ET.fromstring(z.read('word/document.xml'))
    paras = []
    for el in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'):
        texts = []
        for t in el.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'):
            if t.text:
                texts.append(t.text)
            if t.tail:
                texts.append(t.tail)
        line = ''.join(texts).strip()
        if line:
            paras.append(line)
    print('\n'.join(paras))
elif p.suffix.lower() == '.doc':
    try:
        import win32com.client
        word = win32com.client.Dispatch('Word.Application')
        word.Visible = False
        doc = word.Documents.Open(str(p.resolve()))
        print(doc.Content.Text)
        doc.Close(False)
        word.Quit()
    except Exception as e:
        print('ERR', e, file=sys.stderr)
        data = p.read_bytes()
        chunks = re.findall(rb'(?:[\x20-\x7e]|[\xc0-\xff]){6,}', data)
        print(b'\n'.join(chunks[:300]).decode('latin-1', errors='ignore'))
