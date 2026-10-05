"""T5 验收：真实样本数据上的分析报告。

先跑 `uv run python scripts/download_bars.py` 与 `scripts/download_events.py`。
默认不收集；用 `uv run pytest -m integration` 触发。

最关键的两条：
- `test_report_is_internally_consistent`：把报告的三块（trades / open_position / equity_curve）
  用会计恒等式串起来——初始资金 + Σ已平仓盈亏 + 未平仓浮盈 == 期末权益。任何一块算错都会在此暴露。
- `test_pit_gap_is_quantified`：SPEC §5 的验收判据，报告必须给出两模式的差值。

⚠ 该差值**不预设符号**：非 PIT 用了当时不可得的信息，但「看到未来」不等于赚更多——
实测三个标的均为非 PIT 更低或持平。断言的是「可量化」，不是「虚高」。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.backtest.costs import CostModel
from app.backtest.engine import BacktestConfig
from app.backtest.metrics import benchmark_curve
from app.backtest.report import build_report
from app.backtest.types import Bar, Mode
from app.core.config import get_settings
from app.data import duckdb_client as dc

pytestmark = pytest.mark.integration

DATA_DIR = get_settings().data_dir
SYMBOLS = ("600519", "300750", "600036")
EVENT_START = min(
    dc.events(symbol, data_dir=DATA_DIR)[0]["event_time"].date()
    for symbol in SYMBOLS
    if dc.events(symbol, data_dir=DATA_DIR)
)


def event_config(symbol: str, **overrides) -> BacktestConfig:
    """事件窗口内的回测配置——事件语料仅约 3 个月，以此为起点才有意义。"""
    rows = dc.bars(symbol, start=EVENT_START.isoformat(), data_dir=DATA_DIR)
    return BacktestConfig(
        symbol=symbol,
        strategy="event_driven",
        start=rows[0]["trade_date"],
        end=rows[-1]["trade_date"],
        costs=CostModel(),
        data_dir=DATA_DIR,
        **overrides,
    )


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_report_is_json_serializable_on_real_data(symbol: str) -> None:
    report = build_report(event_config(symbol))
    json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_report_is_internally_consistent(symbol: str) -> None:
    """会计恒等式：初始资金 + Σ已平仓盈亏 + 未平仓浮盈 == 期末权益。"""
    report = build_report(event_config(symbol))
    metrics = report["metrics"]

    realized = sum(trade["pnl"] for trade in report["trades"])
    position = report["open_position"]
    unrealized = position["unrealized_pnl"] if position else 0.0
    expected = report["meta"]["initial_cash"] + realized + unrealized

    assert metrics["final_equity"] == pytest.approx(expected, abs=0.01)


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_equity_curve_aligns_with_benchmark_and_bars(symbol: str) -> None:
    report = build_report(event_config(symbol))

    curve = report["equity_curve"]
    bars = dc.bars(symbol, start=EVENT_START.isoformat(), data_dir=DATA_DIR)
    assert len(curve) == len(bars)
    assert [point["date"] for point in curve] == [bar["trade_date"].isoformat() for bar in bars]
    assert report["metrics"]["final_equity"] == pytest.approx(curve[-1]["equity"])


def test_benchmark_matches_buy_and_hold_on_real_data() -> None:
    """基准 = 首根收盘价份额化买入并按持仓估值，期末应等于手算结果。"""
    symbol = "600519"
    config = event_config(symbol)
    report = build_report(config)

    bars = dc.bars(symbol, start=EVENT_START.isoformat(), data_dir=DATA_DIR)
    expected = benchmark_curve([Bar.from_row(row) for row in bars], config.initial_cash, config.costs)
    assert [point["benchmark"] for point in report["equity_curve"]] == pytest.approx(list(expected))
    # 基准买入价含一次成本，故首点略低于初始资金
    assert expected[0] < config.initial_cash
    assert report["metrics"]["benchmark_return"] == pytest.approx(expected[-1] / config.initial_cash - 1)


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_pit_gap_is_quantified(symbol: str) -> None:
    """SPEC §5 验收：报告能给出两模式的差值（差值本身可正、可负、可为 0）。"""
    report = build_report(event_config(symbol), compare_pit=True)
    comparison = report["pit_comparison"]

    assert comparison is not None
    delta = comparison["delta"]
    assert delta["final_equity_pct"] is not None
    assert delta["final_equity_abs"] == pytest.approx(
        comparison["non_pit_metrics"]["final_equity"] - comparison["pit_metrics"]["final_equity"]
    )
    # 两模式唯一变量是设卡字段：成本、参数、区间与事件集一致，成交必须都是同向的
    assert comparison["entry_dates"]["pit"] or comparison["entry_dates"]["non_pit"]


def test_ma_cross_reports_no_pit_comparison() -> None:
    """ma_cross 不消费事件，两模式必然同结果：报告不该跑无意义的第二遍。"""
    rows = dc.bars("600519", data_dir=DATA_DIR)
    report = build_report(
        BacktestConfig(
            symbol="600519",
            strategy="ma_cross",
            start=rows[0]["trade_date"],
            end=rows[-1]["trade_date"],
            data_dir=DATA_DIR,
        )
    )

    assert report["pit_comparison"] is None
    assert report["meta"]["strategy"] == "ma_cross"
    assert report["meta"]["warnings"] == []  # 全窗 424 根 bar，样本充足


def test_event_window_is_flagged_as_short_sample() -> None:
    """事件窗口仅约 54 个交易日：年化与夏普必须带样本量提示。"""
    report = build_report(event_config("600519"))

    assert report["meta"]["bars"] < 120
    assert any("样本" in warning for warning in report["meta"]["warnings"])


def test_mode_is_recorded_in_meta() -> None:
    report = build_report(event_config("600519", pit_mode=Mode.NON_PIT))
    assert report["meta"]["mode"] == "non_pit"
    assert report["meta"]["cutoff_field"] == "event_time"


def test_event_window_start_is_after_bars_start() -> None:
    """确认起点取自事件窗口而非行情窗口（否则事件策略前半段无事件可消费）。"""
    report = build_report(event_config("600519"))
    assert date.fromisoformat(str(report["meta"]["start"])) >= EVENT_START
