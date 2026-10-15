"""M8b 整图（`app.agent.deep.graph`）：假模型驱动，验四件自动化层之外看不见的事。

1. **降级不拖垮兄弟节点**——一个角色炸掉，图照常跑完，`errors` 与 `gaps` 里如实有它；
2. **辩论硬上限**——造一个每轮都能编出新论据的模型，断言**恰好 3 轮**收住；
3. **闸门三态各自的走向**——放行 / 要求重写一次（再不过转降级，不无限重写）/ 超限 interrupt；
4. **HITL 可恢复**——`interrupt()` 停在闸门，`Command(resume=…)` 之后继续跑完。

`ctx` 走闭包（状态里只有纯 JSON），故图按用例构建；checkpointer 用 `InMemorySaver`。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agent.deep.budget import Budget, Limits
from app.agent.deep.graph import build_deep_graph
from app.agent.deep.roles.base import RoleContext
from app.agent.deep.types import Brief

AS_OF = datetime.fromisoformat("2026-09-30T15:00:00+08:00")
SYMBOL = "600519"  # 用真实标的：行情/公告/截面三类取数走真实 Parquet（沿用项目口径）


def brief() -> Brief:
    return Brief(question="分析一下值不值得买", symbols=(SYMBOL,), as_of=AS_OF)


class DeepFake(FakeMessagesListChatModel):
    """按 prompt 内容分派：角色解读 / 辩论发言 / 综合各给一种回答。

    真实运行里所有调用共用同一个模型对象，所以这里也必须是一个对象分派——
    按调用顺序塞脚本会在「并行角色 + 多轮辩论」下变成不可读的脆弱用例。
    """

    #: **必须声明成字段**：基类是 pydantic 模型，`self.x = …` 会直接报 `object has no field`
    #: （与 `ChatLiteLLM` 那次同根因——传参要走构造期，别在实例上挂属性）
    role_findings: str = "- 区间收益 -11.75%，估值回落"
    debate_payload: dict[str, Any] | None = None
    synthesis_script: list[dict[str, Any]] = []
    prompts: list[str] = []

    def __init__(self, **data: Any) -> None:
        data.setdefault("responses", [AIMessage(content="unused")])
        super().__init__(**data)

    def _answer(self, messages: list[Any]) -> AIMessage:
        prompt = str(messages[-1].content)
        self.prompts.append(prompt)
        if '"stance"' in prompt:
            found = re.search(r"第 (\d+) 轮发言", prompt)
            round_ = int(found.group(1)) if found else 1
            side = "bull" if "多方" in prompt.splitlines()[0] else "bear"
            payload = self.debate_payload or {
                "stance": f"{side} 立场",
                "arguments": [f"第 {round_} 轮 {side} 论据"],
                "citations": [],
            }
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))
        if '"conclusion"' in prompt:
            payload = self.synthesis_script.pop(0) if self.synthesis_script else {
                "conclusion": "多空分歧集中在需求端，证据有限。",
                "disagreements": ["需求端"],
                "risks": ["证据不足"],
                "citations": [],
            }
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))
        return AIMessage(content=self.role_findings)

    def _generate(self, messages: list[Any], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._answer(messages))])

    async def _agenerate(self, messages: list[Any], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        return self._generate(messages)


def make_ctx(chat: Any = None, *, limits: Limits | None = None) -> RoleContext:
    return RoleContext(
        brief=brief(),
        budget=Budget(limits=limits) if limits else Budget(),
        chat=chat or DeepFake(),
        thinking=False,
        timeout=5.0,
    )


@pytest.fixture(autouse=True)
def stub_rag(monkeypatch: pytest.MonkeyPatch):
    """RAG 在离线用例里走替身（真实检索要 Qdrant + 4GB 模型，那不是离线用例该付的代价）。"""
    import app.agent.deep.roles.news as news_mod
    from app.rag.retrieve import RetrievedEvent

    def fake_search(spec, **kwargs):
        return [
            RetrievedEvent(
                event_id="news:9001",
                day="2026-08-15",
                title="贵州茅台中报净利下滑",
                summary="净利同比下滑",
                event_time="2026-08-15T10:11:51+08:00",
                available_at="2026-08-15T10:40:10+08:00",
                event_type="news",
                direction_norm="bearish",
                importance_score=80.0,
                symbols=(SYMBOL,),
                industries=("食品饮料",),
                source="xiaoshi-archive",
                original_source="东方财富",
                source_url="https://example.invalid/1",
                content_hash="a" * 12,
                text="贵州茅台中报净利下滑",
                score=0.9,
                score_kind="rerank",
            )
        ]

    monkeypatch.setattr(news_mod, "search", fake_search)


def run_graph(ctx: RoleContext, *, thread: str = "t1", max_rounds: int | None = None):
    graph = build_deep_graph(ctx=ctx, checkpointer=InMemorySaver(), max_rounds=max_rounds)
    config = {"configurable": {"thread_id": thread}}
    result = asyncio.run(graph.ainvoke({"run_id": thread, "brief": brief().to_payload()}, config))
    return graph, config, result


# ── 跑通 ──


def test_full_run_produces_draft_and_passes_gate() -> None:
    ctx = make_ctx()
    _, _, state = run_graph(ctx)

    # 原始状态里是**完成顺序**（并行 fan-out 天然如此），五个都在即可……
    assert {r["role"] for r in state["role_results"]} == {"market", "news", "fundamental", "policy", "factor"}
    # ……但归并产物必须是固定顺序：报告要冻结、要算指纹，顺序不稳会让同一份输入产出不同报告
    assert [r["role"] for r in state["merged"]["roles"]] == [
        "market", "news", "fundamental", "policy", "factor",
    ]
    assert state["merged"]["evidence"], "归并后必须有证据"
    assert state["draft"]["conclusion"]
    assert state["gate"]["decision"] == "pass"
    assert state["debate_round"] >= 1
    assert ctx.budget.llm_calls >= 8  # 5 角色 + 辩论 + 综合


def test_role_failure_does_not_break_siblings(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个角色的取数炸掉 → 图照常跑完，errors 与 gaps 里如实有它（其余角色不受影响）。"""
    import app.agent.deep.roles.market as market_mod

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("行情源拔掉了")

    monkeypatch.setattr(market_mod.dc, "bars", boom)

    ctx = make_ctx()
    _, _, state = run_graph(ctx)

    assert any("行情异常" in e or "market" in e for e in state["errors"])
    assert any("行情技术面" in gap for gap in state["merged"]["gaps"])
    assert state["gate"]["decision"] == "pass"  # 缺口不阻塞出报告，但要如实标注
    assert len(state["role_results"]) == 5


