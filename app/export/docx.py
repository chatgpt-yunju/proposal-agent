"""把方案 Markdown 渲染为 Word(.docx)。

标题的字体、字号、加粗、对齐、段前段后，以及正文字体字号缩进行距，全部取自
模板上传时采集到的样式定义（style dict），不再在代码里写死某一套公文样式。
没有模板样式时回退到 DEFAULT_STYLE（常见政府公文体例）。
"""
from __future__ import annotations

import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from ..template_style import DEFAULT_STYLE, level_spec
from .blocks import parse_markdown

_PENDING_MARK = "（本章尚未生成）"
# 图题/表题：以「图1」「表 2」开头的一整段，套模板 Caption 样式
_CAPTION_RE = re.compile(r"^(图|表)\s*[0-9一二三四五六七八九十]+")


def _add_footer_page_number(doc, size: float = 14, font: str = "仿宋_GB2312") -> None:
    """页脚居中的纯页码（模板页脚只放了一个 PAGE 域，没有前后缀文字）。"""
    from docx.oxml import OxmlElement

    for sec in doc.sections:
        footer = sec.footer
        p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run()
        begin = OxmlElement("w:fldChar")
        begin.set(qn("w:fldCharType"), "begin")
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = " PAGE  \\* MERGEFORMAT "
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        run._r.append(begin)
        run._r.append(instr)
        run._r.append(end)
        run.font.size = Pt(size)
        _set_run_east_asia(run, font)


def _add_front_matter(doc, style: dict, front: dict) -> None:
    """在目录之前插入「前 言」「摘 要」：各起一页，标题居中，正文用正文样式。"""
    front = front or {}
    lv1 = level_spec(style, 1)
    for key, title in (("preface", "前　言"), ("abstract", "摘　要")):
        text = (front.get(key) or "").strip()
        if not text:
            continue
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        head = doc.add_paragraph()
        head.alignment = WD_ALIGN_PARAGRAPH.CENTER
        head.paragraph_format.first_line_indent = Pt(0)
        head.paragraph_format.space_before = Pt(0)
        head.paragraph_format.space_after = Pt(12)
        hr = head.add_run(title)
        hr.font.size = Pt(lv1.get("size") or 18)
        hr.bold = True
        _set_run_east_asia(hr, lv1.get("font") or "黑体")
        for para in text.split("\n"):
            para = para.strip()
            if para:
                doc.add_paragraph(para)


def _add_toc(doc, style: dict) -> None:
    """在正文前插入「目 录」页：分页 + 目录标题 + 真正的 TOC 域（1~2 级，与模板一致）。

    页码交给 Word 自己算；同时在 settings 里打开 updateFields，
    这样用 Word 打开时会自动刷新页码，不用手动按 F9。
    """
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    lv1 = level_spec(style, 1)
    head = doc.add_paragraph()
    head.alignment = WD_ALIGN_PARAGRAPH.CENTER
    head.paragraph_format.first_line_indent = Pt(0)
    head.paragraph_format.space_before = Pt(0)
    head.paragraph_format.space_after = Pt(18)
    hr = head.add_run("目　录")
    hr.font.size = Pt(lv1.get("size") or 18)
    hr.bold = True
    _set_run_east_asia(hr, lv1.get("font") or "黑体")

    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = Pt(0)
    p.paragraph_format.space_after = Pt(0)

    r = p.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    r._r.append(begin)

    r = p.add_run()
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = ' TOC \\o "1-2" \\h \\u '
    r._r.append(instr)

    r = p.add_run()
    sep = OxmlElement("w:fldChar")
    sep.set(qn("w:fldCharType"), "separate")
    r._r.append(sep)

    r = p.add_run("（目录页码在 Word 中打开时自动生成；若未显示，Ctrl+A 后按 F9 更新域）")
    r.font.size = Pt(12)
    _set_run_east_asia(r, "仿宋_GB2312")

    r = p.add_run()
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    r._r.append(end)

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # 让 Word 打开即更新目录页码
    try:
        settings = doc.settings.element
        el = settings.find(qn("w:updateFields"))
        if el is None:
            el = OxmlElement("w:updateFields")
            settings.append(el)
        el.set(qn("w:val"), "true")
    except Exception:
        pass


def _set_east_asia(style, east: str) -> None:
    rpr = style.element.get_or_add_rPr()
    rpr.get_or_add_rFonts().set(qn("w:eastAsia"), east)


def _set_run_east_asia(run, east: str) -> None:
    rpr = run._element.get_or_add_rPr()
    rpr.get_or_add_rFonts().set(qn("w:eastAsia"), east)


