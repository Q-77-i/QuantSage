"""归因（M7a）：标的级与事件级。纯函数。

**如实收窄**（SPEC §8）：本地**没有标的行业分类数据**（M2a 已核 `cn-daily` 无行业字段、
sector 归档仅数日），所以「行业归因」的口径是**驱动事件的 industries**，不是持仓的行业分类——
报告里必须把这句话写出来。因子归因也不做（M5c 的因子面板是独立尺子，硬接会造一个假接口）。

事件级分组的两个已知性质，展示时要如实说：
* 一笔回合可以计入**多个**行业组（事件挂了多个行业）⇒ **各组之和大于整体是正常的**；
* 没有事件来源的买入（纯价量策略，如双均线）单列「无事件来源」组，不塞进任何方向/行业。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.backtest.types import Side
from app.memory.settle import Trip
from app.paper.types import Decision, DecisionStatus
from app.report.evidence import EvidenceItem, evidence_key

#: 方向 → 中文标签。措辞与 `frontend/components/backtest/events-table.tsx` 的 DIRECTION 一致
#: （同一份语料在两处展示，不兴第二套话术）。
DIRECTION_LABELS: dict[str, str] = {"bullish": "利多", "bearish": "利空", "neutral": "中性"}
UNKNOWN_DIRECTION = "未标注方向"
NO_EVENT = "无事件来源"


@dataclass(frozen=True, slots=True)
class SymbolRow:
    """一只标的对资金的贡献。金额单位元，`contribution_pp` 单位百分点。"""

    symbol: str
    trips: int
    closed: int
    wins: int
    realized_pnl: float
    unrealized_pnl: float
    #: 未平仓但**取不到估值价**的回合数：浮盈记 0 但必须单独说，否则「0」会被读成「不赚不赔」
    unmarked: int
    contribution_pp: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "trips": self.trips,
            "closed": self.closed,
            "wins": self.wins,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "unmarked": self.unmarked,
            "contribution_pp": self.contribution_pp,
        }


@dataclass(frozen=True, slots=True)
class SignalRow:
    """一个方向组 / 行业组的回合结果。"""

    label: str
    trips: int
    closed: int
    wins: int
    pnl: float
    unmarked: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "trips": self.trips,
            "closed": self.closed,
            "wins": self.wins,
            "pnl": self.pnl,
            "unmarked": self.unmarked,
        }


def _closed(items: Sequence[Trip]) -> list[Trip]:
    return [t for t in items if not t.is_open and t.pnl is not None]


def _signal_rows(buckets: Mapping[str, list[Trip]]) -> list[SignalRow]:
    """标签桶 → 行；按回合数降序、标签升序（稳定，两次调用逐字段相等）。"""
    return [
        SignalRow(
            label=label,
            trips=len(items),
            closed=len(_closed(items)),
            wins=sum(1 for t in _closed(items) if (t.pnl or 0.0) > 0),
            pnl=sum(t.pnl or 0.0 for t in items),
            unmarked=sum(1 for t in items if t.pnl is None),
        )
        for label, items in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]


def symbol_attribution(trips: Sequence[Trip], initial_cash: float) -> list[SymbolRow]:
    """按标的汇总，贡献大的在前（贡献 = (已实现 + 浮盈) / 初始资金 × 100）。"""
    grouped: dict[str, list[Trip]] = {}
    for trip in trips:
        grouped.setdefault(trip.symbol, []).append(trip)

    rows: list[SymbolRow] = []
    for symbol, items in grouped.items():
        closed = _closed(items)
        opened = [t for t in items if t.is_open]
        realized = sum(t.pnl or 0.0 for t in closed)
        unrealized = sum(t.pnl or 0.0 for t in opened)
        rows.append(
            SymbolRow(
                symbol=symbol,
                trips=len(items),
                closed=len(closed),
                wins=sum(1 for t in closed if (t.pnl or 0.0) > 0),
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                unmarked=sum(1 for t in opened if t.pnl is None),
                contribution_pp=(
                    (realized + unrealized) / initial_cash * 100.0 if initial_cash else 0.0
                ),
            )
        )
    rows.sort(key=lambda row: (-row.contribution_pp, row.symbol))
    return rows


def signal_attribution(
    buys: Sequence[Decision],
    trips: Sequence[Trip],
    evidence: Mapping[str, EvidenceItem],
) -> dict[str, list[SignalRow]]:
    """事件级归因：按驱动事件的**方向**与 **industries** 分组，逐组给回合结果。

    `buys` 是**已成交的买入决策**（未成交的不做反事实收益）；被后买覆盖、或配不出回合的
    买入自动跳过（在 `trips` 里找不到就不计）。
    """
    trip_by_decision = {trip.entry_decision_id: trip for trip in trips}
    by_direction: dict[str, list[Trip]] = {}
    by_industry: dict[str, list[Trip]] = {}

    for decision in buys:
        if decision.status is not DecisionStatus.FILLED or decision.side is not Side.BUY:
            continue
        trip = trip_by_decision.get(decision.id)
        if trip is None:
            continue
        sources = decision.sources or {}
        key = evidence_key(
            str(sources["event_id"]) if sources.get("event_id") else None,
            str(sources["event_time"]) if sources.get("event_time") else None,
        )
        item = evidence.get(key) if key else None
        if item is None:
            by_direction.setdefault(NO_EVENT, []).append(trip)
            continue
        by_direction.setdefault(
            DIRECTION_LABELS.get(item.direction_norm or "", UNKNOWN_DIRECTION), []
        ).append(trip)
        for industry in item.industries:
            by_industry.setdefault(industry, []).append(trip)

    return {"direction": _signal_rows(by_direction), "industry": _signal_rows(by_industry)}
