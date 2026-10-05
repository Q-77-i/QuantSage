"""内置策略注册表。

新增策略只需在此登记名字 → 构造器，CLI 与（T6 的）API 自动可用。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from app.backtest.strategies.base import Strategy
from app.backtest.strategies.event_driven import EventDriven
from app.backtest.strategies.ma_cross import MaCross
from app.backtest.types import BacktestError

_BUILDERS: dict[str, Callable[[Mapping[str, float | int]], Strategy]] = {
    MaCross.name: MaCross.from_params,
    EventDriven.name: EventDriven.from_params,
}


def available_strategies() -> tuple[str, ...]:
    return tuple(_BUILDERS)


def build_strategy(name: str, params: Mapping[str, float | int] | None = None) -> Strategy:
    builder = _BUILDERS.get(name)
    if builder is None:
        available = "、".join(_BUILDERS)
        raise BacktestError(f"未知策略 {name!r}；可用策略：{available}")
    return builder(params or {})


__all__ = ["Strategy", "EventDriven", "MaCross", "available_strategies", "build_strategy"]
