"""FastAPI 入口：项目/章节管理、三级知识库、写作流生成与导出、生成历史、静态前端。"""
import os
import re
import json
import queue
import shutil
import time
import uuid
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# 配置日志系统
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('data/app.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 降级兜底文本的特征串：出现即说明本次不是真实模型产出，不能写库覆盖已有正文
_DEGRADED_MARK = "本章模型响应超时"

from . import db
from .config import settings
from .kb.models import Scope
from .kb.router import KnowledgeBase
from .llm.chat import build_chat
from .workflow import ProposalWorkflow
from .export import export_proposal, md_to_docx, md_to_pdf
from .template_style import DEFAULT_STYLE, extract_from_docx, number_chapters
from .review import review_chapter, strip_source_marks
from .schemas import (
    ChapterCreate,
    ChapterGenOut,
    ChapterOut,
    ChapterRename,
    FrontMatterIn,
    CitationOut,
    ProjectCreate,
    ProjectOut,
    RecordOut,
    RetrieveRequest,
    RetrieveResponse,
    StatsOut,
    WorkflowGenerateRequest,
    WorkflowOut,
)


def _backfill_generation_records() -> None:
    """把历史已有产物（data/output/<项目>_方案.{md,docx,pdf}）补录为生成记录，并做版本快照，保证可追溯。"""
    out_dir = settings.resolved_output_dir
    os.makedirs(out_dir, exist_ok=True)
    for p in db.list_projects():
        if db.list_generation_records(p["id"]):
            continue  # 已有记录的项目不重复补录
        base = os.path.join(out_dir, f"{p['name']}_方案")
        md_p, docx_p, pdf_p = base + ".md", base + ".docx", base + ".pdf"
        if not all(os.path.exists(x) for x in (md_p, docx_p, pdf_p)):
            continue
        try:
            md_text = open(md_p, encoding="utf-8").read()
        except OSError:
            continue
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap = os.path.join(out_dir, f"{p['name']}_方案_{ts}")
        for ext in ("md", "docx", "pdf"):
            shutil.copyfile(base + "." + ext, snap + "." + ext)
        chapter_count = len(re.findall(r"(?m)^## \d{2} ", md_text))
        db.add_generation_record(p["id"], 0, [], len(md_text),
                                 snap + ".md", snap + ".docx", snap + ".pdf",
                                 chapter_count=chapter_count)
        print(f"[backfill] {p['name']} -> {chapter_count} 章记录")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    _backfill_generation_records()
    yield
    # 退出前把向量库内存增量落盘，避免重启丢数据
    try:
        kb.flush()
    except Exception:
        logger.warning("退出时向量库 flush 失败", exc_info=True)


app = FastAPI(title="写方案智能体 · 三级知识库核心", version="0.1.0", lifespan=lifespan)
kb = KnowledgeBase()

# 请求日志中间件
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"→ {request.method} {request.url.path}")
    try:
        response = await call_next(request)
        logger.info(f"← {response.status_code} {request.url.path}")
        return response
    except Exception as e:
        logger.error(f"✗ {request.method} {request.url.path} 出错: {str(e)}", exc_info=True)
        raise


# ---------- 操作审计 ----------
def audit(project_id, action: str, target: str = "", status: str = "success",
          detail: str = "", chapter_id=None) -> None:
    """记录一条操作审计日志。审计属于旁路能力，失败不得影响主业务流程。"""
    try:
        db.add_operation(project_id, action, target, status, detail, chapter_id)
    except Exception:
        logger.warning(f"写入操作审计失败: action={action} target={target}", exc_info=True)


@app.get("/api/projects/{pid}/operations")
def list_project_operations(pid: str, limit: int = 100):
    """返回指定项目的操作审计流水，按时间倒序。"""
    return db.list_operations(pid, limit)


@app.get("/api/operations")
def list_all_operations(limit: int = 100):
    """返回全局操作审计流水（跨项目）。"""
    return db.list_operations(None, limit)


# ---------- 项目 ----------
@app.post("/api/projects", response_model=ProjectOut)
def create_project(body: ProjectCreate):
    proj = db.create_project(body.name, body.description)
    audit(proj["id"], "project_create", body.name, detail="创建项目")
    return proj


@app.get("/api/projects", response_model=List[ProjectOut])
def list_projects(include_archived: bool = False):
    """项目列表。默认隐藏已归档的项目，前端勾选「显示已归档」时才带 include_archived=1。"""
    return db.list_projects(include_archived=include_archived)


@app.post("/api/projects/{pid}/archive", response_model=ProjectOut)
def archive_project(pid: str, body: dict):
    """归档 / 取消归档项目：归档后不出现在项目列表里，数据仍然保留。"""
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    archived = 1 if (body or {}).get("archived", True) else 0
    proj = db.update_project(pid, archived=archived)
    audit(pid, "project_archive" if archived else "project_unarchive",
          proj.get("name", ""), detail="归档项目" if archived else "取消归档")
    return proj


@app.put("/api/projects/{pid}", response_model=ProjectOut)
def rename_project(pid: str, body: ProjectCreate):
    """重命名项目。"""
    proj = db.update_project(pid, name=body.name, description=body.description)
    if not proj:
        raise HTTPException(404, "项目不存在")
    audit(pid, "project_update", body.name, detail="更新项目信息")
    return proj


# ---------- 章节 ----------
@app.post("/api/projects/{pid}/chapters", response_model=ChapterOut)
def create_chapter(pid: str, body: ChapterCreate):
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    ch = db.create_chapter(pid, body.code, body.title, body.description)
    audit(pid, "chapter_create", body.title, detail=f"编号 {body.code or '无'}", chapter_id=ch["id"])
    return ch


@app.get("/api/projects/{pid}/chapters", response_model=List[ChapterOut])
def list_chapters(pid: str):
    return db.list_chapters(pid)


@app.delete("/api/chapters/{cid}")
def delete_chapter(cid: str):
    ch = db.get_chapter(cid)
    if not db.delete_chapter(cid):
        raise HTTPException(404, "章节不存在")
    if ch:
        audit(ch.get("project_id"), "chapter_delete", ch.get("title", ""),
              detail="删除章节", chapter_id=cid)
    return {"deleted": cid}


