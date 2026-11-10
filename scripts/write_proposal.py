"""M3 端到端演示：重置数据 → 建项目/章节 → 三级入库样例 → LangGraph 写作流生成完整方案。

运行：.venv\\Scripts\\python.exe scripts\\write_proposal.py
默认用离线兜底 LLM（LLM_PROVIDER=local）验证工作流管线；
配置 api 后（.env：LLM_PROVIDER=api + LLM_BASE_URL/LLM_API_KEY/LLM_MODEL）获得真正智能生成。
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.db import init_db, create_project, create_chapter
from app.kb.router import KnowledgeBase
from app.llm.chat import build_chat
from app.workflow import ProposalWorkflow
from app.export import export_proposal

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "samples")


def reset_data() -> None:
    for p in (settings.resolved_vector_dir, settings.resolved_db_path):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)


def ingest_samples(kb: KnowledgeBase, pid: str, cid1: str) -> None:
    for tier, d, extra in (("global", "global", {}),
                           ("project", "project", {"project_id": pid}),
                           ("chapter", "chapter", {"project_id": pid, "chapter_id": cid1})):
        for fname in os.listdir(os.path.join(BASE, d)):
            kb.ingest_file(os.path.join(BASE, d, fname), tier, **extra)


def main() -> None:
    reset_data()
    init_db()

    proj = create_project("XX大学智慧校园", "综合智慧校园建设项目")
    pid = proj["id"]
    c1 = create_chapter(pid, "03", "技术架构方案", "系统分层架构、微服务与容器化、安全与等保设计")
    c2 = create_chapter(pid, "07", "项目实施计划", "分阶段实施、里程碑、工期、培训与运维保障")
    chapters = [{"id": c1["id"], "code": c1["code"], "title": c1["title"],
                 "description": c1["description"], "sort": c1["sort"]},
                {"id": c2["id"], "code": c2["code"], "title": c2["title"],
                 "description": c2["description"], "sort": c2["sort"]}]

    kb = KnowledgeBase()
    ingest_samples(kb, pid, c1["id"])
    print(f"[1] 三级知识库入库完成，统计：{kb.stats()}")

    chat = build_chat()
    wf = ProposalWorkflow(kb, chat)
    state = wf.run(pid, proj["name"], chapters)

    print(f"[2] 写作流完成（迭代 {state['iterations']} 轮）")
    for ch in chapters:
        cid = ch["id"]
        n_src = len({c.get("source") for c in state["retrievals"].get(cid, [])})
        print(f"   - {ch['code']} {ch['title']}: {len(state['drafts'][cid])}字, "
              f"引用 {n_src} 份, 审查: {'通过' if not state['reviews'][cid] else state['reviews'][cid]}")

    out_dir = settings.resolved_output_dir
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{proj['name']}_方案.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(state["proposal"])
    print(f"[3] 完整方案已生成：{out_path}")
    print(f"    引用来源总数：{state['proposal'].count('[')} 条；章节数：{len(chapters)}")

    # M4：同时导出 Word / PDF 交付形态
    base = os.path.splitext(out_path)[0]
    exported = export_proposal(state["proposal"], base + ".docx", base + ".pdf")
    print(f"[4] 已导出交付文档：Word={exported['docx']}")
    print(f"                    PDF={exported['pdf']}")
    print("WRITE_OK")


if __name__ == "__main__":
    main()
