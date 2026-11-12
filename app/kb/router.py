"""知识库总入口：入库 + 三级检索路由（章节 > 项目 > 全局），dense+BM25 混合 + 加权融合 + 引用。"""
import re
from typing import List, Optional
from uuid import uuid4

from ..config import settings
from ..llm.embeddings import build_embedder
from .bm25 import SparseIndex
from .chunker import chunk_file
from .models import Chunk, Citation, Scope
from .storage import build_store, VectorStore

# 县级/市级地名（砀山县 / 黄山市 / 徽州区 / 内蒙古某旗）
_COUNTY_RE = re.compile(r"[\u4e00-\u9fa5]{1,4}?(?:县|市|区|旗)")


def _own_county(project_name: str) -> str:
    """从项目名里抠出本县名（砀山县矿产资源十五五规划 → 砀山县）。"""
    if not project_name:
        return ""
    m = _COUNTY_RE.search(project_name)
    return m.group(0) if m else ""


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
        # BM25 索引是纯内存结构，进程重启后会清空；从已落盘的向量库重建，
        # 否则混合检索会退化成只有稠密一路，召回质量明显下降。
        self._rebuild_sparse()

    def _rebuild_sparse(self) -> None:
        try:
            items = self.store.list_all()
        except Exception:
            import logging
            logging.getLogger(__name__).warning("重建 BM25 索引失败：读取向量库异常", exc_info=True)
            return
        if not items:
            return
        import time
        t0 = time.time()
        # 先清空再灌入：add() 是追加语义，重复重建会让同一块在 BM25 里出现多次
        self.sparse.clear()
        chunks: List[Chunk] = []
        for cid, pay, _ in items:
            text = pay.get("text") or ""
            if not text:
                continue
            chunks.append(Chunk(
                id=str(cid),
                text=text,
                scope=Scope(
                    tier=pay.get("tier") or "global",
                    project_id=pay.get("project_id") or None,
                    chapter_id=pay.get("chapter_id") or None,
                ),
                doc_id=pay.get("doc_id") or "",
                source=pay.get("source") or "",
                title=pay.get("title") or "",
            ))
        if chunks:
            self.sparse.add(chunks)
            import logging
            logging.getLogger(__name__).info(
                "BM25 索引已重建：%d 条，耗时 %.2fs", len(chunks), time.time() - t0
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
        # flat 后端把增量放在内存 pending 里、累计 200 条才落盘；
        # 入库后立即 flush，保证进程重启不丢知识库数据。
        self.flush()
        return len(chunks)

    def flush(self) -> None:
        """把向量库的内存增量落盘（无 flush 能力的后端为空操作）。"""
        fn = getattr(self.store, "flush", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                import logging
                logging.getLogger(__name__).warning("向量库 flush 失败", exc_info=True)

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
                 chapter_id: Optional[str] = None, top_k: Optional[int] = None,
                 project_name: str = "") -> List[Citation]:
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

        # 地域软偏置：本县提权、外县降权（砀山方案不再把龙陵/桃江排进前排）
        if project_name:
            for cit, _ in combined:
                blob = f"{getattr(cit, 'text', '')} {getattr(cit, 'title', '')} {getattr(cit, 'source', '')}"
                own = _own_county(project_name)
                if own and own in blob:
                    cit.score = round(cit.score * 1.15, 6)
                if own:
                    others = {c for c in _COUNTY_RE.findall(blob) if c and c != own}
                    if others:
                        cit.score = round(cit.score * 0.75, 6)
            combined.sort(key=lambda x: x[1].score, reverse=True)

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

    def list_docs(self, project_id: Optional[str] = None, with_text: bool = False,
                  max_chunks_per_doc: int = 3) -> List[dict]:
        """按「层级 + 归属 + 文档」分组列出知识库内容。

        分组键必须带 tier/project_id/chapter_id，不能只用 doc_id：
        同一份资料常常既入了全局层、又被某个项目再入库一次（编制指南、法规原文、
        同类规划案例都会这样），只用 doc_id 分组会把项目层并进同名的全局文档——
        层级字段取自先遍历到的那一条（通常是全局），前端就只剩「全局」一类，
        看起来像「三级知识库没同步过来」；更糟的是不同项目的同名文档会被并成一份，
        列表里把 A 项目的内容显示在 B 项目下面。

        with_text=False 时不返回分块正文，只给 chunk_count：全库 19869 块原文
        拼进一次响应是 10 MB 级，前端列表只用到条数与来源，没必要传正文。
        """
        all_items = self.store.list_all()
        docs = {}
        for cid, pay, _ in all_items:
            tier = pay.get("tier") or ""
            pid = pay.get("project_id") or ""
            chid = pay.get("chapter_id") or ""
            doc_id = pay.get("doc_id") or "unknown"
            key = (tier, pid, chid, doc_id)
            doc = docs.get(key)
            if doc is None:
                doc = docs[key] = {
                    "doc_id": doc_id,
                    "source": pay.get("source", ""),
                    "title": pay.get("title", ""),
                    "tier": tier,
                    "project_id": pid,
                    "chapter_id": chid,
                    "chunk_count": 0,
                    "chunks": [],
                }
            doc["chunk_count"] += 1
            if with_text and len(doc["chunks"]) < max_chunks_per_doc:
                doc["chunks"].append(pay.get("text", ""))

        # 过滤：如果指定了 project_id，只返回该项目相关的 + 全局的
        result = []
        for doc in docs.values():
            if project_id:
                if doc["tier"] == "global" or doc["project_id"] == project_id:
                    result.append(doc)
            else:
                result.append(doc)

        # 按 tier 排序：global → project → chapter；同层内按来源、归属稳定排序
        tier_order = {"global": 0, "project": 1, "chapter": 2}
        result.sort(key=lambda x: (tier_order.get(x["tier"], 9), x["source"], x["project_id"], x["chapter_id"]))
        return result

    def clone_docs(self, src_pid: str, dst_pid: str, idmap: dict) -> int:
        """把源项目的「项目层 + 章节层」知识库整批复制到新项目。

        复用原向量、只重写一次向量文件、只对新块做一轮 BM25 分词：
        这是复制项目唯一安全的做法——走 ingest_chunks 会每 200 块触发一次全库 flush，
        621MB 库反复进内存，直接把生产机拖进 OOM。
        """
        fn_iter = getattr(self.store, "iter_rows", None)
        fn_clone = getattr(self.store, "clone_rows", None)
        if not callable(fn_iter) or not callable(fn_clone):
            raise RuntimeError("当前向量后端不支持整批克隆（请确认 KB_VECTOR_BACKEND=flat）")

        def pick(m: dict) -> bool:
            tier = m.get("tier") or ""
            if tier == "project":
                return (m.get("project_id") or "") == src_pid
            if tier == "chapter":
                return (m.get("chapter_id") or "") in idmap
            return False

        rows = []
        chunks: List[Chunk] = []
        for pay, vec in fn_iter(pick):
            text = pay.get("text") or ""
            if not text:
                continue
            tier = pay.get("tier") or ""
            if tier == "project":
                scope = Scope("project", dst_pid)
            else:
                scope = Scope("chapter", dst_pid, idmap.get(pay.get("chapter_id") or ""))
            new_id = f"{tier}-{uuid4().hex[:12]}"
            new_pay = {
                **scope.payload(),
                "id": new_id,
                "doc_id": pay.get("doc_id") or "",
                "source": pay.get("source") or "",
                "title": pay.get("title") or "",
                "text": text,
            }
            rows.append((new_pay, vec))
            chunks.append(Chunk(id=new_id, text=text, scope=scope,
                                doc_id=new_pay["doc_id"], source=new_pay["source"],
                                title=new_pay["title"]))
        if not rows:
            return 0
        n = fn_clone(rows)
        if chunks:
            self.sparse.add(chunks)
        return n

    # ---------- 在线编辑：删除 / 全局与项目互转 ----------
    @staticmethod
    def _doc_matcher(tier: str, project_id: Optional[str], chapter_id: Optional[str], doc_id: str):
        """按「层级 + 归属 + 文档」精确定位一份文档的全部块。

        必须带 tier 与归属：同一份资料常常同时入了全局层和某项目层，
        只按 doc_id 删会把两个层级的同名文档一起删掉。
        """
        def match(m: dict) -> bool:
            return ((m.get("tier") or "") == (tier or "")
                    and (m.get("project_id") or "") == (project_id or "")
                    and (m.get("chapter_id") or "") == (chapter_id or "")
                    and (m.get("doc_id") or "") == (doc_id or ""))
        return match

    def _ids_of(self, match) -> List[str]:
        try:
            return [str(cid) for cid, pay, _ in self.store.list_all() if match(pay)]
        except Exception:
            return []

    def delete_doc(self, tier: str, project_id: Optional[str], chapter_id: Optional[str],
                   doc_id: str) -> int:
        """物理删除一份文档的全部块，返回删除条数。

        向量库重写后 BM25 必须同步摘除，否则会出现「稠密已删、稀疏仍能召回」的幽灵结果。
        """
        fn = getattr(self.store, "delete_docs", None)
        if not callable(fn):
            raise RuntimeError("当前向量后端不支持在线删除（请确认 KB_VECTOR_BACKEND=flat）")
        match = self._doc_matcher(tier, project_id, chapter_id, doc_id)
        ids = self._ids_of(match)
        n = fn(match)
        if ids:
            self.sparse.remove_ids(ids)
        return n

    def move_doc(self, tier: str, project_id: Optional[str], chapter_id: Optional[str], doc_id: str,
                 target_tier: str, target_project_id: Optional[str] = None) -> int:
        """在全局库与项目知识库之间迁移一份文档，返回迁移条数。

        向量不变、只改归属；迁移后该文档的章级绑定一律清空（跨层迁移还带着 chapter_id
        会指向别的章节，检索时既命不中也容易误命中本层其它内容）。
        """
        fn = getattr(self.store, "reassign_docs", None)
        if not callable(fn):
            raise RuntimeError("当前向量后端不支持在线迁移（请确认 KB_VECTOR_BACKEND=flat）")
        if target_tier == "project":
            if not target_project_id:
                raise ValueError("迁移到项目知识库必须指定目标项目")
            patch = {"tier": "project", "project_id": target_project_id, "chapter_id": ""}
        elif target_tier == "global":
            patch = {"tier": "global", "project_id": "", "chapter_id": ""}
        else:
            raise ValueError("只支持在「全局知识库」与「项目知识库」之间互转")
        match = self._doc_matcher(tier, project_id, chapter_id, doc_id)
        ids = self._ids_of(match)
        n = fn(match, patch)
        if ids:
            # BM25 里存的是 Chunk 快照，含旧的 scope；必须摘掉后按新归属重新灌入
            self.sparse.remove_ids(ids)
            fresh: List[Chunk] = []
            for cid, pay, _ in self.store.list_all():
                if str(cid) in set(ids):
                    text = pay.get("text") or ""
                    if not text:
                        continue
                    fresh.append(Chunk(
                        id=str(cid), text=text,
                        scope=Scope(tier=pay.get("tier") or "global",
                                    project_id=pay.get("project_id") or None,
                                    chapter_id=pay.get("chapter_id") or None),
                        doc_id=pay.get("doc_id") or "",
                        source=pay.get("source") or "",
                        title=pay.get("title") or "",
                    ))
            if fresh:
                self.sparse.add(fresh)
        return n
