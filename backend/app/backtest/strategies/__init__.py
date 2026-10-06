"""内置策略注册表。

新增策略只需在此登记名字 → 策略类，CLI 与（T6 的）API 自动可用。
登记的是**类**而非构造器：API 层要按类拿参数名做校验（见 `known_params`）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields

from app.backtest.strategies.base import Strategy
from app.backtest.strategies.event_driven import EventDriven
from app.backtest.strategies.ma_cross import MaCross
from app.backtest.types import BacktestError

_STRATEGIES: dict[str, type[MaCross] | type[EventDriven]] = {
    MaCross.name: MaCross,
    EventDriven.name: EventDriven,
}


def available_strategies() -> tuple[str, ...]:
    return tuple(_STRATEGIES)


def _lookup(name: str) -> type[MaCross] | type[EventDriven]:
    cls = _STRATEGIES.get(name)
    if cls is None:
        available = "、".join(_STRATEGIES)
        raise BacktestError(f"未知策略 {name!r}；可用策略：{available}")
    return cls


def build_strategy(name: str, params: Mapping[str, float | int] | None = None) -> Strategy:
    return _lookup(name).from_params(params or {})


def known_params(name: str) -> frozenset[str]:
    """该策略接受的参数键。

    `from_params` 会**静默忽略**未知键，接口层不能跟着沉默——否则用户把 `min_score`
    拼成 `minscore` 时，参数没生效却不报错。API 用它把未知键挡成 422。
    """
    return frozenset(f.name for f in fields(_lookup(name)))


__all__ = [
    "Strategy",
    "EventDriven",
    "MaCross",
    "available_strategies",
    "build_strategy",
    "known_params",
]
