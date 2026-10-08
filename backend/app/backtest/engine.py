"""T4 回测引擎：唯一编排者。

事件循环顺序本身就是防前视的关键——
**先按本根开盘撮合上一根收盘挂下的信号，再推进收盘状态、最后才问策略要新信号**。
信号永远在 bar T 收盘生成、成交永远在 bar T+1 开盘，A 股 T+1 限制由结构天然满足。

`BacktestConfig` / `BacktestResult` 定义在此而非 types.py：它们需要 `CostModel`，
放进 types.py 会形成 types → costs → types 的循环。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.backtest.broker import Broker
from app.backtest.costs import CostModel
from app.backtest.events import build_feed
from app.backtest.portfolio import Portfolio
from app.backtest.strategies import Strategy, build_strategy
from app.backtest.types import (
    Bar,
    BarContext,
    BacktestError,
    DroppedSignal,
    EquityPoint,
    Fill,
    Mode,
    Position,
    Signal,
    Trade,
)
from app.data import duckdb_client as dc

log = logging.getLogger(__name__)

#: 停牌顺延上限：连续这么多根 bar 无法撮合则丢弃信号（实测样本内不触发）。
MAX_DEFER_BARS = 5


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    symbol: str
    strategy: str
    start: date | None = None
    end: date | None = None
    adjust: str = "qfq"
    initial_cash: float = 1_000_000.0
    pit_mode: Mode = Mode.PIT
    costs: CostModel = CostModel()
    params: Mapping[str, float | int] = field(default_factory=dict)
    data_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    config: BacktestConfig
    bars: tuple[Bar, ...]
    equity_curve: tuple[EquityPoint, ...]
    fills: tuple[Fill, ...]
    trades: tuple[Trade, ...]
    open_position: Position
    dropped_signals: tuple[DroppedSignal, ...]
    visible_event_ids: tuple[str, ...]
    cutoff_field: str

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1].equity if self.equity_curve else self.config.initial_cash

    @property
    def total_fees(self) -> float:
        return sum(fill.fees for fill in self.fills)

    @property
    def total_slippage_cost(self) -> float:
        return sum(fill.slippage_cost for fill in self.fills)

    @property
    def total_return(self) -> float:
        return self.final_equity / self.config.initial_cash - 1.0

    @property
    def events_seen(self) -> int:
        return len(self.visible_event_ids)

    @property
    def entry_dates(self) -> tuple[date, ...]:
        """买入成交日序列——PIT 与非 PIT 的差异对比用（首个入场日可能相同）。"""
        return tuple(fill.trade_date for fill in self.fills if fill.side.value == "buy")


def _load_bars(config: BacktestConfig) -> list[Bar]:
    rows = dc.bars(
        config.symbol,
        start=config.start.isoformat() if config.start else None,
        end=config.end.isoformat() if config.end else None,
        adjust=config.adjust,
        data_dir=config.data_dir,
    )
    if not rows:
        raise BacktestError(
            f"{config.symbol} 在 {config.start} → {config.end} 无行情数据；"
            "先跑 scripts/download_bars.py，或放宽区间"
        )
    return [Bar.from_row(row) for row in rows]


def run_backtest(config: BacktestConfig, strategy: Strategy | None = None) -> BacktestResult:
    """按 bar 时间驱动回测；信号在 bar 收盘生成，成交在下一 bar 开盘。

    `strategy` 是**用户策略（M4）的唯一注入点**：内置策略一律传 `None`，仍按 `config.strategy`
    查注册表——那条路径一行没动，是「沙箱出问题不影响既有回测」的保证。
    注入的实例每次调用都应新建（见 `report.build_report` 的 `strategy_factory`）：
    用户代码可以在模块级持有状态，跨运行复用同一实例会让第二遍带上第一遍的残留。
    """
    bars = _load_bars(config)
    if strategy is None:
        strategy = build_strategy(config.strategy, config.params)

    # 事件一次性取全量、不按 start/end 预过滤：dc.events() 的窗口过滤打在 event_time 上，
    # 拿它做 PIT 预筛会误删「事发在窗口前、但窗口内才可得」的事件。可见性一律交给 feed。
    feed = build_feed(dc.events(config.symbol, data_dir=config.data_dir), config.pit_mode)

    portfolio = Portfolio(config.initial_cash)
    broker = Broker(config.costs)

    pending: list[Signal] = []
    fills: list[Fill] = []
    dropped: list[DroppedSignal] = []
    deferred = 0

    for index, bar in enumerate(bars):
        # ① 先撮合上一根收盘挂下的信号：用本根开盘价
        if pending:
            if bar.tradable:
                for signal in pending:
                    fill = broker.execute(signal, bar, portfolio)
                    if fill is None:
                        dropped.append(DroppedSignal(bar.trade_date, signal, "拒单：资金或持仓不满足"))
                    else:
                        portfolio.settle(fill, bar_index=index)
                        fills.append(fill)
                pending = []
                deferred = 0
            else:
                deferred += 1
                if deferred > MAX_DEFER_BARS:
                    dropped.extend(
                        DroppedSignal(bar.trade_date, s, f"停牌连续 {deferred} 根无法撮合，丢弃")
                        for s in pending
                    )
                    pending = []
                    deferred = 0

        # ② 收盘推进事件可见性（PIT 闸门在此生效）
        fresh = feed.advance(bar)

        # ③ 收盘估值（含当日新成交）
        point = portfolio.mark(bar)

        # ④ 构造上下文并问策略要信号；信号留到下一根开盘成交
        context = BarContext(
            bar=bar,
            index=index,
            history=tuple(bars[: index + 1]),
            position=portfolio.position,
            cash=portfolio.cash,
            equity=point.equity,
            events=feed.visible,
            new_events=fresh,
        )
        fresh_signals = list(strategy.on_bar(context))
        if pending:
            # 上一根的单还没成交（停牌顺延中）：保留原单，本根新信号不叠加也不静默丢弃
            dropped.extend(
                DroppedSignal(bar.trade_date, signal, "上一根信号仍未成交，本根新信号被忽略")
                for signal in fresh_signals
            )
        else:
            pending = fresh_signals

    if pending:
        last = bars[-1]
        dropped.extend(
            DroppedSignal(last.trade_date, s, "最后一根 bar 的信号无法成交") for s in pending
        )

    log.info(
        "回测完成：%s/%s 模式=%s bar=%d 成交=%d 平仓=%d 期末=%.2f",
        config.symbol,
        config.strategy,
        config.pit_mode.value,
        len(bars),
        len(fills),
        len(portfolio.trades),
        portfolio.equity_curve[-1].equity,
    )

    return BacktestResult(
        config=config,
        bars=tuple(bars),
        equity_curve=portfolio.equity_curve,
        fills=tuple(fills),
        trades=portfolio.trades,
        open_position=portfolio.position,
        dropped_signals=tuple(dropped),
        visible_event_ids=tuple(event.event_id for event in feed.visible),
        cutoff_field=feed.cutoff_field,
    )
