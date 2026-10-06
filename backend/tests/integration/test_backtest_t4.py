"""T4 验收：真实样本数据上的回测不变量。

先跑 `uv run python scripts/download_bars.py` 与 `scripts/download_events.py`。
默认不收集；用 `uv run pytest -m integration` 触发。

最重要的是 `test_no_lookahead_on_real_data`：对每一笔由事件触发的买入成交，
反查其事件的 `available_at` 是否真的早于「成交前一根 bar 的收盘时刻」——
这是项目护城河在真实数据上的直接证明。
"""

from __future__ import annotations

import pytest

from app.backtest.costs import CostModel
from app.backtest.engine import BacktestConfig, run_backtest
from app.backtest.events import bar_cutoff
from app.backtest.types import Mode, Side
from app.core.config import get_settings
from app.data import duckdb_client as dc

pytestmark = pytest.mark.integration

DATA_DIR = get_settings().data_dir
SYMBOL = "600519"  # 旗舰标的，与 T2 验收同源
SYMBOLS_UNDER_TEST = ("600519", "300750")


def full_run(symbol: str, strategy: str, **overrides):
    rows = dc.bars(symbol, data_dir=DATA_DIR)
    config = BacktestConfig(
        symbol=symbol,
        strategy=strategy,
        start=rows[0]["trade_date"],
        end=rows[-1]["trade_date"],
        data_dir=DATA_DIR,
        **overrides,
    )
    return run_backtest(config)


def event_window_run(symbol: str, mode: Mode, costs: CostModel | None = None):
    events = dc.events(symbol, data_dir=DATA_DIR)
    bars = dc.bars(symbol, start=min(e["event_time"].date() for e in events).isoformat(), data_dir=DATA_DIR)
    return run_backtest(
        BacktestConfig(
            symbol=symbol,
            strategy="event_driven",
            start=bars[0]["trade_date"],
            end=bars[-1]["trade_date"],
            pit_mode=mode,
            costs=costs or CostModel(),
            data_dir=DATA_DIR,
        )
    )


@pytest.mark.parametrize("symbol", SYMBOLS_UNDER_TEST)
@pytest.mark.parametrize("strategy", ["ma_cross", "event_driven"])
def test_both_strategies_trade_on_real_data(symbol: str, strategy: str) -> None:
    """验收判据①：两个策略在真实数据上都能跑通并产生成交。"""
    result = event_window_run(symbol, Mode.PIT) if strategy == "event_driven" else full_run(symbol, strategy)

    assert result.fills, f"{symbol}/{strategy} 没有产生任何成交"
    assert len(result.equity_curve) == len(result.bars)
    assert result.bars[0].symbol == symbol


def test_costs_change_results_on_real_data() -> None:
    """验收判据②：成本开关对结果有可见影响。"""
    with_costs = full_run(SYMBOL, "ma_cross")
    without = full_run(SYMBOL, "ma_cross", costs=CostModel.disabled())

    assert with_costs.total_fees > 0
    assert with_costs.total_slippage_cost > 0
    assert without.final_equity > with_costs.final_equity
    assert without.total_fees == 0.0


def test_pit_and_non_pit_share_events_but_not_cutoffs() -> None:
    """两口径的差别只在**设卡字段**：同一批事件、不同的可见时刻。

    M2a 时这条断言写的是「入场日序列必须不同」——那是**数据性质**而非代码性质：只有当
    「入场驱动事件的可见性跨过某根 bar 的收盘时刻」时才会分岔。M2b 换语料后 600519 恰好
    不分岔（16 条驱动事件里 2 条跨日可见，但都落在持仓期内）。分岔与否交给报告如实呈现
    （`pit_comparison.delta` 可为 0），测试守的是机制。
    """
    pit = event_window_run(SYMBOL, Mode.PIT)
    non_pit = event_window_run(SYMBOL, Mode.NON_PIT)

    assert pit.cutoff_field == "available_at"
    assert non_pit.cutoff_field == "event_time"
    assert set(pit.visible_event_ids) == set(non_pit.visible_event_ids)


