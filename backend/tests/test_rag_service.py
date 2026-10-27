"""RAG 服务用例（S3）：队列/超时/鉴权/健康/指标。

**全部注入假模型**，不载真权重、不连 Qdrant；真机验收另走手动轨（P4-S3.md §5.2）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.rag import RagOverloaded
from app.rag.service import Runtime, create_app
from tests.fakes_rag import DIM, FakeEmbedder


class SlowReranker:
    """可计并发峰值的假精排：服务必须让它同时只跑一个（MPS 串行）。"""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.active = 0
        self.peak = 0

    def rerank(
        self, query: str, docs: Sequence[str], *, request_class: str = "interactive"
    ) -> list[float]:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            return [float(index) for index, _ in enumerate(docs)]
        finally:
            self.active -= 1


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """确定的服务配置：token 空（回环放行）、队列/超时用测试档。"""
    for key, value in {
        "RAG_SERVICE_TOKEN": "",
        "RAG_PRELOAD": "false",
        "RAG_EMBED_INFLIGHT": "2",
        "RAG_RERANK_INFLIGHT": "1",
        "RAG_INTERACTIVE_MAX_QUEUE": "8",
        "RAG_INTERACTIVE_WAIT_SECONDS": "1.5",
        "RAG_RESEARCH_MAX_QUEUE": "32",
        "RAG_RESEARCH_WAIT_SECONDS": "10",
        "RAG_BATCH_MAX_QUEUE": "8",
        "RAG_BATCH_WAIT_SECONDS": "600",
        "RAG_EMBED_WAIT_SECONDS": "5",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── HTTP 契约 ─────────────────────────────────────────────


def test_embed_returns_dense_and_sparse(env: None) -> None:
    app = create_app(embedder=FakeEmbedder(DIM), reranker=SlowReranker(0))
    with TestClient(app) as client:
        response = client.post("/v1/embed", json={"texts": ["a", "b"]})

    assert response.status_code == 200
    body = response.json()
    assert len(body["dense"]) == 2
    assert len(body["sparse"]) == 2
    assert body["dim"] == DIM


def test_rerank_returns_scores(env: None) -> None:
    app = create_app(embedder=FakeEmbedder(DIM), reranker=SlowReranker(0))
    with TestClient(app) as client:
        response = client.post(
            "/v1/rerank", json={"query": "降准", "documents": ["a", "b", "c"]}
        )

    assert response.status_code == 200
    assert response.json()["scores"] == [0.0, 1.0, 2.0]


def test_health_reports_models_and_queue(env: None) -> None:
    app = create_app(embedder=FakeEmbedder(DIM), reranker=SlowReranker(0))
    with TestClient(app) as client:
        body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["models"] == {"embedder": True, "reranker": True}
    assert "queue" in body
    assert body["queue"]["limits"]["interactive"]["max_queue"] == 8


def test_token_is_required_when_configured(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_SERVICE_TOKEN", "secret")
    get_settings.cache_clear()
    app = create_app(embedder=FakeEmbedder(DIM), reranker=SlowReranker(0))
    with TestClient(app) as client:
        assert client.post("/v1/embed", json={"texts": ["a"]}).status_code == 401
        assert (
            client.post(
                "/v1/embed",
                json={"texts": ["a"]},
                headers={"Authorization": "Bearer wrong"},
            ).status_code
            == 401
        )
        ok = client.post(
            "/v1/embed",
            json={"texts": ["a"]},
            headers={"Authorization": "Bearer secret"},
        )
    assert ok.status_code == 200


def test_metrics_exposes_rag_names(env: None) -> None:
    app = create_app(embedder=FakeEmbedder(DIM), reranker=SlowReranker(0))
    with TestClient(app) as client:
        client.post("/v1/rerank", json={"query": "q", "documents": ["a"]})
        text = client.get("/metrics").text

    assert "rag_rerank_wait_seconds" in text
    assert "rag_rerank_inflight" in text
    assert "rag_rerank_queue_depth" in text


# ── 排队语义（直接打 Runtime，不隔 HTTP）───────────────


@pytest.mark.asyncio
async def test_rerank_is_serialized(env: None) -> None:
    reranker = SlowReranker(0.05)
    runtime = Runtime(embedder=FakeEmbedder(DIM), reranker=reranker)
    runtime.start()
    try:
        await asyncio.gather(
            *(
                runtime.run(
                    "rerank", "interactive", lambda: reranker.rerank("q", ["a"])
                )
                for _ in range(3)
            )
        )
    finally:
        runtime.stop()

    assert reranker.peak == 1  # MPS 并发会 abort，队列必须把它压成 1


@pytest.mark.asyncio
async def test_queue_full_rejects_immediately(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_INTERACTIVE_MAX_QUEUE", "0")
    get_settings.cache_clear()
    runtime = Runtime(embedder=FakeEmbedder(DIM), reranker=SlowReranker(0))
    runtime.start()
    try:
        with pytest.raises(RagOverloaded) as excinfo:
            await runtime.admit("rerank", "interactive")
    finally:
        runtime.stop()

    assert excinfo.value.reason == "queue_full"


@pytest.mark.asyncio
async def test_wait_timeout_releases_the_waiter(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_INTERACTIVE_WAIT_SECONDS", "0.05")
    get_settings.cache_clear()
    runtime = Runtime(embedder=FakeEmbedder(DIM), reranker=SlowReranker(0))
    runtime.start()
    try:
        await runtime.admit("rerank", "interactive")  # 占住唯一的精排槽位
        try:
            with pytest.raises(RagOverloaded) as excinfo:
                await runtime.admit("rerank", "interactive")
            assert excinfo.value.reason == "timeout"
        finally:
            runtime.release("rerank")
        # 超时者不能把队列计数留在 1：下一次请求得能再进来
        await runtime.admit("rerank", "interactive")
        runtime.release("rerank")
        assert runtime.queue()["waiting"]["rerank"] == 0
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_embed_uses_its_own_wait(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """嵌入不能用精排的短超时：查询嵌入是硬依赖，降级不了。"""
    monkeypatch.setenv("RAG_INTERACTIVE_WAIT_SECONDS", "0.01")
    monkeypatch.setenv("RAG_EMBED_WAIT_SECONDS", "0.2")
    get_settings.cache_clear()
    runtime = Runtime(embedder=FakeEmbedder(DIM), reranker=SlowReranker(0))
    runtime.start()
    try:
        await runtime.admit("embed", "interactive")
        await runtime.admit("embed", "interactive")  # 占满 2 个嵌入槽位
        started = time.perf_counter()
        try:
            with pytest.raises(RagOverloaded) as excinfo:
                await runtime.admit("embed", "interactive")
        finally:
            runtime.release("embed")
            runtime.release("embed")
        elapsed = time.perf_counter() - started
    finally:
        runtime.stop()

    assert excinfo.value.reason == "timeout"
    assert elapsed >= 0.15  # 用的是 embed 的 0.2s，不是精排的 0.01s


@pytest.mark.asyncio
async def test_embed_and_rerank_queues_are_independent(env: None) -> None:
    """精排堵死不该让嵌入也排队（两条通道各自的信号量）。"""
    runtime = Runtime(embedder=FakeEmbedder(DIM), reranker=SlowReranker(0))
    runtime.start()
    try:
        await runtime.admit("rerank", "interactive")
        try:
            encoded: Any = await asyncio.wait_for(
                runtime.run("embed", "interactive", lambda: FakeEmbedder(DIM).encode_query("q")),
                timeout=1,
            )
            assert encoded
        finally:
            runtime.release("rerank")
    finally:
        runtime.stop()