def _apply_heading(doc, word_lvl: int, spec: dict) -> None:
    """把某一级的标题样式写进 Word 内置 Heading N 样式。"""
    hs = doc.styles[f"Heading {word_lvl}"]
    size = spec.get("size") or 16
    hs.font.name = "Times New Roman"
    hs.font.size = Pt(size)
    hs.font.bold = bool(spec.get("bold"))
    # python-docx 内置的 Heading 4/6 样式自带斜体，模板里没有，必须显式关掉
    hs.font.italic = False
    hs.font.color.rgb = RGBColor(0x00, 0x00, 0x00)
    _set_east_asia(hs, spec.get("font") or "宋体")
    hpf = hs.paragraph_format
    before = spec.get("spaceBefore")
    after = spec.get("spaceAfter")
    # 模板标题样式没有段前段后就不加：默认的 size*0.6 / size*0.3 会让标题比人工稿松
    hpf.space_before = Pt(before if before is not None else 0)
    hpf.space_after = Pt(after if after is not None else 0)
    # 3~6 级标题在模板里带 2 字符首行缩进，不是全部顶格
    indent_chars = spec.get("indentChars") or 0
    hpf.first_line_indent = Pt(size * indent_chars)
    hpf.alignment = (WD_ALIGN_PARAGRAPH.CENTER if spec.get("align") == "center"
                     else WD_ALIGN_PARAGRAPH.LEFT)


def _add_table(doc, rows, body_size: float, body_font: str, table_spec: dict = None) -> None:
    """把 Markdown 表格渲染成真正的 Word 表格（模板人工稿里的数据表就靠这个还原）。"""
    spec = table_spec or DEFAULT_STYLE.get("table") or {}
    n_cols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=n_cols)
    table.style = "Table Grid"
    table.autofit = True
    # 字号照抄模板表格：模板单元格多半继承 Normal，硬减 4pt 会比人工稿小一号
    cell_size = spec.get("size") or (body_size - 2 if body_size > 12 else 12)
    header_bold = spec.get("headerBold", True)
    for ri, row in enumerate(rows):
        for ci in range(n_cols):
            cell = table.cell(ri, ci)
            cell.text = ""
            p = cell.paragraphs[0]
            run = p.add_run(row[ci] if ci < len(row) else "")
            run.font.size = Pt(cell_size)
            _set_run_east_asia(run, body_font)
            if ri == 0 and header_bold:
                run.bold = True
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Pt(0)
            p.paragraph_format.line_spacing = 1.0
            p.paragraph_format.space_after = Pt(0)
    # 表格后补一个空段，避免和下一段正文粘在一起
    doc.add_paragraph()


