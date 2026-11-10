"""模板 Word 样式与编号定义采集。

上传 docx 模板时，除了章节结构，还要把「标题层级长什么样」一起读出来：
每级标题的字号、中文字体、加粗、对齐、编号格式（numFmt + lvlText），以及正文
的字号、字体、首行缩进、行距。这些信息随项目存下来，导出 Word/PDF 与网页预览
都按这份定义渲染，换一份模板（比如「1.1」「第一节」体例）也不需要改代码。

采集结果是一个纯 dict（可直接 JSON 序列化），结构见 DEFAULT_STYLE。
"""
from __future__ import annotations

import re
import zipfile
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# 缺少模板样式信息时的兜底：政府公文常用体例（第N章 / 一、 / （一）/ 1.）
DEFAULT_STYLE: Dict = {
    "source": "default",
    "title": {"size": 26, "font": "方正小标宋简体", "bold": False, "align": "center"},
    "body": {"size": 16, "font": "仿宋_GB2312", "indentChars": 2, "lineSpacing": 1.5,
             "lineSpacingPt": None, "align": "justify", "leftChars": 0, "rightChars": 0,
             "spaceBefore": 0, "spaceAfter": 2},
    # 表格：单元格字号、表头是否加粗；图题（Caption）同理，都是模板里能读到的东西
    "table": {"size": 12, "headerBold": True, "border": True},
    "caption": {"size": 12, "font": "宋体", "bold": False, "align": "center"},
    "levels": [
        {"level": 1, "size": 18, "font": "方正小标宋简体", "bold": True, "align": "center",
         "numFmt": "chineseCountingThousand", "lvlText": "第%1章", "indentChars": 0},
        {"level": 2, "size": 16, "font": "黑体", "bold": True, "align": "left",
         "numFmt": "chineseCountingThousand", "lvlText": "%2、", "indentChars": 0},
        {"level": 3, "size": 16, "font": "仿宋_GB2312", "bold": True, "align": "left",
         "numFmt": "chineseCountingThousand", "lvlText": "（%3）", "indentChars": 0},
        {"level": 4, "size": 16, "font": "仿宋_GB2312", "bold": True, "align": "left",
         "numFmt": "decimal", "lvlText": "%4.", "indentChars": 0},
    ],
}

_CN_DIGITS = "零一二三四五六七八九"


