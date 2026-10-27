"""远程 RAG 推理客户端（S3）：实现 `Embedder` / `Reranker` 协议，模型在独立进程。

**错误语义**（SPEC §4.3/§4.4）：

* 503 + `{"reason"}` → `RagOverloaded(reason)`：调用方在已算好的 RRF 顺序上降级；
* 连接失败 / 非 503 的 HTTP 错误 → `RagNotReady`：精排降级为 `unavailable`，嵌入如实报不可用；
* 客户端超时 → `RagOverloaded("timeout")`（服务端没在承诺的等待上限内回话）。

HTTP 客户端是**同步**的：调用方本来就在 `asyncio.to_thread` 的线程里跑检索
（`retrieve.search` 是同步函数），这里不再引入一层 async。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from app.core.config import get_settings
from app.rag import RagNotReady, RagOverloaded
from app.rag.protocols import DIM, EncodedDocs

#: 索引任务每请求的文本条数：太大请求体/内存，太小 HTTP 次数多。
EMBED_CHUNK = 64

#: 客户端在服务端排队上限之外再留的余量（服务端回 503 的正常路径不该靠它）
_CLIENT_MARGIN_SECONDS = 5.0


def _timeout_for(request_class: str) -> float:
    settings = get_settings()
    waits = {
        "interactive": settings.rag_interactive_wait_seconds,
        "research": settings.rag_research_wait_seconds,
        "batch": settings.rag_batch_wait_seconds,
    }
    return waits.get(request_class, settings.rag_interactive_wait_seconds) + _CLIENT_MARGIN_SECONDS


def _embed_timeout_for(request_class: str) -> float:
    """嵌入用服务端那把独立尺子（交互/研究 5s、批量 600s），不套用精排的短超时。"""
    settings = get_settings()
    wait = (
        settings.rag_batch_wait_seconds
        if request_class == "batch"
        else settings.rag_embed_wait_seconds
    )
    return wait + _CLIENT_MARGIN_SECONDS


class _RemoteBase:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=None
        )

    def _post(self, path: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        try:
            response = self._client.post(path, json=payload, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise RagOverloaded("timeout") from exc
        except httpx.HTTPError as exc:
            raise RagNotReady(f"RAG 服务不可用（{type(exc).__name__}）") from exc
        if response.status_code == 503:
            try:
                reason = str(response.json().get("reason") or "overloaded")
            except ValueError:
                reason = "overloaded"
            raise RagOverloaded(reason)
        if response.status_code >= 400:
            raise RagNotReady(
                f"RAG 服务返回 {response.status_code}：{response.text[:200]}"
            )
        return response.json()


class RemoteEmbedder(_RemoteBase):
    """远程编码器。索引任务按 `EMBED_CHUNK` 分块并标 `batch` 类。"""

    dim = DIM

    def encode_documents(self, texts: Sequence[str]) -> EncodedDocs:
        dense: list[list[float]] = []
        sparse: list[dict[int, float]] = []
        for start in range(0, len(texts), EMBED_CHUNK):
            chunk = list(texts[start : start + EMBED_CHUNK])
            payload = self._post(
                "/v1/embed",
                {"texts": chunk, "request_class": "batch"},
                timeout=_embed_timeout_for("batch"),
            )
            dense.extend(payload["dense"])
            sparse.extend(
                {int(key): float(value) for key, value in item.items()}
                for item in payload["sparse"]
            )
        return EncodedDocs(dense=dense, sparse=sparse)

    def encode_query(self, text: str) -> tuple[list[float], dict[int, float]]:
        payload = self._post(
            "/v1/embed",
            {"texts": [text], "request_class": "interactive"},
            timeout=_embed_timeout_for("interactive"),
        )
        dense = payload["dense"][0]
        sparse = {int(key): float(value) for key, value in payload["sparse"][0].items()}
        return [float(value) for value in dense], sparse


class RemoteReranker(_RemoteBase):
    """远程精排器；`request_class` 决定服务端的排队上限与等待超时。"""

    def rerank(
        self, query: str, docs: Sequence[str], *, request_class: str = "interactive"
    ) -> list[float]:
        if not docs:
            return []
        payload = self._post(
            "/v1/rerank",
            {
                "query": query,
                "documents": list(docs),
                "request_class": request_class,
            },
            timeout=_timeout_for(request_class),
        )
        return [float(score) for score in payload["scores"]]


__all__ = ["EMBED_CHUNK", "RemoteEmbedder", "RemoteReranker"]
