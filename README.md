# 写方案智能体 · 三级知识库 + LangGraph 写作流 + Word/PDF 导出 + Web UI

基于开源组件自建、面向「写方案」的智能体核心实现（M1 工程骨架 + M2 三级知识库 + M3 LangGraph 写作流 + M4 Word/PDF 导出 + M5 Web UI/Docker）。
知识库分三级：**全局 / 项目 / 章节**，写作时按当前章节自动路由检索（章节 > 项目 > 全局），
提纲 → 逐章节生成 → 审查 → 修订（循环）→ 汇编成完整方案，并可导出 Word / PDF 交付甲方。

## 技术栈（全部开源）

| 组件 | 用途 |
| --- | --- |
| FastAPI | API 层 |
| Qdrant（本地嵌入式） | 稠密向量存储，按元数据作用域过滤 |
| rank-bm25 + jieba | 稀疏（BM25）检索，中文分词 |
| OpenAI 兼容 SDK | LLM / 嵌入接入（可切豆包、DeepSeek、本地 vLLM） |
| LangGraph | 写作流状态图（生成 → 审查 → 修订循环 → 汇编） |
| PyMuPDF / python-docx | PDF / Word 解析 |
| numpy（兜底） | 本地嵌入（字符 bigram 哈希向量），离线可运行 |

## 目录结构

```
proposal-agent/
  app/
    config.py        # 配置（环境变量 / .env）
    db.py            # SQLite：项目 / 章节
    schemas.py       # API 模型
    main.py          # FastAPI 入口
    llm/embeddings.py# 嵌入（api / local 可插拔）
    llm/chat.py     # 写作 LLM（api / local 可插拔）
    kb/
      models.py      # Scope / Chunk / Citation
      chunker.py     # 文档解析 + 切块
      storage.py     # Qdrant / numpy 向量存储
      bm25.py        # BM25 稀疏索引
      router.py      # 入库 + 三级检索路由
    workflow/
      state.py       # 写作流状态
      engine.py      # LangGraph：生成/审查/修订/汇编
  scripts/
    ingest.py        # CLI 入库
    demo.py          # 端到端演示（检索路由）
    write_proposal.py# 端到端演示（生成完整方案 + 导出 Word/PDF）
    export_proposal.py# 已有方案 .md → Word/PDF
  app/export/        # Markdown → docx / pdf
  app/static/        # Web UI（index.html）
  Dockerfile / docker-compose.yml  # 容器化部署
  tests/test_router.py  # 知识库单元测试
  tests/test_workflow.py# 写作流单元测试
  data/samples/      # 三级样例文档
```

## 快速开始

```powershell
# 1. 创建虚拟环境并安装依赖（已含于 .venv，可跳过）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2.（可选）按需复制 .env.example 为 .env 并配置嵌入
Copy-Item .env.example .env

# 3. 运行端到端演示（建项目 + 三级入库 + 检索路由验证）
.\.venv\Scripts\python.exe scripts\demo.py

# 4. 生成完整方案并导出 Word / PDF（LangGraph 写作流 → 汇编 → 导出）
.\.venv\Scripts\python.exe scripts\write_proposal.py
#   产物：data\output\<项目>_方案.{md,docx,pdf}（默认用离线兜底 LLM；配 api 后获得真正智能生成）
#   也可对已有 .md 单独导出：
.\.venv\Scripts\python.exe scripts\export_proposal.py data\output\<项目>_方案.md

# 5. 运行单元测试
.\.venv\Scripts\python.exe -m pytest tests -q

# 6. 启动 API + Web UI（浏览器打开 http://127.0.0.1:8000：建项目 → 传知识 → 生成 → 下载）
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
#   接口文档：http://127.0.0.1:8000/docs

# 7.（可选）Docker 部署
docker compose up --build -d     # 映射 8000 端口，数据卷 ./data:/app/data
```

## 主要接口

