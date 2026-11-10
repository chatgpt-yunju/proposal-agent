"""嵌入：可插拔。api（OpenAI 兼容）质量最高；local（numpy 字符 bigram 哈希）离线兜底。"""
import hashlib
import math
import re
from typing import List

import numpy as np


class Embedder:
    dim: int = 0

    def embed(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError


class ApiEmbedder(Embedder):
    """OpenAI 兼容 /embeddings 客户端。dim 在首次调用后确定。"""

    def __init__(self, base_url: str, api_key: str, model: str):
        from openai import OpenAI
        self._client = OpenAI(base_url=base_url, api_key=api_key or "EMPTY")
        self._model = model
        self.dim = 0

    def embed(self, texts: List[str]) -> List[List[float]]:
        resp = self._client.embeddings.create(model=self._model, input=texts)
        data = sorted(resp.data, key=lambda x: x.index)
        vecs = [d.embedding for d in data]
        if not self.dim:
            self.dim = len(vecs[0])
        return vecs


class LocalEmbedder(Embedder):
    """numpy 分词向量，带 IDF 加权（L2 归一化）。无需下载模型，离线可跑。

    用 jieba 分词（与 BM25 同源），对高出现频次的通用词（如“公司”）经 IDF 降权、
    稀缺特征词（如“资质/认证”）提权，使离线稠密向量具备可用的语义对齐能力。
    """

    def __init__(self, dim: int = 8192):
        self.dim = dim
        self._n = 0
        self._df: dict = {}  # token -> 包含它的文档数

    @staticmethod
    def _tokens(text: str) -> List[str]:
        import jieba
        return [t for t in jieba.cut(text) if t.strip()]

    def add_docs(self, texts: List[str]) -> None:
        """登记一批文档的 token 出现频次，用于计算 IDF。"""
        for t in texts:
            for tok in set(self._tokens(t)):
                self._df[tok] = self._df.get(tok, 0) + 1
            self._n += 1

    def _idf(self, tok: str) -> float:
        df = self._df.get(tok, 0)
        return math.log((1.0 + self._n) / (1.0 + df)) + 1.0

    def embed(self, texts: List[str]) -> List[List[float]]:
        out = []
        for t in texts:
            v = np.zeros(self.dim, dtype=np.float32)
            for tok in self._tokens(t):
                h = int(hashlib.sha1(tok.encode("utf-8")).hexdigest(), 16)
                v[h % self.dim] += self._idf(tok)
            norm = float(np.linalg.norm(v))
            if norm > 0:
                v /= norm
            out.append(v.tolist())
        return out


def build_embedder(provider: str, base_url: str, api_key: str, model: str, local_dim: int) -> Embedder:
    if provider == "api":
        if not base_url:
            raise ValueError("embedding.provider=api 需要配置 EMBEDDING_BASE_URL")
        return ApiEmbedder(base_url, api_key, model)
    return LocalEmbedder(local_dim)
