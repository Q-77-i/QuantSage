"""离线单测：会话续期中间件（E8a，SPEC §8.3）。

**为什么续期是中间件而不是挂在 `require_user` 的注入 `Response` 上**：本仓 SSE 端点直出
`StreamingResponse`，FastAPI 在那条路径上不合并注入 Response 的头（`app/core/session.py`
的模块 docstring 记了代码位置）。挂在依赖上会**静默失效**——响应照常 200，只是没有新
cookie，谁也不会发现。本文件最后一条用例就是这条设计理由的行为证据：它跑**真实的**
`/api/v1/tasks/{id}/events`（一条 StreamingResponse 路由）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.auth import COOKIE_NAME, decode_session, issue_token
from app.main import app
from app.tasks import bus
from tests.fakes import FakeDatabase

pytestmark = pytest.mark.usefixtures("jwt_secret")

USER_ID = 1
TASK_ID = "8a1f0f2e-0000-4000-8000-000000000001"


@pytest.fixture
def db() -> Any:
    """内存业务库 + 一个真实存在的用户。

    刻意**不覆盖鉴权依赖**：撤销比对要真读到 `token_version`，覆盖掉就等于把被测的那条
    路径换成替身了。用户 id 从 1 起（与 `FakeDatabase` 的真库同源行为一致）。
    """
    database = FakeDatabase()
    asyncio.run(database.create_user("renewal@example.com", "not-a-real-hash"))
    app.state.db = database
    yield database
    app.state.db = None


@pytest.fixture
def client(db: Any) -> TestClient:
    return TestClient(app)


def aged_token(user_id: int = USER_ID, *, days: float = 4.0, version: int = 0) -> str:
    """一张**已过半程**的票：TTL 7 天、已用 4 天（> 3.5 天）。"""
    return issue_token(user_id, now=datetime.now(UTC) - timedelta(days=days), version=version)


def test_half_life_token_is_renewed(client: TestClient) -> None:
    """过半程即换新票——且**不挑端点**：`/health` 这种不需鉴权的响应照样续。"""
    old = aged_token()
    client.cookies.set(COOKIE_NAME, old)

    response = client.get("/health")

    assert response.status_code == 200
    fresh = response.cookies.get(COOKIE_NAME)
    assert fresh is not None and fresh != old
    session = decode_session(fresh)
    assert session is not None
    assert session.user_id == USER_ID
    assert session.version == 0  # 版本取自票自身：成功响应意味着它已经比对过
    # 新票是「刚签的」：剩余寿命接近整个 TTL（不是把旧票原样再发一遍）
    assert session.expires_at - datetime.now(UTC) > timedelta(days=6)


def test_fresh_token_is_not_renewed(client: TestClient) -> None:
    """**过半程才换**：刚签的票不换——否则每个响应都带 `Set-Cookie`。"""
    client.cookies.set(COOKIE_NAME, issue_token(USER_ID))

    response = client.get("/health")

    assert COOKIE_NAME not in response.cookies


def test_failed_request_does_not_renew(client: TestClient) -> None:
    """401 不续命：用户已删（或票已被撤销）时，不能顺手再发一张新票。"""
    client.cookies.set(COOKIE_NAME, aged_token(user_id=999))  # 库里没有这个人

    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert COOKIE_NAME not in response.cookies


def test_invalid_tokens_are_never_renewed(client: TestClient) -> None:
    """坏票与过期票都不续（`decode_session` 返回 None 是续期的唯一前提）。"""
    expired = issue_token(USER_ID, now=datetime.now(UTC) - timedelta(days=8))
    for token in ("not-a-jwt", expired):
        client.cookies.set(COOKIE_NAME, token)
        assert COOKIE_NAME not in client.get("/health").cookies


def test_renewal_also_lands_on_sse_routes(client: TestClient, db: FakeDatabase) -> None:
    """**本文件最重要的一条**：`StreamingResponse` 路由上也续期（设计约束的行为证据）。

    跑的是真路由 `/api/v1/tasks/{id}/events`（`sse_response` → `StreamingResponse`）。
    把续期挂在依赖注入的 `Response` 上时，这条会红在「没有新 cookie」上，而 200 与帧
    全都正常——那正是它安静得可怕的地方。
    """
    asyncio.run(_seed_finished_task(db))
    client.cookies.set(COOKIE_NAME, aged_token())

    response = client.get(f"/api/v1/tasks/{TASK_ID}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    fresh = response.cookies.get(COOKIE_NAME)
    assert fresh is not None
    session = decode_session(fresh)
    assert session is not None and session.user_id == USER_ID


async def _seed_finished_task(db: FakeDatabase) -> None:
    """一张**已终结**的网格任务：订阅它走「从库回放 + 收流」，流会自己结束（不会挂住）。"""
    request = {
        "kind": "grid",
        "symbol": "600519",
        "exec": {
            "cells": [],
            "options": {},
            "start": {"kind": "grid", "total": 1, "symbol": "600519"},
        },
    }
    await db.create_task(TASK_ID, USER_ID, "grid", request, total=1)
    db.tasks[TASK_ID]["status"] = "running"
    await db.save_optimization_run(
        "run-1",
        USER_ID,
        {"kind": "grid"},
        {
            "kind": "grid",
            "cells": [{"index": 0, "ok": True}],
            "cells_total": 1,
            "cells_ok": 1,
            "best_index": 0,
            "overfit": {"n_trials": 1},
            "duration_s": 0.1,
        },
    )
    await db.finish_task(TASK_ID, result={"run_id": "run-1"})
    bus.release(TASK_ID)  # worker 收尾时会做这件事
