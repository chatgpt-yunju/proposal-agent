"""把方案 Markdown 渲染为 PDF（A4）。用 PyMuPDF 逐行绘制，嵌入系统中文字体。

字体查找顺序：PDF_FONT_PATH（环境变量）→ Windows（微软雅黑/黑体/宋体）→ Linux（Noto/SourceHan/WQY）。
容器内请安装 CJK 字体（见 Dockerfile：fonts-noto-cjk）或设置 PDF_FONT_PATH。

为了和网页预览、Word 导出的层级观感一致，这里统一按「字号 + 加粗 + 段前距」表达标题层级，
正文一律首行缩进 2 字符；未生成章节的占位文字用灰色斜体效果（灰度 + 括号）呈现。
"""
from __future__ import annotations

import os
import re

import pymupdf

from ..template_style import DEFAULT_STYLE, level_spec
from .blocks import parse_markdown

# 图题/表题：以「图1」「表 2」开头的一整段，套模板 Caption 样式
_CAPTION_RE = re.compile(r"^(图|表)\s*[0-9一二三四五六七八九十]+")

_PAGE_W, _PAGE_H = 595, 842  # A4 pt
_MARGIN = 56

_FONT_CANDIDATES = {
    # 小标宋/宋体族：方案名与一级标题
    "song": [
        os.environ.get("PDF_FONT_SONG", ""),
        r"C:\Windows\Fonts\FZXBSJW.TTF",
        r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ],
    # 黑体族
    "hei": [
        os.environ.get("PDF_FONT_HEI", ""),
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ],
    # 仿宋族（正文默认）
    "fang": [
        os.environ.get("PDF_FONT_FANG", ""),
        r"C:\Windows\Fonts\simfang.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ],
}

_FONT_ALIAS = {"song": "f_song", "hei": "f_hei", "fang": "f_fang"}

_PENDING_COLOR = (0x9c, 0xa3, 0xaf)
_GRAY = (0x6b, 0x72, 0x80)
_BLACK = (0x00, 0x00, 0x00)
_PENDING_MARK = "（本章尚未生成）"
# 上标/特殊符号在中文字体里常缺字形，绘制前降级为可见的普通字符
_CHAR_FALLBACK = str.maketrans({"²": "2", "³": "3", "㎡": "m2", "㎜": "mm", "～": "~"})


def _font_family(font_name: str) -> str:
    """把模板里写的中文字体名映射到本地可嵌入的字体族。"""
    name = (font_name or "").strip()
    if any(k in name for k in ("小标宋", "宋体", "宋", "仿宋")):
        if "仿宋" in name:
            return "fang"
        return "song"
    if any(k in name for k in ("黑体", "雅黑", "等线")):
        return "hei"
    if any(k in name for k in ("楷",)):
        return "fang"
    return "fang"


def _build_style_map(style: dict) -> dict:
    """把模板样式定义翻译成 PDF 绘制用的 (字号,颜色,行高,段前,段后,加粗,对齐,字体族,首行缩进字符)。"""
    style = style or DEFAULT_STYLE
    body = style.get("body") or DEFAULT_STYLE["body"]
    body_size = body.get("size") or 16
    body_family = _font_family(body.get("font"))
    indent_chars = body.get("indentChars")
    indent_chars = 2 if indent_chars is None else indent_chars
    line_spacing = body.get("lineSpacing") or 1.5
    # 模板若用「固定行距」磅值，换算成相对字号的倍数，保证行与行之间的实际距离一致
    line_spacing_pt = body.get("lineSpacingPt")
    lh_para = (float(line_spacing_pt) / float(body_size)) if line_spacing_pt else (line_spacing + 0.1)

    m = {
        "para": (body_size, _BLACK, lh_para, 0,
                 body.get("spaceAfter") if body.get("spaceAfter") is not None else 0,
                 False, "indent", body_family, indent_chars),
        "list": (body_size, _BLACK, line_spacing, 0, 0, False, "list", body_family, 0),
        "quote": (max(10, body_size - 2), _GRAY, 1.5, 0, 0, False, "left", body_family, 0),
    }

    title = style.get("title") or DEFAULT_STYLE["title"]
    tsize = title.get("size") or 26
    m["h1"] = (tsize, _BLACK, 1.3, 0, tsize * 0.7, bool(title.get("bold")),
               "center" if title.get("align", "center") == "center" else "left",
               _font_family(title.get("font")), 0)

    for idx, md_lvl in enumerate((2, 3, 4, 5, 6, 7), start=1):
        spec = level_spec(style, idx)
        size = spec.get("size") or 16
        before = spec.get("spaceBefore")
        after = spec.get("spaceAfter")
        m[f"h{md_lvl}"] = (
            size, _BLACK, 1.4,
            # 模板标题没有段前段后就不加，和 Word 导出保持一致
            before if before is not None else 0,
            after if after is not None else 0,
            bool(spec.get("bold")),
            "center" if spec.get("align") == "center" else "left",
            _font_family(spec.get("font")),
            spec.get("indentChars") or 0,
        )
    return m


def _find_font(kind: str) -> str:
    for p in _FONT_CANDIDATES[kind]:
        if p and os.path.exists(p):
            return p
    for p in _FONT_CANDIDATES["fang"]:
        if p and os.path.exists(p):
            return p
    raise RuntimeError("未找到可用的中文字体，请安装系统 CJK 字体或设置 PDF_FONT_PATH")


def _text_width(text: str, fs: float) -> float:
    """CJK 近似宽度：中文按 1em，ASCII 按 0.55em。"""
    w = 0.0
    for ch in text:
        w += fs if ord(ch) > 0x2E80 else fs * 0.55
    return w


def _wrap(text: str, fs: float, width: float) -> list:
    """按近似字符宽度换行，避免切在行首标点。"""
    lines, cur, cur_w = [], "", 0.0
    for ch in text:
        w = fs if ord(ch) > 0x2E80 else fs * 0.55
        if cur_w + w > width and cur:
            if ch in "，。、；：？！）】」』%":
                cur += ch
                lines.append(cur)
                cur, cur_w = "", 0.0
                continue
            lines.append(cur)
            cur, cur_w = "", 0.0
        cur += ch
        cur_w += w
    if cur:
        lines.append(cur)
    return lines


def md_to_pdf(md_text: str, out_path: str, style: dict = None, front: dict = None) -> str:
    style_map = _build_style_map(style or DEFAULT_STYLE)

    # 页面尺寸与页边距按模板（cm → pt），不再固定用 56pt 边距，
    # 否则版心宽度和 Word 差十几 pt，行数、页数都对不上人工稿。
    page_cfg = (style or DEFAULT_STYLE).get("page") or {}

    def _cm2pt(key: str, default: float) -> float:
        return round((page_cfg.get(key) if page_cfg.get(key) is not None else default) / 2.54 * 72, 1)

    PW = _cm2pt("width", 21.0)
    PH = _cm2pt("height", 29.7)
    ML = _cm2pt("marginLeft", 3.15)
    MR = _cm2pt("marginRight", 3.15)
    MT = _cm2pt("marginTop", 2.52)
    MB = _cm2pt("marginBottom", 1.48)
    body_family = _font_family(((style or DEFAULT_STYLE).get("body") or DEFAULT_STYLE["body"]).get("font"))

    fonts = {}
    for kind in ("song", "hei", "fang"):
        path = _find_font(kind)
        fonts[kind] = path

    # 两端对齐要按字定位，TextWriter 用这些 Font 对象一次性写整行（比逐字 insert_text 快得多）
    tw_fonts = {}
    for kind, path in fonts.items():
        try:
            tw_fonts[_FONT_ALIAS[kind]] = pymupdf.Font(fontfile=path)
        except Exception:
            tw_fonts[_FONT_ALIAS[kind]] = pymupdf.Font(fontfile=fonts["fang"])

    doc = pymupdf.open()
    page = doc.new_page(width=PW, height=PH)
    toc = []  # (级别, 标题文本, 所在页索引)

    def register(page_obj):
        """为每页注册用到的字体别名；缺某个字体时回退到仿宋。"""
        for kind, path in fonts.items():
            alias = _FONT_ALIAS[kind]
            try:
                page_obj.insert_font(fontname=alias, fontfile=path)
            except Exception:
                page_obj.insert_font(fontname=alias, fontfile=fonts["fang"])

    register(page)

    avail_w = PW - ML - MR
    y = MT
    front_pages = 0   # 前言/摘要占的页数（不含封面页），决定目录插在哪、正文页码从几算
    front_done = False

    def new_page():
        nonlocal page, y
        page = doc.new_page(width=PW, height=PH)
        register(page)
        y = MT

    def draw(text: str, fs: float, color, x: float, baseline: float, bold: bool, alias: str) -> None:
        # 中文字体缺上标字符（m²、m³ 会渲染成方框），先降级成普通数字
        text = text.translate(_CHAR_FALLBACK)
        rgb = tuple(c / 255.0 for c in color)
        page.insert_text((x, baseline), text, fontname=alias, fontsize=fs, color=rgb)
        if bold:
            # PyMuPDF 无法指定字重：微偏移叠印模拟加粗（描边模式在中文密集笔画上会糊成实心块）
            page.insert_text((x + fs * 0.018, baseline), text, fontname=alias, fontsize=fs, color=rgb)

    def draw_line(text: str, fs: float, color, x: float, width: float, baseline: float,
                  bold: bool, alias: str, justify: bool = False) -> None:
        """两端对齐：行内字距均摊到右边距，最后一行不拉（模板正文就是两端对齐）。

        用 insert_text 的 charspace 一次铺满整行，而不是逐字绘制：
        逐字方案在 300 页长文档上会把导出时间从 40 秒拖到 200 秒以上。
        """
        if not justify:
            draw(text, fs, color, x, baseline, bold, alias)
            return
        n = len(text)
        nat = _text_width(text, fs)
        if n <= 1 or width <= nat + fs * 0.3:
            draw(text, fs, color, x, baseline, bold, alias)
            return
        fnt = tw_fonts.get(alias)
        if fnt is None:
            draw(text, fs, color, x, baseline, bold, alias)
            return
        gap = (width - nat) / n
        text = text.translate(_CHAR_FALLBACK)
        rgb = tuple(c / 255.0 for c in color)
        tw = pymupdf.TextWriter(page.rect)
        cx = x
        for ch in text:
            tw.append((cx, baseline), ch, font=fnt, fontsize=fs)
            cx += _text_width(ch, fs) + gap
        tw.write_text(page, color=rgb)
        if bold:
            tw2 = pymupdf.TextWriter(page.rect)
            cx = x + fs * 0.018
            for ch in text:
                tw2.append((cx, baseline), ch, font=fnt, fontsize=fs)
                cx += _text_width(ch, fs) + gap
            tw2.write_text(page, color=rgb)

    def draw_table(rows, cell_size: float, family: str) -> None:
        """画真表格：等宽列 + 网格线 + 单元格内换行，跨页自动续（续页重画表头）。"""
        nonlocal y
        n_cols = max(len(r) for r in rows)
        # 字号由调用方传入（模板表格的字号），不再固定减 4pt
        cell_fs = max(9.0, float(cell_size or 12))
        col_w = avail_w / n_cols
        pad = 4.0
        alias = _FONT_ALIAS[family]

        def cell_lines(txt: str) -> list:
            return _wrap((txt or "").strip(), cell_fs, col_w - 2 * pad) or [""]

        def row_height(row) -> float:
            n = max(len(cell_lines(c)) for c in row)
            return n * cell_fs * 1.25 + 2 * pad

        header = rows[0]
        body_rows = rows[1:]
        y += 4

        def emit(row, is_head: bool) -> None:
            nonlocal y
            h = row_height(row)
            if y + h > PH - MB:
                new_page()
            x = ML
            for ci in range(n_cols):
                page.draw_rect(pymupdf.Rect(x, y, x + col_w, y + h),
                               color=(0, 0, 0), width=0.6)
                lines = cell_lines(row[ci] if ci < len(row) else "")
                ty = y + pad
                for ln in lines:
                    if is_head:
                        tx = x + max(pad, (col_w - _text_width(ln, cell_fs)) / 2)
                    else:
                        tx = x + pad
                    draw(ln, cell_fs, _BLACK, tx, ty + cell_fs, bool(is_head), alias)
                    ty += cell_fs * 1.25
                x += col_w
            y += h

        emit(header, True)
        for r in body_rows:
            emit(r, False)
        y += 6

    for b in parse_markdown(md_text):
        if b.kind in ("h2", "h3"):
            # 目录只收 1~2 级标题（模板 TOC 就是 \o "1-2"）
            toc.append((1 if b.kind == "h2" else 2, b.text.strip(), doc.page_count - 1))
        if b.kind == "table":
            body = (style or DEFAULT_STYLE).get("body") or DEFAULT_STYLE["body"]
            tbl = (style or DEFAULT_STYLE).get("table") or {}
            draw_table(b.rows, tbl.get("size") or (body.get("size") or 16) - 2,
                       _font_family(body.get("font")))
            continue
        fs, color, lh, before, after, bold, align, family, indent_chars = style_map.get(b.kind, style_map["para"])
        indent_px = (indent_chars or 0) * fs

        # 图题/表题按模板 Caption 样式渲染（居中、字号比正文小）
        cap = (style or DEFAULT_STYLE).get("caption") or {}
        if cap and b.kind not in ("h1",) and _CAPTION_RE.match(b.text.strip()):
            fs = cap.get("size") or fs
            indent_px = 0
            before = after = 0
            align = "center" if cap.get("align", "center") == "center" else "left"
            bold = bool(cap.get("bold"))
        y += before

        is_pending = b.text.strip() == _PENDING_MARK
        if is_pending:
            color = _PENDING_COLOR

        alias = _FONT_ALIAS[family]
        prefix = "• " if align == "list" else ""
        text = prefix + b.text
        lines = _wrap(text, fs, avail_w - (indent_px if align in ("indent", "left_head") else 0))

        for i, ln in enumerate(lines):
            line_h = fs * lh
            if y + line_h > PH - MB:
                new_page()
            if align == "center":
                x = max(ML, (PW - _text_width(ln, fs)) / 2)
            elif align == "indent":
                # 正文：只有第一行缩进
                x = ML + (indent_px if i == 0 else 0)
            elif align == "list":
                x = ML + fs * 1.2
            else:
                # 标题等：整段左移模板要求的缩进字符
                x = ML + indent_px
            width = avail_w - (indent_px if (align == "indent" and i == 0) else 0)
            body_justify = (align == "indent" and not bold
                            and str(((style or DEFAULT_STYLE).get("body") or {}).get("align") or "justify") == "justify"
                            and i < len(lines) - 1)
            draw_line(ln, fs, color, x, width, y + fs, bold, alias, justify=body_justify)
            y += line_h

        y += after

        # 方案名（封面）之后：前言 / 摘要各起一页，正文再从新页开始
        if b.kind == "h1" and not front_done:
            front_done = True
            if front and ((front.get("preface") or "").strip() or (front.get("abstract") or "").strip()):
                body_cfg = (style or DEFAULT_STYLE).get("body") or DEFAULT_STYLE["body"]
                b_fs = body_cfg.get("size") or 16
                b_indent = body_cfg.get("indentChars")
                if b_indent is None:
                    b_indent = 2
                b_lh = 1.5
                body_alias = _FONT_ALIAS[body_family]
                for key, label in (("preface", "前　言"), ("abstract", "摘　要")):
                    txt = (front.get(key) or "").strip()
                    if not txt:
                        continue
                    new_page()
                    h_fs = 18.0
                    hw = _text_width(label, h_fs)
                    draw(label, h_fs, _BLACK, (PW - hw) / 2, y + h_fs, True, _FONT_ALIAS["hei"])
                    y += h_fs * 2.0
                    for para in txt.split("\n"):
                        para = para.strip()
                        if not para:
                            continue
                        for i, ln in enumerate(_wrap(para, b_fs, avail_w - b_fs * b_indent)):
                            if y + b_fs * b_lh > PH - MB:
                                new_page()
                            x = ML + (b_fs * b_indent if i == 0 else 0)
                            draw(ln, b_fs, _BLACK, x, y + b_fs, False, body_alias)
                            y += b_fs * b_lh
                        y += 6
                front_pages = doc.page_count - 1
                new_page()

    # ---------- 目录页：插到正文最前面，页码用渲染时记录的正文页 ----------
    toc_pages = 0
    if toc:
        toc_fs = 14.0
        toc_lh = toc_fs * 1.9
        head_fs = 18.0
        alias_body = _FONT_ALIAS[body_family]
        alias_head = "f_hei"

        entries = []
        for lvl, text, pidx in toc:
            indent = 0 if lvl == 1 else toc_fs * 2
            maxw = avail_w - indent - _text_width("9999", toc_fs) - 8
            lines = _wrap(text, toc_fs, maxw) or [""]
            entries.append((lines, pidx, indent))

        first_top = MT + head_fs * 2.2
        per_page = max(1, int((PH - MB - first_top) / toc_lh) - 1)
        total_lines = sum(len(e[0]) for e in entries)
        toc_pages = max(1, total_lines // per_page + (1 if total_lines % per_page else 0))

        # 目录插在「封面 + 前言/摘要」之后、正文之前（与模板的 前言→摘要→目录→正文 一致）
        toc_insert_at = (1 + front_pages) if front_pages else 0
        for k in range(toc_pages):
            pg = doc.new_page(pno=toc_insert_at + k, width=PW, height=PH)
            register(pg)
            title = "目　录"
            tw = _text_width(title, head_fs)
            pg.insert_text(((PW - tw) / 2, MT + head_fs), title,
                           fontname=alias_head, fontsize=head_fs, color=(0, 0, 0))

        pi = 0
        y = first_top
        for lines, pidx, indent in entries:
            need = len(lines) * toc_lh
            if y + need > PH - MB and y > first_top:
                pi += 1
                if pi >= toc_pages:
                    # 估算偏小：补一页继续排（插在目录块末尾，正文整体后移一页）
                    doc.new_page(pno=toc_insert_at + pi, width=PW, height=PH)
                    register(doc[toc_insert_at + pi])
                    toc_pages += 1
                y = first_top
            pg = doc[toc_insert_at + pi]
            for ln in lines:
                pg.insert_text((ML + indent, y + toc_fs), ln,
                               fontname=alias_body, fontsize=toc_fs, color=(0, 0, 0))
                y += toc_lh
            # 目录里的页码 = 正文页码（封面、前言、摘要、目录页都不计入）
            num = str(pidx + 1 - (front_pages + 1 if front_pages else 0))
            nw = _text_width(num, toc_fs)
            pg.insert_text((PW - MR - nw, y - toc_lh + toc_fs), num,
                           fontname=alias_body, fontsize=toc_fs, color=(0, 0, 0))
            y += 6

    # 页脚居中纯页码，和 Word 导出的页脚 PAGE 域对齐（封面/前言/摘要/目录不编号，正文从 1 起）
    skip_pages = (1 + front_pages if front_pages else 0) + toc_pages
    page_no_alias = _FONT_ALIAS[body_family]
    for i, pg in enumerate(doc):
        if i < skip_pages:
            continue
        label = str(i + 1 - skip_pages)
        fw = _text_width(label, 14)
        pg.insert_text(((PW - fw) / 2, PH - MB * 0.55), label,
                       fontname=page_no_alias, fontsize=14, color=(0, 0, 0))

    doc.subset_fonts()  # 裁剪到实际用到的字形，避免每页嵌入整份中文字体（10MB+ → 数百KB）
    doc.save(out_path)
    return out_path