@app.patch("/api/chapters/{cid}", response_model=ChapterOut)
def rename_chapter(cid: str, body: ChapterRename):
    """改章节标题/编号。模板解析出来的章节名常常带多余字，允许逐个改。"""
    ch = db.get_chapter(cid)
    if not ch:
        raise HTTPException(404, "章节不存在")
    fields = {}
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(422, "标题不能为空")
        fields["title"] = title
    if body.code is not None:
        fields["code"] = body.code.strip()
    if body.description is not None:
        fields["description"] = body.description.strip()
    if not fields:
        return ch
    updated = db.update_chapter_fields(cid, **fields)
    if not updated:
        raise HTTPException(404, "章节不存在")
    audit(ch.get("project_id"), "chapter_rename", fields.get("title", ch.get("title", "")),
          detail="改标题：" + (ch.get("title") or "") + " → " + fields.get("title", ch.get("title") or ""),
          chapter_id=cid)
    return updated


# ---------- 模板上传：自动解析章节结构 ----------
def _parse_template_chunks(text: str) -> list:
    """从模板文本中解析章节列表，支持六级标题层级。

    约定：Markdown 的 # ~ ###### 分别对应第 1~6 级标题（上传 docx 时按 Heading 1~6 转换而来）。
    """
    import re
    chapters = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip().lstrip("#").strip()
            if not title:
                continue
            code = str(len(chapters) + 1).zfill(2)
            chapters.append({"code": code, "title": title, "description": "", "level": level})
            continue
    return chapters


def _docx_to_template_text(path: str) -> str:
    """把 Word 模板的段落转成带 # 级别标记的文本，供 _parse_template_chunks 解析。

    只有「Heading N / 标题 N」这种带级别序号的样式才算章节标题；
    封面标题、目录标题、图表标题不带序号，不能当章节，否则会把方案名解析成一章。
    """
    from docx import Document
    doc = Document(path)
    lines = []
    for para in doc.paragraphs:
        if not para.text.strip():
            continue
        style = para.style.name if para.style else ""
        text_line = para.text.strip()
        if "Heading" in style or "标题" in style:
            m = re.search(r"(\d+)", style)
            if not m:
                lines.append(text_line)
                continue
            level = int(m.group(1))
            # 第 1~6 级标题都转成 Markdown 标题（# ~ ######）：
            # 原来只认前三级、把 Heading 4~6 当正文丢弃，导致模板里的深层子节整片丢失
            if 1 <= level <= 6:
                lines.append("#" * level + " " + text_line)
            else:
                lines.append(text_line)
        else:
            lines.append(text_line)
    return "\n".join(lines)


TEMPLATE_MAX_BYTES = 30 * 1024 * 1024  # 30 MB：模板是章节骨架，超出这个量级基本是误传正文


def _assert_docx_payload(content: bytes, filename: str) -> None:
    """模板入口的格式闸门：只放行真正的 .docx。

    三重校验，避免只改扩展名或空文件混进来：
    1. 扩展名必须是 .docx（.doc / .md / .pdf 一律拒绝并给出可操作提示）；
    2. ZIP 魔数 PK\\x03\\x04 —— Office Open XML 本质是 zip 容器，旧版 .doc 是 OLE 复合文档（D0 CF 11 E0）；
    3. 包内必须存在 word/document.xml，否则是改了后缀的其它 zip。
    """
    import zipfile
    import io as _io

    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".doc":
        raise HTTPException(
            422,
            "不支持旧版 .doc（Word 97-2003）模板，请在 Word 中「另存为」.docx 后再上传",
        )
    if ext != ".docx":
        raise HTTPException(
            422,
            f"模板仅支持 Word（.docx）格式，当前文件类型为 {ext or '未知'}",
        )
    if not content:
        raise HTTPException(422, "上传的模板文件为空")
    if len(content) > TEMPLATE_MAX_BYTES:
        raise HTTPException(
            413,
            f"模板文件过大（{len(content) / 1024 / 1024:.1f} MB），上限 {TEMPLATE_MAX_BYTES // 1024 // 1024} MB",
        )
    if not content.startswith(b"PK\x03\x04"):
        raise HTTPException(
            422,
            "文件内容不是有效的 .docx（疑似把其它格式改了扩展名），请用 Word 另存为 .docx",
        )
    try:
        with zipfile.ZipFile(_io.BytesIO(content)) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        raise HTTPException(422, "文件已损坏或不是有效的 .docx 压缩包")
    if "word/document.xml" not in names:
        raise HTTPException(
            422,
            "文件缺少 word/document.xml，不是标准的 Word 文档",
        )


@app.post("/api/projects/{pid}/upload_template")
def upload_template(pid: str, file: UploadFile = File(...)):
    """上传方案模板（仅 .docx），自动解析章节结构、采集版式并批量创建章节。

    格式限制：只收 Word（.docx）。历史版本还支持 .md/.txt/.pdf，但 PDF 抽文本靠
    启发式猜标题、markdown 无法携带字体字号与编号定义，两者都无法驱动「按模板版式导出」，
    因此收口为单一入口，避免产出格式漂移。
    """
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")

    filename = file.filename or ""
    content = file.file.read()
    _assert_docx_payload(content, filename)

    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx")
    tmp.write(content)
    tmp.close()
    try:
        # 关键：同一份模板里既解析章节结构，也采集标题/正文样式与编号定义，
        # 这样导出 Word/PDF 和网页预览都按模板自己的规则渲染，而不是写死一套公文样式。
        template_style = extract_from_docx(tmp.name)
        template_style["source"] = f"docx:{os.path.basename(filename)}"
        text = _docx_to_template_text(tmp.name)
    except HTTPException:
        raise
    except Exception as exc:  # python-docx 对损坏文件抛各种异常，统一转成 422
        raise HTTPException(422, f"模板解析失败，请确认文件可在 Word 中正常打开：{exc}")
    finally:
        os.unlink(tmp.name)

    # 解析章节
    chapters = _parse_template_chunks(text)
    if not chapters:
        raise HTTPException(
            400,
            "未从模板中识别到章节结构：请在 Word 里用「标题 1 / 标题 2 …」样式标记章节后再上传",
        )

    # 批量创建/更新章节（已存在则更新层级）
    existing = db.list_chapters(pid)
    existing_map = {c["title"]: c for c in existing}
    created = []
    updated = []
    for ch in chapters:
        if ch["title"] in existing_map:
            # 已存在则更新层级
            db.update_chapter_level(existing_map[ch["title"]]["id"], ch.get("level", 1))
            updated.append(ch["title"])
            continue
        db.create_chapter(pid, ch["code"], ch["title"], ch["description"], level=ch.get("level", 1))
        created.append(ch)

    # 保存模板样式定义（标题各级字体字号/加粗/对齐 + 编号格式 + 正文字体）
    style_saved = False
    if template_style:
        db.update_project(pid, style_json=json.dumps(template_style, ensure_ascii=False))
        style_saved = True

    audit(pid, "template_import", os.path.basename(file.filename or "模板"),
          detail=(f"识别 {len(chapters)} 章，新增 {len(created)}，更新 {len(updated)}"
                  + ("，已采集模板样式与编号定义" if style_saved else "")))

    return {
        "total": len(chapters),
        "created": len(created),
        "updated": len(updated),
        "chapters": created,
        "updated_titles": updated,
        "style": template_style,
        "style_saved": style_saved,
    }