# ---------- 数字/编号格式化 ----------
def cn_num(n: int) -> str:
    """1→一，12→十二，105→一百零五。"""
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    if n < 100:
        return _CN_DIGITS[n // 10] + "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    if n < 1000:
        q, r = divmod(n, 100)
        head = _CN_DIGITS[q] + "百"
        if r == 0:
            return head
        if r < 10:
            return head + "零" + _CN_DIGITS[r]
        return head + cn_num(r)
    return str(n)


_ROMAN = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
          (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def format_number(n: int, fmt: str) -> str:
    """按 Word 的 numFmt 把计数转换成显示文本。"""
    fmt = fmt or "decimal"
    if fmt in ("chineseCounting", "chineseCountingThousand", "chineseLegalSimplified",
               "chineseCountingThousandTraditional", "taiwaneseCounting",
               "taiwaneseCountingThousand"):
        return cn_num(n)
    if fmt in ("lowerLetter", "upperLetter"):
        letters = "".join(chr(ord("a") + ((n - 1) // 26 ** i) % 26)
                          for i in (1, 0)) if n > 26 else chr(ord("a") + (n - 1) % 26)
        return letters.upper() if fmt == "upperLetter" else letters
    if fmt in ("lowerRoman", "upperRoman"):
        num, out = n, ""
        for v, s in _ROMAN:
            while num >= v:
                out += s
                num -= v
        return out.upper() if fmt == "upperRoman" else out
    if fmt == "decimalEnclosedCircle":
        return _CIRCLED[n - 1] if 1 <= n <= 20 else str(n)
    if fmt == "decimalZero":
        return f"{n:02d}"
    return str(n)


def render_label(style: Dict, counters: Dict[int, int], level: int) -> str:
    """按模板 lvlText + numFmt 生成某一级的编号前缀（如「第一章 」「一、」「（一）」）。"""
    levels = style.get("levels") or []
    lv = levels[level - 1] if 0 <= level - 1 < len(levels) else None
    tpl = (lv or {}).get("lvlText") or ""
    if not tpl:
        return ""

    def repl(m):
        k = int(m.group(1))
        n = counters.get(k, 0)
        fmt = "decimal"
        if 0 <= k - 1 < len(levels):
            fmt = levels[k - 1].get("numFmt") or "decimal"
        return format_number(n, fmt) if n else ""

    label = re.sub(r"%(\d)", repl, tpl)
    if not label:
        return ""
    # 模板编号后面是「第X章」这类带文字的，补一个分隔空格；以标点结尾的不补
    tail = tpl[-1] if tpl else ""
    if tail and tail not in "、，。．.）,）：:；;】":
        label += " "
    return label


def number_chapters(chapters: List[dict], style: Dict) -> List[dict]:
    """给章节列表补编号前缀 no 与同层序号 seq（同层递进、下层归零）。"""
    levels = style.get("levels") or DEFAULT_STYLE["levels"]
    max_lvl = max(1, min(len(levels), 6))
    counters = {i: 0 for i in range(1, max_lvl + 1)}
    out = []
    for c in chapters:
        lvl = int(c.get("level") or 1)
        lvl = max(1, min(lvl, max_lvl))
        counters[lvl] += 1
        for deeper in range(lvl + 1, max_lvl + 1):
            counters[deeper] = 0
        item = dict(c)
        item["no"] = render_label(style, counters, lvl)
        item["seq"] = counters[lvl]
        out.append(item)
    return out


# ---------- 从 docx 采集 ----------
def _int(val, default=0):
    try:
        return int(str(val))
    except (TypeError, ValueError):
        return default


def _attr(el, name):
    return el.get(f"{W}{name}") if el is not None else None


def _read_part(zf: zipfile.ZipFile, name: str) -> Optional[ET.Element]:
    if name not in zf.namelist():
        return None
    try:
        return ET.fromstring(zf.read(name))
    except ET.ParseError:
        return None


def _style_level(style_name: str) -> int:
    """从样式名判断标题级别：Heading 1 / heading 1 / 标题 1 → 1。"""
    low = (style_name or "").strip().lower()
    m = re.search(r"heading\s*(\d)", low)
    if m:
        return int(m.group(1))
    m = re.match(r"标题\s*(\d)", low)
    if m:
        return int(m.group(1))
    return 0


def _style_info(el) -> Dict:
    """抽取一个 <w:style> 的字号、中文字体、加粗、对齐、编号引用。"""
    info = {"size": None, "font": None, "fontAscii": None, "bold": None, "align": None,
            "numId": None, "ilvl": 0, "firstLineChars": None, "lineRule": None, "line": None,
            "spaceBefore": None, "spaceAfter": None}
    ppr = el.find(f"{W}pPr")
    if ppr is not None:
        jc = ppr.find(f"{W}jc")
        if jc is not None:
            info["align"] = _attr(jc, "val")
        ind = ppr.find(f"{W}ind")
        if ind is not None:
            info["firstLineChars"] = _attr(ind, "firstLineChars")
        spacing = ppr.find(f"{W}spacing")
        if spacing is not None:
            info["line"] = _attr(spacing, "line")
            info["lineRule"] = _attr(spacing, "lineRule")
            before = _attr(spacing, "before")
            after = _attr(spacing, "after")
            if before:
                info["spaceBefore"] = _int(before) / 20.0  # twips → pt
            if after:
                info["spaceAfter"] = _int(after) / 20.0
        numpr = ppr.find(f"{W}numPr")
        if numpr is not None:
            numid = numpr.find(f"{W}numId")
            if numid is not None:
                info["numId"] = _attr(numid, "val")
            ilvl = numpr.find(f"{W}ilvl")
            if ilvl is not None:
                info["ilvl"] = _int(_attr(ilvl, "val"), 0)
    rpr = el.find(f"{W}rPr")
    if rpr is not None:
        sz = rpr.find(f"{W}sz")
        if sz is not None:
            info["size"] = _int(_attr(sz, "val")) / 2.0
        rf = rpr.find(f"{W}rFonts")
        if rf is not None:
            info["font"] = _attr(rf, "eastAsia")
            # 西文字体（数字、括号、英文）在 Word 里走 ascii/hAnsi，和中文的 eastAsia 是两套
            info["fontAscii"] = _attr(rf, "ascii") or _attr(rf, "hAnsi")
        b = rpr.find(f"{W}b")
        if b is not None:
            info["bold"] = _attr(b, "val") not in ("0", "false", "none")
    return info


def _twips_to_cm(v) -> Optional[float]:
    """twips → 厘米（1 英寸 = 1440 twips = 2.54cm）。"""
    if v is None:
        return None
    try:
        return round(int(str(v)) / 1440.0 * 2.54, 2)
    except (TypeError, ValueError):
        return None


def _page_info(doc_root: Optional[ET.Element]) -> Optional[Dict]:
    """采集纸张尺寸与页边距，导出 Word 时按模板而不是 python-docx 默认的 Letter。"""
    if doc_root is None:
        return None
    sect = doc_root.find(f"{W}body/{W}sectPr")
    if sect is None:
        return None
    out: Dict = {}
    pgsz = sect.find(f"{W}pgSz")
    if pgsz is not None:
        w = _twips_to_cm(_attr(pgsz, "w"))
        h = _twips_to_cm(_attr(pgsz, "h"))
        if w and h:
            out["width"] = w
            out["height"] = h
    pgmar = sect.find(f"{W}pgMar")
    if pgmar is not None:
        for key, name in (("top", "marginTop"), ("bottom", "marginBottom"),
                          ("left", "marginLeft"), ("right", "marginRight"),
                          ("footer", "footer"), ("header", "header")):
            val = _twips_to_cm(_attr(pgmar, key))
            if val is not None:
                out[name] = val
    return out or None


def _table_info(doc_root: Optional[ET.Element], body_size: float,
                base_size: float = None) -> Dict:
    """采集模板表格：单元格常用字号（按 run 的 sz 众数）、表头是否加粗。

    模板表格往往不在样式表里写字号，而是继承了 Normal，导出时必须照抄这个值，
    否则我们的表格比模板小一号，视觉上就不一致。没写 sz 的 run 就记作继承的
    base_size（通常是 Normal 的字号），否则会被少数小字单元格把众数带偏。
    """
    base = base_size or body_size
    fallback = {"size": base, "headerBold": True, "border": True}
    if doc_root is None:
        return fallback
    size_stats: Dict[float, int] = {}
    head_runs = 0
    head_bold_runs = 0
    tables = list(doc_root.iter(f"{W}tbl"))
    if not tables:
        return fallback
    for tbl in tables:
        for ri, tr in enumerate(tbl.findall(f"{W}tr")):
            is_head = ri == 0
            for tc in tr.findall(f"{W}tc"):
                for p in tc.iter(f"{W}p"):
                    for r in p.findall(f"{W}r"):
                        sz = r.find(f"{W}rPr/{W}sz")
                        pt = base
                        if sz is not None:
                            v = _int(_attr(sz, "val"), 0)
                            if v:
                                pt = round(v / 2.0, 1)
                        size_stats[pt] = size_stats.get(pt, 0) + 1
                        if is_head:
                            head_runs += 1
                            b = r.find(f"{W}rPr/{W}b")
                            if b is not None and (_attr(b, "val") or "") not in ("0", "false", "none"):
                                head_bold_runs += 1
    size = base
    if size_stats:
        size = max(size_stats.items(), key=lambda kv: kv[1])[0]
    header_bold = (head_bold_runs >= max(1, head_runs * 0.5)) if head_runs else True
    return {"size": size, "headerBold": header_bold, "border": True}


def _caption_info(by_id: Dict[str, Dict]) -> Dict:
    """采集图题/表题（Caption / 题注）样式，正文里以「图N」「表N」开头的段落套它。"""
    spec = dict(DEFAULT_STYLE["caption"])
    for sid, info in by_id.items():
        name = (info.get("name") or "").lower()
        if "caption" in name or "题注" in name or "图题" in name:
            if info.get("size"):
                spec["size"] = info["size"]
            if info.get("font"):
                spec["font"] = info["font"]
            spec["bold"] = bool(info.get("bold"))
            spec["align"] = "center" if info.get("align") == "center" else "left"
            break
    return spec


def _heading_para_indent(doc_root: Optional[ET.Element], by_id: Dict[str, Dict]) -> Dict[int, float]:
    """样式里没写首行缩进时，用该级标题段落实际使用的缩进（按字符计）补上。

    模板里 4 级标题继承 3 级样式、本身没有 ind 定义，但每个段落都写了
    firstLine=643（合 2 字符），只读样式会漏掉。
    """
    if doc_root is None:
        return {}
    stats: Dict[int, Dict[float, int]] = {}
    for p in doc_root.iter(f"{W}p"):
        pstyle = p.find(f"{W}pPr/{W}pStyle")
        if pstyle is None:
            continue
        info = by_id.get(_attr(pstyle, "val") or "")
        if not info:
            continue
        lvl = _style_level(info.get("name") or "")
        if not lvl:
            continue
        ppr = p.find(f"{W}pPr")
        if ppr is None:
            continue
        ind = ppr.find(f"{W}ind")
        if ind is None:
            continue
        chars = None
        ch = _attr(ind, "firstLineChars")
        if ch:
            chars = round(_int(ch) / 100.0, 2)
        else:
            fl = _attr(ind, "firstLine")
            size = info.get("size") or 0
            if fl and size:
                chars = round((_int(fl) / 20.0) / size, 2)
        if chars is not None:
            stats.setdefault(lvl, {})
            stats[lvl][chars] = stats[lvl].get(chars, 0) + 1
    return {lvl: max(m.items(), key=lambda kv: kv[1])[0] for lvl, m in stats.items()}


def _numbering_map(numbering: Optional[ET.Element]) -> Dict[str, Dict[int, Dict]]:
    """numId → {ilvl: {numFmt, lvlText}}。"""
    if numbering is None:
        return {}
    abstract: Dict[str, Dict[int, Dict]] = {}
    for an in numbering.findall(f"{W}abstractNum"):
        aid = _attr(an, "abstractNumId")
        lv_map = {}
        for lvl in an.findall(f"{W}lvl"):
            il = _int(_attr(lvl, "ilvl"), 0)
            fmt = lvl.find(f"{W}numFmt")
            txt = lvl.find(f"{W}lvlText")
            lv_map[il] = {
                "numFmt": _attr(fmt, "val") if fmt is not None else "decimal",
                "lvlText": _attr(txt, "val") if txt is not None else "",
            }
        abstract[aid] = lv_map

    num_map: Dict[str, Dict[int, Dict]] = {}
    for num in numbering.findall(f"{W}num"):
        nid = _attr(num, "numId")
        aid_el = num.find(f"{W}abstractNumId")
        aid = _attr(aid_el, "val") if aid_el is not None else None
        if aid is not None and aid in abstract:
            num_map[nid] = abstract[aid]
    return num_map


def _most_common_style(body_rels: Dict[str, int], heading_styles: set) -> Optional[str]:
    for name, cnt in sorted(body_rels.items(), key=lambda kv: kv[1], reverse=True):
        if name not in heading_styles and cnt > 0:
            return name
    return None


def _body_para_attrs(doc_root, body_name: str) -> Dict:
    """统计模板正文段落的实际段落属性（对齐、缩进、行距、段前后）。

    样式表里常常只写了一半（对齐、左右缩进多半直接写在段落 pPr 上），
    只读样式会漏掉"两端对齐"这类关键设置，导出后版心与模板不一致。
    """
    if doc_root is None:
        return {}
    counters: Dict[str, Dict[str, int]] = {}

    def bump(key, val):
        if val in (None, ""):
            return
        counters.setdefault(key, {})
        counters[key][str(val)] = counters[key].get(str(val), 0) + 1

    for p in doc_root.iter(f"{W}p"):
        ps = p.find(f"{W}pPr/{W}pStyle")
        name = _attr(ps, "val") if ps is not None else "Normal"
        if name != body_name:
            continue
        if not "".join(t.text or "" for t in p.iter(f"{W}t")).strip():
            continue   # 纯空段落不参与统计
        ppr = p.find(f"{W}pPr")
        if ppr is None:
            bump("align", "both")   # Word 默认两端对齐
            continue
        jc = ppr.find(f"{W}jc")
        bump("align", _attr(jc, "val") if jc is not None else "both")
        ind = ppr.find(f"{W}ind")
        if ind is not None:
            bump("firstLineChars", _attr(ind, "firstLineChars"))
            bump("leftChars", _attr(ind, "leftChars"))
            bump("rightChars", _attr(ind, "rightChars"))
        sp = ppr.find(f"{W}spacing")
        if sp is not None:
            bump("line", _attr(sp, "line"))
            bump("lineRule", _attr(sp, "lineRule"))
            bump("before", _attr(sp, "before"))
            bump("after", _attr(sp, "after"))

    out: Dict = {}
    for key, vals in counters.items():
        out[key] = max(vals.items(), key=lambda kv: kv[1])[0]
    return out


def _body_run_attrs(doc_root, body_name: str) -> Dict:
    """统计模板正文段落的实际字体字号（run 级 rPr 常直接压过样式）。"""
    if doc_root is None:
        return {}
    sz, font = {}, {}
    for p in doc_root.iter(f"{W}p"):
        ps = p.find(f"{W}pPr/{W}pStyle")
        name = _attr(ps, "val") if ps is not None else "Normal"
        if name != body_name:
            continue
        for r in p.iter(f"{W}r"):
            rpr = r.find(f"{W}rPr")
            if rpr is None:
                continue
            s = rpr.find(f"{W}sz")
            if s is not None:
                v = _attr(s, "val")
                sz[v] = sz.get(v, 0) + 1
            rf = rpr.find(f"{W}rFonts")
            if rf is not None:
                v = _attr(rf, "eastAsia")
                if v:
                    font[v] = font.get(v, 0) + 1
    out = {}
    if sz:
        out["size"] = _int(max(sz.items(), key=lambda kv: kv[1])[0]) / 2.0
    if font:
        out["font"] = max(font.items(), key=lambda kv: kv[1])[0]
    return out


def extract_from_docx(path: str) -> Dict:
    """从 docx 模板采集标题层级样式、编号定义与正文字体。失败时返回 DEFAULT_STYLE。"""
    try:
        with zipfile.ZipFile(path) as zf:
            styles_root = _read_part(zf, "word/styles.xml")
            numbering_root = _read_part(zf, "word/numbering.xml")
            doc_root = _read_part(zf, "word/document.xml")
    except (zipfile.BadZipFile, OSError):
        return dict(DEFAULT_STYLE)

    if styles_root is None:
        return dict(DEFAULT_STYLE)

    # 样式表：id → 名称/信息。标题样式常靠 basedOn 继承上一级（模板 4 级标题就是
    # 继承 3 级的字号/加粗/缩进，自己只写了编号），必须沿继承链合并，否则会读成
    # 不加粗、无缩进，导出后和模板差一档。
    raw_els: Dict[str, ET.Element] = {}
    raw_names: Dict[str, str] = {}
    for st in styles_root.findall(f"{W}style"):
        sid = _attr(st, "styleId") or ""
        nm = st.find(f"{W}name")
        raw_els[sid] = st
        raw_names[sid] = _attr(nm, "val") if nm is not None else sid

    resolved: Dict[str, Dict] = {}

    def resolve(sid: str) -> Dict:
        if sid in resolved:
            return resolved[sid]
        st = raw_els.get(sid)
        if st is None:
            return {}
        info = _style_info(st)
        based_el = st.find(f"{W}basedOn")
        based = _attr(based_el, "val") if based_el is not None else None
        merged = dict(info)
        if based and based in raw_els and based != sid:
            parent = resolve(based)
            for k, v in parent.items():
                if merged.get(k) is None and v is not None:
                    merged[k] = v
        resolved[sid] = merged
        return merged

    by_id: Dict[str, Dict] = {}
    heading_levels: Dict[int, Dict] = {}
    for sid, st in raw_els.items():
        info = resolve(sid)
        info["name"] = raw_names.get(sid, sid)
        by_id[sid] = info
        lvl = _style_level(info["name"])
        if lvl and 1 <= lvl <= 6 and lvl not in heading_levels:
            heading_levels[lvl] = info

    if not heading_levels:
        return dict(DEFAULT_STYLE)

    # 正文样式：统计文档里用得最多的非标题段落样式
    counts: Dict[str, int] = {}
    if doc_root is not None:
        for p in doc_root.iter(f"{W}p"):
            ps = p.find(f"{W}pPr/{W}pStyle")
            if ps is None:
                counts["Normal"] = counts.get("Normal", 0) + 1
                continue
            val = _attr(ps, "val")
            counts[val] = counts.get(val, 0) + 1
    heading_ids = {sid for sid, info in by_id.items() if _style_level(info.get("name", ""))}

    def _is_body_candidate(sid: str) -> bool:
        """正文样式候选：排除标题、表格、题注、目录这类专用样式。"""
        nm = (by_id.get(sid) or {}).get("name") or sid
        low = nm.lower()
        if _style_level(nm):
            return False
        bad = ("表格", "caption", "题注", "目录", "toc", "table", "header", "footer", "页脚", "页眉")
        return not any(k in nm or k in low for k in bad)

    # 正文样式：优先取"专用的正文样式"（模板里是 a4「Normal Indent」），
    # 只有确实没有专用正文样式时才退回计无 pStyle 段落的 Normal——
    # 否则封面、表格、目录这些 Normal 段落会把正文样式的字号和字体带偏。
    cand = {sid: cnt for sid, cnt in counts.items() if sid != "Normal" and _is_body_candidate(sid)}
    body_name = _most_common_style(cand, set()) or _most_common_style(counts, heading_ids) or "Normal"
    body_info = by_id.get(body_name) or by_id.get("Normal") or {}

    num_map = _numbering_map(numbering_root)
    para_indent = _heading_para_indent(doc_root, by_id)
    # 表格字号多数继承 Normal，用 Normal 的字号做基准再去统计显式写的值
    normal_size = (by_id.get("Normal") or {}).get("size") or (body_info.get("size") or 14)

    levels: List[Dict] = []
    for lvl in sorted(heading_levels.keys()):
        info = heading_levels[lvl]
        numid = info.get("numId")
        ilvl = info.get("ilvl") or (lvl - 1)
        fmt, txt = "decimal", ""
        if numid and numid in num_map and ilvl in num_map[numid]:
            fmt = num_map[numid][ilvl].get("numFmt") or "decimal"
            txt = num_map[numid][ilvl].get("lvlText") or ""
        # 该级的占位符按「第几级」校正，避免模板 ilvl 与标题级别错位导致编号串位
        if txt:
            txt = re.sub(r"%\d", f"%{lvl}", txt)
        levels.append({
            "level": lvl,
            "size": info.get("size") or DEFAULT_STYLE["levels"][min(lvl, 4) - 1]["size"],
            "font": info.get("font") or DEFAULT_STYLE["levels"][min(lvl, 4) - 1]["font"],
            "bold": bool(info.get("bold")),
            "align": "center" if info.get("align") == "center" else "left",
            "numFmt": fmt,
            "lvlText": txt,
            "indentChars": (round(_int(info.get("firstLineChars"), 0) / 100, 2)
                            if info.get("firstLineChars")
                            else para_indent.get(lvl, 0)),
            "spaceBefore": info.get("spaceBefore"),
            "spaceAfter": info.get("spaceAfter"),
        })

    para_attrs = _body_para_attrs(doc_root, body_name)
    run_attrs = _body_run_attrs(doc_root, body_name)

    # 字号字体以正文样式为准；样式没写才看正文段落的 run（run 上零星的例外字体不能反过来盖掉样式）
    body_size = body_info.get("size") or run_attrs.get("size") or DEFAULT_STYLE["body"]["size"]
    body_font = body_info.get("font") or run_attrs.get("font") or DEFAULT_STYLE["body"]["font"]

    # 行距：auto 规则下 line 是 1/240 行的倍数；exact/atLeast 是磅值（twips/20）
    line = _int(para_attrs.get("line") or body_info.get("line"), 0)
    line_rule = para_attrs.get("lineRule") or body_info.get("lineRule")
    line_spacing_pt = None
    if line and str(line_rule) in ("exact", "atLeast"):
        line_spacing = DEFAULT_STYLE["body"]["lineSpacing"]
        line_spacing_pt = round(line / 20.0, 1)
    elif line:
        line_spacing = round(line / 240, 2)
    else:
        line_spacing = DEFAULT_STYLE["body"]["lineSpacing"]

    indent = para_attrs.get("firstLineChars") or body_info.get("firstLineChars")
    indent_chars = round(_int(indent, 0) / 100, 2) if indent else 0

    jc = para_attrs.get("align") or body_info.get("align") or "both"
    align = {"both": "justify", "justify": "justify", "distribute": "justify",
             "center": "center", "right": "right", "left": "left"}.get(str(jc).lower(), "justify")

    def _chars(v) -> float:
        return round(_int(v, 0) / 100.0, 2) if v else 0

    space_after = para_attrs.get("after") or body_info.get("spaceAfter")
    space_after = round(_int(space_after, 0) / 20.0, 1) if space_after else 0
    space_before = para_attrs.get("before") or body_info.get("spaceBefore")
    space_before = round(_int(space_before, 0) / 20.0, 1) if space_before else 0

    title_spec = dict(DEFAULT_STYLE["title"])
    # 方案名（文档标题）：用模板封面标题样式的字号字体，没有就沿用兜底
    for st in styles_root.findall(f"{W}style"):
        nm = st.find(f"{W}name")
        name = (_attr(nm, "val") if nm is not None else "") or ""
        if ("covertitle" in name.lower() or "封面标题" in name) and "字符" not in name:
            info = _style_info(st)
            if info.get("size"):
                title_spec["size"] = info["size"]
            if info.get("font"):
                title_spec["font"] = info["font"]
            title_spec["bold"] = bool(info.get("bold"))
            break

    return {
        "source": f"docx:{path}",
        "title": title_spec,
        "page": _page_info(doc_root),
        "table": _table_info(doc_root, body_size, normal_size),
        "caption": _caption_info(by_id),
        "body": {
            "size": body_size,
            "font": body_font,
            "fontAscii": body_info.get("fontAscii") or "Times New Roman",
            "indentChars": indent_chars,
            "lineSpacing": line_spacing or 1.5,
            "lineSpacingPt": line_spacing_pt,
            "align": align,
            "leftChars": _chars(para_attrs.get("leftChars") or body_info.get("leftChars")),
            "rightChars": _chars(para_attrs.get("rightChars") or body_info.get("rightChars")),
            "spaceBefore": space_before,
            # 模板正文没有段后就不要凭空加，否则每段多出空隙、整篇比人工稿松
            "spaceAfter": space_after,
        },
        "levels": levels,
    }


def level_spec(style: Dict, level: int) -> Dict:
    """取某一级的样式定义；越界时回退到最后一个已知级别或默认。"""
    levels = style.get("levels") or DEFAULT_STYLE["levels"]
    if not levels:
        levels = DEFAULT_STYLE["levels"]
    idx = max(1, min(level, len(levels))) - 1
    return levels[idx]