def test_pit_never_trades_earlier_than_non_pit() -> None:
    """**不变量**：PIT 的入场只会晚于或等于非 PIT，永远不会更早。

    `available_at >= event_time` ⇒ 按可得时间设卡只会把事件推后，不可能提前。这条与数据无关，
    换任何标的、任何窗口都该成立——比「两者必须不同」强得多（后者只是某些数据下恰好为真）。
    """
    pit = event_window_run(SYMBOL, Mode.PIT)
    non_pit = event_window_run(SYMBOL, Mode.NON_PIT)

    def event_fills(result) -> dict[str, object]:
        return {
            fill.event_id: fill.trade_date
            for fill in result.fills
            if fill.event_id and fill.side.value == "buy"
        }

    pit_by_event = event_fills(pit)
    shared = 0
    for event_id, day in event_fills(non_pit).items():
        if event_id in pit_by_event:
            shared += 1
            assert pit_by_event[event_id] >= day, f"{event_id} 在 PIT 下反而更早成交：{pit_by_event[event_id]} < {day}"
    assert shared > 0, "两种口径没有共同的入场事件，这条断言没有验证到任何东西"


def test_no_lookahead_on_real_data() -> None:
    """护城河不变量：每笔事件买入所依据的事件，在成交前一根 bar 收盘时已可得。"""
    result = event_window_run(SYMBOL, Mode.PIT)
    by_id = {event["event_id"]: event for event in dc.events(SYMBOL, data_dir=DATA_DIR)}
    index_of = {bar.trade_date: i for i, bar in enumerate(result.bars)}

    checked = 0
    for fill in result.fills:
        if fill.side is not Side.BUY or not fill.event_id:
            continue
        bar_index = index_of[fill.trade_date]
        assert bar_index >= 1, "信号必须在成交前一根 bar 产生"
        signal_bar = result.bars[bar_index - 1]
        available_at = by_id[fill.event_id]["available_at"]

        assert available_at <= bar_cutoff(signal_bar.trade_date), (
            f"{fill.event_id} 在信号 bar {signal_bar.trade_date} 收盘时还不可得，属前视"
        )
        checked += 1

    assert checked > 0, "没有校验到任何事件驱动的买入成交"


def test_non_pit_mode_would_be_lookahead() -> None:
    """反向证明：同一批成交放在非 PIT 口径下会被判定为前视（即该模式确实「穿越」了）。"""
    result = event_window_run(SYMBOL, Mode.NON_PIT)
    by_id = {event["event_id"]: event for event in dc.events(SYMBOL, data_dir=DATA_DIR)}
    index_of = {bar.trade_date: i for i, bar in enumerate(result.bars)}

    violations = 0
    for fill in result.fills:
        if fill.side is not Side.BUY or not fill.event_id:
            continue
        signal_bar = result.bars[index_of[fill.trade_date] - 1]
        if by_id[fill.event_id]["available_at"] > bar_cutoff(signal_bar.trade_date):
            violations += 1

    assert violations > 0, "非 PIT 模式本应含前视，实测却没有违例——口径可能写错了"


def test_ma_cross_never_holds_through_a_death_cross_window() -> None:
    """结构检查：ma_cross 的持仓区间应落在交叉信号之间，不出现空仓期凭空成交。"""
    result = full_run(SYMBOL, "ma_cross")
    assert len(result.fills) >= 2
    dates = [fill.trade_date for fill in result.fills]
    assert dates == sorted(dates), "成交必须按时间递增"
    # 买卖严格交替（单标的、不加仓）
    sides = [fill.side for fill in result.fills]
    assert all(a is not b for a, b in zip(sides, sides[1:], strict=False)), "买卖应严格交替"


def test_events_are_loaded_even_when_backtest_starts_before_them() -> None:
    """事件窗口晚于行情窗口：从行情首根 bar 起跑也要能看到 2026-07 之后的事件。

    M2a 后行情是七年全历史（首根是 2020-01-02 而非 2025-01-02），故这里改成拿**数据自己的**
    首根 bar 比对——写死日期会让这条用例在每次数据扩容时假失败，而它要证的其实是
    「起点早于事件窗口也不会漏掉事件」。
    """
    result = full_run(SYMBOL, "event_driven")
    assert result.events_seen > 0
    assert result.fills, "全窗起跑的事件策略也应成交"
    assert result.bars[0].trade_date == dc.bars(SYMBOL, data_dir=DATA_DIR)[0]["trade_date"]
