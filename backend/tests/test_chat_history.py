"""离线单测：会话历史端点、会话删除，以及 checkpoint 消息的转换。

不联网：图用 `ToolCallingFakeModel` 脚本驱动，checkpointer 用 `InMemorySaver`。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi.testclient import TestClient
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import TOOL_RESULT_PREVIEW, astream_chat, build_agent, preview_text
from app.api.chat import _RUN_LOCKS, _run_lock
from app.agent.history import messages_to_history
from app.main import app
from tests.conftest import TEST_USER
from tests.fakes import ToolCallingFakeModel


@tool
def echo(text: str) -> str:
    """回显输入文本（测试用）。"""
    return f"ECHO:{text}"


def _call(name: str = "echo", call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": {"text": "hi"}, "id": call_id}]
    )


# ── 转换语义 ────────────────────────────────────────────────


def test_single_tool_turn_merges_into_one_assistant() -> None:
    """标准一轮：工具步骤并入其后有正文的助手消息，而不是各自成条。"""
    rows = messages_to_history(
        [HumanMessage("问"), _call(), ToolMessage("结果", tool_call_id="call_1"), AIMessage("答")]
    )

    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0] == {"role": "user", "content": "问", "tools": []}
    step = rows[1]["tools"][0]
    assert (step["name"], step["content"], step["is_error"]) == ("echo", "结果", False)
    assert rows[1]["content"] == "答"


def test_round_with_text_before_and_after_tools_is_one_message() -> None:
    """真实模型的形态：先说话再调工具，拿到结果再接着说——整回合仍是一条助手消息。

    假模型不会暴露这个形态（脚本里的工具轮 content 是空的），是真实对话冒烟时发现的。
    """
    rows = messages_to_history(
        [
            HumanMessage("问"),
            AIMessage(
                content="我先查一下。",
                tool_calls=[{"name": "echo", "args": {"text": "hi"}, "id": "call_1"}],
            ),
            ToolMessage("结果", tool_call_id="call_1"),
            AIMessage("结论是这样。"),
        ]
    )

    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[1]["content"] == "我先查一下。结论是这样。"
    assert len(rows[1]["tools"]) == 1
    assert rows[1]["tools"][0]["content"] == "结果"


def test_multiple_tool_rounds_accumulate_in_order() -> None:
    """多轮工具调用累计到同一条助手消息，顺序按调用先后。"""
    rows = messages_to_history(
        [
            HumanMessage("问"),
            _call(call_id="call_1"),
            ToolMessage("第一轮", tool_call_id="call_1"),
            _call(call_id="call_2"),
            ToolMessage("第二轮", tool_call_id="call_2"),
            AIMessage("答"),
        ]
    )

    assert len(rows) == 2
    assert [step["content"] for step in rows[1]["tools"]] == ["第一轮", "第二轮"]


def test_content_blocks_are_flattened() -> None:
    """content 是内容块列表时取正文文本。"""
    message = AIMessage(content=[{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}])
    assert messages_to_history([message])[0]["content"] == "第一段第二段"


def test_reasoning_blocks_are_excluded() -> None:
    """思考块不进用户可见的正文。"""
    message = AIMessage(
        content=[{"type": "reasoning", "reasoning": "内心戏"}, {"type": "text", "text": "正文"}]
    )
    assert messages_to_history([message])[0]["content"] == "正文"


def test_long_tool_result_uses_same_truncation_as_sse() -> None:
    """截断口径必须与 SSE 预览一致，否则同一步骤在两个入口下显示不同内容。"""
    long_text = "长" * (TOOL_RESULT_PREVIEW + 50)
    rows = messages_to_history(
        [HumanMessage("问"), _call(), ToolMessage(long_text, tool_call_id="call_1"), AIMessage("答")]
    )

    content = rows[1]["tools"][0]["content"]
    assert content == preview_text(long_text)
    assert "已截断，原文" in content


def test_failed_tool_result_is_flagged() -> None:
    rows = messages_to_history(
        [
            _call(),
            ToolMessage("炸了", tool_call_id="call_1", status="error"),
            AIMessage("答"),
        ]
    )
    assert rows[0]["tools"][0]["is_error"] is True


def test_orphan_tool_message_is_dropped() -> None:
    """没有对应调用的结果无法归属，丢弃而不是凭空造一步。"""
    rows = messages_to_history([HumanMessage("问"), ToolMessage("孤儿", tool_call_id="ghost")])
    assert [row["role"] for row in rows] == ["user"]


def test_untracked_message_types_are_skipped() -> None:
    rows = messages_to_history(
        [
            SystemMessage("你是助手"),
            HumanMessage("问"),
            RemoveMessage(id="whatever"),
            AIMessage("答"),
        ]
    )
    assert [row["role"] for row in rows] == ["user", "assistant"]


def test_interrupted_turn_does_not_leak_into_next() -> None:
    """上一轮调了工具没等到正文：单列一条，不挂到下一轮的回答上。"""
    rows = messages_to_history(
        [
            HumanMessage("第一问"),
            _call(call_id="call_1"),
            ToolMessage("结果", tool_call_id="call_1"),
            HumanMessage("第二问"),
            AIMessage("第二答"),
        ]
    )

    assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[1]["content"] == ""  # 中断形态：有步骤无正文
    assert rows[3]["tools"] == []  # 下一轮不背上一轮的步骤


def test_trailing_pending_is_flushed() -> None:
    """序列末尾悬挂的调用同样单列。"""
    rows = messages_to_history([HumanMessage("问"), _call()])
    assert rows[-1] == {
        "role": "assistant",
        "content": "",
        "tools": [
            {"id": "call_1", "name": "echo", "args": {"text": "hi"}, "content": None, "is_error": False}
        ],
    }


def test_empty_history() -> None:
    assert messages_to_history([]) == []


def test_text_and_tool_calls_on_same_message() -> None:
    """模型边说话边调工具：正文与步骤落在同一条上，结果仍能回填。"""
    message = AIMessage(
        content="我先查一下",
        tool_calls=[{"name": "echo", "args": {"text": "hi"}, "id": "call_1"}],
    )
    rows = messages_to_history([message, ToolMessage("结果", tool_call_id="call_1")])

    assert len(rows) == 1
    assert rows[0]["content"] == "我先查一下"
    assert rows[0]["tools"][0]["content"] == "结果"


def test_missing_call_id_gets_synthetic_one() -> None:
    """模型不给 id 时补合成 id：前端契约要求它是 string，且要能配对。"""
    message = AIMessage(content="", tool_calls=[{"name": "echo", "args": {}, "id": None}])
    rows = messages_to_history([message, ToolMessage("结果", tool_call_id="call-0-0")])

    assert rows[0]["tools"][0]["id"] == "call-0-0"
    assert rows[0]["tools"][0]["content"] == "结果"


def test_empty_assistant_message_produces_no_bubble() -> None:
    """既无正文也无工具调用的空消息不产生空气泡。"""
    assert messages_to_history([AIMessage("")]) == []


# ── HTTP 层（不启动 lifespan）────────────────────────────────


def test_thread_messages_returns_503_without_checkpointer(signed_in: object) -> None:
    app.state.checkpointer = None
    response = TestClient(app).get(f"/api/v1/chat/threads/{uuid.uuid4()}/messages")
    assert response.status_code == 503


def test_thread_messages_rejects_bad_thread_id(signed_in: object) -> None:
    response = TestClient(app).get("/api/v1/chat/threads/oops/messages")
    assert response.status_code == 422


def test_thread_messages_returns_404_for_unknown_thread(signed_in: object) -> None:
    app.state.checkpointer = InMemorySaver()
    try:
        response = TestClient(app).get(f"/api/v1/chat/threads/{uuid.uuid4()}/messages")
    finally:
        app.state.checkpointer = None
    assert response.status_code == 404


def test_thread_messages_after_real_turn(signed_in: Any) -> None:
    """真跑一轮图（假模型 + 内存 checkpointer），确认端点还原出合并后的历史。"""
    thread_id = str(uuid.uuid4())
    saver = InMemorySaver()
    agent = build_agent(
        ToolCallingFakeModel(responses=[_call(), AIMessage("好了")]),
        [echo],
        checkpointer=saver,
    )

    async def drain() -> None:
        async for _ in astream_chat(agent, message="测试", thread_id=thread_id):
            pass

    asyncio.run(drain())
    asyncio.run(signed_in.claim_thread(thread_id, TEST_USER["id"]))  # 直接跑图不经端点，归属行要自己落

    app.state.checkpointer = saver
    try:
        response = TestClient(app).get(f"/api/v1/chat/threads/{thread_id}/messages")
    finally:
        app.state.checkpointer = None

    assert response.status_code == 200
    body = response.json()
    assert body["thread_id"] == thread_id
    assert [row["role"] for row in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == "测试"

    assistant = body["messages"][1]
    assert assistant["content"] == "好了"
    assert len(assistant["tools"]) == 1
    assert "ECHO:hi" in assistant["tools"][0]["content"]
    assert body["running"] is False  # 没人在跑这个会话


def test_messages_reports_running_while_a_turn_is_in_flight(signed_in: Any) -> None:
    """有图在跑时报 `running: true` —— 前端据此显示「回答中…」并轮询。

    刷新断流后那一轮仍在服务端跑（断连不再取消图），端点不告诉前端「还在跑」的话，
    用户就得手动再刷一次才看得到答案。这里用**替身锁**直接验端点契约（比造真并发确定）。
    """
    thread_id = str(uuid.uuid4())
    saver = InMemorySaver()
    agent = build_agent(
        ToolCallingFakeModel(responses=[AIMessage(content="先说着")]), [echo], checkpointer=saver
    )

    async def seed() -> None:
        async for _ in astream_chat(agent, message="测试", thread_id=thread_id):
            pass

    asyncio.run(seed())
    asyncio.run(signed_in.claim_thread(thread_id, TEST_USER["id"]))

    class _Busy:
        """替身：只要「这个会话的锁被持有」，端点就该报 running。"""

        def locked(self) -> bool:
            return True

    app.state.checkpointer = saver
    _RUN_LOCKS[thread_id] = _Busy()  # type: ignore[assignment]
    try:
        body = TestClient(app).get(f"/api/v1/chat/threads/{thread_id}/messages").json()
    finally:
        app.state.checkpointer = None
        _RUN_LOCKS.pop(thread_id, None)

    assert body["running"] is True

# ── 会话删除 ────────────────────────────────────────────────


def test_delete_thread_returns_503_without_checkpointer(signed_in: object) -> None:
    app.state.checkpointer = None
    response = TestClient(app).delete(f"/api/v1/chat/threads/{uuid.uuid4()}")
    assert response.status_code == 503


def test_delete_thread_rejects_bad_thread_id(signed_in: object) -> None:
    response = TestClient(app).delete("/api/v1/chat/threads/oops")
    assert response.status_code == 422


def test_delete_thread_returns_404_for_unknown_thread(signed_in: object) -> None:
    """`adelete_thread` 对不存在的 thread 静默成功，端点必须自己先验归属。"""
    app.state.checkpointer = InMemorySaver()
    try:
        response = TestClient(app).delete(f"/api/v1/chat/threads/{uuid.uuid4()}")
    finally:
        app.state.checkpointer = None
    assert response.status_code == 404


def test_delete_thread_removes_messages(signed_in: Any) -> None:
    """删完之后历史端点应当 404（归属行也一并删掉，列表里不再有它）。"""
    thread_id = str(uuid.uuid4())
    saver = InMemorySaver()
    agent = build_agent(
        ToolCallingFakeModel(responses=[AIMessage("你好")]), [], checkpointer=saver
    )

    async def drain() -> None:
        async for _ in astream_chat(agent, message="测试", thread_id=thread_id):
            pass

    asyncio.run(drain())
    asyncio.run(signed_in.claim_thread(thread_id, TEST_USER["id"]))

    app.state.checkpointer = saver
    try:
        client = TestClient(app)
        created = client.delete(f"/api/v1/chat/threads/{thread_id}")
        assert created.status_code == 200
        assert created.json() == {"thread_id": thread_id, "deleted": True}

        assert client.get(f"/api/v1/chat/threads/{thread_id}/messages").status_code == 404
        assert client.get("/api/v1/chat/threads").json() == []
    finally:
        app.state.checkpointer = None
