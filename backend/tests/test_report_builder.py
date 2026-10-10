"""M7a 冻结产物组装与 claim 闸门（`app.report.builder`）。

闸门是容器对 M8 的承诺：LLM 写的事实必须**能回链**（证据键存在）或**可复算**（数字路径
在正文里对得上），否则整份报告拒收、不落库。这四条拒收用例就是那道闸门的规格。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.backtest.benchmark import MarketBenchmark
from app.memory.settle import mark_open_trips, pair_trips
from app.paper.types import Decision, PaperConfig
from app.report.builder import (
    ClaimError,
    assemble_report,
    build_facts,
    validate_claims,
)
from app.report.evidence import EvidenceItem
from app.report.narrative import PROMPT_VERSION, Narrative
from app.report.snapshot import canonical_json
from tests.test_memory_settle import buy, sell

CONFIG = PaperConfig(
    initial_cash=100_000.0,
    symbols=("600519",),
    strategy="event_driven",
    start=date(2026, 8, 3),
    end=date(2026, 9, 30),
)
ACCOUNT = {
    "id": "acct-1",
    "name": "银行事件驱动",
    "status": "finished",
    "as_of": date(2026, 9, 30),
    "created_at": "2026-10-10T00:00:00+00:00",
}
SNAPSHOT = {
    "bars": {"shards": [{"name": "a.parquet", "sha256": "x" * 64}], "digest": "d" * 64},
    "events": {"digest": "e" * 64, "days": 1, "last_day": "2026-09-30", "rows": 10},
    "corpus": {"start": "2026-07-07", "end": "2026-09-30"},
    "market_end": "2026-09-30",
}
BENCHMARK = MarketBenchmark(
    levels=(100_000.0, 99_000.0, 98_100.0), total_return=-0.019, excluded=0, sample_days=2,
    avg_samples=5400,
)


def _inputs() -> dict[str, Any]:
    source = {"event_id": "news:1", "event_time": "2026-08-03 09:00:00+08:00"}
    decisions = [
        buy(date(2026, 8, 3), qty=1000, price=10.0, did="d-buy").replace(sources=source),
        sell(date(2026, 8, 10), did="d-sell"),
        buy(date(2026, 9, 30), qty=100, price=50.0, did="d-open"),
    ]
    trips = mark_open_trips(pair_trips(decisions), {"600519": 52.0})
    evidence = {
        "news:1|2026-08-03": EvidenceItem(
            event_id="news:1",
            day=date(2026, 8, 3),
            title="重大合同公告",
            summary="摘要",
            event_time=source["event_time"],
            available_at="2026-08-03 09:10:00+08:00",
            source="xiaoshi-archive",
            original_source="华尔街见闻",
            content_hash="hash-a",
            industries=("银行",),
            direction_norm="bullish",
            found=True,
            decision_ids=("d-buy",),
        )
    }
    equity_rows = [
        {"trade_date": date(2026, 8, 3), "equity": 100_000.0},
        {"trade_date": date(2026, 8, 10), "equity": 100_984.5},
        {"trade_date": date(2026, 9, 30), "equity": 101_084.5},
    ]
    return {
        "account": ACCOUNT,
        "config": CONFIG,
        "decisions": decisions,
        "equity_rows": equity_rows,
        "trips": trips,
        "benchmark": BENCHMARK,
        "snapshot": SNAPSHOT,
        "evidence": evidence,
    }


def test_facts_are_deterministic_and_pass_the_gate() -> None:
    """同一批输入两次组装 → 逐字段相等；生成的事实层自己过闸门。"""
    first = build_facts(**_inputs())
    second = build_facts(**_inputs())

    assert canonical_json(first) == canonical_json(second)
    validate_claims(assemble_report(first, Narrative("综述文本", "deepseek/deepseek-flash")))


def test_facts_carry_metrics_attribution_and_warnings() -> None:
    """正文的关键块：指标、标的级与事件级归因、如实标注（未平仓 / 基准口径）。"""
    facts = build_facts(**_inputs())

    assert facts["metrics"]["total_return"] == pytest.approx(0.010845)
    assert facts["metrics"]["benchmark_return"] == pytest.approx(-0.019)
    assert facts["metrics"]["trade_count"] == 1
    assert facts["account"]["as_of"] == "2026-09-30"
    assert facts["account"]["data_end"] == "2026-09-30"
    assert facts["attribution"]["symbols"][0]["symbol"] == "600519"
    assert facts["attribution"]["direction"][0]["label"] == "利多"
    assert facts["attribution"]["industry"][0]["label"] == "银行"
    assert any("尚未平仓" in w for w in facts["warnings"])
    assert any("全市场等权" in w for w in facts["warnings"])
    assert facts["equity_curve"][-1]["benchmark"] == pytest.approx(98_100.0)


def test_assemble_appends_the_inference_block_with_identity() -> None:
    """综述进正文时带模型与 prompt 版本；缺席时带原因（如实标注，不静默）。"""
    facts = build_facts(**_inputs())
    body = assemble_report(facts, Narrative("一段综述", "deepseek/deepseek-flash"))
    block = body["blocks"][-1]

    assert block["kind"] == "inference" and block["id"] == "narrative"
    assert block["model"] == "deepseek/deepseek-flash"
    assert block["prompt_version"] == PROMPT_VERSION

    absent = assemble_report(facts, Narrative(None, "deepseek/deepseek-flash", note="综述超时"))
    assert absent["blocks"][-1]["text"] is None
    assert absent["blocks"][-1]["note"] == "综述超时"


def test_report_hash_moves_with_the_narrative_only_when_it_changes() -> None:
    """同一份综述 ⇒ 同一个 report_hash（重放一致的落地前提）；换一段就变。"""
    from app.report.snapshot import report_hash

    facts = build_facts(**_inputs())
    same = report_hash(assemble_report(facts, Narrative("一段综述", "m")))
    again = report_hash(assemble_report(build_facts(**_inputs()), Narrative("一段综述", "m")))
    other = report_hash(assemble_report(facts, Narrative("另一段", "m")))

    assert same == again
    assert same != other


# ── M7b：逐笔复盘块 ────────────────────────────────────────


def _review_payload() -> dict[str, Any]:
    return {
        "account": {"id": "acct-1"},
        "as_of": "2026-09-30",
        "data_end": "2026-09-30",
        "summary": {"settled": 1, "open": 1, "unfilled": 1, "saved": 2, "reused": 0, "lessons": 1},
        "settled": [
            {
                "decision_id": "d-buy",
                "symbol": "600519",
                "entry_date": "2026-08-03",
                "exit_date": "2026-08-10",
                "settled": True,
                "pnl": 984.5,
                "return_pct": 0.0984,
                "benchmark_pct": 0.02,
                "alpha_pp": 7.84,
                "window_days": 6,
                "entry_reason": "MA 金叉",
                "exit_reason": "持有到期",
                "direction": "bullish",
                "evidence_key": "news:1|2026-08-03",
                "as_of": "2026-09-30",
                "settled_at": "2026-10-10T12:00:00+00:00",
                "reflection": {"text": "吃到了金叉后的主升段。", "model": "m", "note": None},
                "sources": None,
                "evidence": None,
            }
        ],
        "open": [
            {
                "decision_id": "d-open",
                "symbol": "600519",
                "entry_date": "2026-09-30",
                "exit_date": None,
                "settled": False,
                "pnl": 1995.0,
                "alpha_pp": 18.94,
                "window_days": 1,
                "settled_at": "2026-10-10T12:00:00+00:00",
                "reflection": None,
            }
        ],
        "unfilled": [
            {
                "decision_id": "d-exp",
                "symbol": "600519",
                "trade_date": "2026-09-20",
                "side": "buy",
                "status": "expired",
                "status_label": "未审批过期（未审批不成交）",
                "reject_code": None,
                "reject_reason": None,
                "sources": None,
                "evidence": None,
            }
        ],
    }


def test_review_block_self_certifies_and_drops_run_time_noise() -> None:
    """复盘块靠**计数**自证；墙钟时间与本次运行读数（saved/reused）都不进冻结正文。"""
    facts = build_facts(**_inputs(), review=_review_payload())
    body = assemble_report(facts, Narrative("综述", "m"))

    assert [block["id"] for block in body["blocks"]] == [
        "overview", "performance", "attribution", "review", "narrative",
    ]
    block = body["blocks"][3]
    assert block["kind"] == "fact"
    assert block["numbers"]["review.summary.settled"] == 1
    assert "saved" not in body["review"]["summary"]  # 运行读数不进正文
    assert "reused" not in body["review"]["summary"]
    assert "settled_at" not in body["review"]["settled"][0]
    validate_claims(body)


def test_review_block_ignores_evidence_keys_that_are_not_in_the_body() -> None:
    """复盘项引用的证据键若不在报告的 `evidence` 里，就不往块上挂（挂上去会被闸门拒收）。"""
    review = _review_payload()
    facts = build_facts(**_inputs(), review=review)  # _inputs 的 evidence 只有 news:1|2026-08-03
    block = next(b for b in facts["blocks"] if b["id"] == "review")

    assert block["evidence"] == ["news:1|2026-08-03"]


def test_absent_review_keeps_the_four_block_shape() -> None:
    """没有复盘（记忆降级 / 旧报告）时块清单退回四块，`review` 为 None。"""
    facts = build_facts(**_inputs())
    body = assemble_report(facts, Narrative("综述", "m"))

    assert [block["id"] for block in body["blocks"]] == [
        "overview", "performance", "attribution", "narrative",
    ]
    assert body["review"] is None


# ── 闸门的拒收规格 ─────────────────────────────────────────


def _body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "metrics": {"total_return": 0.02},
        "evidence": [{"event_id": "news:1", "day": "2026-08-03"}],
        "blocks": [{"id": "b1", "kind": "fact", "numbers": {"metrics.total_return": 0.02}}],
    }
    body.update(over)
    return body


def test_fact_layer_survives_an_account_without_any_event_evidence() -> None:
    """纯价量策略（双均线）没有事件证据：归因块靠**回合合计数字**自证，报告照常成立。"""
    inputs = _inputs()
    price_only = [
        buy(date(2026, 8, 3), qty=1000, price=10.0, did="p-buy"),
        sell(date(2026, 8, 10), did="p-sell"),
    ]
    from app.memory.settle import pair_trips

    inputs["decisions"] = price_only
    inputs["trips"] = pair_trips(price_only)
    inputs["evidence"] = {}

    facts = build_facts(**inputs)
    body = assemble_report(facts, Narrative(None, "m", note="未配置"))

    assert facts["evidence"] == []
    assert facts["attribution"]["direction"][0]["label"] == "无事件来源"
    assert facts["attribution"]["industry"] == []
    assert facts["attribution"]["summary"]["trips_realized_pnl"] == pytest.approx(984.50)
    validate_claims(body)


def test_gate_rejects_fact_block_that_cannot_self_certify() -> None:
    with pytest.raises(ClaimError, match="无法自证"):
        validate_claims(_body(blocks=[{"id": "b1", "kind": "fact"}]))


def test_gate_rejects_number_that_does_not_match_the_body() -> None:
    """「可复算」是硬判据：路径对得上但值不一样，同样拒收。"""
    with pytest.raises(ClaimError, match="与正文不一致"):
        validate_claims(_body(blocks=[{"id": "b1", "kind": "fact", "numbers": {"metrics.total_return": 0.99}}]))


def test_gate_rejects_dangling_evidence_key() -> None:
    with pytest.raises(ClaimError, match="不存在的证据"):
        validate_claims(_body(blocks=[{"id": "b1", "kind": "fact", "evidence": ["news:404|2026-01-01"]}]))


def test_gate_rejects_inference_carrying_numbers() -> None:
    with pytest.raises(ClaimError, match="不得携带 numbers"):
        validate_claims(_body(blocks=[{"id": "b1", "kind": "inference", "numbers": {"metrics.total_return": 0.02}}]))


def test_gate_rejects_unknown_block_kind() -> None:
    with pytest.raises(ClaimError, match="非法"):
        validate_claims(_body(blocks=[{"id": "b1", "kind": "hunch", "text": "直觉"}]))


def test_decision_sequence_is_not_mutated_by_building() -> None:
    """组装不改输入（决策是不可变对象、trips 是冻结 dataclass，这条是防回归的手铐）。"""
    inputs = _inputs()
    before = canonical_json([d.id for d in inputs["decisions"]])
    build_facts(**inputs)

    assert canonical_json([d.id for d in inputs["decisions"]]) == before
    assert all(isinstance(d, Decision) for d in inputs["decisions"])
