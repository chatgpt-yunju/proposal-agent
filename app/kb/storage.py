"""向量存储抽象：Qdrant(本地嵌入式) 优先，numpy 内存实现兜底，保证离线可运行。"""
from typing import List, Optional

from .models import Scope


class VectorStore:
    def ensure(self, dim: int) -> None: ...
    def upsert(self, ids: List[str], vectors: List[list], payloads: List[dict]) -> None: ...
    def search(self, vector: list, top_k: int, scope: Scope) -> List[tuple]: ...
    def count(self, scope: Optional[Scope] = None) -> int: ...
    def list_all(self) -> List[tuple]: ...


class QdrantStore(VectorStore):
    """Qdrant 本地嵌入式模式：QdrantClient(path=...)，无需外部服务。"""

    def __init__(self, path: str, collection: str):
        from qdrant_client import QdrantClient, models as qm
        self._qm = qm
        self._client = QdrantClient(path=path)
        self._collection = collection
        self._dim = 0

    def ensure(self, dim: int) -> None:
        if self._dim == dim:
            return
        self._dim = dim
        cols = [c.name for c in self._client.get_collections().collections]
        if self._collection in cols:
            self._client.delete_collection(self._collection)
        self._client.create_collection(
            collection_name=self._collection,
            vectors_config=self._qm.VectorParams(size=dim, distance=self._qm.Distance.COSINE),
        )

    def upsert(self, ids: List[str], vectors: List[list], payloads: List[dict]) -> None:
        points = [
            self._qm.PointStruct(id=ids[i], vector=vectors[i], payload=payloads[i])
            for i in range(len(ids))
        ]
        self._client.upsert(collection_name=self._collection, points=points)

    def _filter(self, scope: Scope):
        must = [
            self._qm.FieldCondition(key=k, match=self._qm.MatchValue(value=v))
            for k, v in scope.filter_must()
        ]
        return self._qm.Filter(must=must) if must else None

    def search(self, vector: list, top_k: int, scope: Scope) -> List[tuple]:
        # 知识库为空/集合尚未创建时返回空结果，不能让整个生成接口 500
        if not self._exists():
            return []
        res = self._client.query_points(
            collection_name=self._collection,
            query=vector,
            query_filter=self._filter(scope),
            limit=top_k,
            with_payload=True,
        )
        return [(r.id, r.payload, r.score) for r in res.points]

    def count(self, scope: Optional[Scope] = None) -> int:
        if not self._exists():
            return 0
        return self._client.count(
            self._collection,
            count_filter=self._filter(scope) if scope else None,
            exact=True,
        ).count

    def _exists(self) -> bool:
        try:
            cols = [c.name for c in self._client.get_collections().collections]
        except Exception:
            return False
        return self._collection in cols

    def list_all(self) -> List[tuple]:
        res = self._client.scroll(self._collection, with_payload=True, limit=10000)
        return [(p.id, p.payload, 0.0) for p in res[0]]


class NumpyStore(VectorStore):
    """内存兜底：纯 numpy，任何 Python 均可运行。"""

    def __init__(self) -> None:
        self._dim = 0
        self._items: List[tuple] = []  # (id, vector, payload)

    def ensure(self, dim: int) -> None:
        if self._dim != dim:
            self._dim = dim
            self._items = []

    def upsert(self, ids: List[str], vectors: List[list], payloads: List[dict]) -> None:
        for i in range(len(ids)):
            self._items.append((ids[i], vectors[i], payloads[i]))

    @staticmethod
    def _matches(payload: dict, scope: Scope) -> bool:
        for k, v in scope.filter_must():
            if str(payload.get(k, "")) != str(v):
                return False
        return True

    def search(self, vector: list, top_k: int, scope: Scope) -> List[tuple]:
        import numpy as np
        vq = np.asarray(vector, dtype=np.float32)
        scored = []
        for cid, vec, pay in self._items:
            if not self._matches(pay, scope):
                continue
            sim = float(vq @ np.asarray(vec, dtype=np.float32))  # 双方已 L2 归一化
            scored.append((cid, pay, sim))
        scored.sort(key=lambda x: x[2], reverse=True)
        return scored[:top_k]

    def count(self, scope: Optional[Scope] = None) -> int:
        if scope is None:
            return len(self._items)
        return sum(1 for _, _, p in self._items if self._matches(p, scope))

    def list_all(self) -> List[tuple]:
        return [(cid, pay, 0.0) for cid, pay, _ in self._items]



