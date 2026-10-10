"""账户绩效指标（M7a）：纯函数，不 import 引擎、不做 I/O。

与回测**同一把尺子**：`sharpe / annual_return / max_drawdown` 直接复用 `backtest.metrics`
（那边是「可被逐一手工复核的数学」，不在本模块复制第二份）。本模块只新增两件：

* **波动率**：日净值收益的样本标准差（ddof=1）× √252。恒定净值为 0（如实），
  样本不足 2 个收益时为 `None`（无定义，不编 0）；
* **超额收益**：账户收益 − 同窗口**全市场等权**（M5a `market_benchmark`，基准取不到时两个键都是 `None`）。

胜率与交易次数只看**已平仓回合**（同 P1 口径：未平仓浮盈不进胜率，否则「全胜」会失真）；
未平仓的浮动盈亏由报告层在归因块里单列。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import sqrt

from app.backtest.metrics import (
    TRADING_DAYS_PER_YEAR,
    annual_return,
    max_drawdown,
    sharpe_ratio,
)
from app.memory.settle import Trip


@dataclass(frozen=True, slots=True)
class AccountMetrics:
    """报告 `metrics` 块的形状。比率一律小数（0.089 = 8.9%）。"""

    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float | None
    volatility: float | None
    win_rate: float | None
    trade_count: int
    final_equity: float
    benchmark_return: float | None
    excess_return: float | None


def _returns(curve: Sequence[float]) -> list[float]:
    """日净值收益；前一点净值归零的那一段无法算收益率，跳过（与 `sharpe_ratio` 同规）。"""
    return [curve[i] / curve[i - 1] - 1.0 for i in range(1, len(curve)) if curve[i - 1] > 0]


def volatility(
    curve: Sequence[float], periods_per_year: int = TRADING_DAYS_PER_YEAR
) -> float | None:
    """年化波动率：日收益样本标准差 × √252；样本不足 2 个时 `None`。"""
    returns = _returns(curve)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return sqrt(max(variance, 0.0)) * sqrt(periods_per_year)


def performance_metrics(
    equity: Sequence[float],
    initial_cash: float,
    *,
    trips: Sequence[Trip] = (),
    benchmark_return: float | None = None,
) -> AccountMetrics:
    """账户绩效指标。`equity` 是逐日净值序列（账户账本的 `equity` 列）。

    `benchmark_return` 由调用方从 `market_benchmark(...).total_return` 取（相对初始资金的
    全窗口收益，**不是** `levels[-1] / levels[0]`——那个会把首日收益漏掉）。
    """
    curve = list(equity)
    final_equity = curve[-1] if curve else initial_cash
    total_return = final_equity / initial_cash - 1.0 if initial_cash else 0.0
    closed = [trip for trip in trips if not trip.is_open and trip.pnl is not None]

    return AccountMetrics(
        total_return=total_return,
        annual_return=annual_return(total_return, len(curve)),
        max_drawdown=max_drawdown(curve),
        sharpe=sharpe_ratio(curve),
        volatility=volatility(curve),
        win_rate=(sum(1 for t in closed if (t.pnl or 0.0) > 0) / len(closed)) if closed else None,
        trade_count=len(closed),
        final_equity=final_equity,
        benchmark_return=benchmark_return,
        excess_return=(
            total_return - benchmark_return if benchmark_return is not None else None
        ),
    )
