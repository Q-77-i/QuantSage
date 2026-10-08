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
    """该策略接受的参数键。"""
    return frozenset(f.name for f in fields(_lookup(name)))


def _type_errors(cls: type, params: Mapping[str, float | int]) -> list[str]:
    """按**字段默认值**定参数类型（与用户策略 `PARAMS` 的 `default` 同一口径）。

    不读类型注解：`from __future__ import annotations` 下 `f.type` 是字符串，按它分支
    等于自己解析注解。默认值天然是可信样本：`fast: int = 5`、`min_score: float = 50.0`。
    """
    errors: list[str] = []
    for field in fields(cls):
        value = params.get(field.name)
        if value is None:
            continue
        if isinstance(field.default, bool):
            ok, hint = isinstance(value, bool), "true / false"
        elif isinstance(field.default, int):
            ok = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and float(value).is_integer()
            )
            hint = "整数"
        else:
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            hint = "数字"
        if not ok:
            errors.append(f"参数 {field.name} 需要{hint}（当前 {value!r}）")
    return errors


def validate_params(name: str, params: Mapping[str, float | int]) -> list[str]:
    """未知键 + 类型 + 值域一次校验，返回中文错误串（空列表 = 通过）。

    这是 M4 补的那道口子（原先只有未知键检查）：`from_params` 对 `fast=20 / slow=5`
    照单全收，会静默跑出没有意义的交叉结果——前端 T6c 早就拦了，**绕过前端直接打 API 却拦不住**。
    规则与前端那份同源，不新增限制：前端能发出来的请求，后端一律接受。
    """
    cls = _lookup(name)
    errors: list[str] = []
    unknown = sorted(set(params) - known_params(name))
    if unknown:
        allowed = "、".join(sorted(known_params(name))) or "（无）"
        errors.append(f"{name} 不接受参数 {unknown}；可用：{allowed}")
    type_errors = _type_errors(cls, params)
    errors.extend(type_errors)
    # 类型不干净就不跑值域规则：`"50" <= 100` 会直接抛 TypeError——把 TypeError 当校验结果
    # 报出去，比「类型不对」这条本身更难懂。先把类型定住，值域才谈得上
    if not type_errors:
        errors.extend(cls.validate_params(params))
    return errors


__all__ = [
    "Strategy",
    "EventDriven",
    "MaCross",
    "available_strategies",
    "build_strategy",
    "known_params",
    "validate_params",
]