def md_to_docx(md_text: str, out_path: str, style: dict = None, front: dict = None) -> str:
    style = style or DEFAULT_STYLE
    body = style.get("body") or DEFAULT_STYLE["body"]
    body_size = body.get("size") or 16
    body_font = body.get("font") or "仿宋_GB2312"
    indent_chars = body.get("indentChars")
    if indent_chars is None:
        indent_chars = 2
    line_spacing = body.get("lineSpacing") or 1.5

    doc = Document()

    # 纸张与页边距按模板：python-docx 默认是 Letter(21.59x27.94)，模板是 A4，
    # 尺寸不同会直接把行宽和总页数拉偏。
    page = style.get("page") or {}
    if page:
        sec = doc.sections[0]
        if page.get("width"):
            sec.page_width = Cm(page["width"])
        if page.get("height"):
            sec.page_height = Cm(page["height"])
        for key, attr in (("marginTop", "top_margin"), ("marginBottom", "bottom_margin"),
                          ("marginLeft", "left_margin"), ("marginRight", "right_margin"),
                          ("footer", "footer_distance"), ("header", "header_distance")):
            if page.get(key) is not None:
                setattr(sec, attr, Cm(page[key]))

    # 模板页脚就是一个居中的 PAGE 域，导出件同样补上页码
    _add_footer_page_number(doc, 14, body_font)

    normal = doc.styles["Normal"]
    # 西文字体也照模板（模板 ascii/hAnsi 是宋体，不是 Times New Roman）
    normal.font.name = body.get("fontAscii") or "Times New Roman"
    normal.font.size = Pt(body_size)
    _set_east_asia(normal, body_font)
    pf = normal.paragraph_format
    # 行距：模板用「固定值/最小值」时给磅值，用「倍数」时给倍数，两者不能混
    if body.get("lineSpacingPt"):
        from docx.enum.text import WD_LINE_SPACING
        pf.line_spacing = Pt(body["lineSpacingPt"])
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    else:
        pf.line_spacing = line_spacing
    pf.first_line_indent = Pt(body_size * indent_chars)
    pf.left_indent = Pt(body_size * (body.get("leftChars") or 0))
    pf.right_indent = Pt(body_size * (body.get("rightChars") or 0))
    pf.space_before = Pt(body.get("spaceBefore") or 0)
    pf.space_after = Pt(body.get("spaceAfter") if body.get("spaceAfter") is not None else 0)
    # 正文对齐（模板多为两端对齐），不采集就会退回左对齐，版心右边参差不齐
    pf.alignment = {
        "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
        "left": WD_ALIGN_PARAGRAPH.LEFT,
    }.get(str(body.get("align") or "justify"), WD_ALIGN_PARAGRAPH.JUSTIFY)

    # 方案名用 Title 样式，字号字体来自模板封面标题
    title_spec = style.get("title") or DEFAULT_STYLE["title"]
    title = doc.styles["Title"]
    title.font.name = "Times New Roman"
    title.font.size = Pt(title_spec.get("size") or 26)
    title.font.bold = bool(title_spec.get("bold"))
    title.font.italic = False
    title.font.color.rgb = RGBColor(0x00, 0x00, 0x00)
    _set_east_asia(title, title_spec.get("font") or "方正小标宋简体")
    # 内置 Title 样式自带一条蓝色下边框，模板封面标题没有，去掉
    tppr = title.element.find(qn("w:pPr"))
    if tppr is not None:
        pbr = tppr.find(qn("w:pBdr"))
        if pbr is not None:
            tppr.remove(pbr)
    tpf = title.paragraph_format
    tpf.alignment = (WD_ALIGN_PARAGRAPH.CENTER if title_spec.get("align", "center") == "center"
                     else WD_ALIGN_PARAGRAPH.LEFT)
    tpf.first_line_indent = Pt(0)
    tpf.space_before = Pt(0)
    tpf.space_after = Pt((title_spec.get("size") or 26) * 0.7)

    # md 的 h2..h7 对应模板的第 1..6 级标题；Word 用 Heading 1..6 承接
    md_levels = [2, 3, 4, 5, 6, 7]
    for idx, md_lvl in enumerate(md_levels, start=1):
        _apply_heading(doc, idx, level_spec(style, idx))

    toc_done = False
    for b in parse_markdown(md_text):
        if b.kind == "h1":
            doc.add_heading(b.text, level=0)
            # 方案名（封面标题）之后：前言/摘要 → 目录 → 正文
            if not toc_done:
                _add_front_matter(doc, style, front)
                _add_toc(doc, style)
                toc_done = True
        elif b.kind == "table":
            _add_table(doc, b.rows, body_size, body_font, style.get("table"))
        elif b.kind[1:].isdigit():
            doc.add_heading(b.text, level=min(max(int(b.kind[1:]) - 1, 1), 9))
        elif b.kind == "list":
            p = doc.add_paragraph(b.text, style="List Bullet")
            p.paragraph_format.first_line_indent = Pt(0)
        elif b.kind == "quote":
            p = doc.add_paragraph()
            run = p.add_run(b.text)
            run.italic = True
            run.font.color.rgb = RGBColor(0x6B, 0x72, 0x80)
            p.paragraph_format.left_indent = Pt(18)
            p.paragraph_format.first_line_indent = Pt(0)
        else:
            if b.text.strip() == _PENDING_MARK:
                # 未生成章节占位：灰色，提示这是待补写的位置
                p = doc.add_paragraph()
                run = p.add_run(b.text)
                run.font.color.rgb = RGBColor(0x9C, 0xA3, 0xAF)
                run.font.size = Pt(body_size)
                _set_run_east_asia(run, body_font)
                p.paragraph_format.first_line_indent = Pt(body_size * indent_chars)
            elif _CAPTION_RE.match(b.text.strip()):
                # 「图1 …」「表1 …」这类题注单独套模板 Caption 样式（居中、比正文小）
                cap = style.get("caption") or DEFAULT_STYLE.get("caption") or {}
                p = doc.add_paragraph()
                run = p.add_run(b.text.strip())
                run.font.size = Pt(cap.get("size") or body_size)
                run.bold = bool(cap.get("bold"))
                _set_run_east_asia(run, cap.get("font") or body_font)
                p.alignment = (WD_ALIGN_PARAGRAPH.CENTER if cap.get("align", "center") == "center"
                               else WD_ALIGN_PARAGRAPH.LEFT)
                p.paragraph_format.first_line_indent = Pt(0)
                p.paragraph_format.space_before = Pt(0)
                p.paragraph_format.space_after = Pt(0)
                # 表题不能单独留在页脚：让它跟后面的表格/图片同页
                p.paragraph_format.keep_with_next = True
            else:
                doc.add_paragraph(b.text)

    doc.save(out_path)
    return out_path
