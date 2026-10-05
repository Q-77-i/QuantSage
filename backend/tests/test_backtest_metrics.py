"""T5 指标单测：全部用**手工可算**的合成净值曲线，期望值在注释里给出推导过程。

刻意不引第三方快照库——SPEC §5 要求「指标与手工计算样例一致」，硬编码期望值
既满足该判据，又让每个数字都能被读者当场复核。边界（空曲线 / 单点 / 恒定净值 /
无平仓交易）与主路径同等重要：真实报告里这些分支都会走到（例如事件窗口只有 54 根 bar）。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.backtest.costs import CostModel
from app.backtest.metrics import (
    TRADING_DAYS_PER_YEAR,
    annual_return,
    benchmark_curve,
    compute_metrics,
    max_drawdown,
    sharpe_ratio,
)
from app.backtest.types import Bar, EquityPoint, Trade

DAY0 = date(2026, 8, 3)


def curve(values: list[float]) -> tuple[EquityPoint, ...]:
    """把净值序列包成权益点；cash/market_value 对指标无影响，按满仓估值填。"""
    return tuple(
        EquityPoint(
            trade_date=DAY0 + timedelta(days=i),
            cash=0.0,
            market_value=v,
            equity=v,
            close=v,
        )
        for i, v in enumerate(values)
    )


def bar(close: float) -> Bar:
    return Bar(
        symbol="600519", trade_date=DAY0, open=close, high=close, low=close, close=close, volume=1e5
    )


def make_trade(pnl: float) -> Trade:
    """只关心 pnl 的交易；其余字段填合法值。"""
    return Trade(
        entry_date=DAY0,
        entry_price=100.0,
        qty=100,
        entry_fees=5.0,
        entry_reason="入场",
        exit_date=DAY0 + timedelta(days=3),
        exit_price=100.0 + pnl / 100,
        exit_fees=5.0,
        exit_reason="出场",
        pnl=pnl,
        return_pct=pnl / 10_000.0,
        hold_bars=3,
    )


# ── 主路径：一条手工可算的曲线 ────────────────────────────────────────────


def test_metrics_match_hand_calculation() -> None:
    """净值 [100, 110, 99, 108.9]，初始资金 100：总收益 8.9%、最大回撤 10%、夏普 4.582576。"""
    result = compute_metrics(
        curve([100.0, 110.0, 99.0, 108.9]),
        [make_trade(10.0), make_trade(-5.0), make_trade(3.0), make_trade(0.0)],
        initial_cash=100.0,
    )

    assert result.total_return == pytest.approx(0.089)  # 108.9/100 - 1
    assert result.final_equity == pytest.approx(108.9)
    assert result.max_drawdown == pytest.approx(0.1)  # 峰 110 → 谷 99 = 11/110
    # 日收益 [0.1, -0.1, 0.1] → 均值 1/30、样本标准差 1/√75
    # 夏普 = (1/30)/(1/√75) × √252 = √18900/30 = 4.582576
    assert result.sharpe == pytest.approx(4.582576, abs=1e-6)
    # 盈亏 [+10, -5, +3, 0]：0 不算盈利，2/4
    assert result.win_rate == pytest.approx(0.5)
    assert result.trade_count == 4


def test_annual_return_is_exact_on_round_numbers() -> None:
    """两年 504 个交易日、总收益 21% → 年化 10%：(1.21)^(252/504) - 1 = 0.1。"""
    assert annual_return(0.21, 504) == pytest.approx(0.1)
    assert annual_return(0.1, TRADING_DAYS_PER_YEAR) == pytest.approx(0.1)


def test_max_drawdown_keeps_the_deepest_peak_to_trough() -> None:
    """[100, 120, 90, 130, 117]：120→90 回撤 25%，130→117 回撤 10%，取 25%。"""
    assert max_drawdown([100.0, 120.0, 90.0, 130.0, 117.0]) == pytest.approx(0.25)


def test_max_drawdown_is_zero_when_monotonic() -> None:
    assert max_drawdown([100.0, 101.0, 102.0]) == 0.0


# ── 边界：真实报告会走到的空/退化分支 ─────────────────────────────────────


def test_empty_curve_degrades_to_flat() -> None:
    result = compute_metrics((), [], initial_cash=1_000_000.0)

    assert result.total_return == 0.0
    assert result.annual_return == 0.0
    assert result.max_drawdown == 0.0
    assert result.sharpe is None
    assert result.win_rate is None
    assert result.trade_count == 0
    assert result.final_equity == pytest.approx(1_000_000.0)


def test_single_point_has_no_return_sample() -> None:
    result = compute_metrics(curve([100.0]), [], initial_cash=100.0)

    assert result.total_return == 0.0
    assert result.sharpe is None  # 一个点算不出收益序列
    assert result.max_drawdown == 0.0


def test_flat_curve_has_undefined_sharpe() -> None:
    """标准差为 0（全程空仓）时夏普无定义，返回 None 而非 0 或抛异常。"""
    result = compute_metrics(curve([100.0, 100.0, 100.0]), [], initial_cash=100.0)

    assert result.sharpe is None
    assert result.max_drawdown == 0.0


def test_two_points_is_not_enough_for_sharpe() -> None:
    result = compute_metrics(curve([100.0, 110.0]), [], initial_cash=100.0)

    assert result.sharpe is None  # 只有 1 个收益样本，ddof=1 无意义
    assert result.total_return == pytest.approx(0.1)


def test_no_closed_trades_leaves_win_rate_undefined() -> None:
    """期末仍持仓时 trades 为空：胜率必须是 None，不能报 0% 或 100%。"""
    result = compute_metrics(curve([100.0, 105.0]), [], initial_cash=100.0)

    assert result.win_rate is None
    assert result.trade_count == 0


def test_all_winning_trades_give_full_win_rate() -> None:
    result = compute_metrics(curve([100.0]), [make_trade(1.0), make_trade(2.0)], initial_cash=100.0)
    assert result.win_rate == pytest.approx(1.0)


def test_wiped_out_equity_annualizes_to_minus_one() -> None:
    """净值归零（growth ≤ 0）时年化无定义，按 -100% 处理，不抛异常。"""
    assert annual_return(-1.0, 252) == -1.0
    assert annual_return(-1.5, 252) == -1.0


def test_sharpe_ratio_needs_at_least_two_returns() -> None:
    assert sharpe_ratio([100.0, 101.0]) is None
    assert sharpe_ratio([]) is None


# ── 基准曲线 ──────────────────────────────────────────────────────────────


def test_benchmark_without_costs_is_pure_price_curve() -> None:
    """成本全关时基准退化为纯价格曲线：初始资金 × close_t/close_0。"""
    bars = [bar(10.0), bar(11.0), bar(12.0)]
    result = benchmark_curve(bars, 1000.0, CostModel.disabled())

    assert result == pytest.approx((1000.0, 1100.0, 1200.0))
    assert result[0] == pytest.approx(1000.0)  # 与策略 equity_curve[0] 对齐


def test_benchmark_charges_one_entry_cost() -> None:
    """默认成本：买入价含 5bps 滑点、佣金万 2.5，只收一次（期末不卖出故无印花税）。

    滑点后买价 = 10 × 1.0005 = 10.005；佣金 = 1000 × 0.00025 = 0.25
    份额 = (1000 - 0.25)/10.005 = 99.9250375 → 首点净值 = ×10 = 999.250375
    """
    bars = [bar(10.0), bar(11.0), bar(12.0)]
    result = benchmark_curve(bars, 1000.0, CostModel())

    assert result[0] == pytest.approx(999.250375)
    assert result[1] == pytest.approx(1099.1754125)  # 99.9250375 × 11
    assert len(result) == len(bars)


def test_benchmark_cost_switches_are_independent() -> None:
    """只关滑点 / 只关费用，各自只消掉自己那一份成本。"""
    bars = [bar(10.0)]
    only_slippage = benchmark_curve(bars, 1000.0, CostModel().without_fees())
    only_fees = benchmark_curve(bars, 1000.0, CostModel().without_slippage())

    assert only_fees[0] == pytest.approx(999.75)  # 只扣佣金 0.25
    assert only_slippage[0] == pytest.approx(999.50025)  # 只扣滑点：1000/10.005×10


def test_benchmark_empty_bars_returns_empty() -> None:
    assert benchmark_curve([], 1000.0, CostModel()) == ()


def test_benchmark_falls_back_to_cash_when_price_unusable() -> None:
    """首根收盘价非正时无法建仓，净值恒为初始资金（防御分支）。"""
    result = benchmark_curve([bar(0.0), bar(11.0)], 1000.0, CostModel())
    assert result == pytest.approx((1000.0, 1000.0))
