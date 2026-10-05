"""T4 内置策略单测：交叉只触发一次 / 阈值与方向过滤 / 持有 N 天口径 / 事件不重复触发。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.backtest.strategies import available_strategies, build_strategy
from app.backtest.strategies.event_driven import EventDriven
from app.backtest.strategies.ma_cross import MaCross
from app.backtest.types import BacktestError, Bar, BarContext, EventView, Position, Side

CST = ZoneInfo("Asia/Shanghai")
START = date(2026, 1, 5)


def ctx_for(
    closes: list[float],
    position: Position | None = None,
    new_events: tuple[EventView, ...] = (),
    index: int | None = None,
) -> BarContext:
    """用收盘价序列拼一个 BarContext；history 覆盖 [0..index]。"""
    idx = len(closes) - 1 if index is None else index
    bars = tuple(
        Bar("600519", START + timedelta(days=i), c, c, c, c, 1e5) for i, c in enumerate(closes)
    )
    return BarContext(
        bar=bars[idx],
        index=idx,
        history=bars[: idx + 1],
        position=position or Position(),
        cash=0.0,
        equity=0.0,
        events=new_events,
        new_events=new_events,
    )


def event(event_id: str = "e1", direction: str | None = "bullish", score: float | None = 60.0) -> EventView:
    ts = datetime(2026, 8, 3, 10, 0, tzinfo=CST)
    return EventView(event_id, "600519", "t", ts, ts, direction, score)


# ── ma_cross ────────────────────────────────────────────────────────────────


def test_ma_cross_returns_nothing_before_enough_history() -> None:
    strategy = MaCross(fast=5, slow=20)
    assert strategy.on_bar(ctx_for([100.0] * 20)) == []  # 需 slow+1 = 21 根
    assert strategy.on_bar(ctx_for([100.0] * 21)) == []  # 平盘无交叉


def test_ma_cross_buys_on_golden_cross() -> None:
    strategy = MaCross(fast=3, slow=5)
    # 先跌再拉，制造上穿
    closes = [100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 110.0]
    signals = strategy.on_bar(ctx_for(closes))
    assert [s.side for s in signals] == [Side.BUY]


def test_ma_cross_does_not_repeat_signal_on_following_bar() -> None:
    """金叉后在同侧继续走高，不应再次触发（无状态实现的关键性质）。"""
    strategy = MaCross(fast=3, slow=5)
    closes = [100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 110.0, 111.0, 112.0]
    assert strategy.on_bar(ctx_for(closes)) == []  # 当前根仍是快线在上，但上一根已在上面


def test_ma_cross_sells_on_death_cross_when_holding() -> None:
    strategy = MaCross(fast=3, slow=5)
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 90.0]
    holding = Position(shares=100, entry_price=100.0, entry_index=0, entry_date=START)
    signals = strategy.on_bar(ctx_for(closes, position=holding))
    assert [s.side for s in signals] == [Side.SELL]


def test_ma_cross_never_buys_while_holding() -> None:
    """死叉被拒单后又金叉时必须不重复入场。"""
    strategy = MaCross(fast=3, slow=5)
    closes = [100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 110.0]
    holding = Position(shares=100, entry_price=100.0, entry_index=0, entry_date=START)
    assert strategy.on_bar(ctx_for(closes, position=holding)) == []


def test_ma_cross_never_sells_when_flat() -> None:
    strategy = MaCross(fast=3, slow=5)
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 90.0]
    assert strategy.on_bar(ctx_for(closes)) == []


# ── event_driven ────────────────────────────────────────────────────────────


def test_event_driven_buys_on_qualifying_bullish_event() -> None:
    strategy = EventDriven(min_score=50.0)
    signals = strategy.on_bar(ctx_for([100.0] * 5, new_events=(event(score=60.0),)))
    assert [s.side for s in signals] == [Side.BUY]
    assert signals[0].event_id == "e1"


@pytest.mark.parametrize(
    ("direction", "score"),
    [
        ("bearish", 90.0),  # 方向不符
        ("neutral", 90.0),  # 方向不符
        (None, 90.0),  # 无方向语义（如实测的高管人事类）
        ("bullish", 49.99),  # 分数不足
        ("bullish", None),  # 分数缺失
    ],
)
def test_event_driven_skips_non_qualifying_events(direction: str | None, score: float | None) -> None:
    strategy = EventDriven(min_score=50.0)
    signals = strategy.on_bar(ctx_for([100.0] * 5, new_events=(event(direction=direction, score=score),)))
    assert signals == []


def test_event_driven_threshold_is_inclusive() -> None:
    strategy = EventDriven(min_score=50.0)
    signals = strategy.on_bar(ctx_for([100.0] * 5, new_events=(event(score=50.0),)))
    assert [s.side for s in signals] == [Side.BUY]


def test_event_driven_ignores_new_events_while_holding() -> None:
    """持有期内不加仓。"""
    strategy = EventDriven(min_score=50.0, hold_days=5)
    holding = Position(shares=100, entry_price=100.0, entry_index=0, entry_date=START)
    signals = strategy.on_bar(
        ctx_for([100.0] * 5, position=holding, new_events=(event(score=90.0),), index=3)
    )
    assert signals == []


def test_event_driven_sells_after_holding_n_days() -> None:
    """持有 N 天口径：成交日算第 1 天，第 N 天收盘出信号。

    entry_index=0（成交日）→ index=4 时 held = 5 → 达到 hold_days=5，当根收盘卖出。
    """
    strategy = EventDriven(min_score=50.0, hold_days=5)
    holding = Position(shares=100, entry_price=100.0, entry_index=0, entry_date=START)

    assert strategy.on_bar(ctx_for([100.0] * 5, position=holding, index=3)) == []  # held=4
    signals = strategy.on_bar(ctx_for([100.0] * 5, position=holding, index=4))  # held=5
    assert [s.side for s in signals] == [Side.SELL]


def test_event_driven_holds_exactly_five_trading_days() -> None:
    """N=5：成交于 bar 0 开盘 → bar 4 收盘出信号 → bar 5 开盘平仓，暴露 5 个交易日。

    注：本用例给的是**静态持仓**（引擎外），故达到持有期后每根 bar 都会再发卖出信号；
    真实引擎里 bar 5 开盘成交后持仓已平，不会重复。这里只断言首次触发的那根。
    """
    strategy = EventDriven(hold_days=5)
    holding = Position(shares=100, entry_price=100.0, entry_index=0, entry_date=START)
    first_sell_bar = next(
        i
        for i in range(10)
        if strategy.on_bar(ctx_for([100.0] * 10, position=holding, index=i))
    )
    assert first_sell_bar == 4  # 信号 bar（held = 4 - 0 + 1 = 5）
    assert first_sell_bar + 1 == 5  # 平仓成交 bar


def test_event_driven_same_event_fires_only_once() -> None:
    """new_events 是增量差集：同一事件不会在后续 bar 重复出现。"""
    strategy = EventDriven()
    ev = event("news:999", score=80.0)
    first = strategy.on_bar(ctx_for([100.0] * 5, new_events=(ev,)))
    assert [s.side for s in first] == [Side.BUY]
    later = strategy.on_bar(ctx_for([100.0] * 5, new_events=()))
    assert later == []


def test_event_driven_reason_carries_event_id_and_score() -> None:
    strategy = EventDriven()
    signal = strategy.on_bar(ctx_for([100.0] * 5, new_events=(event("news:7", score=66.6),)))[0]
    assert "news:7" in signal.reason
    assert "66.6" in signal.reason


# ── 注册表 ──────────────────────────────────────────────────────────────────


def test_registry_exposes_both_builtin_strategies() -> None:
    assert set(available_strategies()) == {"ma_cross", "event_driven"}


def test_registry_builds_with_params() -> None:
    strategy = build_strategy("event_driven", {"min_score": 70, "hold_days": 3})
    assert isinstance(strategy, EventDriven)
    assert strategy.min_score == 70.0
    assert strategy.hold_days == 3

    ma = build_strategy("ma_cross", {"fast": 10, "slow": 30})
    assert isinstance(ma, MaCross)
    assert (ma.fast, ma.slow) == (10, 30)


def test_registry_rejects_unknown_strategy_with_actionable_message() -> None:
    with pytest.raises(BacktestError, match="ma_cross"):
        build_strategy("nope")


def test_strategy_name_attribute_matches_registry_key() -> None:
    for name in available_strategies():
        assert build_strategy(name).name == name
