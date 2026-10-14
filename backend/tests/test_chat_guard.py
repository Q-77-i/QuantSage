"""E4c：chat 路径的护栏（L6）与失败告知（L5）。

两组判据：

  * **到限即停、部分结果照留**：token / 成本顶到上限时，这一轮停下并给一句人话，
    已经吐出来的正文不丢——半截回答配一句「已达上限」比静默断流强得多；
  * **失败按分类给话术**：上游挂了、限流了、超时了，对用户该做的事不一样
    （等一会儿 / 换个问法 / 报 bug），不能一律「内部错误」。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import astream_chat, build_agent, budget_message
from app.api.chat import failure_frame
from app.core import reliability
from app.core.config import get_settings
from app.core.reliability import CircuitOpen
from tests.fakes import ToolCallingFakeModel


@tool
def echo(text: str) -> str:
    """把输入原样返回来（与 test_chat_sse.py 同款的最小工具）。"""
    return f"ECHO:{text}"


def usage_agent(*, input_tokens: int, output_tokens: int) -> Any:
    """一个「一步就答完」的假图，但每次调用都报出身量可观的用量。"""
    message = AIMessage(
        content="结论如下",
        # `model_name` 要带上：成本那条是按模型价目表算的，取不到模型名就**不猜价**（计 0）
        response_metadata={"model_name": "deepseek/deepseek-flash"},
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )
    return build_agent(ToolCallingFakeModel(responses=[message]), [echo], checkpointer=InMemorySaver())


def drive(agent: Any) -> list[tuple[str, dict[str, Any]]]:
    async def collect() -> list[tuple[str, dict[str, Any]]]:
        return [
            frame
            async for frame in astream_chat(agent, message="测试", thread_id="guard-1")
        ]

    return asyncio.run(collect())


@pytest.fixture
def chat_limits(monkeypatch: pytest.MonkeyPatch):
    """把单轮上限压到用例好触发的量级。"""
    settings = get_settings()

    def tune(*, tokens: int = 10**9, seconds: float = 10**9, cost: float = 10**9) -> None:
        monkeypatch.setattr(settings, "chat_max_tokens", tokens, raising=False)
        monkeypatch.setattr(settings, "chat_max_seconds", seconds, raising=False)
        monkeypatch.setattr(settings, "chat_max_cost_usd", cost, raising=False)

    return tune


def test_token_cap_stops_the_turn_and_keeps_the_partial_answer(chat_limits: Any) -> None:
    chat_limits(tokens=100)

    frames = drive(usage_agent(input_tokens=80, output_tokens=40))  # 合计 120 > 100

    errors = [payload for name, payload in frames if name == "error"]
    assert errors and errors[0]["code"] == "budget"
    assert "token" in errors[0]["message"]
    # 到限是「停住」不是「失败」：正文照常留在 token 帧里，且**不发 done**
    # （发了 done 前端会当成正常收尾，把上限这件事盖过去）
    assert "".join(p["text"] for n, p in frames if n == "token") == "结论如下"
    assert not [p for n, p in frames if n == "done"]


def test_cost_cap_stops_the_turn(chat_limits: Any) -> None:
    chat_limits(cost=1e-9)  # 任何一次真实用量都会超

    frames = drive(usage_agent(input_tokens=1000, output_tokens=1000))

    errors = [payload for name, payload in frames if name == "error"]
    assert errors and "成本" in errors[0]["message"]


def test_wall_clock_cap_stops_the_turn(chat_limits: Any) -> None:
    chat_limits(seconds=-1.0)  # 一开始就已经「超时」

    frames = drive(usage_agent(input_tokens=1, output_tokens=1))

    assert [payload for name, payload in frames if name == "error"][0]["code"] == "budget"


def test_under_the_limits_nothing_changes(chat_limits: Any) -> None:
    """正常一轮不该有任何新行为——护栏的价值在于平时不出现。"""
    chat_limits(tokens=10**6, seconds=100.0, cost=1.0)

    frames = drive(usage_agent(input_tokens=100, output_tokens=50))

    assert not [p for name, p in frames if name == "error"]
    done = [payload for name, payload in frames if name == "done"]
    assert done and done[0]["content"] == "结论如下"


def test_budget_message_says_what_stopped_it() -> None:
    assert "token 用量" in budget_message("tokens")
    assert "已产出" in budget_message("cost_usd")


# ── 失败告知：按分类给话术 ──────────────────────────────────────


class _Boom(Exception):
    def __init__(self, status: int | None = None) -> None:
        super().__init__("boom")
        if status is not None:
            self.status_code = status


def test_failure_messages_are_classified() -> None:
    assert failure_frame(CircuitOpen("llm:pro", 30.0))["code"] == "upstream_unavailable"
    assert "熔断" in failure_frame(CircuitOpen("llm:pro", 30.0))["message"]

    assert failure_frame(TimeoutError())["code"] == "upstream_timeout"
    assert failure_frame(_Boom(408))["code"] == "upstream_timeout"
    assert failure_frame(_Boom(429))["code"] == "rate_limited"
    assert failure_frame(_Boom(503))["code"] == "upstream_unavailable"
    assert failure_frame(_Boom(401))["code"] == "internal"  # 鉴权错对用户就是「内部错误」


def test_cancelled_never_reaches_the_frame_builder() -> None:
    """`CancelledError` 走的是「客户端断连」那条路，不该被翻成一句用户可见的话。"""
    assert failure_frame(asyncio.CancelledError())["code"] == "internal"

    # 而兜底那句仍然是原样的
    assert failure_frame(ValueError("怪东西"))["message"] == "内部错误，请重试"


def test_open_circuit_degrades_a_role_with_a_readable_reason() -> None:
    """数据源熔断打开时，角色降级要**写明是熔断**（不是一句「取证异常」了事）。

    判据落在 `RoleResult.errors` 的文案上——它一路流到报告的 gaps 块。
    """
    from app.agent.deep.budget import Budget
    from app.agent.deep.roles.base import RoleContext, interpret
    from app.agent.deep.types import Brief
    from datetime import datetime

    ctx = RoleContext(
        brief=Brief(
            question="分析一下",
            symbols=("600519",),
            as_of=datetime.fromisoformat("2026-09-30T15:00:00+08:00"),
        ),
        budget=Budget(),
        thinking=False,
    )

    reliability.reset()
    # **必须按当前时钟开路**：`trip(0.0)` 会让 `allow()` 认为「冷却早已过」而放行一次探测——
    # 这个用例就会真发一次网络调用（第一版就是这么写的，结果「通过」得毫无意义）
    reliability.trip("llm:deepseek-flash")
    try:
        # 角色解读走的是 flash 档；熔断开着时策略层**不发调用**，CircuitOpen 抛到角色层
        result = asyncio.run(interpret("market", ctx, material="m"))

        # 角色照旧降级（不抛），但**文案要说是熔断**——否则读者只知道「有个异常」，
        # 不知道是「等一会儿就好」还是「出 bug 了」
        assert result.errors and "熔断" in result.errors[0]
        assert "llm:deepseek-flash" in result.errors[0]
    finally:
        reliability.reset()
