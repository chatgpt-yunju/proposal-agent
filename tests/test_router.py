"""三级知识库核心单元测试：作用域优先级 + 项目隔离。

运行：python -m pytest tests -q   （离线，默认 local 嵌入 + numpy 存储）
"""
import os
import sys
import tempfile

# 必须在导入 app.config 之前设置环境变量，保证离线确定性
os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ["KB_VECTOR_BACKEND"] = "numpy"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.kb.router import KnowledgeBase  # noqa: E402


def _md(text: str) -> str:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "x.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def test_tier_scope_priority():
    kb = KnowledgeBase()
    g = _md("# 公司概况 云际网络 拥有 ISO9001 与 CMMI3 资质，是高新技术企业。")
    pj = _md("# 本项目采用统一身份认证与信创适配，工期120天。")
    ch = _md("# 技术架构采用微服务与容器化部署，满足等保2.0三级安全要求。")
    kb.ingest_file(g, "global")
    kb.ingest_file(pj, "project", project_id="P1")
    kb.ingest_file(ch, "chapter", project_id="P1", chapter_id="C1")

    # 写技术架构章节时，相关 query 应命中章节级
    cites = kb.retrieve("微服务 容器化 等保 部署", project_id="P1", chapter_id="C1", top_k=3)
    assert cites, "应返回结果"
    assert cites[0].tier == "chapter", f"期望章节级命中，实际 {cites[0].tier}: {cites[0].text}"

    # 资质类全局 query 应命中全局级
    cites2 = kb.retrieve("公司资质 认证 高新技术", project_id="P1", chapter_id="C1", top_k=3)
    assert cites2 and cites2[0].tier == "global", f"期望全局级命中，实际 {cites2[0].tier if cites2 else '空'}"


def test_project_isolation():
    kb = KnowledgeBase()
    a = _md("# A项目专用 数据中台 采购清单 服务器 存储")
    b = _md("# B项目专用 智慧后勤 采购清单 门禁 安防")
    kb.ingest_file(a, "project", project_id="P_A")
    kb.ingest_file(b, "project", project_id="P_B")

    # 查询话题与 P_A 内容重叠、与 P_B 不同：应命中 P_A，且绝不返回 P_B 的项目级知识
    cites = kb.retrieve("数据中台 采购", project_id="P_A", top_k=3)
    assert cites, "应返回结果"
    assert all(c.project_id != "P_B" for c in cites), "不得返回其他项目的项目级知识"
    assert any(c.project_id == "P_A" for c in cites), "应返回本项目知识"


def test_global_always_available_without_project():
    kb = KnowledgeBase()
    g = _md("# 公司 方法论 分阶段交付 需求分析 设计 开发 测试 上线")
    kb.ingest_file(g, "global")
    cites = kb.retrieve("分阶段交付 方法论", top_k=3)  # 无项目/章节上下文
    assert cites and cites[0].tier == "global"
