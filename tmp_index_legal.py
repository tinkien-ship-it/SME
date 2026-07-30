"""Quét text pháp lý đã chuyển đổi — lập mục lục theo Chương/Điều/Phụ lục."""
from __future__ import annotations

import os
import re
from pathlib import Path

TEXT_DIR = Path(r"C:\SME\data\legal\text")
INDEX_PATH = Path(r"C:\SME\data\legal\INDEX.md")

HEADING_RE = re.compile(
    r"(?m)^(?:\s*)((?:PHẦN|CHƯƠNG|Chương|ĐIỀU|Điều|PHỤ LỤC|Phụ lục|MỤC|Mục|TIỂU MỤC|"
    r"I{1,3}|IV|V|VI{0,3}|IX|X)\b[^\n]{0,120})"
)


def first_meaningful_lines(text: str, n: int = 12) -> list[str]:
    lines = []
    for raw in text.splitlines():
        s = raw.strip().strip("\x07")
        if not s:
            continue
        # skip form-feed / page markers noise
        if len(s) < 3:
            continue
        lines.append(s)
        if len(lines) >= n:
            break
    return lines


def extract_headings(text: str, limit: int = 80) -> list[str]:
    found = []
    seen = set()
    for m in HEADING_RE.finditer(text):
        h = re.sub(r"\s+", " ", m.group(1)).strip()
        if len(h) < 5 or h in seen:
            continue
        # filter noise like single Roman numerals alone later used mid-sentence
        if re.fullmatch(r"[IVX]+", h):
            continue
        seen.add(h)
        found.append(h)
        if len(found) >= limit:
            break
    return found


def main():
    files = sorted(TEXT_DIR.glob("*.txt"), key=lambda p: p.name)
    parts = ["# Mục lục tài liệu pháp lý SME (đã chuyển text)", ""]
    parts.append("Nguồn gốc: thư mục tài liệu dự án trên máy local. Bản `.doc`/`.docx` lưu tại `data/legal/tt99_2025` và `data/legal/tt58_2026`; bản text UTF-8 tại `data/legal/text/` để trợ lý AI và lập trình tham chiếu.")
    parts.append("")
    parts.append("| File text | Ký hiệu gốc | Dung lượng text | Ghi chú nhanh |")
    parts.append("|---|---|---:|---|")

    detail_blocks = []
    for f in files:
        raw = f.read_text(encoding="utf-8", errors="replace")
        # strip Word special chars
        text = raw.replace("\x07", "").replace("\xa0", " ")
        preview = first_meaningful_lines(text, 8)
        heads = extract_headings(text, 60)
        note = preview[0][:80] if preview else "(trống)"
        origin = f.name.replace("TT99_", "").replace(".txt", "")
        parts.append(f"| `{f.name}` | {origin} | {len(text):,} | {note} |")

        detail_blocks.append(f"## {f.name}")
        detail_blocks.append("")
        detail_blocks.append("**Mở đầu:**")
        for line in preview[:6]:
            detail_blocks.append(f"- {line[:160]}")
        detail_blocks.append("")
        if heads:
            detail_blocks.append("**Tiêu đề phát hiện (mẫu):**")
            for h in heads[:40]:
                detail_blocks.append(f"- {h[:140]}")
        else:
            detail_blocks.append("*(Không tách được nhiều tiêu đề — có thể là phụ lục dạng bảng/ảnh.)*")
        detail_blocks.append("")

    parts.append("")
    parts.extend(detail_blocks)
    INDEX_PATH.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {INDEX_PATH} ({INDEX_PATH.stat().st_size} bytes)")
    # quick TT58 keywords
    tt58 = (TEXT_DIR / "TT58_2026_TT-BTC.txt").read_text(encoding="utf-8", errors="replace")
    for kw in ["Điều 5", "Điều 10", "S1-DNSN", "B01-DNSN", "hiệu lực", "132/2018"]:
        print(f"TT58 '{kw}':", tt58.count(kw))


if __name__ == "__main__":
    main()
