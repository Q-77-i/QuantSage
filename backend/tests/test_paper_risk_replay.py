"""E5 风控重放用例：缩量、止损自动退出、回撤禁买、确定性、沙箱信封往返。

与纯函数用例分开：那边验判据，这里验**接线**——风控卖出是否真的 T+1 成交、
缩量后的股数是否在成交时仍是硬上限、审计行是否随重放稳定复现。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from app.backtest.costs import CostModel
from app.backtest.types import BarContext, Side, Signal
from app.data import calendar as cal
from app.paper.replay import replay
from app.paper.store import result_from_payload, result_to_payload
from app.paper.types import DecisionStatus, PaperConfig, RiskLimits
from tests.conftest import make_backtest_dir, write_bars_parquet

START = date(2026, 8, 3)
SYMBOL = "600519"


class Scripted:
    name = "scripted"

    def __init__(self, actions: dict[int, list[Signal]]) -> None:
        self._actions = actions

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        return list(self._actions.get(ctx.index, []))


def sessions(count: int) -> list[date]:
    days = cal.sessions(START, START + timedelta(days=count * 3 + 10))
    assert len(days) >= count
    return days[:count]


def rows(days: list[date], prices: list[float]) -> list[dict]:
    return [
        {
            "trade_date": day,
            "open": price,
            "close": price,
            "high": price + 1,
            "low": price - 1,
        }
        for day, price in zip(days, prices, strict=True)
    ]


def build_dir(tmp_path: Path, bars: list[dict]) -> Path:
    root = make_backtest_dir(tmp_path, bars, events=[], symbol=SYMBOL)
    write_bars_parquet(root / "bars", SYMBOL, bars, adjust="raw")
    return root


def config(end: date, risk: RiskLimits | None) -> PaperConfig:
    return PaperConfig(
        initial_cash=1_000_000.0,
        symbols=(SYMBOL,),
        strategy="scripted",
        start=START,
        end=end,
        costs=CostModel(),
        risk=risk,
    )


def risk_limits(**over: float) -> RiskLimits:
    base = {
        "max_gross_exposure": 100.0,
        "max_position": 100.0,
        "stop_loss": 100.0,
        "max_drawdown": 100.0,
    }
    base.update(over)
    return RiskLimits(**base)  # type: ignore[arg-type]


# ── 风控关闭 ────────────────────────────────────────────────


def test_risk_off_keeps_m6_behaviour_and_writes_no_audit(tmp_path: Path) -> None:
    days = sessions(4)
    data_dir = build_dir(tmp_path, rows(days, [100.0, 100.0, 100.0, 100.0]))
    outcome = replay(
        config(days[-1], None),
        auto=DecisionStatus.APPROVED,
        strategy=Scripted({0: [Signal(Side.BUY, reason="进场")]}),
        data_dir=data_dir,
    )
    assert outcome.risk_events == ()
    assert outcome.risk_state is None
    assert outcome.decisions[0].origin == "strategy"


# ── 缩量 ────────────────────────────────────────────────────


def test_capped_buy_uses_the_capped_qty_at_fill_time(tmp_path: Path) -> None:
    """权益 100 万、单标的上限 30% ⇒ 决策日缩量到 2,900 股；次日成交仍是 2,900 股。

    这条是「缩量不是只改一个展示数字」的证明：成交走 `Broker` 的 `qty_cap`，跳空低开
    也不会把仓位买回上限以上。
    """
    days = sessions(4)
    data_dir = build_dir(tmp_path, rows(days, [100.0, 100.0, 100.0, 100.0]))
    outcome = replay(
        config(days[-1], risk_limits(max_position=30.0)),
        auto=DecisionStatus.APPROVED,
        strategy=Scripted({0: [Signal(Side.BUY, reason="进场")]}),
        data_dir=data_dir,
    )
    decision = outcome.decisions[0]
    assert decision.est_qty == 2900
    assert decision.fill is not None
    assert decision.fill.qty == 2900
    assert outcome.state.positions[SYMBOL].shares == 2900

    events = [event for event in outcome.risk_events if event.kind == "proposal"]
    assert len(events) == 1
    event = events[0]
    assert event.verdict == "cap"
    assert event.gate == "concentration"
    assert event.metrics["requested_qty"] == 9900
    assert event.metrics["final_qty"] == 2900


def test_capped_buy_does_not_buy_more_on_a_gap_down(tmp_path: Path) -> None:
    """决策日收盘 100、次日开盘 80：按 `affordable_qty` 本可以买更多，但风控上限是硬顶。"""
    days = sessions(4)
    data_dir = build_dir(tmp_path, rows(days, [100.0, 80.0, 80.0, 80.0]))
    outcome = replay(
        config(days[-1], risk_limits(max_position=30.0)),
        auto=DecisionStatus.APPROVED,
        strategy=Scripted({0: [Signal(Side.BUY, reason="进场")]}),
        data_dir=data_dir,
    )
    decision = outcome.decisions[0]
    assert decision.fill is not None
    assert decision.fill.qty == 2900
    assert outcome.state.positions[SYMBOL].shares == 2900


# ── 止损 ────────────────────────────────────────────────────


def test_stop_loss_generates_an_approved_sell_that_fills_next_open(tmp_path: Path) -> None:
    days = sessions(6)
    # 第 1 天开盘买入，第 2 天收盘跌到 88（相对成本约 -12%），第 3 天开盘卖出
    data_dir = build_dir(tmp_path, rows(days, [100.0, 100.0, 88.0, 88.0, 88.0, 88.0]))
    outcome = replay(
        config(days[-1], risk_limits(stop_loss=10.0)),
        auto=DecisionStatus.APPROVED,
        strategy=Scripted({0: [Signal(Side.BUY, reason="进场")]}),
        data_dir=data_dir,
    )

    buys = [d for d in outcome.decisions if d.side is Side.BUY]
    exits = [d for d in outcome.decisions if d.origin == "risk"]
    assert len(buys) == 1
    assert len(exits) == 1
    exit_decision = exits[0]
    assert exit_decision.side is Side.SELL
    assert exit_decision.status is DecisionStatus.FILLED
    assert exit_decision.reason.startswith("risk:止损触发")
    assert exit_decision.fill is not None
    assert exit_decision.fill.trade_date == days[3]
    assert buys[0].fill is not None
    assert exit_decision.fill.qty == buys[0].fill.qty

    assert outcome.state.positions == {}
    triggers = [e for e in outcome.risk_events if e.kind == "stop_loss"]
    assert len(triggers) == 1
    trigger = triggers[0]
    assert trigger.verdict == "trigger"
    assert trigger.gate == "stop_loss"
    assert trigger.decision_id == exit_decision.id
    assert trigger.metrics["loss_pct"] is not None
    assert trigger.metrics["loss_pct"] > 10.0


def test_risk_exit_survives_auto_reject(tmp_path: Path) -> None:
    """`run(approve="none")` 只驳策略决策；风控止损仍执行（SPEC §6.4）。"""
    days = sessions(6)
    data_dir = build_dir(tmp_path, rows(days, [100.0, 100.0, 88.0, 88.0, 88.0, 88.0]))
    cfg = config(days[-1], risk_limits(stop_loss=10.0))
    actions = {0: [Signal(Side.BUY, reason="进场")]}
    # 先把买入做成已成交的日志（模拟用户已经批过这一单）
    first = replay(
        cfg,
        auto=DecisionStatus.APPROVED,
        strategy=Scripted(actions),
        data_dir=data_dir,
        through=days[1],
    )
    assert any(decision.fill is not None for decision in first.decisions)

    # 再按「全部驳回」跑完整窗口：风控止损单生来 approved，不受 auto 影响
    outcome = replay(
        cfg,
        decisions=first.decisions,
        auto=DecisionStatus.REJECTED,
        strategy=Scripted(actions),
        data_dir=data_dir,
    )
    exits = [decision for decision in outcome.decisions if decision.origin == "risk"]
    assert len(exits) == 1
    assert exits[0].status is DecisionStatus.FILLED
    assert outcome.state.positions == {}


def test_stop_loss_exit_is_reported_as_risk_exit_not_generated(tmp_path: Path) -> None:
    days = sessions(6)
    data_dir = build_dir(tmp_path, rows(days, [100.0, 100.0, 88.0, 88.0, 88.0, 88.0]))
    outcome = replay(
        config(days[-1], risk_limits(stop_loss=10.0)),
        auto=DecisionStatus.APPROVED,
        strategy=Scripted({0: [Signal(Side.BUY, reason="进场")]}),
        data_dir=data_dir,
    )
    day2 = outcome.days[2]
    assert len(day2.risk_exits) == 1
    assert outcome.days[2].generated == ()  # 风控退出不混进「新生成待审批」


# ── 回撤 ────────────────────────────────────────────────────


def test_drawdown_breach_blocks_new_buys(tmp_path: Path) -> None:
    days = sessions(6)
    # 第 1 天买入；第 2 天收盘跌到 80（权益回撤约 20%）；第 2 天策略卖出、第 3 天成交；
    # 第 4 天再给买入信号——此时已回撤超限，必须被拦截。
    data_dir = build_dir(tmp_path, rows(days, [100.0, 100.0, 80.0, 80.0, 80.0, 80.0]))
    outcome = replay(
        config(days[-1], risk_limits(max_drawdown=10.0)),
        auto=DecisionStatus.APPROVED,
        strategy=Scripted(
            {
                0: [Signal(Side.BUY, reason="进场")],
                2: [Signal(Side.SELL, reason="离场")],
                4: [Signal(Side.BUY, reason="再进场")],
            }
        ),
        data_dir=data_dir,
    )

    blocked = [
        e for e in outcome.risk_events if e.kind == "proposal" and e.verdict == "block"
    ]
    assert len(blocked) == 1
    assert blocked[0].gate == "drawdown"
    assert blocked[0].trade_date == days[4]
    # 第 4 天没有买入决策；最后持仓为空
    assert [d for d in outcome.decisions if d.trade_date == days[4]] == []
    assert outcome.state.positions == {}
    assert outcome.risk_state is not None
    assert outcome.risk_state.blocked_by_drawdown is True

    breaches = [
        e for e in outcome.risk_events if e.kind == "drawdown_state" and e.verdict == "breach"
    ]
    assert len(breaches) == 1
    assert breaches[0].trade_date == days[2]


# ── 确定性与信封 ────────────────────────────────────────────


def test_risk_events_are_deterministic_across_replays(tmp_path: Path) -> None:
    days = sessions(6)
    data_dir = build_dir(tmp_path, rows(days, [100.0, 100.0, 88.0, 88.0, 88.0, 88.0]))
    cfg = config(days[-1], risk_limits(stop_loss=10.0))
    actions = {0: [Signal(Side.BUY, reason="进场")]}
    first = replay(
        cfg, auto=DecisionStatus.APPROVED, strategy=Scripted(actions), data_dir=data_dir
    )
    second = replay(
        cfg, auto=DecisionStatus.APPROVED, strategy=Scripted(actions), data_dir=data_dir
    )
    assert first.risk_events == second.risk_events
    assert first.risk_state == second.risk_state
    assert [d.id for d in first.decisions] == [d.id for d in second.decisions]


def test_result_payload_round_trips_risk_events_and_state(tmp_path: Path) -> None:
    days = sessions(6)
    data_dir = build_dir(tmp_path, rows(days, [100.0, 100.0, 88.0, 88.0, 88.0, 88.0]))
    outcome = replay(
        config(days[-1], risk_limits(stop_loss=10.0)),
        auto=DecisionStatus.APPROVED,
        strategy=Scripted({0: [Signal(Side.BUY, reason="进场")]}),
        data_dir=data_dir,
    )
    restored = result_from_payload(result_to_payload(outcome))
    assert restored.risk_events == outcome.risk_events
    assert restored.risk_state == outcome.risk_state
    assert [d.origin for d in restored.decisions] == [d.origin for d in outcome.decisions]
    assert [len(day.risk_exits) for day in restored.days] == [
        len(day.risk_exits) for day in outcome.days
    ]
