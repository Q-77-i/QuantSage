"""集成用例：幂等键与限流走**真库、真 lifespan**（P3-E4b）。

离线用例用内存替身，验的是**逻辑**；幂等那条的真相在 SQL 里——`ON CONFLICT` 的推断子句
必须连 `WHERE idempotency_key IS NOT NULL` 一起写才对得上那个**部分**唯一索引，
写错了离线全绿、线上每一次带键的提交都 500。这正是「替身会骗人」的经典形状（M4c-1）。

运行方式（README）：`uv run pytest -m integration`（需容器与真实密钥）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.integration.conftest import purge_rows, sign_up

pytestmark = pytest.mark.integration

SYMBOL = "600519"
HEADER = "Idempotency-Key"


def email(prefix: str) -> str:
    return f"e4b-{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def submit(member: TestClient, key: str | None = None, **overrides: Any) -> Any:
    body: dict[str, Any] = {"strategy": "ma_cross", "symbol": SYMBOL, **overrides}
    headers = {HEADER: key} if key else {}
    return member.post("/api/v1/backtest", json=body, headers=headers)


def task_count(emails: list[str]) -> int:
    """直接数这些账号名下的任务行——判据落在真表上，不看端点的自述。

    走同步 psycopg 直连（与 `purge_rows` 同法）：应用侧的异步池绑在 TestClient 的
    事件循环上，测试线程碰不得。
    """
    import psycopg
    from app.core.config import get_settings

    with psycopg.connect(get_settings().postgres_dsn) as conn:
        row = conn.execute(
            "SELECT count(*) FROM tasks t JOIN users u ON u.id = t.user_id "
            "WHERE u.email = ANY(%s)",
            (emails,),
        ).fetchone()
    return int(row[0])


def test_same_key_creates_one_row_on_the_real_table(real_stack: TestClient) -> None:
    address = email("idem")
    member = sign_up(address)
    key = f"e4b-{uuid.uuid4().hex}"

    try:
        first = submit(member, key)
        second = submit(member, key)

        assert first.status_code == second.status_code == 202, (first.text, second.text)
        assert first.json()["task_id"] == second.json()["task_id"]
        # 真表上只有一行（`ON CONFLICT` 那条子句与部分唯一索引真对得上）
        assert task_count([address]) == 1
    finally:
        purge_rows([address], [])


def test_same_key_different_body_is_409_on_the_real_stack(real_stack: TestClient) -> None:
    address = email("conflict")
    member = sign_up(address)
    key = f"e4b-{uuid.uuid4().hex}"

    try:
        submit(member, key)
        response = submit(member, key, symbol="300750")

        assert response.status_code == 409
        assert HEADER in response.json()["detail"]
    finally:
        purge_rows([address], [])


def test_without_key_two_rows(real_stack: TestClient) -> None:
    address = email("nokey")
    member = sign_up(address)

    try:
        submit(member)
        submit(member)

        assert task_count([address]) == 2
    finally:
        purge_rows([address], [])


def test_rate_limit_rejects_before_the_stream_opens(real_stack: TestClient) -> None:
    """chat 是 SSE：额度用尽必须回 **JSON 的 429**，而不是一帧 error。

    额度按 settings 配，这里临时压到 1——验的是「拒绝发生在开流之前」这条机制。
    """
    from app.core import reliability
    from app.core.config import get_settings

    address = email("limit")
    member = sign_up(address)
    settings = get_settings()
    monkey_retry, monkey_window = settings.rate_limit_chat, settings.rate_limit_chat_window
    settings.rate_limit_chat, settings.rate_limit_chat_window = 1, 60.0
    reliability.reset_limiters()
    try:
        member.post("/api/v1/chat", json={"message": "你好"})
        response = member.post("/api/v1/chat", json={"message": "你好"})
    finally:
        settings.rate_limit_chat, settings.rate_limit_chat_window = monkey_retry, monkey_window
        reliability.reset_limiters()
        purge_rows([address], [])

    assert response.status_code == 429
    assert response.headers["content-type"].startswith("application/json")
    assert int(response.headers["retry-after"]) >= 1
