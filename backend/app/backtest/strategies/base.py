"""T4 策略协议（SPEC §4）。

策略是纯函数式的：只读 `BarContext`，返回本根收盘的信号；不持有跨 bar 状态，
需要的历史一律从 `ctx.history` 取（因此天然不可能看到未来）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.backtest.types import BarContext, Signal


@runtime_checkable
class Strategy(Protocol):
    name: str

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        """在 bar 收盘后被调用；返回的信号由引擎留到下一根开盘成交。"""
        ...
