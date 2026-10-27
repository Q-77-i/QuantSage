"""模型入口的**唯一选择点**（S3）：远程 RAG 服务，或进程内回滚。

- `RAG_SERVICE_URL` 非空 → `remote.RemoteEmbedder` / `RemoteReranker`：API/MCP 进程
  **不 import `encoder`**（惰性 import 只在本地分支发生），模型只活在 RAG 服务进程；
- 为空 → 进程内 `encoder.get_embedder/get_reranker`，与 S3 之前的行为一致（回滚路径）。

`lru_cache` 是进程级单例；测试改配置后要 `get_embedder.cache_clear()`，
与 `get_settings.cache_clear()` 同一条纪律。
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.rag.protocols import Embedder, Reranker


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    settings = get_settings()
    if settings.rag_service_url:
        from app.rag.remote import RemoteEmbedder

        return RemoteEmbedder(
            settings.rag_service_url,
            settings.rag_service_token.get_secret_value(),
        )
    from app.rag.encoder import get_embedder as local

    return local()


@lru_cache(maxsize=1)
def get_reranker() -> Reranker:
    settings = get_settings()
    if settings.rag_service_url:
        from app.rag.remote import RemoteReranker

        return RemoteReranker(
            settings.rag_service_url,
            settings.rag_service_token.get_secret_value(),
        )
    from app.rag.encoder import get_reranker as local

    return local()


def reset() -> None:
    """清掉单例（测试与「改配置后重建」用）。"""
    get_embedder.cache_clear()
    get_reranker.cache_clear()


__all__ = ["get_embedder", "get_reranker", "reset"]
