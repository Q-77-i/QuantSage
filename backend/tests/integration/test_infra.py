"""集成测试：需要本地容器在跑（docker compose up -d --wait）。

默认不收集；用 `uv run pytest -m integration` 触发。
"""

from __future__ import annotations

import asyncio
from typing import TypedDict
from urllib.parse import urlparse

import httpx
import psycopg
import pytest
from langgraph.graph import END, START, StateGraph

from app.core.checkpoint import open_checkpointer
from app.core.config import get_settings

pytestmark = pytest.mark.integration


async def test_postgres_reachable_and_langfuse_db_exists() -> None:
    settings = get_settings()
    conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
    async with conn:
        cursor = await conn.execute("select current_database()")
        row = await cursor.fetchone()
        assert row is not None and row[0] == settings.postgres_db

        cursor = await conn.execute("select 1 from pg_database where datname = 'langfuse'")
        assert await cursor.fetchone() is not None, "langfuse 库未建（init 脚本未执行？）"


async def test_redis_reachable() -> None:
    parsed = urlparse(get_settings().redis_url)
    reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port or 6379)
    try:
        writer.write(b"*1\r\n$4\r\nPING\r\n")
        await writer.drain()
        reply = await asyncio.wait_for(reader.readline(), timeout=5)
    finally:
        writer.close()
    assert reply.decode().strip() == "+PONG"


async def test_qdrant_ready() -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"{get_settings().qdrant_url}/readyz")
    assert response.status_code == 200
    assert "ready" in response.text


class _Counter(TypedDict):
    n: int


def _bump(state: _Counter) -> _Counter:
    return {"n": state["n"] + 1}


async def test_checkpointer_round_trip() -> None:
    """真实写入 Postgres 并读回 —— 证明 checkpointer 接线正确。"""
    builder = StateGraph(_Counter)
    builder.add_node("bump", _bump)
    builder.add_edge(START, "bump")
    builder.add_edge("bump", END)

    async with open_checkpointer(get_settings().postgres_dsn) as saver:
        await saver.setup()
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "t1-integration-checkpointer"}}

        result = await graph.ainvoke({"n": 0}, config)
        assert result["n"] == 1

        snapshot = await graph.aget_state(config)
        assert snapshot.values["n"] == 1
