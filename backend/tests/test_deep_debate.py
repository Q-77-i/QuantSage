"""M8b 辩论（`app.agent.deep.debate`）：**≤3 轮硬上限**与**可断言的收敛判据**。

PRD 的验收写的是「辩论严格 ≤3 轮」——所以这里的两条主线是：
  * 轮次推进是纯函数（`should_continue`），**第 3 轮后无条件停**，不看模型脸色；
  * 收敛判据**可断言**（本轮相对上一轮没有新增论据、也没有新增引用），
    不是「感觉收敛了」——永不收敛的假模型必须被硬上限截住。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent.deep.budget import Budget
from app.agent.deep.debate import (
    MAX_ROUNDS_HARD,
    DebateTurn,
    converged,
    debate_prompt,
    parse_turn,
    run_turn,
    should_continue,
)
from app.agent.deep.merge import merge
from app.agent.deep.roles.base import RoleContext
from app.agent.deep.types import Brief, EvidenceRef, RoleResult

EVIDENCE = (
    EvidenceRef(event_id="news:1", day="2026-08-15", title="中报净利下滑"),
    EvidenceRef(event_id="announcement:9", day="2026-08-20", title="股东减持"),
)
KEYS = tuple(e.key for e in EVIDENCE)


def brief() -> Brief:
    return Brief(question="值不值得买", symbols=("600519",), as_of=__import__("datetime").datetime.fromisoformat(
        "2026-09-30T15:00:00+08:00"))


def ctx(chat: Any = None) -> RoleContext:
    return RoleContext(brief=brief(), budget=Budget(), chat=chat, thinking=False)


def _fake(text: str) -> Any:
    return FakeMessagesListChatModel(responses=[AIMessage(content=text)])


def turn(round_: int, side: str, args: tuple[str, ...], cites: tuple[str, ...]) -> DebateTurn:
    return DebateTurn(round=round_, side=side, stance="s", arguments=args, citations=cites)


# ── 解析：只认归并证据里的引用 ──


def test_parse_turn_reads_json_and_drops_unknown_citations() -> None:
    text = (
        '{"stance": "偏空", "arguments": ["中报净利下滑", "减持压制估值"],'
        ' "citations": ["news:1|2026-08-15", "编造:1|2026-01-01"]}'
    )

    parsed = parse_turn(text, allowed_keys=KEYS)

    assert parsed["stance"] == "偏空"
    assert parsed["arguments"] == ("中报净利下滑", "减持压制估值")
    # 不在证据集里的引用一律丢掉并留痕——引用不能凭空存在（claim 闸门同一条纪律）
    assert parsed["citations"] == ("news:1|2026-08-15",)
    assert parsed["dropped_citations"] == ("编造:1|2026-01-01",)


def test_parse_turn_tolerates_fences_and_prose() -> None:
    text = '我的判断是：```json\n{"stance": "偏多", "arguments": ["a"], "citations": []}\n```'
    assert parse_turn(text, allowed_keys=KEYS)["stance"] == "偏多"


def test_parse_turn_on_garbage_returns_empty_structure() -> None:
    parsed = parse_turn("我说不清楚", allowed_keys=KEYS)

    assert parsed["stance"] == "" and parsed["arguments"] == () and parsed["citations"] == ()


# ── 收敛判据：本轮相对上一轮有没有新增 ──


def test_converged_when_nothing_new() -> None:
    turns = [
        turn(1, "bull", ("净利仍居行业首位",), ("news:1|2026-08-15",)),
        turn(1, "bear", ("净利下滑",), ("news:1|2026-08-15",)),
        turn(2, "bull", ("净利仍居行业首位",), ("news:1|2026-08-15",)),
        turn(2, "bear", ("净利下滑",), ("news:1|2026-08-15",)),
    ]

    assert converged(turns) is True


def test_not_converged_when_new_citation_appears() -> None:
    turns = [
        turn(1, "bull", ("a",), ("news:1|2026-08-15",)),
        turn(1, "bear", ("b",), ("news:1|2026-08-15",)),
        turn(2, "bull", ("a",), ("news:1|2026-08-15", "announcement:9|2026-08-20")),
        turn(2, "bear", ("b",), ("news:1|2026-08-15",)),
    ]

    assert converged(turns) is False


def test_not_converged_when_new_argument_appears() -> None:
    turns = [
        turn(1, "bull", ("a",), ()),
        turn(1, "bear", ("b",), ()),
        turn(2, "bull", ("a",), ()),
        turn(2, "bear", ("b", "b2"), ()),
    ]

    assert converged(turns) is False


def test_converged_needs_two_full_rounds() -> None:
    assert converged([turn(1, "bull", ("a",), ())]) is False


# ── 轮次推进：硬上限不看模型脸色 ──


def test_should_continue_stops_at_hard_cap() -> None:
    never = [turn(r, s, (f"arg{r}{s}",), (f"k{r}{s}",)) for r in (1, 2, 3) for s in ("bull", "bear")]

    # 第 3 轮结束（round=3）后**无条件停**——即便模型每轮都在产出新论据
    assert should_continue(round_=3, max_rounds=3, turns=tuple(never)) is False


def test_should_continue_respects_configured_rounds_and_convergence() -> None:
    fresh = (turn(1, "bull", ("a",), ()), turn(1, "bear", ("b",), ()))

    assert should_continue(round_=1, max_rounds=2, turns=fresh) is True
    assert should_continue(round_=2, max_rounds=2, turns=fresh) is False

    settled = (*fresh, turn(2, "bull", ("a",), ()), turn(2, "bear", ("b",), ()))
    assert should_continue(round_=2, max_rounds=3, turns=settled) is False  # 收敛就提前停


def test_hard_cap_is_three_no_matter_what_config_says() -> None:
    assert MAX_ROUNDS_HARD == 3
    assert should_continue(round_=3, max_rounds=99, turns=()) is False


# ── prompt 与一次发言 ──


def test_prompt_carries_evidence_keys_and_the_max_length_rule() -> None:
    prompt = debate_prompt("bull", round_=1, brief=brief(), evidence=EVIDENCE, transcript=())

    for key in KEYS:
        assert key in prompt
    assert "bull" in prompt.lower() or "多方" in prompt
    assert "JSON" in prompt


def test_prompt_second_round_includes_previous_turns() -> None:
    turns = (turn(1, "bear", ("减持压制估值",), ("announcement:9|2026-08-20",)),)

    prompt = debate_prompt("bull", round_=2, brief=brief(), evidence=EVIDENCE, transcript=turns)

    assert "减持压制估值" in prompt and "第 2 轮" in prompt


def test_run_turn_returns_turn_and_records_budget() -> None:
    ctx_ = ctx(_fake('{"stance": "偏多", "arguments": ["估值回到中枢"], "citations": ["news:1|2026-08-15"]}'))

    result = asyncio.run(run_turn(ctx_, side="bull", round_=1, evidence=EVIDENCE, transcript=()))

    assert result.turn.stance == "偏多" and result.turn.citations == ("news:1|2026-08-15",)
    assert ctx_.budget.llm_calls == 1
    assert result.error is None


def test_run_turn_degrades_on_model_failure() -> None:
    class _Boom(FakeMessagesListChatModel):
        async def _agenerate(self, *a: Any, **k: Any) -> Any:  # type: ignore[override]
            raise RuntimeError("上游 500")

    result = asyncio.run(
        run_turn(ctx(_Boom(responses=[])), side="bear", round_=1, evidence=EVIDENCE, transcript=())
    )

    assert result.error and "RuntimeError" in result.error
    assert result.turn.arguments == ()  # 降级成空发言，但轮次照记（不让辩论无限等下去）


def test_merged_evidence_keys_are_what_the_debate_may_cite() -> None:
    """辩论能引用的键 = 归并后的证据集（M8a 的产出直接喂进来）。"""
    merged = merge([RoleResult("news", evidence=EVIDENCE)])

    prompt = debate_prompt("bull", round_=1, brief=brief(), evidence=merged.evidence, transcript=())

    assert "news:1|2026-08-15" in prompt and "announcement:9|2026-08-20" in prompt
