"""T6b 集成测试：历史读取路径打真实 Postgres checkpointer。

用**假模型**驱动，不调真实 LLM——要验的是 checkpointer 的读取（离线用例走的是
`InMemorySaver`）与合并语义在真实基础设施上成立，不是模型能力，因此零 API 费用。

M1 起会话列表改由自有表 `chat_threads` 出（不再直读 `checkpoints`），所以这里同时
验归属行的落库、列出与删除——「列表里不再出现」这条只能在真库上成立。

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
from app.api.chat import _load_messages
from app.core.checkpoint import open_checkpointer
from app.core.config import get_settings
from app.core.db import Database, init_schema, open_pool
from tests.fakes import ToolCallingFakeModel

pytestmark = pytest.mark.integration


@tool
def echo(text: str) -> str:
    """回显输入文本（测试用）。"""
    return f"ECHO:{text}"


async def test_thread_round_trip_through_real_checkpointer() -> None:
    """灌一轮「调工具 → 作答」，确认能从 Postgres 还原出合并后的历史，且删得干净。

    归属行与列表排序走真实 SQL，所以「谁的会话出现在谁的列表里」只能在真库上验。
    """
    settings = get_settings()
    thread_id = str(uuid.uuid4())
    email = f"t6b-roundtrip-{uuid.uuid4().hex[:8]}@example.com"

    async with open_pool(settings.postgres_dsn) as pool:
        await init_schema(pool)
        db = Database(pool)
        user = await db.create_user(email, "not-a-real-hash")
        try:
            async with open_checkpointer(settings.postgres_dsn) as saver:
                agent = build_agent(
                    ToolCallingFakeModel(
                        responses=[
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {"name": "echo", "args": {"text": "hi"}, "id": "call_1"}
                                ],
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

                await db.claim_thread(thread_id, int(user["id"]))
                claimed_owner = await db.thread_owner(thread_id)
                listed = [row["thread_id"] for row in await db.list_threads(int(user["id"]), 50)]

                await saver.adelete_thread(thread_id)
                await db.drop_thread(thread_id, int(user["id"]))
                deleted = await _load_messages(saver, thread_id)
                still_listed = thread_id in [
                    row["thread_id"] for row in await db.list_threads(int(user["id"]), 50)
                ]
        finally:
            async with pool.connection() as conn:
                await conn.execute("DELETE FROM users WHERE email = %s", (email,))

    assert messages is not None, "刚存下的会话读不回来"
    assert missing is None, "不存在的会话应当返回 None（端点据此给 404）"
    assert claimed_owner == int(user["id"]), "归属行没落上"
    assert thread_id in listed, "本人的会话没出现在自己的列表里"
    assert deleted is None, "删除后仍然读得到"
    assert not still_listed, "删除后仍出现在会话列表里"

    rows = messages_to_history(messages)
    assert [row["role"] for row in rows] == ["user", "assistant"]

    assistant = rows[1]
    assert assistant["content"] == "好了", "工具轮没有并入其后有正文的助手消息"
    assert [step["name"] for step in assistant["tools"]] == ["echo"]
    assert "ECHO:hi" in assistant["tools"][0]["content"]