# ---------- 知识库入库 ----------
def _temp_write(content: bytes, filename: str) -> str:
    tmp_dir = os.path.join(os.path.dirname(settings.resolved_vector_dir), "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    p = os.path.join(tmp_dir, f"u_{uuid.uuid4().hex}{os.path.splitext(filename or 'x')[1]}")
    with open(p, "wb") as fh:
        fh.write(content)
    return p


@app.post("/api/kb/ingest")
def ingest(
    tier: str = Form(...),
    project_id: Optional[str] = Form(None),
    chapter_id: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
):
    scope = Scope(tier=tier, project_id=project_id, chapter_id=chapter_id)
    try:
        scope.validate()
    except ValueError as e:
        raise HTTPException(422, str(e))
    if tier in ("project", "chapter") and project_id and not db.get_project(project_id):
        raise HTTPException(404, "项目不存在")
    if tier == "chapter" and chapter_id:
        if project_id is None or not any(c["id"] == chapter_id for c in db.list_chapters(project_id)):
            raise HTTPException(404, "章节不存在")

    total, per_doc = 0, []
    for f in files:
        label = f.filename or "x.txt"
        path = _temp_write(f.file.read(), label)
        doc_id = os.path.splitext(label)[0] or label
        try:
            n = kb.ingest_file(path, tier, project_id, chapter_id,
                               doc_id=doc_id, source_label=label)
            total += n
            per_doc.append({"file": label, "chunks": n})
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    tier_label = {"global": "全局库", "project": "项目库", "chapter": "章节库"}.get(tier, tier)
    file_names = "、".join(x["file"] for x in per_doc)[:300]
    audit(project_id, "kb_ingest", file_names,
          detail=f"入库到{tier_label}，共 {total} 个切块，{len(per_doc)} 个文件"
                 + (f"，目标章节 {chapter_id}" if chapter_id else ""))
    return {"ingested": total, "files": per_doc}


# ---------- 检索 ----------
@app.post("/api/kb/retrieve", response_model=RetrieveResponse)
def retrieve(body: RetrieveRequest):
    cites = kb.retrieve(body.query, body.project_id, body.chapter_id, body.top_k)
    return RetrieveResponse(query=body.query, citations=[CitationOut(**c.__dict__) for c in cites])


@app.get("/api/kb/stats", response_model=StatsOut)
def stats():
    return kb.stats()


@app.get("/api/kb/list")
def kb_list(project_id: Optional[str] = None, with_text: bool = False):
    """列出知识库文档。with_text=0（默认）只返回条数与来源，避免把全库原文塞进一次响应。"""
    docs = kb.list_docs(project_id, with_text=with_text)
    return {"docs": docs, "total": len(docs)}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "embedding_provider": settings.embedding_provider,
        "vector_backend": settings.kb_vector_backend,
    }


# ---------- M3/M4：写作流生成与导出 ----------
# 方案 md 是"整份读入 → 改一节 → 整份写回"，并发写会互相覆盖丢章节，
# 因此读改写这一段加进程内互斥；LLM 与检索都在锁外，不影响并发提速。
_MD_LOCK = threading.Lock()


def _proposal_paths(project_name: str) -> dict:
    out_dir = settings.resolved_output_dir
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"{project_name}_方案")
    return {"md": base + ".md", "docx": base + ".docx", "pdf": base + ".pdf"}


