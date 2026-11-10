"""M3 写作流：提纲 → 逐章节检索知识库生成 → 审查 → 修订（循环，有界）→ 汇编。

用 LangGraph 状态图表达，核心是一个"生成 → 审查 → 修订 → 再审"的反馈循环：
review_all 的 conditional edge 决定是继续 revise（仍有问题且未达轮次上限）还是进入 assemble。
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict
from typing import Dict, List

from langgraph.graph import END, START, StateGraph

from ..config import settings
from ..llm.chat import ChatProvider
from .state import ProposalState


def _source_key(source: str) -> str:
    """来源归一化：去路径、去“:第N页”、去扩展名、去空白，便于与模型正文做主干匹配。"""
    if not source:
        return ""
    base = os.path.basename(source)
    base = re.sub(r"[:：]?第?\d+\s*页", "", base)
    base = os.path.splitext(base)[0]
    return re.sub(r"\s+", "", base)


class ProposalWorkflow:
    def __init__(self, kb, chat: ChatProvider) -> None:
        self.kb = kb
        self.chat = chat
        self._graph = None

    # ---------- 节点 ----------
    def _generate_all(self, state: ProposalState) -> Dict:
        """为每个章节：以章节标题/要求为查询，三级检索知识库，再调用 LLM 生成正文。"""
        retrievals: Dict[str, List[Dict]] = {}
        drafts: Dict[str, str] = {}
        for ch in state["chapters"]:
            query = f"{ch.get('title', '')} {ch.get('description', '')}".strip()
            cites = self.kb.retrieve(
                query, project_id=state["project_id"], chapter_id=ch["id"],
                top_k=settings.retrieval_top_k,
            )
            cite_dicts = [asdict(c) for c in cites]
            retrievals[ch["id"]] = cite_dicts
            drafts[ch["id"]] = self.chat.generate_chapter(
                chapter=ch, project_name=state["project_name"], citations=cite_dicts,
            )
        return {"retrievals": retrievals, "drafts": drafts}

    def _review_all(self, state: ProposalState) -> Dict:
        """审查每个章节：内容长度、是否标注知识库来源。问题列表为空表示通过。"""
        reviews: Dict[str, List[str]] = {}
        for ch in state["chapters"]:
            cid = ch["id"]
            draft = state["drafts"].get(cid, "")
            sources = [c.get("source", "") for c in state["retrievals"].get(cid, [])]
            issues: List[str] = []
            if len(draft.strip()) < 100:
                issues.append("章节内容过短（不足 100 字）")
            keys = [k for k in (_source_key(s) for s in sources) if k]
            draft_flat = re.sub(r"\s+", "", draft)
            if keys and not any(k in draft_flat for k in keys):
                issues.append("未标注知识库来源（应包含来源文件名）")
            reviews[cid] = issues
        return {"reviews": reviews}

    def _should_revise(self, state: ProposalState) -> str:
        if state["iterations"] >= state["max_iterations"]:
            return "assemble"
        if any(state["reviews"].get(ch["id"]) for ch in state["chapters"]):
            return "revise"
        return "assemble"

    def _revise_all(self, state: ProposalState) -> Dict:
        """针对有问题的章节，携带审查问题重新生成。"""
        drafts = dict(state["drafts"])
        for ch in state["chapters"]:
            cid = ch["id"]
            issues = state["reviews"].get(cid, [])
            if issues:
                drafts[cid] = self.chat.generate_chapter(
                    chapter=ch, project_name=state["project_name"],
                    citations=state["retrievals"].get(cid, []), issues=issues,
                )
        return {"drafts": drafts, "iterations": state["iterations"] + 1}

    def _assemble(self, state: ProposalState) -> Dict:
        """按章节顺序汇编完整方案：标题 + 目录 + 各章正文 + 引用来源。"""
        chapters = sorted(state["chapters"], key=lambda x: x.get("sort", 0))
        parts = [
            f"# {state['project_name']} 技术方案",
            "",
            "> 本文档由写方案智能体自动生成。每个章节基于三级知识库（全局/项目/章节）检索引用编制，引用可回溯至原始文档。",
            "",
        ]
        toc = ["## 目录"]
        for ch in chapters:
            toc.append(f"- {ch.get('code', '')} {ch.get('title', '')}")
        parts.append("\n".join(toc))
        parts.append("")

        refs: set = set()
        for ch in chapters:
            draft = state["drafts"].get(ch["id"], "").strip()
            if draft:
                parts.append(draft)
                parts.append("")
            else:
                parts.append(f"## {ch.get('code', '')} {ch.get('title', '')}")
                parts.append("")
                parts.append("（本章未生成有效正文，可重跑写作流补齐。）")
                parts.append("")
            for c in state["retrievals"].get(ch["id"], []):
                refs.add((c.get("tier", ""), c.get("source", "")))

        parts.append("## 引用来源")
        parts.extend(f"- [{tier}级] {src}" for tier, src in sorted(refs))
        return {"proposal": "\n".join(parts)}

    # ---------- 构图与运行 ----------
    def build(self):
        g = StateGraph(ProposalState)
        g.add_node("generate_all", self._generate_all)
        g.add_node("review_all", self._review_all)
        g.add_node("revise_all", self._revise_all)
        g.add_node("assemble", self._assemble)
        g.set_entry_point("generate_all")
        g.add_edge("generate_all", "review_all")
        g.add_conditional_edges(
            "review_all", self._should_revise, {"revise": "revise_all", "assemble": "assemble"}
        )
        g.add_edge("revise_all", "review_all")
        g.add_edge("assemble", END)
        self._graph = g.compile()
        return self._graph

    def run(self, project_id: str, project_name: str, chapters: List[Dict]) -> ProposalState:
        if self._graph is None:
            self.build()
        initial: ProposalState = {
            "project_id": project_id,
            "project_name": project_name,
            "chapters": chapters,
            "retrievals": {},
            "drafts": {},
            "reviews": {},
            "iterations": 0,
            "max_iterations": settings.workflow_max_iterations,
            "proposal": "",
            "log": [],
        }
        return self._graph.invoke(initial)
