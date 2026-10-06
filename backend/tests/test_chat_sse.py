"""离线单测：T3 的 SSE 编码、事件翻译、工具与降级行为。

全部不联网：模型用 `ToolCallingFakeModel` 脚本驱动，行情用合成的 Parquet。
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import _tool_result_text, astream_chat, build_agent
from app.agent.tools import filter_allowlist, query_market_bars
from app.api.chat import normalize_thread_id, sse_frame
from app.data import duckdb_client
from app.main import app
from tests.conftest import make_backtest_dir, trading_days
from tests.fakes import ToolCallingFakeModel


@tool
def echo(text: str) -> str:
    """回显输入文本（测试用）。"""
    return f"ECHO:{text}"


# ── SSE 帧编码 ──────────────────────────────────────────────


def test_sse_frame_is_single_line_json() -> None:
    """data 必须单行——工具文本里的换行会截断帧。"""
    frame = sse_frame("token", {"text": "第一行\n第二行"})
    assert frame.startswith("event: token\ndata: ")
    assert frame.endswith("\n\n")
    body = frame.split("data: ", 1)[1].rstrip("\n")
    assert "\n" not in body
    assert json.loads(body) == {"text": "第一行\n第二行"}


def test_sse_frame_keeps_chinese_readable() -> None:
    """ensure_ascii=False：中文不转义，便于 debug 时肉眼读。"""
    assert "贵州茅台" in sse_frame("token", {"text": "贵州茅台"})


@pytest.mark.parametrize("raw", [None, ""])
def test_thread_id_generated_when_absent(raw: str | None) -> None:
    generated = normalize_thread_id(raw)
    assert len(generated) == 36


def test_thread_id_rejected_when_not_uuid() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        normalize_thread_id("not-a-uuid")
    assert exc.value.status_code == 422


# ── 事件翻译 ────────────────────────────────────────────────


def _scripted_agent() -> object:
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "echo", "args": {"text": "hi"}, "id": "call_1"}],
            ),
            AIMessage(content="好了"),
        ]
    )
    return build_agent(model, [echo], checkpointer=InMemorySaver())


async def test_astream_chat_event_sequence() -> None:
    """一轮「调工具 → 作答」应产出 tool_call → tool_result → token… → done。"""
    agent = _scripted_agent()
    events = [
        (name, payload)
        async for name, payload in astream_chat(
            agent, message="测试", thread_id="thread-1"
        )
    ]
    kinds = [name for name, _ in events]

    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1
    assert kinds.count("done") == 1
    assert kinds.index("tool_call") < kinds.index("tool_result") < kinds.index("done")

    call = next(p for n, p in events if n == "tool_call")
    assert call["name"] == "echo"
    assert call["args"] == {"text": "hi"}  # input 是纯参数，不含 name/id/type

    result = next(p for n, p in events if n == "tool_result")
    assert "ECHO:hi" in result["content"]
    assert result["is_error"] is False
    # 前端靠 id 把调用和结果配对，两端必须一致
    assert call["id"] == result["id"]
    assert call["id"]

    # 逐 token：假模型按字符吐，token 帧数应远多于 1（否则说明没真流式）
    tokens = [p["text"] for n, p in events if n == "token"]
    assert len(tokens) > 1
    assert "".join(tokens) == "好了"

    done = next(p for n, p in events if n == "done")
    assert done["thread_id"] == "thread-1"
    assert done["content"] == "好了"


async def test_done_content_matches_joined_tokens() -> None:
    agent = _scripted_agent()
    events = [
        (n, p) async for n, p in astream_chat(agent, message="x", thread_id="t")
    ]
    tokens = "".join(p["text"] for n, p in events if n == "token")
    done = next(p for n, p in events if n == "done")
    assert done["content"] == tokens


def test_tool_result_text_handles_content_blocks() -> None:
    """MCP 工具返回的是 content blocks 列表，不是字符串。"""

    class FakeMessage:
        content = [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]

    assert _tool_result_text(FakeMessage()) == "第一段第二段"
    assert _tool_result_text("纯文本") == "纯文本"


# ── HTTP 层（不启动 lifespan） ──────────────────────────────


def test_chat_returns_503_without_agent() -> None:
    """lifespan 未跑（或 Agent 装配失败）时给 503，而不是 AttributeError。"""
    app.state.agent = None
    response = TestClient(app).post("/api/v1/chat", json={"message": "你好"})
    assert response.status_code == 503


def test_chat_rejects_bad_thread_id() -> None:
    response = TestClient(app).post(
        "/api/v1/chat", json={"message": "你好", "thread_id": "oops"}
    )
    assert response.status_code == 422


def test_threads_returns_503_without_checkpointer() -> None:
    app.state.checkpointer = None
    response = TestClient(app).get("/api/v1/chat/threads")
    assert response.status_code == 503


def test_chat_streams_sse_end_to_end() -> None:
    """端到端：假图 + 真实路由 + 帧编码，确认客户端能按 \\n\\n 切出事件。"""
    app.state.agent = _scripted_agent()
    app.state.langfuse_handler = None
    try:
        client = TestClient(app)
        with client.stream("POST", "/api/v1/chat", json={"message": "测试"}) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert len(response.headers["x-thread-id"]) == 36
            body = "".join(response.iter_text())
    finally:
        app.state.agent = None

    events = [
        block.split("\n", 1)[0].removeprefix("event: ")
        for block in body.split("\n\n")
        if block.startswith("event: ")
    ]
    assert events[0] == "token" or "tool_call" in events
    assert "done" in events
    assert events[-1] == "done"


# ── 工具层 ──────────────────────────────────────────────────


def test_allowlist_drops_action_tools() -> None:
    class T:
        def __init__(self, name: str) -> None:
            self.name = name

    tools = [
        T("get_live_quote"),
        T("plan_history_download"),  # 动作型：绝不能进工具集
        T("prepare_local_research"),
    ]
    picked, missing = filter_allowlist(tools)  # type: ignore[arg-type]
    assert [t.name for t in picked] == ["get_live_quote"]
    assert missing == {"get_event_timeline", "search_financial_news", "get_data_catalog"}


async def test_query_market_bars_renders_readable_text(monkeypatch, tmp_path) -> None:
    days = trading_days(date(2026, 1, 5), 3)
    make_backtest_dir(
        tmp_path,
        bars=[
            {"trade_date": d, "open": 100.0, "close": 100.0 + i, "volume": 1_000_000}
            for i, d in enumerate(days)
        ],
        symbol="600519",
    )
    # 查询层默认读 settings.data_dir；这里把解析函数指向临时目录
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path)

    text = await query_market_bars.ainvoke({"symbol": "600519", "limit": 2})

    assert "600519" in text
    assert "共 3 个交易日" in text
    assert str(days[-1]) in text  # 明细里带最新交易日


async def test_query_market_bars_reports_unknown_symbol(monkeypatch, tmp_path) -> None:
    make_backtest_dir(
        tmp_path,
        bars=[{"trade_date": date(2026, 1, 5), "open": 1.0, "close": 1.0}],
        symbol="600519",
    )
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path)

    text = await query_market_bars.ainvoke({"symbol": "000001"})
    assert "没有" in text and "600519" in text
