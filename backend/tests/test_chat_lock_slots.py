"""P2 A8：chat 的池外 advisory 锁连接上限。

锁连接要持有到整轮图结束，不能占业务池槽位；但也不能无上限地新建。这里测两个
公开行为：槽位满时快速拒绝、释放后可复用；路由在开流前把拒绝翻成 503。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import db_base
from app.core.db_base import ChatLockBusy
from app.main import app


def test_lock_slots_are_bounded_and_reusable(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        chat_lock_max_connections=1,
        chat_lock_acquire_timeout_seconds=0.01,
    )
    monkeypatch.setattr(db_base, "get_settings", lambda: settings)
    repo = db_base.BaseRepo(pool=None)  # type: ignore[arg-type]

    async def scenario() -> None:
        await repo.acquire_chat_lock_slot()
        with pytest.raises(ChatLockBusy):
            await repo.acquire_chat_lock_slot()
        repo.release_chat_lock_slot()
        await repo.acquire_chat_lock_slot()
        repo.release_chat_lock_slot()

    asyncio.run(scenario())


def test_chat_returns_503_before_stream_when_slots_are_full(
    signed_in: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def busy() -> None:
        raise ChatLockBusy("会话并发已满，请稍后再试")

    monkeypatch.setattr(signed_in, "acquire_chat_lock_slot", busy, raising=False)
    app.state.agent = object()
    try:
        response = TestClient(app).post("/api/v1/chat", json={"message": "你好"})
    finally:
        app.state.agent = None

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert "会话并发已满" in response.json()["detail"]
