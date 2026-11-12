"""方案质量检查：输出「可解释的问题清单」，不出无依据的分数。

对应 PRD 4.5 的五个检查项：
- 需求覆盖：每条建设要求在哪些章节得到响应，哪些待核查/未响应
- 信息缺失：预算、部署环境等未确认事项
- 来源有效性：章节引用的来源是否指向真实入库资料
- 结构完整性：已确认大纲的章节是否都生成了正文、大纲是否已确认
- 内容冲突：跨章数字口径、与项目档案相矛盾的说法

全部规则化实现，不额外调用模型：结论可复现、每条都能说清依据。
"""
from __future__ import annotations

import re
from typing import Dict, List

from .review import check_conflicts

# 需求覆盖用的停用词：这些词出现在任何方案里都不算「响应了该要求」
_STOP = set("""项目 建设 要求 必须 需要 应当 应该 可以 能够 支持 具备 提供 实现
进行 相关 内容 方面 情况 工作 系统 平台 功能 服务 管理 数据 以及 并且 或者 通过
一个 我们 他们 甲方 乙方 单位 部门 方式 主要 基本 有效 保证 确保 按照 根据 对于
其中 包括 同时 进一步 不断 持续 提升 加强 完善 推进 开展 需求 方案 设计 原则""".split())

_UNIT_PRICE = re.compile(r"总投资[^。；]{0,12}?(\d+(?:\.\d+)?)\s*(亿元|万元|元)")
_DEPLOY_WORDS = {
    "私有化": ("私有化", "本地部署", "内网部署", "自建机房"),
    "公有云": ("公有云", "阿里云", "腾讯云", "华为云", "SaaS"),
}


def _terms(text: str) -> List[str]:
    """把一条要求切成可核对的实词（2 字以上、去掉泛化词）。"""
    import jieba
    out = []
    for w in jieba.lcut(text or ""):
        w = w.strip()
        if len(w) < 2 or w in _STOP:
            continue
        if re.fullmatch(r"\d+(\.\d+)?", w):
            continue
        if w not in out:
            out.append(w)
    return out


def _cover(requirement: str, chapters: List[Dict]) -> Dict:
    terms = _terms(requirement)
    if not terms:
        return {"requirement": requirement, "status": "待核查",
                "chapters": [], "matched": [], "missing": [],
                "note": "该条要求没有可核对的实词，需人工确认"}
    hits: Dict[str, float] = {}
    matched_terms: set = set()
    for ch in chapters:
        body = ch.get("content") or ""
        if not body.strip():
            continue
        got = [t for t in terms if t in body]
        matched_terms.update(got)
        if got:
            hits[ch["title"]] = round(len(got) / len(terms), 2)
    ratio = len(matched_terms) / len(terms)
    missing = [t for t in terms if t not in matched_terms]
    if ratio >= 0.5:
        status = "已响应"
    elif matched_terms:
        status = "待核查"
    else:
        status = "未响应"
    return {
        "requirement": requirement,
        "status": status,
        "chapters": [k for k, _ in sorted(hits.items(), key=lambda kv: -kv[1])][:4],
        "matched": sorted(matched_terms),
        "missing": missing[:6],
        "note": f"命中 {len(matched_terms)}/{len(terms)} 个关键词",
    }


def _source_validity(chapters: List[Dict], docs: List[Dict]) -> List[Dict]:
    ready = {d["id"] for d in docs if d.get("status") == "ready"}
    by_doc = {d["id"]: d.get("filename") or "" for d in docs}
    items = []
    for ch in chapters:
        if not (ch.get("content") or "").strip():
            continue
        srcs = ch.get("sources") or []
        if not srcs:
            items.append({
                "chapter": ch["title"], "level": "warn",
                "message": "本章没有可追溯的依据（可能是纯经验撰写或资料未入库）",
            })
            continue
        bad = [s for s in srcs if s.get("doc_id") and s.get("doc_id") not in ready]
        if bad:
            items.append({
                "chapter": ch["title"], "level": "fail",
                "message": f"{len(bad)} 条来源指向不存在或未可用的资料："
                           + "、".join(sorted({by_doc.get(s.get('doc_id'), s.get('source') or '未知')
                                              for s in bad})[:4]),
            })
    return items


