"""文档解析与切块：txt/md/csv → 文本；pdf → PyMuPDF 逐页；docx → python-docx。"""
import re
import uuid
from pathlib import Path
from typing import List

import pymupdf

from .models import Chunk, Scope

MAX_CHARS = 500  # 每块目标长度


def _make_chunks(text: str, base_title: str, doc_id: str, source_prefix: str, scope: Scope) -> List[Chunk]:
    raw_blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    # 将短标题/标题行与其后内容合并，避免“标题-内容”碎片化稀释召回质量
    blocks: List[str] = []
    i = 0
    while i < len(raw_blocks):
        b = raw_blocks[i]
        is_heading = bool(re.match(r"^#{1,6}\s", b)) or len(b) <= 12
        if is_heading and i + 1 < len(raw_blocks):
            blocks.append(b + "\n" + raw_blocks[i + 1])
            i += 2
        else:
            blocks.append(b)
            i += 1

    chunks: List[Chunk] = []
    for blk in blocks:
        if len(blk) <= MAX_CHARS:
            chunks.append(Chunk(id=str(uuid.uuid4()), text=blk, scope=scope,
                                doc_id=doc_id, source=source_prefix, title=base_title))
            continue
        start = 0
        while start < len(blk):
            end = min(start + MAX_CHARS, len(blk))
            cut = blk.rfind("。", start + MAX_CHARS // 2, end)
            if cut != -1:
                end = cut + 1
            seg = blk[start:end].strip()
            if seg:
                chunks.append(Chunk(id=str(uuid.uuid4()), text=seg, scope=scope,
                                    doc_id=doc_id, source=source_prefix, title=base_title))
            start = end
    return chunks


def chunk_file(path: str, scope: Scope, doc_id: str, source_label: str | None = None) -> List[Chunk]:
    p = Path(path)
    ext = p.suffix.lower()
    name = source_label or p.name
    if ext in (".txt", ".md", ".markdown", ".csv"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        return _make_chunks(text, name, doc_id, name, scope)
    if ext == ".pdf":
        out: List[Chunk] = []
        with pymupdf.open(str(p)) as doc:
            for pno in range(doc.page_count):
                text = doc[pno].get_text("text")
                if text.strip():
                    out.extend(_make_chunks(text, name, doc_id, f"{name}:第{pno + 1}页", scope))
        if not out:
            raise ValueError(f"PDF 未提取到文本（可能为扫描件，需 OCR 增强）：{name}")
        return out
    if ext == ".docx":
        from docx import Document
        doc = Document(str(p))
        text = "\n\n".join(para.text for para in doc.paragraphs if para.text.strip())
        return _make_chunks(text, name, doc_id, name, scope)
    if ext == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(str(p), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if cells:
                    rows.append(" ".join(cells))
            if rows:
                parts.append(f"【表：{ws.title}】\n" + "\n".join(rows))
        text = "\n\n".join(parts)
        if not text.strip():
            raise ValueError(f"XLSX 未提取到内容：{name}")
        return _make_chunks(text, name, doc_id, name, scope)
    if ext == ".xls":
        import xlrd
        wb = xlrd.open_workbook(str(p))
        parts = []
        for sheet in wb.sheets():
            rows = []
            for row_idx in range(sheet.nrows):
                row = sheet.row_values(row_idx)
                cells = [str(c).strip() for c in row if c and str(c).strip()]
                if cells:
                    rows.append(" ".join(cells))
            if rows:
                parts.append(f"【表：{sheet.name}】\n" + "\n".join(rows))
        text = "\n\n".join(parts)
        if not text.strip():
            raise ValueError(f"XLS 未提取到内容：{name}")
        return _make_chunks(text, name, doc_id, name, scope)
    if ext == ".pptx":
        from pptx import Presentation
        prs = Presentation(str(p))
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            texts = []
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    texts.append(shape.text.strip())
            if texts:
                parts.append(f"【幻灯片 {i}】\n" + "\n".join(texts))
        text = "\n\n".join(parts)
        if not text.strip():
            raise ValueError(f"PPTX 未提取到内容：{name}")
        return _make_chunks(text, name, doc_id, name, scope)
    raise ValueError(f"暂不支持该文件类型 {ext!r}（支持 txt/md/csv/pdf/docx/xlsx/xls/pptx）")