class FlatStore(VectorStore):
    """mmap 版向量库：vectors.npy + meta.jsonl。

    用途：云主机 / FUSE 盘 / 小内存机器上，qdrant 嵌入式模式初始化一个 1.2G
    sqlite 库要 150s 以上且内存翻倍，FlatStore 加载在 1s 内、内存按需映射。
    """

    def __init__(self, path: str) -> None:
        import numpy as np
        self._dir = path
        self._dim = 0
        self._meta: List[dict] = []
        self._V = None
        self._codes = None
        self._keys: dict = {}
        self._extra: List[tuple] = []
        self._pending: List[tuple] = []
        self._load()

    # ---------- 持久化 ----------
    def _paths(self):
        import os
        return (
            os.path.join(self._dir, 'vectors.npy'),
            os.path.join(self._dir, 'meta.jsonl'),
            os.path.join(self._dir, 'extra.npy'),
            os.path.join(self._dir, 'extra.jsonl'),
        )

    def _load(self) -> None:
        import json, os
        import numpy as np
        vp, mp, ep, emp = self._paths()
        if os.path.exists(vp) and os.path.exists(mp):
            self._V = np.load(vp, mmap_mode='r')
            self._dim = int(self._V.shape[1])
            codes = []
            with open(mp, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    m = json.loads(line)
                    self._meta.append(m)
                    codes.append(self._key_code(m))
            self._codes = np.asarray(codes, dtype=np.int32)
        if os.path.exists(ep) and os.path.exists(emp):
            ex = np.load(ep)
            with open(emp, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    m = json.loads(line)
                    self._extra.append((m, np.asarray(ex[len(self._extra)], dtype=np.float32)))
            if self._extra and not self._dim:
                self._dim = int(ex.shape[1]) if ex.ndim == 2 else 0

    def _key_code(self, m: dict) -> int:
        k = (m.get('tier') or '', m.get('project_id') or '', m.get('chapter_id') or '')
        if k not in self._keys:
            self._keys[k] = len(self._keys)
        return self._keys[k]

    def flush(self) -> None:
        """把 pending 增量合并进 vectors.npy（原子替换）。"""
        if not self._pending:
            return
        import json, os
        import numpy as np
        vp, mp, ep, emp = self._paths()
        os.makedirs(self._dir, exist_ok=True)
        base = np.asarray(self._V) if self._V is not None else np.zeros((0, self._dim), np.float32)
        newv = np.vstack([base, np.stack([v for _, v, _ in self._pending])]).astype(np.float32)
        tmpv = vp + '.tmp'
        np.save(tmpv, newv)
        os.replace(tmpv + '.npy' if not tmpv.endswith('.npy') else tmpv, vp)
        with open(mp, 'a', encoding='utf-8') as f:
            for pid, _, pay in self._pending:
                m = dict(pay)
                m['id'] = pid
                f.write(json.dumps(m, ensure_ascii=False) + '\n')
        for pid, _, pay in self._pending:
            m = dict(pay)
            m['id'] = pid
            self._meta.append(m)
            self._pending_code = self._key_code(m)
        self._codes = np.asarray(
            [self._key_code(m) for m in self._meta], dtype=np.int32
        ) if self._meta else np.zeros((0,), np.int32)
        self._V = np.load(vp, mmap_mode='r')
        self._pending = []

    # ---------- VectorStore 接口 ----------
    def ensure(self, dim: int) -> None:
        import os
        import numpy as np
        os.makedirs(self._dir, exist_ok=True)
        if dim != self._dim:
            # embedding 维度变了：旧库不可用，重建空库
            self._dim = dim
            self._V = np.zeros((0, dim), np.float32)
            self._meta = []
            self._codes = np.zeros((0,), np.int32)
            self._keys = {}
            self._extra = []
            self._pending = []
            vp, mp, _, _ = self._paths()
            np.save(vp, self._V)
            open(mp, 'w', encoding='utf-8').close()

    def upsert(self, ids: List[str], vectors: List[list], payloads: List[dict]) -> None:
        import numpy as np
        for i in range(len(ids)):
            v = np.asarray(vectors[i], dtype=np.float32)
            self._extra.append((dict(payloads[i], id=ids[i]), v))
            self._pending.append((ids[i], v, dict(payloads[i], id=ids[i])))
        if len(self._pending) >= 200:
            self.flush()

    def _scope_mask(self, scope: Scope) -> 'np.ndarray':
        import numpy as np
        code = self._keys.get((scope.tier, scope.project_id or '', scope.chapter_id or ''))
        if code is None or self._codes is None:
            return np.zeros((0,), dtype=np.int64)
        return np.nonzero(self._codes == code)[0]

    def _exists(self) -> bool:
        return (self._V is not None and len(self._meta) > 0) or bool(self._extra)

    def search(self, vector: list, top_k: int, scope: Scope) -> List[tuple]:
        import numpy as np
        if not self._exists():
            return []
        q = np.asarray(vector, dtype=np.float32)
        nrm = float(np.linalg.norm(q))
        if nrm > 0:
            q = q / nrm
        scored: List[tuple] = []
        idx = self._scope_mask(scope)
        if idx.size:
            scores = np.asarray(self._V[idx]) @ q
            order = np.argsort(-scores)[: max(top_k * 4, top_k)]
            for j in order:
                m = self._meta[int(idx[j])]
                scored.append((m['id'], self._payload(m), float(scores[j])))
        for m, v in self._extra:
            k = (m.get('tier') or '', m.get('project_id') or '', m.get('chapter_id') or '')
            if k != (scope.tier, scope.project_id or '', scope.chapter_id or ''):
                continue
            vv = v / (np.linalg.norm(v) or 1.0)
            scored.append((m['id'], self._payload(m), float(q @ vv)))
        scored.sort(key=lambda x: x[2], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _payload(m: dict) -> dict:
        return {
            'tier': m.get('tier'), 'project_id': m.get('project_id'),
            'chapter_id': m.get('chapter_id'), 'doc_id': m.get('doc_id'),
            'source': m.get('source'), 'title': m.get('title'),
            'text': m.get('text') or '',
        }

    def count(self, scope: Optional[Scope] = None) -> int:
        if scope is None:
            return len(self._meta) + len(self._extra)
        n = int(self._scope_mask(scope).size)
        k = (scope.tier, scope.project_id or '', scope.chapter_id or '')
        n += sum(1 for m, _ in self._extra
                 if (m.get('tier') or '', m.get('project_id') or '', m.get('chapter_id') or '') == k)
        return n

    def list_all(self) -> List[tuple]:
        return [(m['id'], self._payload(m), 0.0) for m in self._meta]


def build_store(backend: str, path: str, collection: str) -> VectorStore:
    if backend in ("flat", "npy", "file"):
        try:
            return FlatStore(path)
        except Exception:
            return NumpyStore()
    if backend == "qdrant":
        try:
            return QdrantStore(path, collection)
        except Exception:
            return NumpyStore()
    return NumpyStore()
