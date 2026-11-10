"""知识库核心数据模型：三级作用域 Scope、切块 Chunk、引用 Citation。"""
from dataclasses import dataclass
from typing import Optional

TIERS = ("global", "project", "chapter")


@dataclass
class Scope:
    """三级作用域：global | project | chapter"""
    tier: str
    project_id: Optional[str] = None
    chapter_id: Optional[str] = None

    def validate(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"tier 必须是 {TIERS} 之一，收到 {self.tier!r}")
        if self.tier == "project" and not self.project_id:
            raise ValueError("project 级知识库必须提供 project_id")
        if self.tier == "chapter" and (not self.project_id or not self.chapter_id):
            raise ValueError("chapter 级知识库必须同时提供 project_id 与 chapter_id")

    def payload(self) -> dict:
        return {
            "tier": self.tier,
            "project_id": self.project_id or "",
            "chapter_id": self.chapter_id or "",
        }

    def filter_must(self) -> list:
        conds = [("tier", self.tier)]
        if self.project_id:
            conds.append(("project_id", self.project_id))
        if self.chapter_id:
            conds.append(("chapter_id", self.chapter_id))
        return conds

    def __str__(self) -> str:
        return f"{self.tier}:{self.project_id or '-'}:{self.chapter_id or '-'}"


@dataclass
class Chunk:
    """一个待入库的知识切块。"""
    id: str
    text: str
    scope: Scope
    doc_id: str
    source: str          # 文件名[:页码]，供引用溯源
    title: str = ""


@dataclass
class Citation:
    """一次检索命中的引用条目。"""
    text: str
    tier: str
    project_id: Optional[str]
    chapter_id: Optional[str]
    doc_id: str
    source: str
    title: str
    score: float
