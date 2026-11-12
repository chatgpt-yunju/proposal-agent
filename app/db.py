"""SQLite 元数据：项目 / 章节 / 方案生成记录（历史）。"""
import json
import os
import sqlite3
import threading
import uuid
from typing import List, Optional

from .config import settings

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    path = settings.resolved_db_path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock:
        c = _conn()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects(
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS chapters(
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                code TEXT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                sort INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                content TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS generation_records(
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                iterations INTEGER DEFAULT 0,
                chapter_count INTEGER DEFAULT 0,
                total_chars INTEGER DEFAULT 0,
                chapters TEXT DEFAULT '[]',
                md_path TEXT NOT NULL,
                docx_path TEXT NOT NULL,
                pdf_path TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations(
                id TEXT PRIMARY KEY,
                project_id TEXT,
                chapter_id TEXT,
                action TEXT NOT NULL,
                target TEXT DEFAULT '',
                status TEXT DEFAULT 'success',
                detail TEXT DEFAULT '',
                file_path TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_operations_project
                ON operations(project_id, created_at);
            -- 资料登记表：知识库导入的「逐文件状态」以此为准，向量库只存切块
            CREATE TABLE IF NOT EXISTS kb_docs(
                id TEXT PRIMARY KEY,
                tier TEXT NOT NULL,
                project_id TEXT DEFAULT '',
                chapter_id TEXT DEFAULT '',
                filename TEXT NOT NULL,
                doc_type TEXT DEFAULT 'other',
                status TEXT DEFAULT 'parsing',
                error TEXT DEFAULT '',
                fingerprint TEXT DEFAULT '',
                chunk_count INTEGER DEFAULT 0,
                chars INTEGER DEFAULT 0,
                summary TEXT DEFAULT '',
                path TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_kb_docs_project
                ON kb_docs(project_id, created_at);
            -- 章节来源：生成该章时实际用到的资料片段，供「依据」查看，不写进正文
            CREATE TABLE IF NOT EXISTS chapter_sources(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chapter_id TEXT NOT NULL,
                doc_id TEXT DEFAULT '',
                source TEXT DEFAULT '',
                title TEXT DEFAULT '',
                text TEXT DEFAULT '',
                score REAL DEFAULT 0,
                page INTEGER,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_chapter_sources_chapter
                ON chapter_sources(chapter_id);
            """
        )
        # 兼容旧表：老库的 operations 没有 file_path 列（后台导出产物的落盘路径）
        try:
            c.execute("SELECT file_path FROM operations LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE operations ADD COLUMN file_path TEXT DEFAULT ''")
        # 兼容旧表：projects 增加 style_json（上传模板时采集的样式表与编号定义）
        try:
            c.execute("SELECT style_json FROM projects LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE projects ADD COLUMN style_json TEXT DEFAULT ''")
        # 兼容旧表：projects 增加 front_matter（前言 / 摘要，存 JSON）
        try:
            c.execute("SELECT front_matter FROM projects LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE projects ADD COLUMN front_matter TEXT DEFAULT ''")
        # 兼容旧表：projects 增加 archived（归档后不在项目列表里显示）
        try:
            c.execute("SELECT archived FROM projects LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE projects ADD COLUMN archived INTEGER DEFAULT 0")
        # ALTER 只改默认值，已有行仍是 NULL，统一补成 0
        c.execute("UPDATE projects SET archived=0 WHERE archived IS NULL")
        # 兼容旧表：项目需求档案（建设目标 / 现状痛点 / 范围 / 预算周期 / 硬性要求）
        try:
            c.execute("SELECT profile_json FROM projects LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE projects ADD COLUMN profile_json TEXT DEFAULT ''")
        # 兼容旧表：需求整理结果（已知事实 / 建设要求 / 方案建议 / 待确认事项）
        try:
            c.execute("SELECT brief_json FROM projects LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE projects ADD COLUMN brief_json TEXT DEFAULT ''")
        # 兼容旧表：大纲是否已人工确认（确认后才允许生成正文）
        try:
            c.execute("SELECT outline_confirmed FROM projects LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE projects ADD COLUMN outline_confirmed INTEGER DEFAULT 0")
            # 只在新增列时回填一次：已有章节的老项目视为已确认，避免升级后被门禁卡住；
            # 新项目建成后该列有值，不受影响，仍需显式确认。
            c.execute(
                "UPDATE projects SET outline_confirmed=1 "
                "WHERE id IN (SELECT project_id FROM chapters)"
            )
        # 兼容旧表：单章重写前的原稿（支持恢复）
        try:
            c.execute("SELECT prev_content FROM chapters LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE chapters ADD COLUMN prev_content TEXT DEFAULT ''")
        # 兼容旧表：章节生成状态（pending / generating / generated / failed）与失败原因
        try:
            c.execute("SELECT gen_status FROM chapters LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE chapters ADD COLUMN gen_status TEXT DEFAULT ''")
        try:
            c.execute("SELECT gen_error FROM chapters LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE chapters ADD COLUMN gen_error TEXT DEFAULT ''")
        c.commit()
        c.close()


def create_project(name: str, description: str = "") -> dict:
    pid = "p_" + uuid.uuid4().hex[:12]
    with _lock:
        c = _conn()
        c.execute("INSERT INTO projects(id,name,description) VALUES(?,?,?)", (pid, name, description))
        c.commit()
        row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        c.close()
    return dict(row)


def list_projects(include_archived: bool = False) -> List[dict]:
    """项目列表。默认只返回未归档的，归档过的要显式要求才给。"""
    with _lock:
        c = _conn()
        if include_archived:
            rows = c.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM projects WHERE COALESCE(archived,0)=0 ORDER BY created_at DESC"
            ).fetchall()
        c.close()
    return [dict(r) for r in rows]


def get_project(pid: str) -> Optional[dict]:
    with _lock:
        c = _conn()
        row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        c.close()
    return dict(row) if row else None


def update_project(pid: str, name: str = None, description: str = None,
                   style_json: str = None, archived: int = None) -> Optional[dict]:
    """更新项目名称、描述、模板样式定义或归档状态。"""
    with _lock:
        c = _conn()
        if name is not None:
            c.execute("UPDATE projects SET name=? WHERE id=?", (name, pid))
        if description is not None:
            c.execute("UPDATE projects SET description=? WHERE id=?", (description, pid))
        if style_json is not None:
            c.execute("UPDATE projects SET style_json=? WHERE id=?", (style_json, pid))
        if archived is not None:
            c.execute("UPDATE projects SET archived=? WHERE id=?", (1 if archived else 0, pid))
        c.commit()
        row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        c.close()
    return dict(row) if row else None


def get_project_front_matter(pid: str) -> dict:
    """取项目的前言 / 摘要（存的是 JSON 字符串，形如 {"preface": "...", "abstract": "..."}）。"""
    import json as _json
    proj = get_project(pid) or {}
    raw = proj.get("front_matter") or ""
    if not raw:
        return {"preface": "", "abstract": ""}
    try:
        data = _json.loads(raw)
    except ValueError:
        return {"preface": "", "abstract": ""}
    if not isinstance(data, dict):
        return {"preface": "", "abstract": ""}
    return {"preface": str(data.get("preface") or ""), "abstract": str(data.get("abstract") or "")}


def update_project_front_matter(pid: str, preface: str = None, abstract: str = None) -> dict:
    """保存前言 / 摘要（只写传进来的字段，另一项保持原值）。"""
    import json as _json
    cur = get_project_front_matter(pid)
    if preface is not None:
        cur["preface"] = preface
    if abstract is not None:
        cur["abstract"] = abstract
    with _lock:
        c = _conn()
        c.execute("UPDATE projects SET front_matter=? WHERE id=?",
                  (_json.dumps(cur, ensure_ascii=False), pid))
        c.commit()
        c.close()
    return cur


def get_project_style(pid: str) -> dict:
    """取项目采集的模板样式定义；没采集过则返回默认公文样式。"""
    import json as _json
    from .template_style import DEFAULT_STYLE
    proj = get_project(pid)
    raw = (proj or {}).get("style_json") or ""
    if not raw:
        return dict(DEFAULT_STYLE)
    try:
        style = _json.loads(raw)
    except ValueError:
        return dict(DEFAULT_STYLE)
    if not isinstance(style, dict) or not style.get("levels"):
        return dict(DEFAULT_STYLE)
    return style


def create_chapter(project_id: str, code: str, title: str, description: str = "", level: int = 1) -> dict:
    cid = "c_" + uuid.uuid4().hex[:12]
    with _lock:
        c = _conn()
        # 兼容旧表：如果 level 列不存在则添加
        try:
            c.execute("SELECT level FROM chapters LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE chapters ADD COLUMN level INTEGER DEFAULT 1")
            c.commit()
        sort = c.execute(
            "SELECT COALESCE(MAX(sort),0)+1 AS s FROM chapters WHERE project_id=?", (project_id,)
        ).fetchone()["s"]
        c.execute(
            "INSERT INTO chapters(id,project_id,code,title,description,sort,level) VALUES(?,?,?,?,?,?,?)",
            (cid, project_id, code, title, description, sort, level),
        )
        c.commit()
        row = c.execute("SELECT * FROM chapters WHERE id=?", (cid,)).fetchone()
        c.close()
    return dict(row)


def list_chapters(project_id: str) -> List[dict]:
    with _lock:
        c = _conn()
        rows = c.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY sort", (project_id,)
        ).fetchall()
        c.close()
    return [dict(r) for r in rows]


def delete_chapter(chapter_id: str) -> bool:
    """删除指定章节，返回是否删除成功。"""
    with _lock:
        c = _conn()
        cur = c.execute("DELETE FROM chapters WHERE id=?", (chapter_id,))
        c.commit()
        c.close()
        return cur.rowcount > 0


def update_chapter_content(chapter_id: str, content: str) -> bool:
    """更新章节生成的内容。"""
    with _lock:
        c = _conn()
        try:
            cur = c.execute("UPDATE chapters SET content=? WHERE id=?", (content, chapter_id))
            c.commit()
        except sqlite3.OperationalError:
            # 兼容旧表：如果 content 列不存在则添加
            c.execute("ALTER TABLE chapters ADD COLUMN content TEXT DEFAULT ''")
            c.commit()
            cur = c.execute("UPDATE chapters SET content=? WHERE id=?", (content, chapter_id))
            c.commit()
        c.close()
        return cur.rowcount > 0


def get_chapter(chapter_id: str) -> Optional[dict]:
    """获取单个章节详情。"""
    with _lock:
        c = _conn()
        row = c.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
        c.close()
        return dict(row) if row else None


def update_chapter_fields(chapter_id: str, **fields) -> Optional[dict]:
    """按需更新章节字段（title / code / description 等），返回更新后的整行。"""
    allowed = {"title", "code", "description"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return get_chapter(chapter_id)
    sets = ", ".join(f"{k}=?" for k in data)
    with _lock:
        c = _conn()
        cur = c.execute(f"UPDATE chapters SET {sets} WHERE id=?", (*data.values(), chapter_id))
        c.commit()
        row = c.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
        c.close()
    if cur.rowcount <= 0 and not row:
        return None
    return dict(row) if row else None


def update_chapter_level(chapter_id: str, level: int) -> bool:
    """更新章节的层级。"""
    with _lock:
        c = _conn()
        try:
            cur = c.execute("UPDATE chapters SET level=? WHERE id=?", (level, chapter_id))
            c.commit()
        except sqlite3.OperationalError:
            # 兼容旧表：如果 level 列不存在则添加
            c.execute("ALTER TABLE chapters ADD COLUMN level INTEGER DEFAULT 1")
            c.commit()
            cur = c.execute("UPDATE chapters SET level=? WHERE id=?", (level, chapter_id))
            c.commit()
        c.close()
        return cur.rowcount > 0


# ---------- 方案生成记录（历史） ----------

def add_generation_record(project_id: str, iterations: int, chapters: List[dict],
                          total_chars: int, md_path: str, docx_path: str, pdf_path: str,
                          chapter_count: Optional[int] = None) -> dict:
    """写入一条生成记录；chapters 为 [{code,title,chars,review_ok}]；chapter_count 缺省取 chapters 长度。"""
    rid = "r_" + uuid.uuid4().hex[:12]
    if chapter_count is None:
        chapter_count = len(chapters)
    with _lock:
        c = _conn()
        c.execute(
            "INSERT INTO generation_records"
            "(id,project_id,iterations,chapter_count,total_chars,chapters,md_path,docx_path,pdf_path)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, project_id, iterations, len(chapters), total_chars,
             json.dumps(chapters, ensure_ascii=False), md_path, docx_path, pdf_path),
        )
        c.commit()
        row = c.execute("SELECT * FROM generation_records WHERE id=?", (rid,)).fetchone()
        c.close()
    return dict(row)


def list_generation_records(project_id: str) -> List[dict]:
    """按生成时间倒序返回某项目的全部生成记录。"""
    with _lock:
        c = _conn()
        rows = c.execute(
            "SELECT * FROM generation_records WHERE project_id=? ORDER BY created_at DESC, rowid DESC",
            (project_id,),
        ).fetchall()
        c.close()
    return [dict(r) for r in rows]


def get_generation_record(record_id: str) -> Optional[dict]:
    with _lock:
        c = _conn()
        row = c.execute("SELECT * FROM generation_records WHERE id=?", (record_id,)).fetchone()
        c.close()
    return dict(row) if row else None


# ---------- 操作审计日志 ----------

def add_operation(project_id: Optional[str], action: str, target: str = "",
                  status: str = "success", detail: str = "",
                  chapter_id: Optional[str] = None,
                  file_path: str = "") -> dict:
    """写入一条操作审计记录。

    action 为动作类型（chapter_generate / chapter_delete / kb_ingest 等），
    target 为操作对象（章节标题或文件名），status 取 success / failed / started，
    detail 存放字数、耗时、错误信息等补充内容。
    file_path 记录后台导出产物的落盘位置，前端据此提供「下载」入口。
    审计写入失败不应影响主业务，因此调用方需自行容错。
    """
    oid = "op_" + uuid.uuid4().hex[:12]
    with _lock:
        c = _conn()
        c.execute(
            "INSERT INTO operations(id,project_id,chapter_id,action,target,status,detail,file_path)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (oid, project_id, chapter_id, action, target, status, detail[:2000], file_path),
        )
        c.commit()
        row = c.execute("SELECT * FROM operations WHERE id=?", (oid,)).fetchone()
        c.close()
    return dict(row)


def update_operation(op_id: str, status: Optional[str] = None, detail: Optional[str] = None,
                     file_path: Optional[str] = None) -> bool:
    """回写审计记录的状态 / 详情 / 产物路径（用于后台任务完成后更新）。"""
    sets, args = [], []
    if status is not None:
        sets.append("status=?")
        args.append(status)
    if detail is not None:
        sets.append("detail=?")
        args.append(detail[:2000])
    if file_path is not None:
        sets.append("file_path=?")
        args.append(file_path)
    if not sets:
        return False
    args.append(op_id)
    with _lock:
        c = _conn()
        cur = c.execute(f"UPDATE operations SET {', '.join(sets)} WHERE id=?", args)
        c.commit()
        c.close()
    return cur.rowcount > 0


def get_operation(op_id: str) -> Optional[dict]:
    """按 id 取单条审计记录。"""
    with _lock:
        c = _conn()
        row = c.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
        c.close()
    return dict(row) if row else None


def list_operations(project_id: Optional[str] = None, limit: int = 100) -> List[dict]:
    """按时间倒序返回操作审计记录；project_id 为空时返回全局记录。"""
    limit = max(1, min(int(limit), 500))
    with _lock:
        c = _conn()
        if project_id:
            rows = c.execute(
                "SELECT * FROM operations WHERE project_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM operations ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        c.close()
    return [dict(r) for r in rows]


# ---------- 项目需求档案 / 需求整理 ----------
_PROFILE_FIELDS = ("goal", "pain_points", "scope_in", "scope_out", "budget",
                   "duration", "deploy_env", "requirements")


def _json_or(raw: str, default):
    import json as _json
    if not raw:
        return default
    try:
        val = _json.loads(raw)
    except ValueError:
        return default
    return val if isinstance(val, type(default)) else default


def get_project_profile(pid: str) -> dict:
    """取项目需求档案；缺省字段一律给空串，便于前端直接绑定。"""
    proj = get_project(pid) or {}
    data = _json_or(proj.get("profile_json") or "", {})
    out = {k: str(data.get(k) or "") for k in _PROFILE_FIELDS}
    return out


def update_project_profile(pid: str, patch: dict) -> dict:
    """只写传入的字段，其余保持原值（用户分步填写时不会被清空）。"""
    import json as _json
    cur = get_project_profile(pid)
    for k in _PROFILE_FIELDS:
        if k in patch and patch[k] is not None:
            cur[k] = str(patch[k])
    with _lock:
        c = _conn()
        c.execute("UPDATE projects SET profile_json=? WHERE id=?",
                  (_json.dumps(cur, ensure_ascii=False), pid))
        c.commit()
        c.close()
    return cur


def get_project_brief(pid: str) -> dict:
    """取需求整理结果；结构固定为四类，前端按四类分组展示。"""
    proj = get_project(pid) or {}
    data = _json_or(proj.get("brief_json") or "", {})
    out = {
        "facts": list(data.get("facts") or []),
        "requirements": list(data.get("requirements") or []),
        "suggestions": list(data.get("suggestions") or []),
        "pending": list(data.get("pending") or []),
        "updated_at": str(data.get("updated_at") or ""),
        "source": str(data.get("source") or ""),
    }
    return out


def update_project_brief(pid: str, brief: dict) -> dict:
    """保存需求整理结果（用户在页面上改完再存，刷新后按这份恢复）。"""
    import json as _json
    data = {
        "facts": [str(x) for x in (brief.get("facts") or []) if str(x).strip()],
        "requirements": [str(x) for x in (brief.get("requirements") or []) if str(x).strip()],
        "suggestions": [str(x) for x in (brief.get("suggestions") or []) if str(x).strip()],
        "pending": [str(x) for x in (brief.get("pending") or []) if str(x).strip()],
        "updated_at": datetime_now(),
        "source": str(brief.get("source") or ""),
    }
    with _lock:
        c = _conn()
        c.execute("UPDATE projects SET brief_json=? WHERE id=?",
                  (_json.dumps(data, ensure_ascii=False), pid))
        c.commit()
        c.close()
    return get_project_brief(pid)


def datetime_now() -> str:
    from datetime import datetime as _dt
    return _dt.now().strftime("%Y-%m-%d %H:%M:%S")


def get_outline_confirmed(pid: str) -> bool:
    proj = get_project(pid) or {}
    return bool(int(proj.get("outline_confirmed") or 0))


def set_outline_confirmed(pid: str, confirmed: bool = True) -> bool:
    with _lock:
        c = _conn()
        c.execute("UPDATE projects SET outline_confirmed=? WHERE id=?",
                  (1 if confirmed else 0, pid))
        c.commit()
        c.close()
    return True


# ---------- 资料登记（逐文件状态 / 去重 / 重试） ----------

def add_kb_doc(doc_id: str, tier: str, filename: str, *, project_id: str = "",
               chapter_id: str = "", doc_type: str = "other", fingerprint: str = "",
               path: str = "", status: str = "parsing") -> dict:
    with _lock:
        c = _conn()
        c.execute(
            "INSERT INTO kb_docs(id,tier,project_id,chapter_id,filename,doc_type,"
            "fingerprint,path,status) VALUES(?,?,?,?,?,?,?,?,?)",
            (doc_id, tier, project_id or "", chapter_id or "", filename, doc_type,
             fingerprint, path, status),
        )
        c.commit()
        row = c.execute("SELECT * FROM kb_docs WHERE id=?", (doc_id,)).fetchone()
        c.close()
    return dict(row)


def get_kb_doc(doc_id: str) -> Optional[dict]:
    with _lock:
        c = _conn()
        row = c.execute("SELECT * FROM kb_docs WHERE id=?", (doc_id,)).fetchone()
        c.close()
    return dict(row) if row else None


def update_kb_doc(doc_id: str, **fields) -> Optional[dict]:
    allowed = {"status", "error", "chunk_count", "chars", "summary", "doc_type",
               "filename", "path", "fingerprint"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return get_kb_doc(doc_id)
    sets = ", ".join(f"{k}=?" for k in data)
    args = list(data.values()) + [datetime_now(), doc_id]
    with _lock:
        c = _conn()
        c.execute(f"UPDATE kb_docs SET {sets}, updated_at=? WHERE id=?", args)
        c.commit()
        row = c.execute("SELECT * FROM kb_docs WHERE id=?", (doc_id,)).fetchone()
        c.close()
    return dict(row) if row else None


def list_kb_docs(project_id: Optional[str] = None) -> List[dict]:
    """项目资料列表；project_id 为空时返回全局库资料。"""
    with _lock:
        c = _conn()
        if project_id:
            rows = c.execute(
                "SELECT * FROM kb_docs WHERE project_id=? OR tier='global' "
                "ORDER BY created_at DESC, rowid DESC", (project_id,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM kb_docs ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        c.close()
    return [dict(r) for r in rows]


def find_kb_doc_by_fingerprint(fingerprint: str, tier: str, project_id: str = "",
                               chapter_id: str = "") -> Optional[dict]:
    """同作用域内内容指纹相同即视为重复资料（同文件重复上传会命中）。"""
    with _lock:
        c = _conn()
        row = c.execute(
            "SELECT * FROM kb_docs WHERE fingerprint=? AND tier=? AND "
            "COALESCE(project_id,'')=? AND COALESCE(chapter_id,'')=? "
            "AND status IN ('ready','parsing') LIMIT 1",
            (fingerprint, tier, project_id or "", chapter_id or ""),
        ).fetchone()
        c.close()
    return dict(row) if row else None


# ---------- 章节来源 / 原稿 ----------

def set_chapter_sources(chapter_id: str, sources: List[dict]) -> int:
    """整体替换某章的来源清单（重新生成时旧来源必须清掉，避免越攒越多）。

    页码优先取显式 page 字段；没有就从「文件名:第N页」里解析，解析不到就留空——
    宁可不显示定位，也不编造一个页码。
    """
    import re as _re
    with _lock:
        c = _conn()
        c.execute("DELETE FROM chapter_sources WHERE chapter_id=?", (chapter_id,))
        for s in sources or []:
            page = s.get("page")
            if page in (None, ""):
                m = _re.search(r"第\s*(\d+)\s*页", s.get("source") or "")
                page = int(m.group(1)) if m else None
            c.execute(
                "INSERT INTO chapter_sources(chapter_id,doc_id,source,title,text,score,page)"
                " VALUES(?,?,?,?,?,?,?)",
                (chapter_id, s.get("doc_id") or "", s.get("source") or "",
                 s.get("title") or "", (s.get("text") or "")[:600],
                 float(s.get("score") or 0), page),
            )
        c.commit()
        c.close()
    return len(sources or [])


def get_chapter_sources(chapter_id: str) -> List[dict]:
    with _lock:
        c = _conn()
        rows = c.execute(
            "SELECT doc_id,source,title,text,score,page FROM chapter_sources "
            "WHERE chapter_id=? ORDER BY score DESC, id ASC", (chapter_id,)
        ).fetchall()
        c.close()
    return [dict(r) for r in rows]


def save_chapter_prev_content(chapter_id: str, prev: str) -> bool:
    """重写前把原稿存档，前端可一键恢复。"""
    with _lock:
        c = _conn()
        c.execute("UPDATE chapters SET prev_content=? WHERE id=?", (prev or "", chapter_id))
        c.commit()
        c.close()
    return True


def get_chapter_prev_content(chapter_id: str) -> str:
    ch = get_chapter(chapter_id) or {}
    return ch.get("prev_content") or ""


def set_chapter_gen_status(chapter_id: str, status: str, error: str = "") -> bool:
    with _lock:
        c = _conn()
        c.execute("UPDATE chapters SET gen_status=?, gen_error=? WHERE id=?",
                  (status, (error or "")[:1000], chapter_id))
        c.commit()
        c.close()
    return True
