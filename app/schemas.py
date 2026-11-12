"""FastAPI 请求/响应模型。"""
from typing import List, Optional

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str
    description: str = ""


class ProjectOut(BaseModel):
    id: str
    name: str
    description: str
    created_at: str
    archived: int = 0


class ChapterCreate(BaseModel):
    code: str = ""
    title: str
    description: str = ""


class ChapterRename(BaseModel):
    """改章节标题/编号/目标字数：只传要改的字段，未传的保持原值。"""
    title: Optional[str] = None
    code: Optional[str] = None
    description: Optional[str] = None
    target_chars: Optional[int] = None


class FrontMatterIn(BaseModel):
    """方案的前言 / 摘要（导出时插在目录之前）。"""
    preface: Optional[str] = None
    abstract: Optional[str] = None


class ChapterOut(BaseModel):
    id: str
    project_id: str
    code: str
    title: str
    description: str
    sort: int
    level: int = 1
    target_chars: int = 0
    content: str = ""


class CitationOut(BaseModel):
    text: str
    tier: str
    project_id: Optional[str] = None
    chapter_id: Optional[str] = None
    doc_id: str
    source: str
    title: str
    score: float


class RetrieveRequest(BaseModel):
    query: str
    project_id: Optional[str] = None
    chapter_id: Optional[str] = None
    top_k: int = Field(default=6, ge=1, le=20)


class RetrieveResponse(BaseModel):
    query: str
    citations: List[CitationOut]


class StatsOut(BaseModel):
    qdrant_count: int
    sparse_count: int
    tiers: dict


# ---- M3/M4/M5：写作流生成与导出 ----
class WorkflowGenerateRequest(BaseModel):
    project_id: str


class ChapterGenOut(BaseModel):
    code: str
    title: str
    chars: int
    review_ok: bool
    issues: List[str] = []


class WorkflowOut(BaseModel):
    project_id: str
    project_name: str
    iterations: int
    proposal: str
    md_path: str
    docx_path: str
    pdf_path: str
    chapters: List[ChapterGenOut]


class RecordOut(BaseModel):
    id: str
    project_id: str
    created_at: str
    iterations: int
    chapter_count: int
    total_chars: int
    chapters: str
    md_path: str
    docx_path: str
    pdf_path: str
