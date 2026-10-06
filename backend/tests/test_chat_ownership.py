"""离线单测：会话归属校验（M1 第一优先级）。

真鉴权依赖 + 真路由，只把业务库换成内存版、checkpointer 换成 `InMemorySaver`。
验的是**规则**：不属于本人的一律 404（与「不存在」同响应），未登录一律 401。

真库那份（过滤写在 SQL 里，内存实现证明不了）在 `tests/integration/test_auth_m1.py` 双跑。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import astream_chat, build_agent
from app.main import app
from tests.fakes import FakeDatabase, ToolCallingFakeModel

pytestmark = pytest.mark.usefixtures("jwt_secret")

PASSWORD = "long-enough-pass"


def _sign_up(email: str) -> TestClient:
    """注册一个账号并拿到带 cookie 的客户端。"""
    client = TestClient(app)
    response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201
    return client


def _seed_thread(thread_id: str, text: str = "测试") -> InMemorySaver:
    """真跑一轮图（假模型），让 checkpointer 里有消息。"""
    saver = InMemorySaver()
    agent = build_agent(
        ToolCallingFakeModel(responses=[AIMessage(f"回:{text}")]), [], checkpointer=saver
    )

    async def drain() -> None:
        async for _ in astream_chat(agent, message=text, thread_id=thread_id):
            pass

    asyncio.run(drain())
    return saver


@pytest.fixture
def db() -> Any:
    database = FakeDatabase()
    app.state.db = database
    yield database
    app.state.db = None


@pytest.fixture
def saver() -> Any:
    checker = InMemorySaver()
    app.state.checkpointer = checker
    yield checker
    app.state.checkpointer = None


# ── 未登录一律 401 ──────────────────────────────────────────


def test_all_chat_endpoints_require_login(db: Any, saver: Any) -> None:
    client = TestClient(app)
    thread_id = str(uuid.uuid4())

    assert client.get("/api/v1/chat/threads").status_code == 401
    assert client.get(f"/api/v1/chat/threads/{thread_id}/messages").status_code == 401
    assert client.delete(f"/api/v1/chat/threads/{thread_id}").status_code == 401
    assert client.post("/api/v1/chat", json={"message": "你好"}).status_code == 401


# ── 越权一律 404（与「不存在」同响应）──────────────────────


def test_other_users_thread_is_invisible_and_untouchable(db: Any, saver: Any) -> None:
    alice = _sign_up("alice@example.com")
    bob = _sign_up("bob@example.com")
    thread_id = str(uuid.uuid4())
    app.state.checkpointer = _seed_thread(thread_id)
    asyncio.run(db.claim_thread(thread_id, 1))  # 归 alice

    # 读：404，且文案与「真不存在」完全一致（不泄露存在性）
    foreign = bob.get(f"/api/v1/chat/threads/{thread_id}/messages")
    missing = bob.get(f"/api/v1/chat/threads/{uuid.uuid4()}/messages")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()

    # 删：404，且 alice 的会话仍在
    assert bob.delete(f"/api/v1/chat/threads/{thread_id}").status_code == 404
    assert alice.get(f"/api/v1/chat/threads/{thread_id}/messages").status_code == 200

    # 续聊：404（这条在碰到 agent 之前就该挡住）
    app.state.agent = None
    resumed = bob.post("/api/v1/chat", json={"message": "接着聊", "thread_id": thread_id})
    assert resumed.status_code == 404


def test_list_threads_only_returns_own(db: Any, saver: Any) -> None:
    alice = _sign_up("alice@example.com")
    bob = _sign_up("bob@example.com")
    mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
    asyncio.run(db.claim_thread(mine, 1))
    asyncio.run(db.claim_thread(theirs, 2))

    assert [row["thread_id"] for row in alice.get("/api/v1/chat/threads").json()] == [mine]
    assert [row["thread_id"] for row in bob.get("/api/v1/chat/threads").json()] == [theirs]


def test_legacy_thread_without_owner_is_invisible_to_everyone(db: Any, saver: Any) -> None:
    """P1 留下的无主会话：不迁移、不认领，任何账号都看不到也拿不到（已拍板口径）。"""
    alice = _sign_up("alice@example.com")
    orphan = str(uuid.uuid4())
    app.state.checkpointer = _seed_thread(orphan)

    assert alice.get("/api/v1/chat/threads").json() == []
    assert alice.get(f"/api/v1/chat/threads/{orphan}/messages").status_code == 404
    assert alice.delete(f"/api/v1/chat/threads/{orphan}").status_code == 404
    assert (
        alice.post(
            "/api/v1/chat", json={"message": "认领", "thread_id": orphan}
        ).status_code
        == 404
    )


# ── 新建 / 续聊 / 删除的归属流转 ────────────────────────────


def test_new_thread_is_claimed_by_its_author(db: Any, saver: Any) -> None:
    """归属行在首字节之前落库：流式结束后列表里就有它，且主人正确。"""
    alice = _sign_up("alice@example.com")
    app.state.agent = build_agent(
        ToolCallingFakeModel(responses=[AIMessage("你好")]), [], checkpointer=saver
    )
    try:
        response = alice.post("/api/v1/chat", json={"message": "第一句"})
        assert response.status_code == 200
        thread_id = response.headers["x-thread-id"]
        assert asyncio.run(db.thread_owner(thread_id)) == 1
        assert [row["thread_id"] for row in alice.get("/api/v1/chat/threads").json()] == [
            thread_id
        ]
    finally:
        app.state.agent = None


def test_delete_removes_thread_from_list(db: Any, saver: Any) -> None:
    alice = _sign_up("alice@example.com")
    thread_id = str(uuid.uuid4())
    asyncio.run(db.claim_thread(thread_id, 1))

    assert alice.delete(f"/api/v1/chat/threads/{thread_id}").json() == {
        "thread_id": thread_id,
        "deleted": True,
    }
    assert alice.get("/api/v1/chat/threads").json() == []
    assert alice.get(f"/api/v1/chat/threads/{thread_id}/messages").status_code == 404
