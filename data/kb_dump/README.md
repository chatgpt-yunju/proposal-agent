# 三级知识库导出（kb_all.jsonl）

这是本系统 `data/vector_db` 里三级知识库的**全量文本导出**，随仓库分发，便于在别处重建知识库或核对引用来源。

- 导出时间：2026-11-11
- 切块总数：**19,869**
- 分布：global（全局知识库）6,777 · project（项目知识库）12,310 · chapter（章节知识库）782
- 覆盖文档：3,449 个（按 `tier + project_id + source` 统计）
- 文件大小：约 16.5 MB

## 为什么不直接放 vector_db

`data/vector_db/collection/proposal_kb/storage.sqlite` 是 1.4 GB（绝大部分是 1024 维向量与 qdrant 索引），超过 GitHub 单文件上限，也不适合放进仓库。这里只保留 payload 文本，体积降到 16.5 MB。

## 每行字段

| 字段 | 说明 |
|---|---|
| `id` | 切块唯一 id |
| `tier` | `global` / `project` / `chapter` |
| `project_id` | 所属项目（global 级无此字段） |
| `chapter_id` | 所属章节（chapter 级才有） |
| `doc_id` | 文档 id |
| `source` | 来源文件名（含页码/工作表时带后缀） |
| `title` | 文档标题 |
| `text` | 切块正文 |

## 如何重建向量库

文本有了，向量要重新算（本仓库不含 embedding 结果）：

1. 逐行读 `kb_all.jsonl`，按 `tier` / `project_id` / `chapter_id` 还原 `Scope`；
2. 用与服务一致的 embedding 模型对 `text` 重新编码；
3. `kb.store.upsert([ids], [vectors], [payloads])`，再 `flush()` 落盘。

也可以直接用系统的 `POST /api/kb/ingest` 重新入库原始文档——比从此文件重建更干净，此文件主要用于核对与迁移。