def _structure(project: Dict, chapters: List[Dict]) -> List[Dict]:
    items = []
    if not int(project.get("outline_confirmed") or 0):
        items.append({"level": "warn", "message": "大纲尚未确认，生成的正文可能与最终章节结构不一致"})
    leaves = []
    for i, ch in enumerate(chapters):
        nxt = chapters[i + 1] if i + 1 < len(chapters) else None
        if not (nxt and int(nxt.get("level") or 1) > int(ch.get("level") or 1)):
            leaves.append(ch)
    pending = [c for c in leaves if not (c.get("content") or "").strip()]
    if pending:
        items.append({
            "level": "fail",
            "message": f"{len(pending)} 个章节尚未生成正文："
                       + "、".join(c["title"] for c in pending[:6]),
        })
    if not leaves:
        items.append({"level": "fail", "message": "项目还没有可生成正文的章节"})
    return items


def _conflicts(chapters: List[Dict], profile: Dict) -> List[Dict]:
    items = []
    generated = [c for c in chapters if (c.get("content") or "").strip()]
    # 跨章数字口径冲突
    for i, ch in enumerate(generated):
        sibs = [{"title": o["title"], "content": o["content"]}
                for j, o in enumerate(generated) if j != i]
        res = check_conflicts(ch.get("content") or "", sibs)
        for issue in res.get("issues") or []:
            items.append({"level": "warn", "chapter": ch["title"], "message": issue})
    # 部署形态与项目档案相矛盾
    deploy = (profile.get("deploy_env") or "").strip()
    if deploy:
        for key, words in _DEPLOY_WORDS.items():
            if key not in deploy:
                hit = [c["title"] for c in generated
                       if any(w in (c.get("content") or "") for w in words)]
                if hit:
                    items.append({
                        "level": "warn",
                        "message": f"项目档案部署环境为「{deploy}」，但 {'、'.join(hit[:3])} 中出现「{key}」表述，需核实",
                    })
    # 预算未确认却写出确定投资额
    if not (profile.get("budget") or "").strip():
        for ch in generated:
            for m in _UNIT_PRICE.finditer(ch.get("content") or ""):
                items.append({
                    "level": "fail", "chapter": ch["title"],
                    "message": f"预算尚未确认，但正文出现确定投资额「{m.group(0)}」，应改为估算并说明假设",
                })
                break
    return items


def run_checks(project: Dict, profile: Dict, brief: Dict,
               chapters: List[Dict], docs: List[Dict]) -> Dict:
    reqs = brief.get("requirements") or []
    coverage = [_cover(r, chapters) for r in reqs]

    missing = [{"level": "warn", "message": p} for p in (brief.get("pending") or [])]
    source_items = _source_validity(chapters, docs)
    structure_items = _structure(project, chapters)
    conflict_items = _conflicts(chapters, profile)
    generated = sum(1 for c in chapters if (c.get("content") or "").strip())

    def _status(items):
        if any(x.get("level") == "fail" for x in items):
            return "fail"
        return "warn" if items else "ok"

    out = [
        {
            "key": "coverage", "name": "需求覆盖",
            "status": ("fail" if any(c["status"] == "未响应" for c in coverage)
                       else "warn" if any(c["status"] == "待核查" for c in coverage)
                       else "ok"),
            "summary": f"{len(coverage)} 条建设要求，已响应 "
                       f"{sum(1 for c in coverage if c['status']=='已响应')} 条",
            "details": coverage,
        },
        {
            "key": "missing", "name": "信息缺失",
            "status": "warn" if missing else "ok",
            "summary": f"{len(missing)} 项信息待确认" if missing else "关键信息已齐备",
            "details": missing,
        },
        {
            "key": "source", "name": "来源有效性",
            "status": _status(source_items),
            "summary": ("来源链接均指向真实入库资料" if not source_items
                        else f"{len(source_items)} 个章节的来源需处理"),
            "details": source_items,
        },
        {
            "key": "structure", "name": "结构完整性",
            "status": _status(structure_items),
            "summary": f"{generated}/{len(chapters)} 章已生成",
            "details": structure_items,
        },
        {
            "key": "conflict", "name": "内容冲突",
            "status": _status(conflict_items),
            "summary": ("未发现跨章矛盾" if not conflict_items
                        else f"{len(conflict_items)} 处需人工核实"),
            "details": conflict_items,
        },
    ]
    total = sum(len(x["details"]) for x in out)
    blocking = [d for x in out for d in x["details"] if d.get("level") == "fail"]
    return {
        "items": out,
        "issue_count": total,
        "blocking_count": len(blocking),
        "note": "自动检查只作辅助提示，不能替代人工审核；未发现的逻辑或事实错误仍需人工核对。",
    }
    total = sum(len(x["details"]) for x in out)
    blocking = [d for x in out for d in x["details"] if d.get("level") == "fail"]
    return {
        "items": out,
        "issue_count": total,
        "blocking_count": len(blocking),
        "note": "自动检查只作辅助提示，不能替代人工审核；未发现的逻辑或事实错误仍需人工核对。",
    }
