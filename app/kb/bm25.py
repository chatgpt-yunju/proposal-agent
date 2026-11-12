"""BM25 稀疏索引，按作用域过滤，与稠密向量做混合检索。"""
from typing import List, Optional

import jieba
from rank_bm25 import BM25Okapi

from .models import Chunk, Scope


def tokenize(text: str) -> List[str]:
    return [t for t in jieba.cut(text) if t.strip()]


class SparseIndex:
    def __init__(self) -> None:
        self._chunks: List[Chunk] = []
        self._corpus: List[List[str]] = []
        self._bm25: Optional[BM25Okapi] = None

    def remove_ids(self, ids) -> int:
        """按块 id 摘除条目并重建统计，返回摘除条数。

        只过滤已分词好的 _corpus 再重建 BM25 统计，不重新分词全库——
        删除一份 243 块的文档若走全量重建，jieba 分词会把接口拖到十几秒。
        """
        dead = set(ids or ())
        if not dead:
            return 0
        kept_chunks, kept_corpus = [], []
        for chunk, toks in zip(self._chunks, self._corpus):
            if chunk.id in dead:
                continue
            kept_chunks.append(chunk)
            kept_corpus.append(toks)
        removed = len(self._chunks) - len(kept_chunks)
        if removed:
            self._chunks = kept_chunks
            self._corpus = kept_corpus
            self._rebuild()
        return removed

    def clear(self) -> None:
        """清空索引。

        删除或迁移文档后必须清空再重建：add() 只追加，旧条目会留在 _chunks 里，
        导致稠密向量已删、BM25 仍能召回，出现「删了还能检索到」的幽灵命中。
        """
        self._chunks = []
        self._corpus = []
        self._bm25 = None

    def add(self, chunks: List[Chunk]) -> None:
        for c in chunks:
            self._chunks.append(c)
            self._corpus.append(tokenize(c.text))
        self._rebuild()

    def _rebuild(self) -> None:
        self._bm25 = BM25Okapi(self._corpus) if self._chunks else None

    @staticmethod
    def matches(chunk: Chunk, scope: Scope) -> bool:
        for key, val in scope.filter_must():
            if key == "tier" and chunk.scope.tier != val:
                return False
            if key == "project_id" and (chunk.scope.project_id or "") != val:
                return False
            if key == "chapter_id" and (chunk.scope.chapter_id or "") != val:
                return False
        return True

    def search(self, query: str, top_k: int, scope: Scope) -> List[tuple]:
        """返回 [(Chunk, bm25_score)]，按分数降序。"""
        if not self._bm25:
            return []
        q = tokenize(query)
        if not q:
            return []
        scores = self._bm25.get_scores(q)
        scored = [
            (self._chunks[i], float(scores[i]))
            for i in range(len(self._chunks))
            if self.matches(self._chunks[i], scope) and scores[i] > 0
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def score_all(self, query: str) -> dict:
        """返回 {chunk_id: (bm25_score, Chunk)}，覆盖全部块，供跨作用域统一归一化。"""
        if not self._bm25:
            return {}
        q = tokenize(query)
        if not q:
            return {}
        scores = self._bm25.get_scores(q)
        return {self._chunks[i].id: (float(scores[i]), self._chunks[i]) for i in range(len(self._chunks))}

    def count(self, scope: Optional[Scope] = None) -> int:
        if scope is None:
            return len(self._chunks)
        return sum(1 for c in self._chunks if self.matches(c, scope))
