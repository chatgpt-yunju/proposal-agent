"""简单 Markdown 块解析：将方案文本解析为标题/表格/列表/引用/段落块，供 Word/PDF 渲染。"""
import re
from dataclasses import dataclass, field
from typing import List


@dataclass
class Block:
    kind: str  # h1..h7 table list quote para
    text: str = ""
    rows: List[List[str]] = field(default_factory=list)


_SEP_ROW = re.compile(r"^\|[\s:\-\|]+\|?$")


def _split_row(line: str) -> List[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_markdown(md: str) -> List[Block]:
    blocks: List[Block] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        # 表格：连续的 | 开头行，分隔行（| --- |）跳过，首行为表头
        if line.startswith("|") and line.count("|") >= 2:
            rows: List[List[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cur = lines[i].strip()
                if not _SEP_ROW.match(cur):
                    rows.append(_split_row(cur))
                i += 1
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                blocks.append(Block("table", rows=rows))
            continue
        m = re.match(r"^(#{1,7})\s+(.+)$", line)
        if m:
            blocks.append(Block(f"h{len(m.group(1))}", m.group(2).strip()))
        elif line.startswith("> "):
            blocks.append(Block("quote", text=line[2:].strip()))
        elif line.startswith("- ") or line.startswith("* "):
            blocks.append(Block("list", text=line[2:].strip()))
        else:
            blocks.append(Block("para", text=line))
        i += 1
    return blocks
