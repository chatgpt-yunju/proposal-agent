"""写作流状态（LangGraph State，total=False 便于增量更新）。"""
from typing import Dict, List, TypedDict


class ProposalState(TypedDict, total=False):
    project_id: str
    project_name: str
    chapters: List[Dict]                # 章节规格：id/code/title/description/sort
    retrievals: Dict[str, List[Dict]]   # cid -> 三级知识库检索引用（Citation dict）
    drafts: Dict[str, str]              # cid -> 章节 Markdown 正文
    reviews: Dict[str, List[str]]       # cid -> 审查问题列表（空 = 通过）
    iterations: int                     # 已执行的修订轮次
    max_iterations: int                 # 审查-修订循环上限
    proposal: str                       # 汇编后的完整方案
    log: List[str]
