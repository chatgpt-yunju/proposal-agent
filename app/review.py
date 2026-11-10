"""章节校审：单章体例自检 + 跨章节一致性核对。

被写作流在「生成」之后调用，产出一份可直接展示的检查结论：
- 单章：无来源标注、篇幅、是否混入编号小标题
- 跨章：与已生成章节的重复段落、同一对象的数字/口径冲突

全部走规则比对，不额外调用大模型，保证每章生成后零成本、可解释、可复现。
"""
from __future__ import annotations

import difflib
import re
from typing import Dict, List, Tuple

# 正文里不该出现的引用标注
_SOURCE_MARK = re.compile(r"(【\s*来源|［\s*来源|\[来源|（\s*来源|来源\s*[:：])")

# 正文里不该出现的编号小标题（要求只输出纯正文）
_HEADING_LINE = re.compile(
    r"^\s*(第[一二三四五六七八九十百零\d]+[章节]"
    r"|[一二三四五六七八九十]+\s*、"
    r"|（[一二三四五六七八九十]+）"
    r"|\d+\.\d+(?:\s|、|$)"
    r"|\d+\.\s|\d+、"
    r"|（[一二三四五六七八九十]+）\s*)"
)

# 数字 + 单位，前面取一段中文作为对象名（用于跨章口径比对）
_NUM_UNIT = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9]{0,12}?)"
    r"(\d+(?:\.\d+)?)\s*(万亩|公顷|平方公里|平方千米|亩|万人|万元|亿元|千米|公里|%|余户|户|人)"
)

_SENT_SPLIT = re.compile(r"[。！？；\n]")
_PUNCT = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]")
_MIN_SENT = 18


def _flat(text: str) -> str:
    return _PUNCT.sub("", text or "")


def _sentences(text: str) -> List[str]:
    out = []
    for s in _SENT_SPLIT.split(text or ""):
        s = s.strip()
        if len(s) >= _MIN_SENT:
            out.append(s)
    return out


def _entities(text: str) -> List[Tuple[str, str, float]]:
    """抽取（对象, 单位, 数值）。对象取数字前最近的中文片段末 6 字，避免前缀过长误配。"""
    items = []
    for m in _NUM_UNIT.finditer(text or ""):
        obj = _PUNCT.sub("", m.group(1))[-6:].lstrip("于在位距至到从向对靠沿约")
        unit = m.group(3)
        try:
            val = float(m.group(2))
        except ValueError:
            continue
        if not obj:
            continue
        items.append((obj, unit, val))
    return items


def _check_source_mark(content: str) -> Dict:
    hits = _SOURCE_MARK.findall(content)
    return {
        "name": "无来源标注",
        "ok": not hits,
        "detail": "正文未出现【来源：…】等标注" if not hits else f"正文出现 {len(hits)} 处来源标注",
        "issues": [] if not hits else ["正文混入了来源标注，正式方案不保留引用标记"],
    }


def _check_length(content: str) -> Dict:
    n = len(_flat(content))
    ok = n >= 100
    return {
        "name": "篇幅",
        "ok": ok,
        "detail": f"{n} 字" + ("" if ok else "（不足 100 字，疑似生成不完整）"),
        "issues": [] if ok else [f"章节内容过短（{n} 字）"],
    }


def _check_heading_leak(content: str) -> Dict:
    lines = [l.strip() for l in (content or "").splitlines() if l.strip()]
    leaked = [l for l in lines if len(l) <= 40 and _HEADING_LINE.match(l)]
    return {
        "name": "正文不含小标题编号",
        "ok": not leaked,
        "detail": "正文为连续段落，无编号小标题" if not leaked else f"发现 {len(leaked)} 行小标题编号",
        "issues": [] if not leaked else [f"正文混入小标题编号：{leaked[0][:20]}"],
    }


def check_duplication(content: str, siblings: List[Dict]) -> Dict:
    """与已生成章节的重复：按句首分桶，再算相似度，避免 O(n*m) 全量比对。"""
    mine = _sentences(content)
    if not mine or not siblings:
        return {"name": "与其他章节不重复", "ok": True,
                "detail": "本节与已生成章节无整句重复", "issues": []}

    buckets: Dict[str, List[Tuple[str, str]]] = {}
    for sib in siblings:
        title = sib.get("title") or ""
        for s in _sentences(sib.get("content") or ""):
            key = _flat(s)[:4]
            if key:
                buckets.setdefault(key, []).append((title, s))

    dups: List[str] = []
    for s in mine:
        key = _flat(s)[:4]
        for title, other in buckets.get(key, [])[:20]:
            ratio = difflib.SequenceMatcher(None, _flat(s), _flat(other)).ratio()
            if ratio >= 0.92:
                dups.append(f"与《{title}》重复：{s[:24]}…")
                break
        if len(dups) >= 3:
            break

    return {
        "name": "与其他章节不重复",
        "ok": not dups,
        "detail": "本节与已生成章节无整句重复" if not dups else f"发现 {len(dups)} 处与前文重复的整句",
        "issues": dups,
    }


def check_conflicts(content: str, siblings: List[Dict]) -> Dict:
    """同一对象的数字口径冲突（如"历口镇13个行政村" vs "历口镇11个行政村"）。"""
    mine = _entities(content)
    if not mine or not siblings:
        return {"name": "数字口径一致", "ok": True,
                "detail": "与已生成章节的数字口径无冲突", "issues": []}

    others: Dict[Tuple[str, str], Tuple[float, str]] = {}
    for sib in siblings:
        title = sib.get("title") or ""
        for obj, unit, val in _entities(sib.get("content") or ""):
            others.setdefault((obj, unit), (val, title))

    conflicts: List[str] = []
    for obj, unit, val in mine:
        hit = others.get((obj, unit))
        if not hit:
            continue
        other_val, title = hit
        if abs(other_val - val) > 0.011:
            conflicts.append(
                f"“{obj}”{unit}：本节 {val:g}，与《{title}》{other_val:g} 不一致")
        if len(conflicts) >= 3:
            break

    return {
        "name": "数字口径一致",
        "ok": not conflicts,
        "detail": "与已生成章节的数字口径无冲突" if not conflicts else f"发现 {len(conflicts)} 处口径不一致",
        "issues": conflicts,
    }


def review_chapter(content: str, siblings: List[Dict] = None) -> Dict:
    """跑一遍全部检查，返回 {passed, checks, issues}。siblings 为其他已生成章节。"""
    siblings = siblings or []
    content = content or ""
    checks: List[Dict] = [
        _check_source_mark(content),
        _check_length(content),
        _check_heading_leak(content),
        check_duplication(content, siblings),
        check_conflicts(content, siblings),
    ]
    issues: List[str] = []
    for c in checks:
        issues.extend(c.get("issues") or [])
    passed = not issues
    return {"passed": passed, "checks": checks, "issues": issues[:8]}
