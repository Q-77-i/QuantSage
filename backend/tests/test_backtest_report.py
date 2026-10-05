"""T5 报告层单测：SPEC §5 输出结构的完整性与映射正确性。

用脚本化策略（同 test_backtest_engine 的做法）精确控制成交，从而断言报告里
每一个数字的来源；PIT 对比则用真实的 `event_driven` 策略 + 合成事件，
因为「两模式入场日不同」正是该结构存在的理由。

最硬的一条断言是 `json.dumps(report)`：T6 的回测端点要直接返回这个 dict，
任何 date / dataclass / numpy 标量漏进结构都会在 FastAPI 序列化时才炸，故在此设卡。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.backtest import engine as engine_module
from app.backtest.costs import CostModel
from app.backtest.engine import BacktestConfig
from app.backtest.report import build_report
from app.backtest.types import BarContext, Mode, Side, Signal
from tests.conftest import make_backtest_dir, trading_days, ts

START = date(2026, 8, 3)
LONG_WINDOW_BARS = 130  # > SHORT_WINDOW_BARS，用于验证「不提示样本量」的分支


class ScriptedStrategy:
    name = "scripted"

    def __init__(self, actions: dict[int, list[Signal]]) -> None:
        self._actions = actions

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        return list(self._actions.get(ctx.index, []))


@pytest.fixture(autouse=True)
def _restore_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.backtest.strategies import build_strategy as real

    yield
    engine_module.build_strategy = real  # type: ignore[assignment]


def flat_bars(count: int, price: float = 100.0) -> list[dict[str, object]]:
    return [
        {"trade_date": day, "open": price, "close": price, "high": price, "low": price}
        for day in trading_days(START, count)
    ]


def build(
    tmp_path: Path,
    bars: list[dict[str, object]],
    actions: dict[int, list[Signal]],
    **overrides,
) -> dict:
    data_dir = make_backtest_dir(tmp_path, bars, events=[])
    engine_module.build_strategy = lambda name, params=None: ScriptedStrategy(actions)  # type: ignore[assignment]
    overrides.setdefault("costs", CostModel().without_slippage())
    config = BacktestConfig(
        symbol="600519",
        strategy="scripted",
        initial_cash=1_000_000.0,
        data_dir=data_dir,
        **overrides,
    )
    return build_report(config)


# ── 结构完整性 ────────────────────────────────────────────────────────────


def test_report_has_all_spec_sections(tmp_path: Path) -> None:
    report = build(tmp_path, flat_bars(6), {0: [Signal(Side.BUY, reason="入场")]})

    assert set(report) == {"meta", "metrics", "equity_curve", "trades", "open_position", "pit_comparison"}


def test_report_is_json_serializable(tmp_path: Path) -> None:
    """T6 要用这个 dict 直接响应 HTTP，序列化必须是结构保证而非运气。"""
    report = build(
        tmp_path,
        flat_bars(6),
        {0: [Signal(Side.BUY, reason="入场")], 3: [Signal(Side.SELL, reason="出场")]},
    )

    json.dumps(report, ensure_ascii=False)  # 有任何 date / dataclass 残留都会在此抛 TypeError


def test_equity_curve_is_point_wise_aligned_with_bars(tmp_path: Path) -> None:
    """净值曲线每个点都要带基准值，且与 bar 一一对应（前端画双线的前提）。"""
    bars = flat_bars(6)
    report = build(tmp_path, bars, {}, costs=CostModel.disabled())

    assert len(report["equity_curve"]) == len(bars)
    for point, raw in zip(report["equity_curve"], bars, strict=True):
        assert point["date"] == raw["trade_date"].isoformat()
        assert set(point) == {"date", "equity", "benchmark"}
        # 无成交、成本关：净值恒为初始资金，基准随价格走（此处价格恒定故也等于初始资金）
        assert point["equity"] == pytest.approx(1_000_000.0)
        assert point["benchmark"] == pytest.approx(1_000_000.0)


def test_meta_records_run_context(tmp_path: Path) -> None:
    report = build(tmp_path, flat_bars(6), {}, pit_mode=Mode.NON_PIT)

    meta = report["meta"]
    assert meta["symbol"] == "600519"
    assert meta["strategy"] == "scripted"
    assert meta["mode"] == "non_pit"
    assert meta["cutoff_field"] == "event_time"
    assert meta["bars"] == 6
    assert meta["initial_cash"] == pytest.approx(1_000_000.0)
    assert meta["start"] == START.isoformat()
    assert meta["end"] == trading_days(START, 6)[-1].isoformat()
    assert "佣金" in meta["costs"] and "滑点" in meta["costs"]


# ── 指标与基准 ────────────────────────────────────────────────────────────


def test_metrics_include_benchmark_and_excess(tmp_path: Path) -> None:
    """阶梯上涨的行情：基准应精确等于价格涨幅（成本关），超额收益为策略减基准。"""
    bars = [
        {"trade_date": day, "open": 100.0 + 10 * i, "close": 100.0 + 10 * i}
        for i, day in enumerate(trading_days(START, 4))
    ]
    report = build(tmp_path, bars, {}, costs=CostModel.disabled())

    metrics = report["metrics"]
    # 收盘价 100 → 130，涨幅 30%
    assert metrics["benchmark_return"] == pytest.approx(0.3)
    assert metrics["excess_return"] == pytest.approx(metrics["total_return"] - 0.3)
    assert metrics["final_equity"] == pytest.approx(report["equity_curve"][-1]["equity"])


def test_benchmark_cost_applies_when_costs_on(tmp_path: Path) -> None:
    """基准承担一次买入成本：成本开启时基准收益略低于纯价格涨幅。"""
    bars = [
        {"trade_date": day, "open": 100.0, "close": 100.0} for day in trading_days(START, 3)
    ]
    data_dir = make_backtest_dir(tmp_path, bars, events=[])
    config = BacktestConfig(symbol="600519", strategy="ma_cross", data_dir=data_dir)

    report = build_report(config)
    assert report["metrics"]["benchmark_return"] < 0  # 价格不变，只亏了一次买入成本


# ── 交易明细 ──────────────────────────────────────────────────────────────


def test_trades_map_spec_fields_and_keep_entry_reason_separate(tmp_path: Path) -> None:
    report = build(
        tmp_path,
        flat_bars(6),
        {0: [Signal(Side.BUY, reason="金叉")], 3: [Signal(Side.SELL, reason="死叉")]},
    )

    assert len(report["trades"]) == 1
    trade = report["trades"][0]
    assert set(trade) == {
        "entry_date",
        "exit_date",
        "pnl",
        "reason",
        "entry_reason",
        "return_pct",
        "hold_bars",
        "qty",
    }
    assert trade["reason"] == "死叉"  # SPEC 的 reason 是出场原因
    assert trade["entry_reason"] == "金叉"
    assert trade["entry_date"] == trading_days(START, 6)[1].isoformat()
    assert trade["exit_date"] == trading_days(START, 6)[4].isoformat()
    assert trade["qty"] == 9900  # 100 万 / 100 元 → 9900 股（整手），见 broker


# ── 期末持仓 ──────────────────────────────────────────────────────────────


def test_open_position_is_null_when_flat(tmp_path: Path) -> None:
    report = build(tmp_path, flat_bars(6), {})
    assert report["open_position"] is None


def test_open_position_reports_unrealized_pnl(tmp_path: Path) -> None:
    """期末仍持仓：浮盈单列。它不进 trades，故胜率仍为 None。"""
    bars = [
        {"trade_date": day, "open": 100.0, "close": 110.0} for day in trading_days(START, 4)
    ]
    report = build(tmp_path, bars, {0: [Signal(Side.BUY, reason="入场")]})

    open_position = report["open_position"]
    assert open_position is not None
    assert open_position["shares"] > 0
    assert open_position["entry_date"] == trading_days(START, 4)[1].isoformat()
    assert open_position["last_close"] == pytest.approx(110.0)
    assert open_position["unrealized_pnl"] == pytest.approx(
        report["equity_curve"][-1]["equity"] - 1_000_000.0
    )
    assert report["metrics"]["win_rate"] is None
    assert report["metrics"]["trade_count"] == 0


# ── PIT 对比 ──────────────────────────────────────────────────────────────


def test_pit_comparison_is_null_unless_requested(tmp_path: Path) -> None:
    report = build(tmp_path, flat_bars(6), {})
    assert report["pit_comparison"] is None


def test_pit_comparison_quantifies_the_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """合成事件「事发早、可得晚」：PIT 顺延入场，非 PIT 当日即入，两者期末权益不同。"""
    days = trading_days(START, 12)
    bars = [
        {"trade_date": day, "open": 100.0 + i, "close": 100.0 + i} for i, day in enumerate(days)
    ]
    events = [
        {
            "event_id": "e1",
            "title": "利好公告",
            "event_time": ts(f"{days[2]} 09:00:00"),  # 事发
            "available_at": ts(f"{days[4]} 20:00:00"),  # 收盘后才可得 → PIT 顺延到下一交易日
            "direction_norm": "bullish",
            "score": 80.0,
        }
    ]
    data_dir = make_backtest_dir(tmp_path, bars, events=events)
    config = BacktestConfig(
        symbol="600519",
        strategy="event_driven",
        initial_cash=1_000_000.0,
        costs=CostModel().without_slippage(),
        data_dir=data_dir,
    )

    report = build_report(config, compare_pit=True)
    comparison = report["pit_comparison"]

    assert set(comparison) == {"pit_metrics", "non_pit_metrics", "delta", "entry_dates"}
    assert comparison["entry_dates"]["pit"] != comparison["entry_dates"]["non_pit"]
    assert comparison["delta"]["final_equity_abs"] != 0
    # 核心量化值：期末权益虚高比例，分母恒为正
    assert comparison["delta"]["final_equity_pct"] == pytest.approx(
        comparison["non_pit_metrics"]["final_equity"] / comparison["pit_metrics"]["final_equity"] - 1
    )
    assert "total_return_pp" in comparison["delta"]
    json.dumps(report, ensure_ascii=False)


# ── 样本量提示 ────────────────────────────────────────────────────────────


def test_short_window_gets_a_sample_size_warning(tmp_path: Path) -> None:
    report = build(tmp_path, flat_bars(6), {})
    assert any("样本" in w for w in report["meta"]["warnings"])


def test_long_window_has_no_warning(tmp_path: Path) -> None:
    report = build(tmp_path, flat_bars(LONG_WINDOW_BARS), {})
    assert report["meta"]["warnings"] == []
