"""全局配置（pydantic-settings，可用环境变量 / .env 覆盖）。"""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 应用
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # 数据库
    db_path: str = "./data/agent.db"

    # 知识库向量存储
    kb_vector_backend: str = "qdrant"      # qdrant | numpy
    kb_vector_dir: str = "./data/vector_db"
    kb_collection: str = "proposal_kb"

    # 嵌入
    embedding_provider: str = "local"      # api | local
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_local_dim: int = 8192

    # 检索
    retrieval_top_k: int = 6
    # 三级作用域权重：轻度加成（相关性为主、具体优先为辅；仅在相关度相当时打破次序）
    scope_weight_chapter: float = 1.2
    scope_weight_project: float = 1.1
    scope_weight_global: float = 1.0
    # 相关性门槛：低于该真实相关度的召回视为无关，不参与融合
    retrieval_min_rel: float = 0.10

    # 写作流 LLM（M3）
    llm_provider: str = "local"        # api | local
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    workflow_max_iterations: int = 2    # 审查-修订循环上限
    proposal_output_dir: str = "./data/output"

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    def _resolve(self, p: str) -> str:
        path = Path(p)
        if not path.is_absolute():
            path = self.project_root / path
        return str(path)

    @property
    def resolved_db_path(self) -> str:
        return self._resolve(self.db_path)

    @property
    def resolved_vector_dir(self) -> str:
        return self._resolve(self.kb_vector_dir)

    @property
    def resolved_output_dir(self) -> str:
        return self._resolve(self.proposal_output_dir)

    def scope_weights(self) -> dict:
        return {
            "global": self.scope_weight_global,
            "project": self.scope_weight_project,
            "chapter": self.scope_weight_chapter,
        }


settings = Settings()
