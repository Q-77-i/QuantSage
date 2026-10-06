"""T4 回测引擎的公共数据结构与错误类型。

本模块是 `app.backtest` 的最底层：**不 import 包内任何其他模块**，供 costs / events /
portfolio / broker / strategies / engine 共享。`BarContext` 同时被策略与引擎需要，
若把它放进 engine.py 会让 strategies → engine → strategies 成环，故单列一层；
`BacktestConfig` / `BacktestResult`（需要 CostModel）因此落在 engine.py，不在本模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Mapping
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")


class BacktestError(RuntimeError):
    """回测配置或数据不满足前提（未知策略名、区间内无 bar、数据未落盘等）。"""


class NoDataError(BacktestError):
    """标的不存在，或指定区间内没有 bar。

    单列一支是为了让 API 能把「没数据」映射成 404、把「配置不对」映射成 400，
    不必去嗅探错误文案。
    """


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Mode(StrEnum):
    """事件可见性口径。

    PIT：按 `available_at` 设卡，只能看到该时点之前平台已发布的信息（本项目护城河）。
    NON_PIT：按 `event_time` 设卡，等同假设「事发即知」，用于量化前视偏差虚高幅度。
    """

    PIT = "pit"
    NON_PIT = "non_pit"


@dataclass(frozen=True, slots=True)
class Bar:
    """单标的单日日线。"""

    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_suspended: bool = False

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> Bar:
        """从 `duckdb_client.bars()` 的行构造；缺列按缺失值处理而非抛异常。"""

        def num(key: str) -> float:
            value = row.get(key)
            return float(value) if isinstance(value, (int, float)) else 0.0

        return cls(
            symbol=str(row.get("symbol") or ""),
            trade_date=row["trade_date"],  # type: ignore[arg-type]
            open=num("open"),
            high=num("high"),
            low=num("low"),
            close=num("close"),
            volume=num("volume"),
            is_suspended=bool(row.get("is_suspended")),
        )

    @property
    def tradable(self) -> bool:
        """停牌或开盘价非正时无法按开盘价撮合。实测样本内恒为可交易。"""
        return not self.is_suspended and self.open > 0


@dataclass(frozen=True, slots=True)
class EventView:
    """回测视角的单条事件（已从落盘行解析出 score）。"""

    event_id: str
    symbol: str
    title: str
    event_time: datetime
    available_at: datetime
    direction_norm: str | None
    score: float | None

    def stamp(self, mode: Mode) -> datetime:
        """与 bar 收盘时刻比较的那个时间戳——PIT 与非 PIT 的**唯一**差别。"""
        return self.available_at if mode is Mode.PIT else self.event_time


@dataclass(frozen=True, slots=True)
class Signal:
    """策略在 bar 收盘产出的意图；实际股数与成交价由 broker 在下一根开盘决定。"""

    side: Side
    reason: str = ""
    event_id: str | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    """一笔成交。`price` 含滑点，`ref_price` 是未含滑点的 bar.open。"""

    trade_date: date
    side: Side
    qty: int
    price: float
    ref_price: float
    commission: float
    stamp_tax: float
    cash_delta: float
    reason: str = ""
    event_id: str | None = None

    @property
    def fees(self) -> float:
        return self.commission + self.stamp_tax

    @property
    def slippage_cost(self) -> float:
        return abs(self.price - self.ref_price) * self.qty


@dataclass(frozen=True, slots=True)
class Position:
    """当前持仓。`entry_index` 是成交 bar 的下标，持有期按交易日口径计数。"""

    shares: int = 0
    entry_price: float = 0.0
    entry_fees: float = 0.0
    entry_date: date | None = None
    entry_index: int | None = None
    entry_reason: str = ""

    @property
    def is_flat(self) -> bool:
        return self.shares == 0


@dataclass(frozen=True, slots=True)
class Trade:
    """一笔已平仓交易（开仓 + 平仓配对）。pnl 已扣双边费用。"""

    entry_date: date
    entry_price: float
    qty: int
    entry_fees: float
    entry_reason: str
    exit_date: date
    exit_price: float
    exit_fees: float
    exit_reason: str
    pnl: float
    return_pct: float
    hold_bars: int


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """每根 bar 一个估值点。"""

    trade_date: date
    cash: float
    market_value: float
    equity: float
    close: float


@dataclass(frozen=True, slots=True)
class DroppedSignal:
    """未能成交的信号（停牌超时顺延、或落在最后一根 bar 无法成交）。"""

    trade_date: date
    signal: Signal
    reason: str


@dataclass(frozen=True, slots=True)
class BarContext:
    """传给 `Strategy.on_bar` 的全部信息。

    `history` 是 `bars[: index + 1]` 的**切片元组**：策略在物理上拿不到未来的 bar，
    无前视由结构保证，而非靠文档约定。
    """

    bar: Bar
    index: int
    history: tuple[Bar, ...]
    position: Position
    cash: float
    equity: float
    events: tuple[EventView, ...]
    new_events: tuple[EventView, ...]
