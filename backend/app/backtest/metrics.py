"""T5 指标层：纯函数，只依赖 `costs.py` / `types.py`，不 import engine / 不做 I/O。

刻意与 `report.py` 分离——本模块是可被逐一手工复核的数学，`report.py` 才负责跑回测
与组装 JSON。报告层因此可以随意改结构，指标口径只有这一处。

全部用标准库实现（不引 numpy/pandas）：样本量在 10^2~10^3 量级，纯 Python 足够快，
且 T5 只多出 100 行依赖更少的代码。真实报告会走到的退化分支（空曲线 / 单点 /
恒定净值 / 无平仓交易）一律返回 `None` 或 0，绝不抛异常——报告不该因为样本退化而崩。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import sqrt

from app.backtest.costs import CostModel
from app.backtest.types import Bar, EquityPoint, Side, Trade

#: 年化折算用。A 股每年约 242~244 个交易日，业界惯例取 252。
TRADING_DAYS_PER_YEAR = 252

#: 少于该 bar 数时报告标注样本量不足：年化与夏普按 252 折算会显著放大噪声。
SHORT_WINDOW_BARS = 120


@dataclass(frozen=True, slots=True)
class Metrics:
    """SPEC §5 的六项指标 + 期末权益。比率一律用小数（0.089 = 8.9%）。"""

    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float | None
    win_rate: float | None
    trade_count: int
    final_equity: float


def max_drawdown(curve: Sequence[float]) -> float:
    """最大回撤幅度，**正值**（0.25 = 回撤 25%）。创新高则不回撤，故单调上涨为 0。"""
    peak = float("-inf")
    worst = 0.0
    for equity in curve:
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def sharpe_ratio(curve: Sequence[float], periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float | None:
    """夏普比率（rf=0）：日净值收益的均值 / 样本标准差（ddof=1）× √252。

    收益样本不足 2 个、或净值恒定（标准差为 0）时无定义，返回 None。
    """
    returns = [
        curve[i] / curve[i - 1] - 1.0
        for i in range(1, len(curve))
        if curve[i - 1] > 0  # 净值归零的那一段无法算收益率，跳过
    ]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if variance <= 0:
        return None
    return mean / sqrt(variance) * sqrt(periods_per_year)


def annual_return(
    total_return: float, n_bars: int, periods_per_year: int = TRADING_DAYS_PER_YEAR
) -> float:
    """按交易日折算年化：`(1+total_return)^(252/n_bars) - 1`。

    净值归零或为负（growth ≤ 0）时年化无定义，按 -100% 返回，不抛异常。
    """
    if n_bars <= 0:
        return 0.0
    growth = 1.0 + total_return
    if growth <= 0:
        return -1.0
    return growth ** (periods_per_year / n_bars) - 1.0


def compute_metrics(
    equity_curve: Sequence[EquityPoint], trades: Sequence[Trade], initial_cash: float
) -> Metrics:
    """从权益曲线与**已平仓**交易算出 SPEC §5 指标。

    胜率与交易次数只看已平仓交易（`BacktestResult.trades` 即此口径）；期末未平仓持仓
    不打进 trades，由 report 层单列，否则「全胜」会失真。
    """
    curve = [point.equity for point in equity_curve]
    final_equity = curve[-1] if curve else initial_cash
    total_return = final_equity / initial_cash - 1.0 if initial_cash else 0.0

    return Metrics(
        total_return=total_return,
        annual_return=annual_return(total_return, len(curve)),
        max_drawdown=max_drawdown(curve),
        sharpe=sharpe_ratio(curve),
        win_rate=(sum(1 for t in trades if t.pnl > 0) / len(trades)) if trades else None,
        trade_count=len(trades),
        final_equity=final_equity,
    )


def benchmark_curve(bars: Sequence[Bar], initial_cash: float, costs: CostModel) -> tuple[float, ...]:
    """基准 = 同标的买入持有：首根 bar 收盘价**份额化**全额买入，扣一次买入成本，持有到期末。

    之所以份额化而不按 100 股整手：整手取整会留下闲置现金，基准被系统性压低
    （茅台 1500 元 × 100 万只能买 600 股，闲置 10%），反而夸大策略超额收益。
    最低 5 元佣金在份额化口径下不适用，且 10^6 量级的佣金远高于该下限，故只用费率。

    期末不卖出，故不计印花税——与策略期末按收盘价估值（而非强制平仓）保持对称。
    成本全关时退化为纯价格曲线 `initial_cash × close_t / close_0`。
    """
    if not bars:
        return ()
    entry_close = bars[0].close
    if entry_close <= 0:
        # 防御：首根无有效价格则无法建仓，净值恒为初始资金
        return tuple(initial_cash for _ in bars)

    buy_price = costs.fill_price(Side.BUY, entry_close)
    commission = initial_cash * costs.commission_rate if costs.fee_enabled else 0.0
    shares = (initial_cash - commission) / buy_price
    return tuple(shares * bar.close for bar in bars)
