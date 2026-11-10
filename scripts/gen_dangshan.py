"""砀山县矿产资源规划：对已建项目/章节跑 LangGraph 写作流并导出 Word/PDF。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.config import settings
from app.kb.router import KnowledgeBase
from app.llm.chat import build_chat
from app.workflow import ProposalWorkflow
from app.export import export_proposal

PROJ_NAME = "砀山县矿产资源规划"


def main():
    db.init_db()
    proj = next((p for p in db.list_projects() if p["name"] == PROJ_NAME), None)
    if not proj:
        print("未找到项目，请先运行 ingest_dangshan.py")
        return
    pid = proj["id"]
    chapters = [{"id": c["id"], "code": c["code"], "title": c["title"],
                 "description": c["description"], "sort": c["sort"]}
                for c in db.list_chapters(pid)]
    print(f"[项目] {proj['name']}，章节数 {len(chapters)}")

    kb = KnowledgeBase()
    chat = build_chat()
    wf = ProposalWorkflow(kb, chat)
    state = wf.run(pid, proj["name"], chapters)

    print(f"[写作流] 完成，迭代 {state['iterations']} 轮")
    for ch in chapters:
        cid = ch["id"]
        n_src = len({c.get("source") for c in state["retrievals"].get(cid, [])})
        print(f"   - {ch['code']} {ch['title']}: {len(state['drafts'][cid])}字, 引用 {n_src} 份, "
              f"审查: {'通过' if not state['reviews'][cid] else state['reviews'][cid]}")

    out_dir = settings.resolved_output_dir
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{proj['name']}_方案.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(state["proposal"])
    print(f"[方案] {out_path}（{len(state['proposal'])} 字）")

    base = os.path.splitext(out_path)[0]
    exported = export_proposal(state["proposal"], base + ".docx", base + ".pdf")
    print(f"[导出] Word={exported['docx']}")
    print(f"       PDF={exported['pdf']}")
    print("GEN_OK")


if __name__ == "__main__":
    main()
