"""端到端演示：建项目 + 建章节 + 三级入库 + 三级检索路由验证。

运行：python scripts/demo.py
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.config import settings
from app.kb.router import KnowledgeBase

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "samples")


def reset_data() -> None:
    """每次演示前清空向量库与元数据库，保证可重复、结果确定。"""
    for p in (settings.resolved_vector_dir, settings.resolved_db_path):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)


def ingest_sample_files(kb: KnowledgeBase, project_id: str, chapters: dict) -> None:
    # 全局知识库
    for f in os.listdir(os.path.join(BASE, "global")):
        kb.ingest_file(os.path.join(BASE, "global", f), "global")
    # 项目知识库
    for f in os.listdir(os.path.join(BASE, "project")):
        kb.ingest_file(os.path.join(BASE, "project", f), "project", project_id=project_id)
    # 章节知识库（分别入到对应章节）
    for f in os.listdir(os.path.join(BASE, "chapter")):
        # 文件名前缀标识章节：tech_arch -> 技术架构章节，implement -> 实施计划章节
        if f.startswith("tech"):
            kb.ingest_file(os.path.join(BASE, "chapter", f), "chapter",
                           project_id=project_id, chapter_id=chapters["tech"])
        else:
            kb.ingest_file(os.path.join(BASE, "chapter", f), "chapter",
                           project_id=project_id, chapter_id=chapters["impl"])


def main() -> None:
    reset_data()
    db.init_db()
    kb = KnowledgeBase()

    project = db.create_project("XX大学智慧校园建设项目", "高校智慧校园（信创 + 数据中台）投标方案示例")
    pid = project["id"]
    ch_tech = db.create_chapter(pid, "03", "技术架构方案")
    ch_impl = db.create_chapter(pid, "07", "项目实施计划")
    chapters = {"tech": ch_tech["id"], "impl": ch_impl["id"]}
    print(f"[1] 已创建项目 {pid}，章节：{ch_tech['title']} / {ch_impl['title']}\n")

    ingest_sample_files(kb, pid, chapters)
    print(f"[2] 三级知识库入库完成，统计：{kb.stats()}\n")

    queries = [
        ("Q1 写「技术架构」章节", "系统应采用微服务与容器化部署并满足等保2.0安全要求", pid, chapters["tech"]),
        ("Q2 写「实施计划」章节", "项目实施分为哪几个阶段，里程碑如何安排，是否包含培训与运维", pid, chapters["impl"]),
        ("Q3 公司资质与案例", "公司具备哪些资质认证，有哪些成功案例", pid, chapters["tech"]),
        ("Q4 项目需求约束", "本项目的核心建设内容和工期要求是什么", pid, None),
    ]

    print("[3] 检索路由验证（输出每条的 tier 与权重命中）\n")
    for name, q, p, c in queries:
        cites = kb.retrieve(q, project_id=p, chapter_id=c, top_k=3)
        print(f"--- {name} ---")
        print(f"   query: {q}")
        if not cites:
            print("   (无结果)")
        for i, cit in enumerate(cites, 1):
            tag = {"chapter": "章节", "project": "项目", "global": "全局"}.get(cit.tier, cit.tier)
            print(f"   #{i} [{tag}] 来源={cit.source} 分值={cit.score:.4f}")
            print(f"       {cit.text[:60]}{'…' if len(cit.text) > 60 else ''}")
        print()

    print("[4] 项目知识库隔离验证：用不存在的项目ID检索，不应返回任何项目/章节级内容")
    cites = kb.retrieve("采购清单 智慧后勤", project_id="p_nonexist", top_k=5)
    tiers = {c.tier for c in cites}
    print(f"   返回层级集合：{tiers or '(空)'}  期望：{'global' if tiers else ''}（无 project/chapter）")


if __name__ == "__main__":
    main()
