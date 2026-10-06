"""M1 集成测试：注册登录流 + A/B 隔离矩阵，打真实 Postgres 与真实 HTTP 栈。

离线那份（`tests/test_chat_ownership.py`）用的是内存业务库，证明不了 SQL 里的
`user_id` 过滤真的生效——**过滤写在 SQL 里，所以必须再打一次真库**（SPEC §12 双跑口径）。

会话用**假模型**驱动：本用例要验的是归属，不是模型能力，因此零 API 费用。
数据自清：用例建的账号在 finally 里连级删除，不污染 dev 库。

前置条件：`docker compose up -d --wait`（postgres 必需）。
默认不收集；用 `uv run pytest -m integration` 触发。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.agent.graph import build_agent
from app.core.config import get_settings
from app.main import app
from tests.fakes import ToolCallingFakeModel
from tests.integration.conftest import purge_rows

pytestmark = pytest.mark.integration

PASSWORD = "integration-pass"


@pytest.fixture
def jwt_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("JWT_SECRET", "integration-secret-not-a-real-key-0123456789")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def real_stack(jwt_env: None) -> Iterator[TestClient]:
    """起真实应用（跑 lifespan：连库、建表；小石 MCP 起不来也只是降级，不影响本用例）。"""
    with TestClient(app) as client:
        yield client


def _sign_up(email: str) -> TestClient:
    client = TestClient(app)
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return client




def test_register_login_and_ab_isolation(real_stack: TestClient) -> None:
    suffix = uuid.uuid4().hex[:8]
    alice_email, bob_email = f"m1-a-{suffix}@example.com", f"m1-b-{suffix}@example.com"
    thread_ids: list[str] = []

    # 假模型替掉真 Agent：验归属不烧 token
    real_agent = app.state.agent
    app.state.agent = build_agent(
        ToolCallingFakeModel(responses=[AIMessage("收到")]),
        [],
        checkpointer=app.state.checkpointer,
    )
    try:
        assert TestClient(app).get("/api/v1/chat/threads").status_code == 401

        alice, bob = _sign_up(alice_email), _sign_up(bob_email)

        # 邮箱唯一约束在真库上生效
        duplicate = TestClient(app).post(
            "/api/v1/auth/register", json={"email": alice_email, "password": PASSWORD}
        )
        assert duplicate.status_code == 409

        # 刷新（重新登录）后会话可恢复：cookie 是唯一凭据，重建客户端也能拿回历史
        assert alice.get("/api/v1/auth/me").json()["email"] == alice_email
        relogin = TestClient(app)
        assert (
            relogin.post(
                "/api/v1/auth/login",
                json={"email": alice_email, "password": PASSWORD},
            ).status_code
            == 200
        )

        posted = alice.post("/api/v1/chat", json={"message": "alice 的会话"})
        assert posted.status_code == 200, posted.text
        thread_ids.append(posted.headers["x-thread-id"])

        # 列表按人过滤
        assert [row["thread_id"] for row in alice.get("/api/v1/chat/threads").json()] == [
            thread_ids[0]
        ]
        assert bob.get("/api/v1/chat/threads").json() == []

        # 越权一律 404（读 / 删 / 续聊三条路），且与「不存在」同响应
        foreign = bob.get(f"/api/v1/chat/threads/{thread_ids[0]}/messages")
        missing = bob.get(f"/api/v1/chat/threads/{uuid.uuid4()}/messages")
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()
        assert bob.delete(f"/api/v1/chat/threads/{thread_ids[0]}").status_code == 404
        assert (
            bob.post(
                "/api/v1/chat", json={"message": "插一句", "thread_id": thread_ids[0]}
            ).status_code
            == 404
        )

        # 本人的历史可回看，续聊后仍在自己的列表里
        restored = relogin.get(f"/api/v1/chat/threads/{thread_ids[0]}/messages")
        assert restored.status_code == 200
        assert [row["role"] for row in restored.json()["messages"]] == ["user", "assistant"]

        resumed = alice.post(
            "/api/v1/chat", json={"message": "接着聊", "thread_id": thread_ids[0]}
        )
        assert resumed.status_code == 200
        assert [row["thread_id"] for row in alice.get("/api/v1/chat/threads").json()] == [
            thread_ids[0]
        ]

        # 删除后从自己的列表与历史里都消失
        assert alice.delete(f"/api/v1/chat/threads/{thread_ids[0]}").status_code == 200
        assert alice.get("/api/v1/chat/threads").json() == []
    finally:
        app.state.agent = real_agent
        purge_rows([alice_email, bob_email], thread_ids)
