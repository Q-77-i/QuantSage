"""T6b 集成测试：历史读取路径打真实 Postgres checkpointer。

用**假模型**驱动，不调真实 LLM——要验的是 checkpointer 的读取（离线用例走的是
`InMemorySaver`）与合并语义在真实基础设施上成立，不是模型能力，因此零 API 费用。

前置条件：`docker compose up -d --wait`（postgres 必需）。
默认不收集；用 `uv run pytest -m integration` 触发。
"""

from __future__ import annotations

import uuid

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from app.agent.graph import astream_chat, build_agent
from app.agent.history import messages_to_history
from app.api.chat import _load_messages, _thread_ids
from app.core.checkpoint import open_checkpointer
from app.core.config import get_settings
from tests.fakes import ToolCallingFakeModel

pytestmark = pytest.mark.integration


@tool
def echo(text: str) -> str:
    """回显输入文本（测试用）。"""
    return f"ECHO:{text}"


async def test_thread_round_trip_through_real_checkpointer() -> None:
    """灌一轮「调工具 → 作答」，确认能从 Postgres 还原出合并后的历史，且删得干净。

    会话列表端点走 Postgres 特有的 SQL（`InMemorySaver` 没有 `conn`），所以
    「删除后从列表消失」这条只能在真实基础设施上验。
    """
    settings = get_settings()
    thread_id = str(uuid.uuid4())

    async with open_checkpointer(settings.postgres_dsn) as saver:
        agent = build_agent(
            ToolCallingFakeModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[{"name": "echo", "args": {"text": "hi"}, "id": "call_1"}],
                    ),
                    AIMessage("好了"),
                ]
            ),
            [echo],
            checkpointer=saver,
        )
        async for _ in astream_chat(agent, message="测试", thread_id=thread_id):
            pass

        messages = await _load_messages(saver, thread_id)
        missing = await _load_messages(saver, str(uuid.uuid4()))

        await saver.adelete_thread(thread_id)
        deleted = await _load_messages(saver, thread_id)
        still_listed = thread_id in await _thread_ids(saver, 50)

    assert messages is not None, "刚存下的会话读不回来"
    assert missing is None, "不存在的会话应当返回 None（端点据此给 404）"
    assert deleted is None, "删除后仍然读得到"
    assert not still_listed, "删除后仍出现在会话列表里"

    rows = messages_to_history(messages)
    assert [row["role"] for row in rows] == ["user", "assistant"]

    assistant = rows[1]
    assert assistant["content"] == "好了", "工具轮没有并入其后有正文的助手消息"
    assert [step["name"] for step in assistant["tools"]] == ["echo"]
    assert "ECHO:hi" in assistant["tools"][0]["content"]
