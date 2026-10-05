"""T4 引擎单测：次 bar 开盘成交 / 无前视 / 权益曲线 / 停牌与尾盘边界 / 拒单。

关键性质「无前视」在这里用**结构性断言**验证：策略拿到的 history 长度恒为 index+1，
且最后一根就是当前 bar——策略在物理上无法触及未来数据。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.backtest import engine as engine_module
from app.backtest.costs import CostModel
from app.backtest.engine import BacktestConfig, run_backtest
from app.backtest.types import BarContext, BacktestError, Mode, Side, Signal
from tests.conftest import make_backtest_dir, trading_days

START = date(2026, 8, 3)


class ScriptedStrategy:
    """按 bar 下标脚本化下单，用于精确断言引擎行为。"""

    name = "scripted"

    def __init__(self, actions: dict[int, list[Signal]]) -> None:
        self._actions = actions
        self.seen_lengths: list[int] = []
        self.last_bar_matches: list[bool] = []

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        self.seen_lengths.append(len(ctx.history))
        self.last_bar_matches.append(ctx.history[-1] == ctx.bar)
        return list(self._actions.get(ctx.index, []))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    days = trading_days(START, 6)
    bars = [
        {"trade_date": day, "open": 100.0 + i, "close": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i}
        for i, day in enumerate(days)
    ]
    return make_backtest_dir(tmp_path, bars, events=[])


def run(data_dir: Path, strategy: ScriptedStrategy, **overrides) -> object:
    overrides.setdefault("costs", CostModel().without_slippage())
    config = BacktestConfig(
        symbol="600519", strategy=strategy.name, data_dir=data_dir, **overrides
    )
    engine_module.build_strategy = lambda name, params=None: strategy  # type: ignore[assignment]
    return run_backtest(config)


@pytest.fixture(autouse=True)
def _restore_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """确保每个用例用完把注册表还原（避免污染其他用例）。"""
    from app.backtest.strategies import build_strategy as real

    yield
    engine_module.build_strategy = real  # type: ignore[assignment]


def test_signal_at_close_fills_at_next_bar_open(data_dir: Path) -> None:
    strategy = ScriptedStrategy({1: [Signal(Side.BUY, reason="t")]})
    result = run(data_dir, strategy)

    assert len(result.fills) == 1
    fill = result.fills[0]
    days = [b.trade_date for b in result.bars]
    assert fill.trade_date == days[2]  # bar 1 收盘出信号 → bar 2 开盘成交
    assert fill.price == result.bars[2].open


def test_strategy_never_sees_future_bars(data_dir: Path) -> None:
    strategy = ScriptedStrategy({})
    result = run(data_dir, strategy)

    assert strategy.seen_lengths == list(range(1, len(result.bars) + 1))
    assert all(strategy.last_bar_matches)


def test_equity_curve_has_one_point_per_bar_starting_at_initial_cash(data_dir: Path) -> None:
    strategy = ScriptedStrategy({})
    result = run(data_dir, strategy, initial_cash=500_000.0)

    assert len(result.equity_curve) == len(result.bars)
    assert result.equity_curve[0].equity == pytest.approx(500_000.0)
    assert result.final_equity == pytest.approx(500_000.0)


def test_signal_on_last_bar_is_dropped(data_dir: Path) -> None:
    strategy = ScriptedStrategy({5: [Signal(Side.BUY, reason="尾盘")]})
    result = run(data_dir, strategy)

    assert result.fills == ()
    assert len(result.dropped_signals) == 1
    assert "最后一根" in result.dropped_signals[0].reason


def test_open_position_is_marked_at_last_close(data_dir: Path) -> None:
    strategy = ScriptedStrategy({0: [Signal(Side.BUY, reason="t")]})
    result = run(data_dir, strategy)

    assert not result.open_position.is_flat
    last = result.bars[-1]
    expected = result.equity_curve[-1].cash + result.open_position.shares * last.close
    assert result.final_equity == pytest.approx(expected)
    assert result.trades == ()  # 未平仓不进 trades


def test_trade_pnl_matches_hand_calculation(data_dir: Path) -> None:
    strategy = ScriptedStrategy(
        {0: [Signal(Side.BUY, reason="入场")], 3: [Signal(Side.SELL, reason="出场")]}
    )
    result = run(data_dir, strategy, initial_cash=10_000_000.0, costs=CostModel().without_slippage())

    assert len(result.trades) == 1
    trade = result.trades[0]
    buy, sell = result.fills[0], result.fills[1]
    expected = (sell.price * sell.qty - sell.fees) - (buy.price * buy.qty + buy.fees)
    assert trade.pnl == pytest.approx(expected)
    assert trade.entry_date == result.bars[1].trade_date
    assert trade.exit_date == result.bars[4].trade_date
    assert trade.hold_bars == 3


def test_rejected_signal_is_recorded_not_raised(data_dir: Path) -> None:
    """空仓卖出被 broker 拒单 → 记入 dropped_signals，不抛异常。"""
    strategy = ScriptedStrategy({0: [Signal(Side.SELL, reason="空仓卖")]})
    result = run(data_dir, strategy)

    assert result.fills == ()
    assert len(result.dropped_signals) == 1
    assert "拒单" in result.dropped_signals[0].reason


def test_suspended_bar_defers_fill_then_drops(tmp_path: Path) -> None:
    """停牌期间无法按开盘价撮合：顺延，超过上限则丢弃（样本内不触发的分支）。"""
    days = trading_days(START, 12)
    bars = [
        {"trade_date": day, "open": 100.0, "close": 100.0, "is_suspended": i >= 2}
        for i, day in enumerate(days)
    ]
    data_dir = make_backtest_dir(tmp_path, bars, events=[])

    strategy = ScriptedStrategy({1: [Signal(Side.BUY, reason="t")]})
    result = run(data_dir, strategy)

    assert result.fills == ()
    assert len(result.dropped_signals) == 1
    assert "停牌" in result.dropped_signals[0].reason


def test_suspended_bar_defers_but_fills_when_trading_resumes(tmp_path: Path) -> None:
    days = trading_days(START, 8)
    bars = [
        {"trade_date": day, "open": 100.0 + i, "close": 100.0 + i, "is_suspended": i in (2, 3)}
        for i, day in enumerate(days)
    ]
    data_dir = make_backtest_dir(tmp_path, bars, events=[])

    strategy = ScriptedStrategy({1: [Signal(Side.BUY, reason="t")]})
    result = run(data_dir, strategy)

    assert len(result.fills) == 1
    assert result.fills[0].trade_date == days[4]  # 停牌 2、3 顺延到 4


def test_empty_window_raises_actionable_error(tmp_path: Path) -> None:
    data_dir = make_backtest_dir(
        tmp_path, [{"trade_date": START, "open": 100.0, "close": 100.0}], events=[]
    )
    config = BacktestConfig(
        symbol="600519", strategy="ma_cross", start=date(2030, 1, 1), data_dir=data_dir
    )
    with pytest.raises(BacktestError, match="download_bars"):
        run_backtest(config)


def test_unknown_strategy_raises_actionable_error(data_dir: Path) -> None:
    config = BacktestConfig(symbol="600519", strategy="nope", data_dir=data_dir)
    with pytest.raises(BacktestError, match="ma_cross"):
        run_backtest(config)


def test_fees_and_slippage_toggle_changes_final_equity(data_dir: Path) -> None:
    """验收判据②：成本开关对结果有可见影响（引擎层面）。"""
    strategy_actions = {0: [Signal(Side.BUY, reason="t")], 4: [Signal(Side.SELL, reason="t")]}

    def run_with(costs: CostModel) -> float:
        engine_module.build_strategy = lambda name, params=None: ScriptedStrategy(strategy_actions)  # type: ignore[assignment]
        return run_backtest(
            BacktestConfig(
                symbol="600519", strategy="x", costs=costs, initial_cash=1_000_000.0, data_dir=data_dir
            )
        ).final_equity

    with_costs = run_with(CostModel())
    without = run_with(CostModel.disabled())
    without_slippage = run_with(CostModel().without_slippage())

    assert without > with_costs
    assert without > without_slippage > with_costs


def test_pit_mode_is_recorded_in_result(data_dir: Path) -> None:
    strategy = ScriptedStrategy({})
    result = run(data_dir, strategy, pit_mode=Mode.NON_PIT)
    assert result.cutoff_field == "event_time"
    assert result.config.pit_mode is Mode.NON_PIT