# ── 辩论轮次 ──


def test_debate_stops_at_hard_cap_even_if_never_converging() -> None:
    """每轮都给全新论据的模型也截得住：**恰好 3 轮**（PRD 的硬上限）。"""

    class NeverConverge(DeepFake):
        def _answer(self, messages: list[Any]) -> AIMessage:
            prompt = str(messages[-1].content)
            self.prompts.append(prompt)
            if '"stance"' in prompt:
                round_ = int(re.search(r"第 (\d+) 轮发言", prompt).group(1))
                side = "bull" if "多方" in prompt.splitlines()[0] else "bear"
                return AIMessage(content=json.dumps({
                    "stance": f"{side}-{round_}",
                    "arguments": [f"第{round_}轮{side}的全新论据"],
                    "citations": [],
                }, ensure_ascii=False))
            return super()._answer(messages)

    ctx = make_ctx(NeverConverge())
    _, _, state = run_graph(ctx, max_rounds=9)  # 配置给 9，硬上限仍然只放 3 轮

    assert state["debate_round"] == 3
    assert len(state["debate"]) == 6  # 3 轮 × 双方


def test_debate_stops_early_when_converged() -> None:
    """收敛就提前停：同一套论据反复说，第 2 轮后停（默认 2 轮，不烧第 3 轮的钱）。"""
    ctx = make_ctx(DeepFake(debate_payload={"stance": "s", "arguments": ["同一套论据"], "citations": []}))
    _, _, state = run_graph(ctx)

    assert state["debate_round"] == 2 and len(state["debate"]) == 4


def test_debate_model_failure_is_recorded_and_debate_ends() -> None:
    class DebateBoom(DeepFake):
        def _answer(self, messages: list[Any]) -> AIMessage:
            prompt = str(messages[-1].content)
            if '"stance"' in prompt:
                raise RuntimeError("辩论模型 500")
            return super()._answer(messages)

    ctx = make_ctx(DebateBoom())
    _, _, state = run_graph(ctx)

    assert any("多方第 1 轮" in e or "空方第 1 轮" in e for e in state["errors"])
    assert state["gate"]["decision"] == "pass"  # 辩论挂了也出报告，缺口如实记


# ── 风控闸门三态 ──


def test_gate_rewrites_once_then_degrades_on_persistent_bad_number() -> None:
    """编造的数字：给一次重写机会，第二次仍不过就降级——**不无限重写**（每次都是真花钱）。"""
    bad = {"conclusion": "有 45% 的上行空间", "citations": []}
    ctx = make_ctx(DeepFake(synthesis_script=[bad, bad]))
    _, _, state = run_graph(ctx)

    assert state["gate"]["decision"] == "degrade"
    assert state.get("rewrite_used") is True
    assert "45%" in state["gate"]["reason"]
    assert state["draft"]["note"], "降级必须在正文上留痕"


def test_gate_rewrite_then_pass() -> None:
    """重写真的能救回来：第二版数字有出处 ⇒ 放行。"""
    bad = {"conclusion": "有 45% 的上行空间", "citations": []}
    good = {"conclusion": "区间收益 -11.75%，已如实标注。", "citations": []}
    ctx = make_ctx(DeepFake(synthesis_script=[bad, good]))
    _, _, state = run_graph(ctx)

    assert state["gate"]["decision"] == "pass" and state.get("rewrite_used") is True


