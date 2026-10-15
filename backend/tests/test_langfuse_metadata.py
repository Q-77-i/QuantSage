"""E7c：Langfuse 四个运行期字段与观测接线（离线，**不连 Langfuse**）。

验收的「真机核对」不在这一档（那是 SPEC §7.9 第 5 条的验收期动作）——这里钉的是
**接线本身**：四个键的构造形状、各调用点真的把 handler 与 metadata 送进了 config。
真机真跑一次读回字段，是另一回事；但接线错了，真机一定读不到。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from app.agent.deep.budget import Budget
from app.agent.deep.roles.base import RoleContext
from app.agent.deep.types import Brief
from app.core.langfuse import (
    TRACE_NAME_CHAT,
    TRACE_NAME_DEEP,
    TRACE_NAME_REPORT,
    TRACE_NAME_SETTLE,
    TraceConfig,
    make_langfuse_metadata,
    settle_trace,
    trace_config,
)


class _GraphSpy:
    """最小图替身：记下 astream / astream_events 收到的 config，产 0 帧即结束。"""

    def __init__(self) -> None:
        self.config: dict[str, Any] | None = None

    def astream(self, *args: Any, **kwargs: Any) -> Any:
        self.config = kwargs.get("config")

        async def empty() -> Any:
            return
            yield  # pragma: no cover —— 让它是 async generator

        return empty()

    def astream_events(self, *args: Any, **kwargs: Any) -> Any:
        # 真实调用形状是位置参数（`graph.astream_events(payload, config, version="v2")`）
        self.config = args[1] if len(args) > 1 else kwargs.get("config")

        async def empty() -> Any:
            return
            yield  # pragma: no cover

        return empty()

    async def aupdate_state(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        return None

    async def aget_state(self, *args: Any, **kwargs: Any) -> Any:
        """两处会读：`heal_dangling_tool_calls`（看 values）与深路径的 `interrupt_payload`（看 tasks）。

        空快照 = 没有历史、没有待批——两条读法都落在「无事发生」这一支。
        """

        class _Snapshot:
            values: dict[str, Any] = {}
            tasks: list[Any] = []

        return _Snapshot()


def _handler() -> Any:
    """handler 标识物：断言「同一个对象被传了下去」。

    但要走真 LangChain 回调管理器（`complete` 的壳那条测试），就得有 `run_inline` 等
    属性——用 `BaseCallbackHandler` 的空子类，别拿 `object()` 硬顶（管理器会直接炸）。
    """
    from langchain_core.callbacks import BaseCallbackHandler

    class _Handler(BaseCallbackHandler):
        pass

    return _Handler()


# ── 一、四个键的形状（SDK 的契约） ──────────────────────────────────────────


def test_make_langfuse_metadata_shape() -> None:
    """四个键全在、类型对：三个 str、tags 是 **list**（元组 SDK 会当没看见）。"""
    meta = make_langfuse_metadata(
        session_id="thread-1", user_id=7, trace_name=TRACE_NAME_CHAT, tags=("chat",)
    )

    assert set(meta) == {
        "langfuse_session_id",
        "langfuse_user_id",
        "langfuse_trace_name",
        "langfuse_tags",
    }
    assert meta["langfuse_session_id"] == "thread-1"
    assert meta["langfuse_user_id"] == "7"  # 数字 id 转 str——SDK 只收 str
    assert meta["langfuse_trace_name"] == "chat"
    assert meta["langfuse_tags"] == ["chat"]
    assert isinstance(meta["langfuse_tags"], list)


def test_trace_config_pairs_callbacks_with_metadata() -> None:
    """`TraceConfig` 的意义就是**两样同行**：只给 handler 不给字段 = 字段全空。"""
    handler = _handler()
    cfg = TraceConfig(callbacks=[handler]).with_metadata(
        session_id="acc-1", user_id=3, trace_name=TRACE_NAME_SETTLE, tags=("settle",)
    )

    kw = cfg.kwargs()
    assert kw["callbacks"] == [handler]
    assert kw["metadata"]["langfuse_session_id"] == "acc-1"
    assert cfg.active is True

    # 空配置：callbacks / metadata 给 None（LangChain 视作无回调），run_name 由字段推导
    assert TraceConfig().kwargs() == {"callbacks": None, "metadata": None, "run_name": None}
    assert TraceConfig().active is False
    assert kw["run_name"] == TRACE_NAME_SETTLE  # 具名壳用的名字取业务名


def test_settle_trace_reads_handler_from_state() -> None:
    """从 app.state 取 handler；没挂载时字段照构造、callbacks 为空（降级零影响）。"""

    class _State:
        langfuse_handler = None

    off = settle_trace(_State(), account_id="acc-1", user_id=3)
    assert off.callbacks == []
    assert off.metadata["langfuse_trace_name"] == TRACE_NAME_SETTLE

    handler = _handler()
    _State.langfuse_handler = handler
    on = settle_trace(_State(), account_id="acc-1", user_id=3)
    assert on.callbacks == [handler]
    assert on.metadata["langfuse_user_id"] == "3"


def test_trace_config_from_state_flag() -> None:
    class _State:
        langfuse_handler = None

    assert trace_config(_State()).active is False


# ── 二、chat：唯一 config 构造点带上四个键 ─────────────────────────────────


@pytest.mark.asyncio
async def test_astream_chat_sends_trace_metadata() -> None:
    from app.agent.graph import astream_chat

    spy = _GraphSpy()
    handler = _handler()
    async for _ in astream_chat(
        spy,  # type: ignore[arg-type]
        message="你好",
        thread_id="thread-1",
        user_id=7,
        callbacks=[handler],
    ):
        pass

    assert spy.config is not None
    meta = spy.config["metadata"]
    assert meta["langfuse_session_id"] == "thread-1"
    assert meta["langfuse_user_id"] == "7"
    assert meta["langfuse_trace_name"] == TRACE_NAME_CHAT
    assert meta["langfuse_tags"] == ["chat"]
    assert spy.config["callbacks"] == [handler]


@pytest.mark.asyncio
async def test_astream_chat_without_user_still_sends_four_keys() -> None:
    """脚本（无登录用户）也要四个键齐全——缺 user 时给空串，不给会被 SDK 丢掉的类型。"""
    from app.agent.graph import astream_chat

    spy = _GraphSpy()
    async for _ in astream_chat(spy, message="你好", thread_id="t"):  # type: ignore[arg-type]
        pass

    assert spy.config is not None
    assert spy.config["metadata"]["langfuse_user_id"] == ""
    assert "callbacks" not in spy.config  # 没传 handler 就不建这个键


# ── 三、深路径：handler 与字段真的进了图 config ────────────────────────────


@pytest.mark.asyncio
async def test_astream_deep_run_forwards_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.deep import runner as runner_mod
    from app.agent.deep.runner import astream_deep_run

    spy = _GraphSpy()
    monkeypatch.setattr(runner_mod, "build_deep_graph", lambda **_: spy)

    handler = _handler()
    meta = make_langfuse_metadata(
        session_id="run-1", user_id=7, trace_name=TRACE_NAME_DEEP, tags=("deep", "research")
    )
    ctx = RoleContext(
        brief=Brief(
            question="q", symbols=("600519",), as_of=datetime.fromisoformat("2026-09-30T15:00:00+08:00")
        ),
        budget=Budget(),
    )
    async for _ in astream_deep_run(
        ctx=ctx, checkpointer=None, run_id="run-1", brief=ctx.brief,
        callbacks=[handler], metadata=meta,
    ):
        pass

    assert spy.config is not None
    assert spy.config["callbacks"] == [handler]
    assert spy.config["metadata"] == meta


# ── 四、报告 / 反思路径：trace 透传到 LLM 调用 ──────────────────────────────


@pytest.mark.asyncio
async def test_build_narrative_passes_trace_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import llm as llm_mod
    from app.report import narrative as narrative_mod

    captured: dict[str, Any] = {}

    async def fake_ask_once(prompt: str, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "综述正文"

    monkeypatch.setattr(llm_mod, "ask_once", fake_ask_once)
    handler = _handler()
    trace = TraceConfig(callbacks=[handler]).with_metadata(
        session_id="acc-1", user_id=3, trace_name=TRACE_NAME_REPORT, tags=("report",)
    )

    result = await narrative_mod.build_narrative({"account": "a"}, trace=trace)

    assert result.text == "综述正文"
    assert captured["callbacks"] == [handler]
    assert captured["metadata"]["langfuse_trace_name"] == TRACE_NAME_REPORT


@pytest.mark.asyncio
async def test_build_narrative_without_trace_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不传 trace 时给 None（老调用方零感知）。"""
    from app.core import llm as llm_mod
    from app.report import narrative as narrative_mod

    captured: dict[str, Any] = {}

    async def fake_ask_once(prompt: str, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "x"

    monkeypatch.setattr(llm_mod, "ask_once", fake_ask_once)
    await narrative_mod.build_narrative({})
    assert captured["callbacks"] is None
    assert captured["metadata"] is None
    assert captured["run_name"] is None


# ── 五、裸模型调用的「具名壳」（E7c 实测逼出来的结构） ──────────────────────


@pytest.mark.asyncio
async def test_complete_wraps_bare_model_call_when_traced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """挂了 handler **且**给了 run_name ⇒ 走具名壳（链）。

    裸模型调用当 trace 根时实测两处不对（trace 名落不上、同会话调用被并走），
    壳提供「chain 父」这个结构。这里钉住结构本身——真机效果见 SPEC §7.6 的对照实验。
    """
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda

    from app.core import llm as llm_mod

    shells: list[str] = []
    real_with_config = RunnableLambda.with_config

    def spy_with_config(self: Any, **kwargs: Any) -> Any:
        if "run_name" in kwargs:
            shells.append(str(kwargs["run_name"]))
        return real_with_config(self, **kwargs)

    monkeypatch.setattr(RunnableLambda, "with_config", spy_with_config)
    monkeypatch.setattr(
        llm_mod,
        "build_chat_model",
        lambda *a, **k: FakeMessagesListChatModel(responses=[AIMessage("好")]),
    )

    result = await llm_mod.complete(
        "说一个字",
        model="x",
        callbacks=[_handler()],
        metadata=make_langfuse_metadata(
            session_id="acc-1", user_id=3, trace_name=TRACE_NAME_REPORT, tags=("report",)
        ),
        run_name=TRACE_NAME_REPORT,
    )

    assert result.text == "好"
    assert shells == [TRACE_NAME_REPORT]  # 壳建了、名字取业务名


@pytest.mark.asyncio
async def test_build_reflection_passes_trace_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """结算路径的 trace 透传（与综述同姿势，但走的是另一个模块）。"""
    from app.core import llm as llm_mod
    from app.memory import reflection as reflection_mod

    captured: dict[str, Any] = {}

    async def fake_ask_once(prompt: str, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "教训正文"

    monkeypatch.setattr(llm_mod, "ask_once", fake_ask_once)
    handler = _handler()
    trace = TraceConfig(callbacks=[handler]).with_metadata(
        session_id="acc-1", user_id=3, trace_name=TRACE_NAME_SETTLE, tags=("settle",)
    )

    result = await reflection_mod.build_reflection({"round": 1}, trace=trace)

    assert result.text == "教训正文"
    assert captured["callbacks"] == [handler]
    assert captured["metadata"]["langfuse_trace_name"] == TRACE_NAME_SETTLE
    assert captured["run_name"] == TRACE_NAME_SETTLE


@pytest.mark.asyncio
async def test_complete_without_trace_is_bare_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """没挂 handler 就不套壳——离线路径行为一字不变。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda

    from app.core import llm as llm_mod

    shells: list[str] = []
    real_with_config = RunnableLambda.with_config

    def spy_with_config(self: Any, **kwargs: Any) -> Any:
        if "run_name" in kwargs:
            shells.append(str(kwargs["run_name"]))
        return real_with_config(self, **kwargs)

    monkeypatch.setattr(RunnableLambda, "with_config", spy_with_config)
    monkeypatch.setattr(
        llm_mod,
        "build_chat_model",
        lambda *a, **k: FakeMessagesListChatModel(responses=[AIMessage("好")]),
    )
    await llm_mod.complete("说一个字", model="x", run_name=TRACE_NAME_REPORT)
    assert shells == []
