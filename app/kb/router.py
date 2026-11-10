"""知识库总入口：入库 + 三级检索路由（章节 > 项目 > 全局），dense+BM25 混合 + 加权融合 + 引用。"""
from typing import List, Optional
from uuid import uuid4

from ..config import settings
from ..llm.embeddings import build_embedder
from .bm25 import SparseIndex
from .chunker import chunk_file
from .models import Chunk, Citation, Scope
from .storage import build_store, VectorStore

class KnowledgeBase:
    def __init__(self) -> None:
        self.store: VectorStore = build_store(
            settings.kb_vector_backend, settings.resolved_vector_dir, settings.kb_collection
        )
        self.sparse = SparseIndex()
        self.embedder = build_embedder(
            settings.embedding_provider,
            settings.embedding_base_url,
            settings.embedding_api_key,
            settings.embedding_model,
            settings.embedding_local_dim,
        )

    # ---------- 入库 ----------
    def ingest_chunks(self, chunks: List[Chunk]) -> int:
        if not chunks:
            return 0
        texts = [c.text for c in chunks]
        if hasattr(self.embedder, "add_docs"):
            self.embedder.add_docs(texts)  # 登记文档频率，供 IDF 加权
        vecs = self.embedder.embed(texts)
        dim = self.embedder.dim or len(vecs[0])
        self.store.ensure(dim)
        payloads = [
            {
                **c.scope.payload(),
                "doc_id": c.doc_id,
                "source": c.source,
                "title": c.title,
                "text": c.text,
            }
            for c in chunks
        ]
        self.store.upsert([c.id for c in chunks], vecs, payloads)
        self.sparse.add(chunks)
        return len(chunks)

    def ingest_file(self, path: str, tier: str, project_id: Optional[str] = None,
                    chapter_id: Optional[str] = None, doc_id: Optional[str] = None,
                    source_label: Optional[str] = None) -> int:
        scope = Scope(tier=tier, project_id=project_id, chapter_id=chapter_id)
        scope.validate()
        doc_id = doc_id or f"{tier}-{uuid4().hex[:8]}"
        chunks = chunk_file(path, scope, doc_id, source_label=source_label)
        return self.ingest_chunks(chunks)

    # ---------- 检索 ----------
    def _scope_search(self, query: str, scope: Scope, top_k: int) -> List[tuple]:
        """单作用域内 dense+sparse 混合。

        相关性 = 0.6*稠密余弦 + 0.4*BM25(全局归一化)，低于门槛的召回视为无关剔除；
        返回 [(Citation, rel_score)]，rel_score 与作用域无关、可在各层间比较。
        """
        vec = self.embedder.embed([query])[0]
        dense = self.store.search(vec, top_k, scope)          # (id, payload, cosine)
        all_bm = self.sparse.score_all(query)                 # {id: (bm25, Chunk)}
        max_b = max((s for s, _ in all_bm.values()), default=0.0)

        cand: dict = {}
        for cid, pay, sim in dense:
            if sim <= 0:
                continue
            cand[cid] = {"dense": float(sim), "sparse": 0.0, "payload": pay, "text": pay.get("text", "")}
        for cid, (b, chunk) in all_bm.items():
            if b <= 0 or not self.sparse.matches(chunk, scope):
                continue
            norm = b / max_b if max_b > 0 else 0.0
            # 补全引用溯源字段（doc_id / source / title）
            pay = {
                **chunk.scope.payload(),
                "doc_id": chunk.doc_id,
                "source": chunk.source,
                "title": chunk.title,
                "text": chunk.text,
            }
            if cid in cand:
                cand[cid]["sparse"] = max(cand[cid]["sparse"], norm)
                if not cand[cid]["payload"].get("source"):
                    cand[cid]["payload"] = pay
            else:
                cand[cid] = {"dense": 0.0, "sparse": norm, "payload": pay, "text": chunk.text}

        result = []
        for cid, m in cand.items():
            rel = 0.6 * m["dense"] + 0.4 * m["sparse"]
            if rel < settings.retrieval_min_rel:
                continue
            pay = m["payload"]
            result.append((
                Citation(
                    text=m["text"],
                    tier=pay.get("tier", scope.tier),
                    project_id=pay.get("project_id") or None,
                    chapter_id=pay.get("chapter_id") or None,
                    doc_id=pay.get("doc_id", ""),
                    source=pay.get("source", ""),
                    title=pay.get("title", ""),
                    score=rel,
                ),
                rel,
            ))
        result.sort(key=lambda x: x[1], reverse=True)
        return result[:top_k]

    def retrieve(self, query: str, project_id: Optional[str] = None,
                 chapter_id: Optional[str] = None, top_k: Optional[int] = None) -> List[Citation]:
        top_k = top_k or settings.retrieval_top_k
        weights = settings.scope_weights()

        scopes: List[tuple] = []  # (scope_name, Scope, weight)
        if chapter_id:
            scopes.append(("chapter", Scope("chapter", project_id, chapter_id), weights["chapter"]))
        if project_id:
            scopes.append(("project", Scope("project", project_id), weights["project"]))
        scopes.append(("global", Scope("global"), weights["global"]))

        # 相关性为主、作用域权重为辅：仅打破同级相关性相当时的次序，不覆盖真实相关度
        combined: List[tuple] = []
        for _, scope, weight in scopes:
            for cit, rel in self._scope_search(query, scope, top_k):
                cit.score = round(rel * weight, 6)
                combined.append((cit, cit.score))
        combined.sort(key=lambda x: x[1], reverse=True)

        seen, result = set(), []
        for cit, _ in combined:
            key = (cit.doc_id, cit.text)
            if key in seen:
                continue
            seen.add(key)
            result.append(cit)
            if len(result) >= top_k:
                break
        return result

    def stats(self) -> dict:
        return {
            "qdrant_count": self.store.count(None),
            "sparse_count": self.sparse.count(None),
            "tiers": {
                "global": self.store.count(Scope("global")),
                "project": self.store.count(Scope("project")),
                "chapter": self.store.count(Scope("chapter")),
            },
        }

    def list_docs(self, project_id: Optional[str] = None) -> List[dict]:
        """按文档分组列出知识库内容，返回 [{doc_id, source, tier, chunk_count, samples}]。"""
        all_items = self.store.list_all()
        # 按 doc_id 分组
        docs = {}
        for cid, pay, _ in all_items:
            doc_id = pay.get("doc_id", "unknown")
            if doc_id not in docs:
                docs[doc_id] = {
                    "doc_id": doc_id,
                    "source": pay.get("source", ""),
                    "title": pay.get("title", ""),
                    "tier": pay.get("tier", ""),
                    "project_id": pay.get("project_id", ""),
                    "chapter_id": pay.get("chapter_id", ""),
                    "chunks": [],
                }
            docs[doc_id]["chunks"].append(pay.get("text", ""))

        # 过滤：如果指定了 project_id，只返回该项目相关的 + 全局的
        result = []
        for doc in docs.values():
            if project_id:
                if doc["tier"] == "global" or doc["project_id"] == project_id:
                    result.append(doc)
            else:
                result.append(doc)

        # 按 tier 排序：global → project → chapter
        tier_order = {"global": 0, "project": 1, "chapter": 2}
        result.sort(key=lambda x: (tier_order.get(x["tier"], 9), x["source"]))
        return result
