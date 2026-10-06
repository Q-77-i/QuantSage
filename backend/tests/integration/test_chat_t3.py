"""T3 集成测试：真实模型 + 真实小石 MCP + 真实 Postgres + 真实 Langfuse。

前置条件：
  * `docker compose up -d`（postgres 必需）
  * `.env` 配好 `DEEPSEEK_API_KEY` 与小石入口
  * Langfuse trace 断言还需 `docker compose --profile observability up -d`

默认不收集；用 `uv run pytest -m integration` 触发。
真实调用会产生少量 API 费用与小石配额消耗。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.agent.graph import astream_chat, build_agent
from app.agent.tools import XIAOSHI_ALLOWLIST, load_xiaoshi_tools, query_market_bars
from app.api.chat import _thread_ids, _thread_summary
from app.core.checkpoint import open_checkpointer
from app.core.config import get_settings
from app.core.langfuse import build_langfuse_handler
from app.core.llm import build_chat_model
from app.main import app

pytestmark = pytest.mark.integration

QUESTION = "贵州茅台最近行情怎么样？"


async def _assemble(saver):
    """按与生产一致的顺序装配（工具 → 模型 → 图）。"""
    tools = [query_market_bars]
    xiaoshi_tools, missing = await load_xiaoshi_tools()
    assert not missing, f"白名单缺项：{sorted(missing)}"
    assert len(xiaoshi_tools) == len(XIAOSHI_ALLOWLIST)
    tools.extend(xiaoshi_tools)
    return build_agent(build_chat_model(), tools, checkpointer=saver)


async def test_agent_calls_tool_and_streams_tokens() -> None:
    """SPEC §6 验收：Agent 自主调工具并流式作答。"""
    settings = get_settings()
    async with open_checkpointer(settings.postgres_dsn) as saver:
        agent = await _assemble(saver)
        thread_id = str(uuid.uuid4())
        events = [
            (name, payload)
            async for name, payload in astream_chat(
                agent, message=QUESTION, thread_id=thread_id
            )
        ]

    kinds = [name for name, _ in events]
    assert "tool_call" in kinds, "Agent 没有调用任何工具"
    assert kinds.count("token") > 1, "没有逐 token 输出（可能退化成整段返回）"
    assert kinds[-1] == "done"

    # 调用与结果必须成对且 id 一致（前端靠它配对渲染）
    calls = [p["id"] for n, p in events if n == "tool_call"]
    results = [p["id"] for n, p in events if n == "tool_result"]
    assert calls == results

    done = events[-1][1]
    assert done["thread_id"] == thread_id
    assert len(done["content"]) > 20, "最终回答过短，可能没答完"
    assert done["usage"] and done["usage"]["total_tokens"] > 0


async def test_conversation_persists_and_lists() -> None:
    """一轮对话后，会话列表能查到它（标题取自首条人类消息）。"""
    settings = get_settings()
    async with open_checkpointer(settings.postgres_dsn) as saver:
        agent = await _assemble(saver)
        thread_id = str(uuid.uuid4())
        async for _ in astream_chat(agent, message=QUESTION, thread_id=thread_id):
            pass

        ids = await _thread_ids(saver, 50)
        assert thread_id in ids, "会话没有落到 checkpointer"

        summary = await _thread_summary(saver, thread_id)
        assert summary["messages"] >= 2  # 至少一问一答
        assert summary["title"].startswith("贵州茅台")


def test_chat_endpoint_streams_sse() -> None:
    """HTTP 层：真实 lifespan + 真实模型，确认 SSE 帧按序到达。"""
    with TestClient(app) as client:  # with 才跑 lifespan（会连库、拉起小石 MCP）
        with client.stream(
            "POST", "/api/v1/chat", json={"message": QUESTION}
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            thread_id = response.headers["x-thread-id"]
            body = "".join(response.iter_text())

        events = [
            block.split("\n", 1)[0].removeprefix("event: ")
            for block in body.split("\n\n")
            if block.startswith("event: ")
        ]
        assert "tool_call" in events
        assert events[-1] == "done"
        for block in body.split("\n\n"):
            if block.startswith("data: "):
                assert "\n" not in block.removeprefix("data: ").strip(), (
                    "帧内出现换行，客户端按 \\n\\n 切帧会错位"
                )

        # 会话列表在同一 lifespan 内查询（退出 with 后 checkpointer 已关闭）
        listed = client.get("/api/v1/chat/threads").json()
        assert any(t["thread_id"] == thread_id for t in listed)


async def test_langfuse_receives_trace() -> None:
    """SPEC §6 验收：Langfuse 能看到完整 trace（含工具调用与成本）。

    这条同时守住「Settings 不进 os.environ」那个静默失败——handler 一旦退化成
    NoOpTracer，这里会因为查不到新 trace 而失败，而不是悄悄通过。
    """
    from langfuse import get_client

    handler = build_langfuse_handler()
    if handler is None:
        pytest.skip("Langfuse 未启用或未配置 key")

    client = get_client()

    def observations() -> list:
        # v4 的 events_only 部署下 trace.list() 已不可用，观测数据走 observations
        return client.api.observations.get_many(limit=50).data

    client.flush()
    before = {o.id for o in observations()}

    settings = get_settings()
    async with open_checkpointer(settings.postgres_dsn) as saver:
        agent = await _assemble(saver)
        async for _ in astream_chat(
            agent,
            message=QUESTION,
            thread_id=str(uuid.uuid4()),
            callbacks=[handler],
        ):
            pass

    # 上报是异步的（OTel 批处理 + ClickHouse 摄入），给几次重试而不是立即断言
    fresh: list = []
    for _ in range(10):
        client.flush()
        fresh = [o for o in observations() if o.id not in before]
        if fresh:
            break
        await asyncio.sleep(2.0)

    assert fresh, "本次运行没有在 Langfuse 产生新观测数据"
    kinds = {o.type for o in fresh}
    names = {o.name for o in fresh}
    assert "TOOL" in kinds, f"trace 里没有工具调用（实际类型：{sorted(kinds)}）"
    assert "query_market_bars" in names
    assert "GENERATION" in kinds, "没有模型调用记录（成本/用量会缺失）"