| 接口 | 说明 |
| --- | --- |
| `POST /api/projects` | 创建项目 |
| `POST /api/projects/{pid}/chapters` | 创建章节 |
| `POST /api/kb/ingest` | 上传文件入库（form：tier、project_id、chapter_id、files） |
| `POST /api/kb/retrieve` | 三级检索（json：query、project_id、chapter_id、top_k） |
| `GET /api/kb/stats` | 各级知识库统计 |
| `POST /api/workflow/generate` | 对某项目跑 LangGraph 写作流，生成完整方案并导出（json：project_id） |
| `GET /api/projects/{pid}/proposal` | 查看/获取已生成方案文本 |
| `GET /api/export/{pid}/{fmt}` | 下载导出文件（fmt：md / docx / pdf） |
| `GET /` | Web UI |

## 三级知识库检索语义

写某章节时并行检索三个作用域，加权融合后按分值排序返回带引用（来源文件/页码）的候选：

- **章节**（tier=chapter，project_id+chapter_id 过滤）：权重最高
- **项目**（tier=project，project_id 过滤）：权重次之
- **全局**（tier=global）：始终参与，权重最低

混合检索 = 稠密向量（Qdrant，相似度阈值过滤）+ BM25（作用域内），RRF 融合 × 作用域权重。
项目知识库按 project_id 硬隔离，杜绝跨项目串料。

## 配置项（.env）

- `EMBEDDING_PROVIDER`：`api`（OpenAI 兼容，推荐，质量最高）或 `local`（离线兜底，默认）
- `KB_VECTOR_BACKEND`：`qdrant`（默认，不可用自动回退 numpy）或 `numpy`
- `SCOPE_WEIGHT_CHAPTER/PROJECT/GLOBAL`：三级权重（默认 1.2 / 1.1 / 1.0，相关性为主、具体优先为辅）
- `RETRIEVAL_MIN_REL`：相关性门槛（默认 0.10，低于此值的召回视为无关）
- `LLM_PROVIDER`：`api`（OpenAI 兼容 /chat/completions，推荐）或 `local`（离线兜底，默认）
- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`：api 提供者所需
- `WORKFLOW_MAX_ITERATIONS`：审查-修订循环上限（默认 2）
- `PROPOSAL_OUTPUT_DIR`：生成方案的输出目录（默认 `./data/output`）
- `PDF_FONT_PATH`（可选）：导出 PDF 用的中文字体路径；不设置时自动探测 Windows/Linux 系统字体，容器内置 Noto CJK

## 文档导出（M4）

`app/export/` 把方案 Markdown 渲染为 Word(.docx，python-docx，宋体正文/微软雅黑标题) 与 PDF
（PyMuPDF，A4，自动换行、嵌入中文字体并 `subset_fonts` 裁剪体积）。写方案时自动导出，也可用
`scripts/export_proposal.py` 单独转换。

## 写作流（M3）

用 LangGraph 状态图表达"生成 → 审查 → 修订 → 汇编"：对每个章节，以章节标题/要求为查询做三级检索，
LLM 依据检索引用（带来源标注）生成正文；审查校验内容长度与来源标注；有问题的章节携带审查问题重新生成
（有界循环）；最后按章节顺序汇编完整方案并在末尾列出全部引用来源。

## 离线嵌入质量说明（重要）

`EMBEDDING_PROVIDER=local` 是无外部依赖的兜底方案：jieba 分词 + IDF 加权哈希向量，
用于离线演示与自动化测试，**语义质量有限**（例如"公司资质/案例"类查询可能因词面重叠
把竞品分析排在真正的公司资质内容之前，这是兜底嵌入的相关度天花板，而非路由错误）。

**生产环境请设置 `EMBEDDING_PROVIDER=api`**，配置 OpenAI 兼容的 `EMBEDDING_BASE_URL /
EMBEDDING_API_KEY / EMBEDDING_MODEL`（可接豆包、DeepSeek、本地 vLLM 等），获得真正的语义检索精度。

## 已实现 / 待实现

- ✅ M1 工程骨架（FastAPI + 配置 + SQLite）
- ✅ M2 三级知识库（解析 / 入库 / 三级检索 / 引用 / 隔离）
- ✅ M3 写作流（LangGraph：提纲 → 逐章节生成 → 审查 → 修订 → 汇编）
- ✅ M4 文档导出（方案 Markdown → Word / PDF）
- ✅ M5 Web UI（建项目 / 传知识 / 生成 / 下载）+ Docker 部署
