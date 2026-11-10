"""提取祁门 PDF 的一级章节大纲，输出到 stdout。"""
import pymupdf, re

PDF = r"D:\fangsheng.yunjunet.cn\祁门县全域土地综合整治工程实施方案_正文_v4_公文版_20261014.pdf"

d = pymupdf.open(PDF)
text = "\n".join(p.get_text("text") for p in d)
lines = text.split("\n")
h1 = re.compile(r"^\s*第[一二三四五六七八九十百]+章\s*\S.*")
seen = []
for ln in lines:
    s = ln.strip()
    if h1.match(s) and s not in seen:
        seen.append(s)
print("=== 祁门方案一级章节（H1）===")
for i, s in enumerate(seen, 1):
    print(f"{i}. {s}")
print(f"--- 共 {len(seen)} 章，总文本 {len(text)} 字 ---")
