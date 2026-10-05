"""事件驱动策略：利多事件触发买入，持有 N 个交易日后卖出。

可见性由 `EventFeed` 按模式（PIT / 非 PIT）设卡，本策略只消费 `ctx.new_events`：
- 增量语义使同一事件天然只触发一次，无需自维护 `seen_ids`；
- 非 PIT 的「穿越未来」在策略层零改动。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import ClassVar

from app.backtest.types import BarContext, Side, Signal

DEFAULT_MIN_SCORE = 50.0
DEFAULT_HOLD_DAYS = 5


@dataclass(frozen=True, slots=True)
class EventDriven:
    name: ClassVar[str] = "event_driven"

    min_score: float = DEFAULT_MIN_SCORE
    hold_days: int = DEFAULT_HOLD_DAYS

    @classmethod
    def from_params(cls, params: Mapping[str, float | int]) -> EventDriven:
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, float | int] = {k: v for k, v in params.items() if k in known}
        if "min_score" in kwargs:
            kwargs["min_score"] = float(kwargs["min_score"])
        if "hold_days" in kwargs:
            kwargs["hold_days"] = int(kwargs["hold_days"])
        return cls(**kwargs)  # type: ignore[arg-type]

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        position = ctx.position
        if not position.is_flat:
            if position.entry_index is None:
                return []
            held = ctx.index - position.entry_index + 1  # 成交日算第 1 天
            if held >= self.hold_days:
                return [Signal(Side.SELL, reason=f"event_driven:hold {held}d")]
            return []  # 持有期内忽略新利多，不加仓

        for event in ctx.new_events:
            if event.direction_norm != "bullish":
                continue
            if event.score is None or event.score < self.min_score:
                continue
            return [
                Signal(
                    Side.BUY,
                    reason=f"event_driven:{event.event_id} score={event.score:g}",
                    event_id=event.event_id,
                )
            ]
        return []
