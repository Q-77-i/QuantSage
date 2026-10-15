"""M8b 深研报组装（`app.report.deep`）：**接 M7 容器，过同一道 claim 闸门**。

判据三条：
  * 组装出来的正文能过 `validate_claims`（事实块自证、推断块不带 numbers）；
  * 数字路径**真的指向正文里的值**——把角色的一个数字改掉，闸门必须当场拒收
    （不然「可复算」只是句口号）；
  * 降级与缺口**进块**，不是只写在日志里。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.agent.deep.budget import Budget
from app.agent.deep.debate import DebateTurn
from app.agent.deep.merge import merge
from app.agent.deep.risk_gate import GateDecision, GateResult
from app.agent.deep.synthesize import Draft
from app.agent.deep.types import Brief, EvidenceRef, RoleResult
from app.report.builder import ClaimError, validate_claims
from app.report.deep import REPORT_KIND, assemble_deep_report, build_deep_body, run_facts_of

BRIEF = Brief(
    question="值不值得买",
    symbols=("600519",),
    as_of=datetime.fromisoformat("2026-09-30T15:00:00+08:00"),
)
EVIDENCE = (
    EvidenceRef(event_id="news:1", day="2026-08-15", title="中报净利下滑", event_time="2026-08-15T10:11:51+08:00"),
    EvidenceRef(event_id="announcement:9", day="2026-08-20", title="股东减持"),
)
TURNS = (
    DebateTurn(round=1, side="bull", stance="偏多", arguments=("估值回落",), citations=("news:1|2026-08-15",)),
    DebateTurn(
        round=1, side="bear", stance="偏空", arguments=("减持压制",),
        citations=("news:1|2026-08-15", "announcement:9|2026-08-20"),
    ),
)


def merged_fixture() -> object:
    return merge([
        RoleResult(
            "market", findings=("区间收益 -11.75%",), numbers={"区间收益": "-11.75%"},
            model="deepseek/deepseek-flash",
        ),
        RoleResult("news", evidence=EVIDENCE, findings=("中报净利下滑",), model="deepseek/deepseek-flash"),
        RoleResult("policy", errors=("该时点之前没有可见的政策事件",)),
    ])


def build(*, draft: Draft | None = None, turns=TURNS, merged=None, rounds: int = 2):
    merged = merged or merged_fixture()
    facts = run_facts_of(
        run_id="run-1", status="done", brief=BRIEF, merged=merged, rounds=rounds,
        budget=Budget(), models={"fast": "deepseek/deepseek-flash", "deep": "deepseek/deepseek-v4-pro"},
        thinking=False,
    )
    body = build_deep_body(
        brief=BRIEF, facts=facts, merged=merged, turns=turns,
        draft=draft or Draft(conclusion="多空分歧集中在需求端。", model="deepseek/deepseek-v4-pro"),
        gate=GateResult(GateDecision.PASS),
    )
    return assemble_deep_report(
        body, merged=merged, turns=turns,
        draft=draft or Draft(conclusion="多空分歧集中在需求端。", model="deepseek/deepseek-v4-pro"),
    )


def test_report_passes_the_same_claim_gate_as_m7() -> None:
    report = build()

    validate_claims(report)  # 不抛即达标（组装时已经过一次，这里是回归）
    assert report["kind"] == REPORT_KIND


def test_blocks_cover_roles_debate_conclusion_and_run_summary() -> None:
    ids = [block["id"] for block in build()["blocks"]]

    assert ids[:3] == ["overview", "role_market", "role_news"]
    assert "debate" in ids and "conclusion" in ids and "run_summary" in ids
    assert "role_policy" not in ids  # 空手而归的角色不单独成块，它在 gaps 里


def test_gap_block_appears_only_when_there_are_gaps() -> None:
    report = build()

    gaps = next(block for block in report["blocks"] if block["id"] == "gaps")
    assert "政策面" in gaps["text"]
    assert gaps["numbers"] == {"run.degraded_count": 1}

    # 无缺口的前提是**每个角色都有解读要点**（归并层会给「只取数没解读」的角色记一条缺口）
    quiet = merge([RoleResult("market", findings=("区间收益 -11.75%",), numbers={"区间收益": "-11.75%"})])
    clean = build(turns=(), merged=quiet)
    assert clean["gaps"] == [] and all(block["id"] != "gaps" for block in clean["blocks"])


def test_fact_block_number_paths_point_at_real_values() -> None:
    """**可复算**不是口号：块里的路径必须真的取到那个值。"""
    block = next(b for b in build()["blocks"] if b["id"] == "role_market")
    path, expected = next(iter(block["numbers"].items()))

    assert path == "facts.market.区间收益" and expected == "-11.75%"


def test_gate_rejects_when_the_figure_does_not_match_the_text() -> None:
    """把正文里的数字改掉、块里还是旧值 ⇒ 闸门当场拒收（「值要对得上」这条真的在跑）。"""
    merged = merged_fixture()
    report = build(merged=merged)

    tampered = {**report, "facts": {"market": {"区间收益": "-99.99%"}}}
    with pytest.raises(ClaimError):
        validate_claims(tampered)


def test_dot_in_label_is_sanitized_so_the_path_still_resolves() -> None:
    """标签里带点时路径会被切错段——建表与引用共用同一个转义函数，两边不漂移。"""
    merged = merge([RoleResult("market", numbers={"MA.20": "1273.42 元"})])

    report = build(turns=(), merged=merged)

    block = next(b for b in report["blocks"] if b["id"] == "role_market")
    assert "facts.market.MA·20" in block["numbers"]
    validate_claims(report)


def test_inference_blocks_never_carry_numbers() -> None:
    report = build()

    for block in report["blocks"]:
        if block["kind"] == "inference":
            assert not block.get("numbers")


def test_dropped_citations_are_surfaced_not_silently_dropped() -> None:
    draft = Draft(conclusion="结论", model="m", dropped_citations=("编造:1|2026-01-01",))

    report = build(draft=draft)

    block = next(b for b in report["blocks"] if b["id"] == "dropped_citations")
    assert "编造:1|2026-01-01" in block["text"]


def test_debate_block_falls_back_to_rounds_when_nothing_was_cited() -> None:
    """辩论一次引用都没有时不能挂空 evidence（闸门会判「引用了不存在的证据」）。"""
    turns = (DebateTurn(round=1, side="bull", stance="s", arguments=("a",), citations=()),)

    report = build(turns=turns, rounds=1)

    block = next(b for b in report["blocks"] if b["id"] == "debate")
    assert block.get("evidence") is None and block["numbers"] == {"run.debate_rounds": 1}
    validate_claims(report)


def test_report_body_is_plain_json() -> None:
    import json

    json.dumps(build(), ensure_ascii=False)


def test_run_facts_carry_budget_and_models() -> None:
    facts = run_facts_of(
        run_id="r", status="done", brief=BRIEF, merged=merged_fixture(), rounds=3,
        budget=Budget(), models={"fast": "f", "deep": "d"}, thinking=False,
    ).to_payload()

    assert facts["debate_rounds"] == 3 and facts["evidence_count"] == 2
    assert facts["degraded_count"] == 1 and facts["thinking"] is False
