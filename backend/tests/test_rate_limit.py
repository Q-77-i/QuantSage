"""E4b / L0：限流的 HTTP 面。

三条判据：
  * **三桶各自独立**（chat / research / task-submit），额度用完回 429；
  * **拒绝发生在开流之前**——chat 与 research 是 SSE，首字节一旦发出就改不了状态码，
    所以 429 必须是 JSON 响应而不是一帧 error；
  * 桶**按用户分**：一个人的额度不该花掉另一个人的。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import reliability
from app.core.auth import require_user
from app.main import app
from tests.conftest import TEST_USER, FakeDatabase

client = TestClient(app)

BUCKETS = ("llm-chat", "llm-research", "task-submit")
CAPACITY = 2


@pytest.fixture(autouse=True)
def small_buckets():
    """把三桶都换成容量 2 的（默认 10/3/20 打起来太慢，且测的是机制不是额度）。"""
    reliability.reset_limiters()
    for name in BUCKETS:
        reliability.install_limiter(
            reliability.Limiter(name=name, bucket=reliability.Bucket(capacity=CAPACITY, window=60.0))
        )
    yield
    reliability.reset_limiters()


def submit_backtest() -> Any:
    return client.post(
        "/api/v1/backtest",
        json={"symbol": "600519", "strategy": "ma_cross", "start": "2026-07-01", "end": "2026-09-30"},
    )


def test_chat_bucket_runs_out(signed_in: FakeDatabase) -> None:
    for _ in range(CAPACITY):
        client.post("/api/v1/chat", json={"message": "你好"})

    response = client.post("/api/v1/chat", json={"message": "你好"})

    assert response.status_code == 429
    # **未开流**：SSE 一旦发出首字节就改不了状态码，所以这里必须是 JSON 响应
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["kind"] == "rate_limited"


def test_rejection_carries_retry_after(signed_in: FakeDatabase) -> None:
    for _ in range(CAPACITY):
        client.post("/api/v1/research/runs", json={"question": "分析一下 600519"})

    response = client.post("/api/v1/research/runs", json={"question": "分析一下 600519"})

    assert response.status_code == 429
    body = response.json()
    assert isinstance(body["retry_after"], int) and body["retry_after"] >= 1
    assert response.headers["retry-after"] == str(body["retry_after"])
    assert "秒后再试" in body["detail"]  # 前端照抄这句


def test_task_submit_bucket_is_shared_by_backtest_and_optimize(signed_in: FakeDatabase) -> None:
    """回测与网格/批量**共用一个桶**——它们都是「排一件活给 worker」这件事。"""
    for _ in range(CAPACITY):
        submit_backtest()

    assert submit_backtest().status_code == 429
    grid = client.post(
        "/api/v1/optimize/grid",
        json={"symbol": "600519", "strategy": "ma_cross", "axes": {"fast": [5, 8]}},
    )
    assert grid.status_code == 429


def test_buckets_do_not_consume_each_other(signed_in: FakeDatabase) -> None:
    for _ in range(CAPACITY):
        client.post("/api/v1/chat", json={"message": "你好"})
    assert client.post("/api/v1/chat", json={"message": "你好"}).status_code == 429

    # chat 打满了，提交任务不该受影响
    assert submit_backtest().status_code != 429


def test_buckets_are_per_user(signed_in: FakeDatabase) -> None:
    for _ in range(CAPACITY):
        client.post("/api/v1/chat", json={"message": "你好"})
    assert client.post("/api/v1/chat", json={"message": "你好"}).status_code == 429

    app.dependency_overrides[require_user] = lambda: {**TEST_USER, "id": 2}
    assert client.post("/api/v1/chat", json={"message": "你好"}).status_code != 429


def test_login_still_comes_first(jwt_secret: Any) -> None:
    """未登录是 401 而不是 429——额度是按用户算的，没有用户就无从谈起。"""
    assert client.post("/api/v1/chat", json={"message": "你好"}).status_code == 401


def test_unlimited_endpoints_stay_unlimited(signed_in: FakeDatabase) -> None:
    """读端点不限流：本地 DuckDB 便宜，限了只是自找麻烦。"""
    for _ in range(CAPACITY + 3):
        assert client.get("/api/v1/tasks").status_code == 200
