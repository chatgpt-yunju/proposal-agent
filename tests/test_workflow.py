"""M3 写作流单元测试：离线确定性环境（numpy 后端 + local LLM），验证完整方案汇编。"""
import os
import sys
import tempfile

# 必须在导入 app 之前设置环境，保证 settings 单例取到离线配置
os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ["KB_VECTOR_BACKEND"] = "numpy"
os.environ["LLM_PROVIDER"] = "local"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.db import init_db, create_chapter, create_project
from app.kb.router import KnowledgeBase
from app.llm.chat import build_chat
from app.workflow import ProposalWorkflow


def _md(name: str, text: str) -> str:
    d = tempfile.mkdtemp()
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


@pytest.fixture()
def kb_tmp(tmp_path):
    """在临时目录使用 numpy 后端与临时 SQLite，隔离运行副产物。"""
    from app.config import settings
    old_v, old_d = settings.kb_vector_dir, settings.db_path
    settings.kb_vector_dir = str(tmp_path / "vec")
    settings.db_path = str(tmp_path / "db.sqlite")
    init_db()
    yield tmp_path
    settings.kb_vector_dir, settings.db_path = old_v, old_d


def test_workflow_assembles_proposal(kb_tmp):
    proj = create_project("测试项目", "demo")
    ch = create_chapter(proj["id"], "03", "技术架构方案", "微服务与安全设计")

    kb = KnowledgeBase()
    kb.ingest_file(_md("company.md", "# 公司 资质 ISO9001 高新技术企业 成功案例"), "global")
    kb.ingest_file(_md("rfp.md", "# A项目 数据中台 智慧校园 建设 需求 工期"), "project", project_id=proj["id"])
    kb.ingest_file(_md("tech.md", "# 技术架构 微服务 容器化 等保2.0"), "chapter",
                   project_id=proj["id"], chapter_id=ch["id"])

    chapters = [{"id": ch["id"], "code": ch["code"], "title": ch["title"],
                 "description": ch["description"], "sort": ch["sort"]}]

    chat = build_chat()
    wf = ProposalWorkflow(kb, chat)
    state = wf.run(proj["id"], proj["name"], chapters)

    # 每个章节都有正文且引用到知识库来源
    draft = state["drafts"][ch["id"]]
    assert draft.strip(), "章节正文不能为空"
    assert len(draft) >= 100, "章节正文过短"
    assert "来源" in draft, "正文应包含来源标注"
    # 完整方案：标题 + 目录 + 章节 + 引用来源
    assert "## 目录" in state["proposal"]
    assert "技术架构方案" in state["proposal"]
    # 引用来源列出了该章节检索命中的文档
    assert "## 引用来源" in state["proposal"]
    assert "tech.md" in state["proposal"], "引用来源应包含章节级命中的 tech.md"
