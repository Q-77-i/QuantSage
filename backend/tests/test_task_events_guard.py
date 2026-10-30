"""P2 A9：任务事件 SSE 的并发连接闸门。

每用户最多 3 条订阅（默认与 `QUOTA_ACTIVE_TASKS=3` 对齐）。TestClient 会把整条流读完
才返回，**不能**用它挂住一条永不结束的 SSE 来验并发；这里把闸门拆成两层测：

* 连接计数本身：占满 → 拒绝 → 释放 → 可再占；
* HTTP 面：计数已满时，请求在开流前拿到 429（不是一帧 error）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import tasks as tasks_api
from app.api.limits import RateLimitedError
from app.api.tasks import (
    _acquire_task_stream_slot,
    _release_task_stream_slot,
    _task_stream_slot,
)
from app.core.config import get_settings
from app.main import app
from tests.conftest import TEST_USER
from tests.fakes import FakeDatabase

client = TestClient(app)
TASK_ID = "00000000-0000-0000-0000-0000000000b2"
USER_ID = int(TEST_USER["id"])


@pytest.fixture(autouse=True)
def clear_stream_slots() -> Any:
    tasks_api._ACTIVE_TASK_STREAMS.clear()
    yield
    tasks_api._ACTIVE_TASK_STREAMS.clear()


def _cap_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        get_settings(), "task_events_max_connections", 1, raising=False
    )


def test_slot_is_bounded_and_reusable(monkeypatch: pytest.MonkeyPatch) -> None:
    _cap_at_one(monkeypatch)
    user = {"id": USER_ID}

    slot_user = _task_stream_slot(user=user)
    with pytest.raises(RateLimitedError):
        _task_stream_slot(user=user)
    assert tasks_api._ACTIVE_TASK_STREAMS[USER_ID] == 1

    _release_task_stream_slot(slot_user)
    assert USER_ID not in tasks_api._ACTIVE_TASK_STREAMS
    _release_task_stream_slot(_task_stream_slot(user=user))


def test_second_subscription_gets_429_before_stream(
    signed_in: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cap_at_one(monkeypatch)
    asyncio.run(
        signed_in.create_task(
            TASK_ID, USER_ID, "grid", {"kind": "grid", "exec": {}}, total=2
        )
    )
    # 模拟第一条连接仍开着；路由必须在返回 StreamingResponse 之前拒绝。
    _acquire_task_stream_slot(USER_ID)

    response = client.get(f"/api/v1/tasks/{TASK_ID}/events")

    assert response.status_code == 429
    assert response.json()["kind"] == "rate_limited"
    assert "同时订阅" in response.json()["detail"]


def test_missing_task_does_not_consume_a_slot(
    signed_in: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cap_at_one(monkeypatch)
    missing = "00000000-0000-0000-0000-0000000000ff"

    assert client.get(f"/api/v1/tasks/{missing}/events").status_code == 404
    assert USER_ID not in tasks_api._ACTIVE_TASK_STREAMS
    assert client.get("/api/v1/tasks/not-a-uuid/events").status_code == 422
    assert USER_ID not in tasks_api._ACTIVE_TASK_STREAMS
