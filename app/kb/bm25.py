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