def test_gate_requests_approval_and_resumes_with_continue() -> None:
    """措辞命中 → `interrupt()` 停在闸门；`Command(resume=…)` 之后继续跑完。"""
    ctx = make_ctx(DeepFake(synthesis_script=[{"conclusion": "建议买入并长期持有。", "citations": []}]))
    graph = build_deep_graph(ctx=ctx, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "hitl-1"}}

    asyncio.run(graph.ainvoke({"run_id": "hitl-1", "brief": brief().to_payload()}, config))

    # 待批载荷从 `tasks[].interrupts` 读——**事件流里没有它**，被 interrupt 的节点
    # 也不会返回任何 state 更新（F5 实测）
    interrupts = [i for t in graph.get_state(config).tasks for i in (t.interrupts or [])]
    assert interrupts and interrupts[0].value["kind"] == "wording"
    assert interrupts[0].value["options"] == ["continue", "stop"]

    resumed = asyncio.run(graph.ainvoke(Command(resume={"choice": "continue"}), config))

    assert resumed["gate"]["approval_choice"] == "continue"
    assert resumed["draft"]["conclusion"].startswith("建议买入")


def test_gate_approval_stop_marks_the_draft_as_degraded() -> None:
    ctx = make_ctx(DeepFake(synthesis_script=[{"conclusion": "建议买入并长期持有。", "citations": []}]))
    graph = build_deep_graph(ctx=ctx, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "hitl-2"}}
    asyncio.run(graph.ainvoke({"run_id": "hitl-2", "brief": brief().to_payload()}, config))

    stopped = asyncio.run(graph.ainvoke(Command(resume={"choice": "stop"}), config))

    assert stopped["gate"]["decision"] == "degrade"
    assert stopped["draft"]["note"] and "停止" in stopped["draft"]["note"]


def test_budget_limit_triggers_approval_instead_of_endless_spending() -> None:
    ctx = make_ctx(limits=Limits(llm_calls=2, tokens=10**9, seconds=999.0, cost_usd=9.0))
    graph = build_deep_graph(ctx=ctx, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "budget-1"}}

    asyncio.run(graph.ainvoke({"run_id": "budget-1", "brief": brief().to_payload()}, config))

    interrupts = [i for t in graph.get_state(config).tasks for i in (t.interrupts or [])]
    assert interrupts and interrupts[0].value["kind"] == "budget"


def test_run_state_is_json_only() -> None:
    """状态里只许有纯 JSON——checkpointer 序列化不了 dataclass（真库会直接拒收）。"""
    _, _, state = run_graph(make_ctx())

    json.dumps(state, ensure_ascii=False)  # 不抛即达标
    assert isinstance(state["role_results"][0], dict)
    assert isinstance(state["budget"], dict) and "llm_calls" in state["budget"]


# ── E4 L2：节点级墙钟兜底（命中即整跑失败，不是降级路径）────────────────


class _Hanging(DeepFake):
    """永远不回话的模型——用来触发节点超时。"""

    async def _agenerate(self, *args: Any, **kwargs: Any) -> ChatResult:
        await asyncio.sleep(5)
        raise AssertionError("节点超时该在它之前开火")


@pytest.fixture
def tiny_node_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    """把节点墙钟压到 0.2s（默认 120s 是给真跑留的余量）。"""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "research_node_timeout_seconds", 0.2)
    return 0.2


def test_node_timeout_raises_and_names_the_node(tiny_node_timeout: float) -> None:
    """**兜底不是降级**：LangGraph 的节点超时节点内接不住（实测——捕获 `CancelledError`
    后整图照样抛 `NodeTimeoutError`），所以它只能当「防未知挂点」的最后一道闸。"""
    from langgraph.errors import NodeTimeoutError

    from app.agent.deep.types import ROLES

    ctx = make_ctx(chat=_Hanging())
    graph = build_deep_graph(ctx=ctx, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}}

    with pytest.raises(NodeTimeoutError) as info:
        asyncio.run(
            graph.ainvoke({"run_id": "t1", "brief": brief().to_payload()}, config)
        )

    assert info.value.node in ROLES
    assert info.value.timeout == pytest.approx(tiny_node_timeout)


def test_runner_reports_a_node_timeout_as_a_failed_run(tiny_node_timeout: float) -> None:
    """runner 把 `NodeTimeoutError` 转成**点名节点的**失败文案——通用那句
    `NodeTimeoutError: Node 'x' exceeded…` 太像内部堆栈，用户看不懂。"""
    from app.agent.deep.runner import STATUS_FAILED, astream_deep_run

    async def collect() -> list[Any]:
        return [
            frame
            async for frame in astream_deep_run(
                ctx=make_ctx(chat=_Hanging()),
                checkpointer=InMemorySaver(),
                run_id="t2",
                brief=brief(),
            )
        ]

    frames = asyncio.run(collect())
    errors = [payload["message"] for kind, payload in frames if kind == "error"]
    done = [payload for kind, payload in frames if kind == "done"]

    assert errors and "超时" in errors[0] and "节点" in errors[0]
    assert done and done[-1]["status"] == STATUS_FAILED
