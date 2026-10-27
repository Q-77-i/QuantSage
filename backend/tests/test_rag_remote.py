"""远程 RAG 客户端用例（S3）：协议往返、分块、错误映射。

全部用 `httpx.MockTransport`，不连服务、不载模型。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.rag import RagNotReady, RagOverloaded
from app.rag.remote import EMBED_CHUNK, RemoteEmbedder, RemoteReranker


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://rag.test")


def test_encode_documents_chunks_and_parses_sparse() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        count = len(body["texts"])
        return httpx.Response(
            200,
            json={
                "dense": [[float(index)] * 2 for index in range(count)],
                "sparse": [{"1": 0.5, "2": 0.25} for _ in range(count)],
                "dim": 1024,
            },
        )

    embedder = RemoteEmbedder("http://rag.test", "tok", client=_client(handler))
    texts = [f"t{index}" for index in range(EMBED_CHUNK + 6)]

    encoded = embedder.encode_documents(texts)

    assert len(requests) == 2
    assert [len(body["texts"]) for body in requests] == [EMBED_CHUNK, 6]
    assert all(body["request_class"] == "batch" for body in requests)
    assert len(encoded.dense) == len(texts)
    assert len(encoded.sparse) == len(texts)
    assert encoded.sparse[0] == {1: 0.5, 2: 0.25}  # 键回到 int


def test_encode_query_returns_first_vector_and_sparse() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"dense": [[0.1, 0.2]], "sparse": [{"7": 0.9}], "dim": 1024},
        )

    embedder = RemoteEmbedder("http://rag.test", "tok", client=_client(handler))

    dense, sparse = embedder.encode_query("降准")

    assert dense == [0.1, 0.2]
    assert sparse == {7: 0.9}
    assert bodies[0]["request_class"] == "interactive"


def test_rerank_passes_request_class() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"scores": [0.9, 0.1]})

    reranker = RemoteReranker("http://rag.test", "tok", client=_client(handler))

    scores = reranker.rerank("q", ["a", "b"], request_class="research")

    assert scores == [0.9, 0.1]
    assert bodies[0]["request_class"] == "research"
    assert bodies[0]["documents"] == ["a", "b"]


def test_503_maps_to_overloaded_with_reason() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"reason": "queue_full"})

    reranker = RemoteReranker("http://rag.test", "tok", client=_client(handler))

    with pytest.raises(RagOverloaded) as excinfo:
        reranker.rerank("q", ["a"])
    assert excinfo.value.reason == "queue_full"


def test_connection_error_maps_to_not_ready() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    reranker = RemoteReranker("http://rag.test", "tok", client=_client(handler))

    with pytest.raises(RagNotReady) as excinfo:
        reranker.rerank("q", ["a"])
    assert not isinstance(excinfo.value, RagOverloaded)


def test_client_timeout_maps_to_overloaded_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    reranker = RemoteReranker("http://rag.test", "tok", client=_client(handler))

    with pytest.raises(RagOverloaded) as excinfo:
        reranker.rerank("q", ["a"])
    assert excinfo.value.reason == "timeout"


def test_empty_documents_short_circuits() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"scores": []})

    reranker = RemoteReranker("http://rag.test", "tok", client=_client(handler))

    assert reranker.rerank("q", []) == []
    assert calls["n"] == 0