@app.post("/api/workflow/generate", response_model=WorkflowOut)
def generate_proposal(body: WorkflowGenerateRequest):
    proj = db.get_project(body.project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    chapters = db.list_chapters(body.project_id)
    if not chapters:
        raise HTTPException(400, "请先为项目创建章节")
    specs = [{"id": c["id"], "code": c["code"], "title": c["title"],
              "description": c["description"], "sort": c["sort"]} for c in chapters]

    chat = build_chat()
    wf = ProposalWorkflow(kb, chat)
    state = wf.run(body.project_id, proj["name"], specs)

    paths = _proposal_paths(proj["name"])
    with open(paths["md"], "w", encoding="utf-8") as f:
        f.write(state["proposal"])
    export_proposal(state["proposal"], paths["docx"], paths["pdf"])

    gens = []
    for ch in specs:
        cid = ch["id"]
        gens.append(ChapterGenOut(
            code=ch["code"], title=ch["title"], chars=len(state["drafts"][cid]),
            review_ok=not state["reviews"][cid], issues=state["reviews"][cid],
        ))

    # 版本快照 + 生成记录（历史可查、可下载任意一版）
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap = os.path.join(os.path.dirname(paths["md"]), f"{proj['name']}_方案_{ts}")
    for ext in ("md", "docx", "pdf"):
        shutil.copyfile(paths[ext], snap + "." + ext)
    rec = db.add_generation_record(
        body.project_id, state["iterations"],
        [{"code": g.code, "title": g.title, "chars": g.chars, "review_ok": g.review_ok} for g in gens],
        sum(g.chars for g in gens), snap + ".md", snap + ".docx", snap + ".pdf",
    )

    return WorkflowOut(
        project_id=body.project_id, project_name=proj["name"], iterations=state["iterations"],
        proposal=state["proposal"], md_path=paths["md"], docx_path=paths["docx"],
        pdf_path=paths["pdf"], chapters=gens,
    )


@app.post("/api/workflow/generate_chapter_stream")
async def generate_chapter_stream(project_id: str, chapter_id: str):
    """流式生成章节，SSE 输出，逐字返回内容。"""
    from fastapi.responses import StreamingResponse
    import json

    logger.info(f"开始生成章节: project={project_id}, chapter={chapter_id}")
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    chapters = db.list_chapters(project_id)
    target = next((c for c in chapters if c["id"] == chapter_id), None)
    if not target:
        raise HTTPException(404, "章节不存在")

    chat = build_chat()
    chapter_spec = {
        "id": target["id"], "code": target["code"], "title": target["title"],
        "description": target["description"], "sort": target["sort"],
    }

    async def event_stream():
        full_content = ""
        full_reasoning = ""

        # 立即发送第一步：开始检索知识库
        yield f"data: {json.dumps({'type': 'step', 'step': 0, 'status': 'running', 'msg': '正在检索三级知识库...'})}\n\n"

        # 检索知识库（同步操作，放在后面执行）
        query = f"{target['title']} {target.get('description', '')}".strip()
        cites = kb.retrieve(query, project_id=project_id, chapter_id=chapter_id,
                            top_k=settings.retrieval_top_k)
        from dataclasses import asdict
        cite_dicts = [asdict(c) for c in cites]

        # 发送第二步：检索完成，开始整理引用
        yield f"data: {json.dumps({'type': 'step', 'step': 0, 'status': 'done', 'msg': f'检索完成，找到 {len(cite_dicts)} 条引用'})}\n\n"
        yield f"data: {json.dumps({'type': 'step', 'step': 1, 'status': 'running', 'msg': '正在整理引用素材...'})}\n\n"
        yield f"data: {json.dumps({'type': 'citations', 'count': len(cite_dicts), 'items': [{'source': c['source'], 'tier': c['tier'], 'text': c['text'][:100] + '...' if len(c['text']) > 100 else c['text']} for c in cite_dicts]})}\n\n"

        # 发送第三步：开始流式生成
        yield f"data: {json.dumps({'type': 'step', 'step': 2, 'status': 'running', 'msg': '正在调用 LLM 流式生成...'})}\n\n"

        # 流式生成内容
        async for content_delta, reasoning_delta in chat.generate_chapter_stream(
            chapter=chapter_spec, project_name=proj["name"], citations=cite_dicts
        ):
            full_content += content_delta
            full_reasoning += reasoning_delta
            yield f"data: {json.dumps({'type': 'content', 'delta': content_delta, 'reasoning_delta': reasoning_delta})}\n\n"

        # 校审：单章体例 + 与其他已生成章节的一致性（重复、数字口径）
        siblings = _review_siblings(project_id, chapter_id)
        review = review_chapter(full_content, siblings)

        # 发送第四步：审查完成，开始汇编
        msg = ("审查通过，格式与口径一致" if review["passed"]
               else f"审查发现 {len(review['issues'])} 处问题")
        yield f"data: {json.dumps({'type': 'review', 'passed': review['passed'], 'checks': review['checks'], 'issues': review['issues']}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'step', 'step': 3, 'status': 'done', 'msg': msg}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'step', 'step': 4, 'status': 'running', 'msg': '正在汇编输出...'})}\n\n"

        # 保存到文件
        paths = _proposal_paths(proj["name"])
        ch_title = target["title"]
        ch_code = target["code"]
        # LLM 只生成纯正文，我们自己拼接标题；来源标注一律剥掉，正文不留引用
        body = strip_source_marks(full_content.strip())
        # 降级兜底文本（模型超时拼装的检索要点）不落库，避免把已生成的正文冲成碎片
        if _DEGRADED_MARK in body:
            yield f"data: {json.dumps({'type': 'error', 'msg': '模型响应超时，本章原正文未改动，请重试'}, ensure_ascii=False)}\n\n"
            return
        new_chapter = f"## {ch_code} {ch_title}\n\n{body}"

        # 正则匹配：从该章节标题开始，到下一个 ## 或文件结束
        pattern = re.compile(
            r"^##\s+" + re.escape(ch_code) + r"\s+.*?(?=^##\s|\Z)",
            re.MULTILINE | re.DOTALL,
        )

        # 读改写整段加锁：并发批量时两个请求同时"整份读入→写回"会互相覆盖、丢章节
        with _MD_LOCK:
            if os.path.exists(paths["md"]):
                with open(paths["md"], "r", encoding="utf-8") as f:
                    old_md = f.read()
            else:
                old_md = f"# {proj['name']} 技术方案\n"

            if pattern.search(old_md):
                new_md = pattern.sub(new_chapter, old_md, count=1)
            else:
                new_md = old_md.rstrip() + "\n\n" + new_chapter + "\n"

            # 先写临时文件，成功后再替换，避免写坏原文件
            tmp_md = paths["md"] + ".tmp"
            try:
                with open(tmp_md, "w", encoding="utf-8") as f:
                    f.write(new_md)
                os.replace(tmp_md, paths["md"])
                # 不再逐章导出 Word/PDF：整份导出走数据库 + 模版样式，逐章导出既慢又是另一套样式
                db.update_chapter_content(chapter_id, body)
                logger.info(f"章节生成完成: chapter={chapter_id}, 字数={len(full_content)}, 引用数={len(cite_dicts)}")
            except Exception as e:
                logger.error(f"章节生成写入失败: {str(e)}", exc_info=True)
                if os.path.exists(tmp_md):
                    os.remove(tmp_md)
                raise

        yield f"data: {json.dumps({'type': 'done', 'chars': len(full_content), 'citations': len(cite_dicts), 'reasoning': full_reasoning, 'proposal': new_md})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.post("/api/workflow/generate_chapter")
def generate_single_chapter(project_id: str, chapter_id: str):
    """单独生成/重新生成指定章节，不影响其他章节。"""
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    chapters = db.list_chapters(project_id)
    target = next((c for c in chapters if c["id"] == chapter_id), None)
    if not target:
        raise HTTPException(404, "章节不存在")

    # 检索知识库 + 生成章节
    query = f"{target['title']} {target.get('description', '')}".strip()
    cites = kb.retrieve(query, project_id=project_id, chapter_id=chapter_id,
                        top_k=settings.retrieval_top_k)
    from dataclasses import asdict
    cite_dicts = [asdict(c) for c in cites]

    chat = build_chat()
    chapter_spec = {
        "id": target["id"], "code": target["code"], "title": target["title"],
        "description": target["description"], "sort": target["sort"],
    }
    audit(project_id, "chapter_generate", target["title"], "started",
          detail="开始生成", chapter_id=chapter_id)
    t_start = time.time()
    try:
        draft = chat.generate_chapter(chapter=chapter_spec, project_name=proj["name"], citations=cite_dicts)
    except Exception as e:
        audit(project_id, "chapter_generate", target["title"], "failed",
              detail=f"生成失败：{str(e)[:300]}", chapter_id=chapter_id)
        logger.error(f"章节生成失败: chapter={chapter_id}, {str(e)}", exc_info=True)
        # 把真实原因带回前端，避免批量生成时只能看到 "HTTP 500"
        raise HTTPException(500, f"生成失败：{str(e)[:200]}")
    reasoning = getattr(chat, '_last_reasoning', '')
    # 来源标注一律剥掉：正文章节里不保留【来源：…】这类引用痕迹
    draft = strip_source_marks(draft)

    # 降级兜底文本不许覆盖已有正文：模型超时/连续失败时 generate_chapter 会返回
    # 「> 说明：本章模型响应超时…」的拼装内容，若照常写库会把原本成文的章节冲成碎片。
    if getattr(chat, '_last_fallback', False) or _DEGRADED_MARK in draft:
        audit(project_id, "chapter_generate", target["title"], "failed",
              detail="模型响应超时触发降级兜底，已保留原正文未覆盖", chapter_id=chapter_id)
        raise HTTPException(502, "模型响应超时（本章原正文已保留，未被覆盖），请稍后重试")

    # 校审：单章体例 + 与其他已生成章节的一致性
    review = review_chapter(draft, _review_siblings(project_id, chapter_id))

    # 更新方案文件：把该章节替换为新生成的内容
    paths = _proposal_paths(proj["name"])
    ch_title = target["title"]
    ch_code = target["code"]
    new_chapter = f"## {ch_code} {ch_title}\n\n{draft}"
    # 只按章号定位，避免标题被改过（或补过编号）后匹配失败、导致同一章在 md 里重复追加
    pattern = re.compile(
        r"^##\s+" + re.escape(ch_code) + r"\s+.*?(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )

    # 读改写整段加锁：并发批量时两个请求同时"整份读入→写回"会互相覆盖、丢章节
    with _MD_LOCK:
        if os.path.exists(paths["md"]):
            with open(paths["md"], "r", encoding="utf-8") as f:
                old_md = f.read()
        else:
            old_md = f"# {proj['name']} 技术方案\n"

        if pattern.search(old_md):
            new_md = pattern.sub(new_chapter, old_md, count=1)
        else:
            # 方案里还没有这个章节，追加到末尾
            new_md = old_md.rstrip() + "\n\n" + new_chapter + "\n"

        # 写回 md（原子写，避免写一半损坏原文件）；不再逐章导出 docx/pdf
        tmp_md = paths["md"] + ".tmp"
        with open(tmp_md, "w", encoding="utf-8") as f:
            f.write(new_md)
        os.replace(tmp_md, paths["md"])

        # 必须写回章节表：章节列表、全文预览、导出都读 chapters.content，
        # 只更新 md 会出现"操作记录在生成、章节管理没变化"的假成功
        db.update_chapter_content(chapter_id, draft.strip())
    logger.info(f"章节生成完成: chapter={chapter_id}, 字数={len(draft)}, 引用数={len(cite_dicts)}")

    elapsed = int(time.time() - t_start)
    review_note = "审查通过" if review["passed"] else f"校审问题 {len(review['issues'])} 处"
    audit(project_id, "chapter_generate", target["title"], "success",
          detail=f"字数 {len(draft)}，引用 {len(cite_dicts)} 条，耗时 {elapsed} 秒，{review_note}",
          chapter_id=chapter_id)

    return {
        "chapter_id": chapter_id,
        "title": target["title"],
        "content": draft,
        "chars": len(draft),
        "citations": len(cite_dicts),
        "reasoning": reasoning,
        "proposal": new_md,
        "review": review,
    }


@app.get("/api/projects/{pid}/proposal")
def get_proposal(pid: str):
    proj = db.get_project(pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    paths = _proposal_paths(proj["name"])
    exists = os.path.exists(paths["md"])
    return {"exists": exists, "md_path": paths["md"], "docx_path": paths["docx"],
            "pdf_path": paths["pdf"],
            "proposal": open(paths["md"], encoding="utf-8").read() if exists else ""}


@app.get("/api/projects/{pid}/full_preview")
def full_preview(pid: str):
    """返回整份方案的结构化预览：按章节顺序列出全部章节，未生成的给出占位标记。"""
    proj = db.get_project(pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    chapters = db.list_chapters(pid)
    style = db.get_project_style(pid)
    numbered = _numbered_chapters(chapters, style)
    marks = _chapter_table_counter(numbered)
    items = []
    for idx, c in enumerate(numbered):
        body = (c.get("content") or "").strip()
        if body:
            chap, sec, start = marks[idx]
            body, _used = _annotate_tables(
                body, chap, sec, (c.get("title") or "").strip(), start)
        # 有下级标题的是结构节点，不需要自己写正文，也不该被算进"待生成"
        nxt = numbered[idx + 1] if idx + 1 < len(numbered) else None
        is_leaf = not (nxt and int(nxt.get("level") or 1) > int(c.get("level") or 1))
        items.append({
            "id": c["id"],
            "code": c.get("code") or "",
            "no": c["no"],
            "title": c["title"],
            "level": c.get("level") or 1,
            "content": body,
            "generated": bool(body),
            "leaf": is_leaf,
            "chars": len(body),
        })
    leaves = [it for it in items if it["leaf"]]
    generated = sum(1 for it in leaves if it["generated"])
    return {
        "project": proj["name"],
        "chapters": items,
        "total": len(items),
        "generated_count": generated,
        "pending_count": len(leaves) - generated,
        "style": style,
        "style_source": "template" if (proj.get("style_json") or "").strip() else "default",
    }


@app.get("/api/export/{pid}/{fmt}")
def export_file(pid: str, fmt: str):
    """兼容旧入口：整份导出统一按数据库章节 + 模版样式现场生成，不再依赖 data/output 下的历史文件。"""
    return export_full_proposal(pid, fmt)


# ---------- 生成记录（历史） ----------
@app.get("/api/projects/{pid}/records", response_model=List[RecordOut])
def list_records(pid: str):
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    return db.list_generation_records(pid)


@app.get("/api/records/{rid}")
def get_record(rid: str):
    rec = db.get_generation_record(rid)
    if not rec:
        raise HTTPException(404, "记录不存在")
    out = dict(rec)
    try:
        out["proposal"] = open(rec["md_path"], encoding="utf-8").read()
    except OSError:
        out["proposal"] = ""
    return out


@app.get("/api/records/{rid}/export/{fmt}")
def export_record_file(rid: str, fmt: str):
    if fmt not in ("md", "docx", "pdf"):
        raise HTTPException(422, "fmt 需为 md / docx / pdf")
    rec = db.get_generation_record(rid)
    if not rec:
        raise HTTPException(404, "记录不存在")
    path = rec[f"{fmt}_path"]
    if not os.path.exists(path):
        raise HTTPException(404, "该版本文件已不存在")
    media = {"md": "text/markdown", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "pdf": "application/pdf"}[fmt]
    return FileResponse(path, filename=os.path.basename(path), media_type=media)


def _review_siblings(project_id: str, chapter_id: str) -> list:
    """取其他已生成章节的正文，供校审做跨章节一致性比对（重复、数字口径）。"""
    out = []
    for c in db.list_chapters(project_id):
        if c["id"] == chapter_id:
            continue
        body = (c.get("content") or "").strip()
        if body:
            out.append({"title": c.get("title") or "", "content": body})
    return out


def _numbered_chapters(chapters: list, style: dict = None) -> list:
    """按模板采集到的编号定义给章节加编号前缀。

    编号规则不写死在这里，而是取自项目上传 Word 模板时读出的 numbering 定义
    （第N章 / 一、 / （一）/ 1. 只是其中一种，换成 1.1 / 第一节 也一样走这段）。
    返回副本列表并补 no（编号文本）与 seq（同层序号）。
    """
    return number_chapters(chapters, style or DEFAULT_STYLE)


_CAP_LINE_RE = re.compile(r"^表\s*[:：]?\s*")
_TABLE_NO_RE = re.compile(r"^表\s*[0-9]")


def _annotate_tables(body: str, chap: int, sec: int, title: str, start: int = 0):
    """给正文里的 Markdown 表格补「表<章>-<节>-<序> 名称」题注（模板就是这么排的）。

    序号按「章-节」全篇累计（start 为同节内已用掉的表数），
    已有「表1-3-1 …」原样保留；模型只写了「表：xxx」这类不带编号的，补上编号；
    完全没写题注的，用所在章节标题当表名（新生成的章节由提示词直接给准表名）。
    返回 (新正文, 本节点已用到第几个表)。
    """
    lines = body.split("\n")
    # 先数清本节有几张表、正文里写死了哪些表号（用于把「见表1-3-7」这类引用对齐到实际编号）
    n_tbl = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if s.startswith("|") and nxt.startswith("|") and set(nxt) <= set("|-: "):
            n_tbl += 1
    cited = sorted(set(re.findall(r"表\s*\d+-\d+-\d+", body)))
    out: List[str] = []
    k = start
    first_no = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        is_tbl = s.startswith("|") and nxt.startswith("|") and set(nxt) <= set("|-: ")
        if is_tbl:
            k += 1
            no = f"{chap}-{sec}-{k}" if sec else f"{chap}-{k}"
            if first_no is None:
                first_no = no
            name = (title or "").strip() or "统计表"
            if "表" not in name[-3:]:
                name += "统计表"
            prev = out[-1].strip() if out else ""
            if _CAP_LINE_RE.match(prev):
                if not _TABLE_NO_RE.match(prev):
                    # 模型写了「表：xxx」但没编号 → 补成标准表号
                    nm = _CAP_LINE_RE.sub("", prev).strip() or name
                    out[-1] = f"表{no} {nm}"
            else:
                # 题注必须紧贴表格上方，否则 Word 里会被正文段落挤开
                if out and out[-1].strip():
                    out.append("")
                out.append(f"表{no} {name}")
        out.append(ln)
    text = "\n".join(out)
    # 本节只有一张表、正文又写死了唯一一个表号时，把引用对齐到实际编号，
    # 否则导出的表题（表1-3-4）与正文「见表1-3-7」互相打架，读者翻不到。
    if n_tbl == 1 and first_no and len(cited) == 1 and cited[0].replace(" ", "") != f"表{first_no}":
        text = text.replace(cited[0], f"表{first_no}")
    return text, k


def _count_md_tables(body: str) -> int:
    """数一段 Markdown 里有几张表（表头行 + | --- | 分隔行才算）。"""
    lines = (body or "").split("\n")
    n = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if s.startswith("|") and nxt.startswith("|") and set(nxt) <= set("|-: "):
            n += 1
    return n


def _chapter_table_counter(chapters: list) -> list:
    """给每个章节标出它在整篇里的（章号, 节号, 同节内已用掉的表数），供表题编号使用。"""
    chap = sec = 0
    used = {}
    marks = []
    for c in chapters:
        lvl = int(c.get("level") or 1)
        seq = int(c.get("seq") or 0)
        if lvl == 1:
            chap, sec = seq, 0
        elif lvl == 2:
            sec = seq
        key = (chap, sec)
        marks.append((chap, sec, used.get(key, 0)))
        used[key] = used.get(key, 0) + _count_md_tables(c.get("content") or "")
    return marks


def _chapters_to_markdown(proj_name: str, chapters: list, style: dict = None) -> str:
    """把章节表按顺序拼成带层级的 Markdown。

    项目名用 h1，章节按 level 依次用 h2 / h3 / h4，
    标题前缀取自模板采集的编号定义，未生成的章节保留标题并写入占位说明，
    保证导出文档结构完整。
    """
    style = style or DEFAULT_STYLE
    max_lvl = len(style.get("levels") or DEFAULT_STYLE["levels"])
    numbered = _numbered_chapters(chapters, style)
    marks = _chapter_table_counter(numbered)
    lines = [f"# {proj_name}技术方案", ""]
    for idx, c in enumerate(numbered):
        lvl = int(c.get("level") or 1)
        lvl = min(max(lvl, 1), max_lvl)
        hashes = "#" * (lvl + 1)
        head = f"{c['no']}{(c.get('title') or '').strip()}".strip()
        lines.append(f"{hashes} {head}")
        lines.append("")
        body = (c.get("content") or "").strip()
        if body:
            chap, sec, start = marks[idx]
            body, _used = _annotate_tables(
                body, chap, sec, (c.get("title") or "").strip(), start)
            lines.extend(body.split("\n"))
        else:
            # 有下级标题的章节只是结构节点，本来就不需要自己写正文，
            # 导出时不能给它打"尚未生成"，否则成稿里会出现一堆无意义的提示
            nxt = numbered[idx + 1] if idx + 1 < len(numbered) else None
            has_children = bool(nxt) and int(nxt.get("level") or 1) > int(c.get("level") or 1)
            if not has_children:
                lines.append("（本章尚未生成）")
        lines.append("")
    return "\n".join(lines)


@app.get("/api/projects/{pid}/front_matter")
def get_front_matter(pid: str):
    """取项目的前言 / 摘要（导出时插在目录之前）。"""
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    front = db.get_project_front_matter(pid)
    return {
        "preface": front.get("preface", ""),
        "abstract": front.get("abstract", ""),
        "has_content": bool((front.get("preface") or "").strip() or (front.get("abstract") or "").strip()),
    }


@app.put("/api/projects/{pid}/front_matter")
def save_front_matter(pid: str, body: FrontMatterIn):
    """保存手工编辑后的前言 / 摘要。"""
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    front = db.update_project_front_matter(pid, preface=body.preface, abstract=body.abstract)
    db.add_operation(pid, "front_matter_update", "前言 / 摘要", status="success",
                     detail=f"前言 {len(front.get('preface') or '')} 字，摘要 {len(front.get('abstract') or '')} 字")
    return front


class _DraftNotReady(Exception):
    """项目还不具备起草条件（无章节 / 无已生成正文）。"""


@app.post("/api/projects/{pid}/front_matter/draft")
def draft_front_matter(pid: str):
    """按已生成章节自动起草前言 / 摘要（只返回草稿，不落库，由用户确认后再保存）。"""
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    try:
        return _draft_front_matter_core(pid)
    except _DraftNotReady as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"自动起草失败：{str(e)[:200]}")


@app.post("/api/projects/{pid}/front_matter/draft/stream")
def draft_front_matter_stream(pid: str):
    """流式起草前言 / 摘要：模型每吐一段就推给前端，让用户在输入框里看到写作过程。

    事件格式（SSE，每行 data: {json}）：
      {"type":"delta","data":"..."}  模型原始增量（JSON 片段，前端边收边解析）
      {"type":"done","data":{...}}   解析后的完整结果（preface / abstract / generated_chapters）
      {"type":"error","data":"..."}  起草失败原因
    """
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")

    q: "queue.Queue" = queue.Queue()

    def worker():
        def on_chunk(c):
            q.put(("delta", c))
        try:
            result = _draft_front_matter_core(pid, on_chunk)
            q.put(("done", result))
        except _DraftNotReady as e:
            q.put(("error", str(e)))
        except Exception as e:
            logger.warning(f"流式起草前言/摘要失败: {pid} {e}")
            q.put(("error", f"自动起草失败：{str(e)[:200]}"))
        finally:
            q.put(("eof", None))

    def gen():
        threading.Thread(target=worker, daemon=True).start()
        while True:
            kind, payload = q.get()
            if kind == "eof":
                break
            yield "data: " + json.dumps({"type": kind, "data": payload}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _draft_front_matter_core(pid: str, on_chunk=None) -> dict:
    proj = db.get_project(pid)
    if not proj:
        raise _DraftNotReady("项目不存在")
    chapters = db.list_chapters(pid)
    if not chapters:
        raise _DraftNotReady("项目暂无章节，无法起草")
    style = db.get_project_style(pid)
    numbered = _numbered_chapters(chapters, style)

    outline = []
    for c in numbered:
        if int(c.get("level") or 1) <= 2:
            outline.append(f"{c.get('no', '')}{(c.get('title') or '').strip()}")
    material = []
    budget = 8000
    generated = 0
    for c in numbered:
        body = (c.get("content") or "").strip()
        if not body:
            continue
        generated += 1
        if budget <= 0:
            continue
        chunk = body[:600]
        material.append(f"【{c.get('no', '')}{(c.get('title') or '').strip()}】{chunk}")
        budget -= len(chunk)

    if not material:
        raise _DraftNotReady("项目还没有任何已生成正文，无法起草；请先生成章节")


    system = (
        "你是国土空间规划与土地综合整治领域的政府公文写作专家，为正式实施方案起草「前 言」和「摘 要」。"
        "要求：\n"
        "1. 只输出一个 JSON 对象，形如 {\"preface\": \"...\", \"abstract\": \"...\"}，不要任何解释、不要 markdown 围栏；\n"
        "2. 前言是叙述性文字：政策背景 → 区域概况 → 存在的主要问题 → 编制本方案的目的，约 500~700 字，分段用 \\n 分隔；\n"
        "3. 摘要是条目式硬信息，按以下条目逐条写：项目名称、项目区域范围、整治区范围、整治规模、整治目标、"
        "主要整治任务、投资估算与资金筹措、实施期限、项目特色；每条一段，形如 \"项目名称：xxx\"；\n"
        "4. 所有数字、地名、面积、投资、期限只能来自给出的正文材料，材料里没有的一律写【待补充】，绝对不要编造；\n"
        "5. 语言为政府公文风格，不用「我们」「本系统」之类的表述。"
    )
    user = (
        f"项目名称：{proj['name']}\n"
        f"章节结构（1~2 级）：\n" + "\n".join(outline) + "\n\n"
        f"已生成章节正文节选（共 {generated} 章有正文）：\n" + "\n".join(material) +
        "\n\n请输出 JSON。"
    )

    try:
        chat = build_chat()
        raw = chat.complete_stream(system=system, user=user, max_tokens=8192, temperature=0.3,
                                   on_chunk=on_chunk)
    except Exception as e:
        raise RuntimeError(f"自动起草失败：{str(e)[:200]}")

    data = _extract_json_obj(raw)
    if not data:
        raise RuntimeError("模型返回的内容无法解析为前言/摘要，请稍后重试")
    preface = str(data.get("preface") or "").strip()
    abstract = str(data.get("abstract") or "").strip()
    if not (preface or abstract):
        raise RuntimeError("模型未返回有效的前言/摘要内容")
    return {"preface": preface, "abstract": abstract, "generated_chapters": generated}


def _extract_json_obj(text: str) -> dict:
    """从模型输出里抠出 JSON 对象（容忍 ```json 围栏、前后废话）。"""
    if not text:
        return {}
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s.strip("`")
        s = s.lstrip("json").strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        obj = json.loads(s[start:end + 1])
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _with_front_matter(md_text: str, front: dict) -> str:
    """md 版导出：把前言/摘要写进方案名之后（docx/pdf 由各自渲染器插入，不用这个）。"""
    front = front or {}
    blocks = []
    for key, label in (("preface", "前 言"), ("abstract", "摘 要")):
        text = (front.get(key) or "").strip()
        if text:
            blocks.append(f"**{label}**")
            blocks.append("")
            blocks.append(text)
            blocks.append("")
    if not blocks:
        return md_text
    lines = md_text.split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            return "\n".join(lines[:i + 1] + [""] + blocks + lines[i + 1:])
    return "\n".join(blocks) + "\n" + md_text


@app.get("/api/export/full/{pid}/{fmt}")
def export_full_proposal(pid: str, fmt: str):
    """导出整份方案（按数据库章节层级拼接，含已生成与未生成章节）。"""
    if fmt not in ("md", "docx", "pdf"):
        raise HTTPException(422, "fmt 需为 md / docx / pdf")
    proj = db.get_project(pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    chapters = db.list_chapters(pid)
    if not chapters:
        raise HTTPException(400, "该项目暂无章节，无法导出")

    style = db.get_project_style(pid)
    # 前言/摘要只在用户点「自动起草」时生成；导出只取已保存的内容，不做任何自动起草
    front = db.get_project_front_matter(pid)
    md_text = _chapters_to_markdown(proj["name"], chapters, style)
    import tempfile
    tmp_dir = tempfile.mkdtemp()
    base = f"{proj['name']}_技术方案".replace("/", "_").replace("\\", "_")
    md_path = os.path.join(tmp_dir, base + ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_with_front_matter(md_text, front))

    if fmt == "md":
        path = md_path
    else:
        out = os.path.join(tmp_dir, base + f".{fmt}")
        if fmt == "docx":
            md_to_docx(md_text, out, style, front)
        else:
            md_to_pdf(md_text, out, style, front)
        path = out

    media = {
        "md": "text/markdown",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pdf": "application/pdf",
    }[fmt]
    generated = sum(1 for c in chapters if (c.get("content") or "").strip())
    audit(pid, "export", f"{proj['name']} 整份方案",
          detail=f"导出全文 {fmt.upper()}：共 {len(chapters)} 章，已生成 {generated} 章")
    return FileResponse(path, filename=os.path.basename(path), media_type=media)


def _export_dir() -> str:
    """后台导出的产物目录：与数据库同级的 exports/。"""
    d = os.path.join(os.path.dirname(settings.resolved_db_path), "exports")
    os.makedirs(d, exist_ok=True)
    return d


def _run_full_export(op_id: str, pid: str, fmt: str) -> None:
    """后台执行整份方案导出，完成后把结果回写到审计记录。"""
    try:
        proj = db.get_project(pid)
        if not proj:
            db.update_operation(op_id, status="failed", detail="导出失败：项目不存在")
            return
        chapters = db.list_chapters(pid)
        if not chapters:
            db.update_operation(op_id, status="failed", detail="导出失败：项目暂无章节")
            return

        style = db.get_project_style(pid)
        # 仅取用户已保存的前言/摘要，导出过程不再自动起草
        front = db.get_project_front_matter(pid)
        md_text = _chapters_to_markdown(proj["name"], chapters, style)
        safe_name = proj["name"].replace("/", "_").replace("\\", "_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(_export_dir(), f"{safe_name}_技术方案_{ts}.{fmt}")

        if fmt == "md":
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(_with_front_matter(md_text, front))
        elif fmt == "docx":
            md_to_docx(md_text, out_path, style, front)
        else:
            md_to_pdf(md_text, out_path, style, front)

        generated = sum(1 for c in chapters if (c.get("content") or "").strip())
        db.update_operation(
            op_id, status="success",
            detail=f"导出 {fmt.upper()} 完成：共 {len(chapters)} 章，已生成 {generated} 章，可点击下载",
            file_path=out_path,
        )
        logger.info(f"后台导出完成: op={op_id}, fmt={fmt}, path={out_path}")
    except Exception as e:
        logger.error(f"后台导出失败: op={op_id}, fmt={fmt}, {str(e)}", exc_info=True)
        db.update_operation(op_id, status="failed", detail=f"导出失败：{str(e)[:200]}")


@app.post("/api/export/full_async")
def export_full_async(background_tasks: BackgroundTasks, project_id: str, fmt: str):
    """提交整份方案的后台导出任务，立即返回审计记录 id。"""
    if fmt not in ("md", "docx", "pdf"):
        raise HTTPException(422, "fmt 需为 md / docx / pdf")
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    chapters = db.list_chapters(project_id)
    if not chapters:
        raise HTTPException(400, "该项目暂无章节，无法导出")

    op = db.add_operation(
        project_id, "export", f"{proj['name']} 整份方案", "started",
        detail=f"后台导出 {fmt.upper()} 任务已提交，正在生成…",
    )
    background_tasks.add_task(_run_full_export, op["id"], project_id, fmt)
    return {"op_id": op["id"], "status": "started", "fmt": fmt}


@app.get("/api/exports/{op_id}/download")
def download_export(op_id: str):
    """下载后台导出任务生成的产物。"""
    op = db.get_operation(op_id)
    if not op:
        raise HTTPException(404, "导出任务不存在")
    if op.get("status") != "success":
        raise HTTPException(400, f"该任务尚未完成（当前状态：{op.get('status')}）")
    path = (op.get("file_path") or "").strip()
    if not path or not os.path.exists(path):
        raise HTTPException(404, "导出文件已不存在，请重新导出")
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    media = {
        "md": "text/markdown",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pdf": "application/pdf",
    }.get(ext, "application/octet-stream")
    return FileResponse(path, filename=os.path.basename(path), media_type=media)


@app.get("/api/export/chapter/{chapter_id}/{fmt}")
def export_chapter_file(chapter_id: str, fmt: str):
    """导出单个章节为 Word/PDF/Markdown。"""
    if fmt not in ("md", "docx", "pdf"):
        raise HTTPException(422, "fmt 需为 md / docx / pdf")
    ch = db.get_chapter(chapter_id)
    if not ch:
        raise HTTPException(404, "章节不存在")
    if not ch.get("content"):
        raise HTTPException(400, "该章节尚未生成内容")
    
    # 自动拼接标题
    title = f"{ch['code']} {ch['title']}".strip()
    # 单章导出同样要带表题，表号按它在整篇里的章-节位置算
    _style = db.get_project_style(ch["project_id"])
    _siblings = _numbered_chapters(db.list_chapters(ch["project_id"]), _style)
    _marks = _chapter_table_counter(_siblings)
    _body = ch["content"]
    for _i, _c in enumerate(_siblings):
        if _c.get("id") == chapter_id:
            _chap, _sec, _start = _marks[_i]
            _body, _used = _annotate_tables(_body, _chap, _sec, ch.get("title") or "", _start)
            break
    content = f"## {title}\n\n{_body}"
    
    proj = db.get_project(ch["project_id"])
    proj_name = proj["name"] if proj else "方案"
    
    # 临时生成文件
    import tempfile
    tmp_dir = tempfile.mkdtemp()
    base_name = f"{proj_name}_{ch['code']}_{ch['title']}".replace("/", "_").replace("\\", "_")
    md_path = os.path.join(tmp_dir, base_name + ".md")
    docx_path = os.path.join(tmp_dir, base_name + ".docx")
    pdf_path = os.path.join(tmp_dir, base_name + ".pdf")
    
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content)
    
    export_proposal(content, docx_path, pdf_path, db.get_project_style(ch["project_id"]))
    
    path = {"md": md_path, "docx": docx_path, "pdf": pdf_path}[fmt]
    media = {"md": "text/markdown", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "pdf": "application/pdf"}[fmt]
    audit(ch["project_id"], "export", ch["title"],
          detail=f"导出单个章节为 {fmt.upper()}，字数 {len(ch['content'])}", chapter_id=chapter_id)
    return FileResponse(path, filename=os.path.basename(path), media_type=media)


# ---------- M5：静态前端 ----------
_static_dir = Path(__file__).resolve().parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        # 禁止浏览器缓存首页，避免改版后用户仍看到旧界面
        return FileResponse(
            _static_dir / "index.html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )

    @app.get("/api/version")
    def frontend_version():
        """返回前端文件当前的修改时间，前端在页头显示，用于判断是否还在跑旧页面。"""
        p = _static_dir / "index.html"
        st = p.stat()
        return {
            "index_mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "index_size": st.st_size,
        }
