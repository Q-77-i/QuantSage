"""M7a 账户绩效指标（`app.report.performance`）。

期望值全部手工算好写死（4 点曲线的夏普/波动率用独立计算器算过），不用被测实现反推。
口径与回测同源：`sharpe / annual_return / max_drawdown` 直接复用 `backtest.metrics`，
本模块新增的只有**波动率**（日收益样本标准差 × √252）与**超额收益**（对全市场等权）。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.memory.settle import Trip, pair_trips
from app.report.performance import performance_metrics
from tests.test_memory_settle import buy, sell


def _closed_win() -> Trip:
    """一笔手算过的盈利回合：pnl = 984.50（见 test_memory_settle 的算例）。"""
    return pair_trips([buy(date(2026, 8, 3)), sell(date(2026, 8, 10))])[0]


def _open_lot() -> Trip:
    return pair_trips([buy(date(2026, 9, 30), did="d-open")])[0]


def test_hand_computed_metrics_on_a_four_point_curve() -> None:
    """净值 [100, 110, 99, 108.9]：收益 +10% / −10% / +10% 的已知序列。

    total_return = 0.089；max_drawdown = (110−99)/110 = 0.1
    日收益均值 0.0333333、样本标准差 0.11547005
    sharpe = 0.0333333 / 0.11547005 × √252 = 4.582576
    volatility = 0.11547005 × √252 = 1.833030
    """
    metrics = performance_metrics(
        [100.0, 110.0, 99.0, 108.9],
        100.0,
        trips=[_closed_win(), _open_lot()],
        benchmark_return=0.05,
    )

    assert metrics.total_return == pytest.approx(0.089)
    assert metrics.max_drawdown == pytest.approx(0.1)
    assert metrics.sharpe == pytest.approx(4.582576, rel=1e-6)
    assert metrics.volatility == pytest.approx(1.833030, rel=1e-6)
    assert metrics.final_equity == pytest.approx(108.9)
    assert metrics.benchmark_return == pytest.approx(0.05)
    assert metrics.excess_return == pytest.approx(0.039)


def test_win_rate_and_trade_count_only_count_closed_trips() -> None:
    """胜率与交易次数只看**已平仓**回合（同 P1 口径）——未平仓的浮盈不进胜率。"""
    metrics = performance_metrics(
        [100.0, 101.0], 100.0, trips=[_closed_win(), _open_lot()], benchmark_return=0.0
    )

    assert metrics.trade_count == 1
    assert metrics.win_rate == pytest.approx(1.0)


def test_no_closed_trips_gives_none_win_rate() -> None:
    """一个回合都没平过时胜率无定义（None），不写 0——「0% 胜率」是一句假话。"""
    metrics = performance_metrics([100.0, 100.5], 100.0, trips=[_open_lot()])

    assert metrics.trade_count == 0
    assert metrics.win_rate is None


def test_degenerate_curves_do_not_raise() -> None:
    """空曲线 / 单点 / 恒定净值：返回退化值而不抛（同 P1「报告不该因样本退化而崩」）。

    恒定净值下日收益标准差为 0：夏普无定义（None），波动率则如实是 0。
    """
    empty = performance_metrics([], 50_000.0)
    assert empty.final_equity == 50_000.0
    assert empty.total_return == 0.0
    assert empty.sharpe is None
    assert empty.volatility is None
    assert empty.max_drawdown == 0.0

    single = performance_metrics([50_000.0], 50_000.0)
    assert single.total_return == 0.0
    assert single.sharpe is None

    flat = performance_metrics([100.0, 100.0, 100.0], 100.0)
    assert flat.sharpe is None
    assert flat.volatility == pytest.approx(0.0)
    assert flat.max_drawdown == pytest.approx(0.0)


def test_missing_benchmark_leaves_excess_none() -> None:
    """基准取不到时（如窗口内没有行情样本）两个基准键都是 None——不编数。"""
    metrics = performance_metrics([100.0, 103.0], 100.0, benchmark_return=None)

    assert metrics.benchmark_return is None
    assert metrics.excess_return is None
