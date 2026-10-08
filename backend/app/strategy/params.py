"""用户策略参数：`PARAMS` schema 的校验与取值归一（纯函数）。

schema 形如：

    PARAMS = {"fast": {"type": "int", "default": 5, "min": 1, "max": 250, "label": "快线周期"}}

三条口径：

- **schema 必须是字面量**——静态解析（`api.parse_meta`）用 `ast.literal_eval` 读它，
  编辑器要据此渲染参数表单而不执行用户代码；非字面量在解析期即被拒（M4b 的检查器给行号）
- **未给的参数一律用 `default` 填满**：策略里可以放心写 `p["fast"]`，不必 `.get(..., 5)`
- **只拒绝、不静默改写**：未知键按「可用键」列出（防拼错后参数没生效却不报错，同内置策略
  `known_params` 的口径）；`int` 要求整值；`min`/`max` 双向夹取

内置策略的值校验（`fast < slow` 这类跨字段约束）走各自 `validate_params`，不在这层——
schema 只能表达单字段约束，跨字段是策略自己的业务判断。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.strategy import StrategyRejected

#: `type` 的取值 → Python 类型。`bool` 单列：Python 里 `isinstance(True, int)` 为真，
#: 不先判 bool 会让 `default: true` 通过 `int` 的检查。
_TYPE_NAMES = ("int", "float", "bool")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class ParamSpec:
    name: str
    type: str
    default: int | float | bool
    min: float | None = None
    max: float | None = None
    label: str = ""


def parse_param_schema(raw: object, *, where: str = "PARAMS") -> dict[str, ParamSpec]:
    """把用户声明的字面量字典解析成 `ParamSpec`；不合规抛 `StrategyRejected`。"""
    if not isinstance(raw, dict):
        raise StrategyRejected(
            f"{where} 必须是字典，形如 {{'fast': {{'type': 'int', 'default': 5}}}}；"
            f"当前是 {type(raw).__name__}"
        )
    specs: dict[str, ParamSpec] = {}
    for name, item in raw.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise StrategyRejected(f"{where} 的键必须是合法标识符（当前 {name!r}）")
        if not isinstance(item, dict):
            raise StrategyRejected(
                f"{where}[{name!r}] 必须是字典，形如 {{'type': 'int', 'default': 5}}"
            )
        ptype = item.get("type")
        if ptype not in _TYPE_NAMES:
            raise StrategyRejected(
                f"{where}[{name!r}]['type'] 必须是 {'、'.join(_TYPE_NAMES)} 之一"
                f"（当前 {ptype!r}）"
            )
        if "default" not in item:
            raise StrategyRejected(f"{where}[{name!r}] 缺 'default'：缺省值同时决定参数类型")
        default = item["default"]
        _check_value_shape(name, ptype, default, where)
        lo, hi = item.get("min"), item.get("max")
        for bound_name, bound in (("min", lo), ("max", hi)):
            if bound is not None and not _is_number(bound):
                raise StrategyRejected(f"{where}[{name!r}]['{bound_name}'] 必须是数字")
        if lo is not None and hi is not None and lo > hi:
            raise StrategyRejected(f"{where}[{name!r}] 的 min({lo}) 大于 max({hi})")
        label = item.get("label", "")
        if not isinstance(label, str):
            raise StrategyRejected(f"{where}[{name!r}]['label'] 必须是字符串")
        specs[name] = ParamSpec(name=name, type=ptype, default=default, min=lo, max=hi, label=label)
    return specs


def _check_value_shape(name: str, ptype: str, value: object, where: str) -> None:
    ok = (
        (ptype == "bool" and isinstance(value, bool))
        or (ptype == "int" and isinstance(value, int) and not isinstance(value, bool))
        or (ptype == "float" and _is_number(value))
    )
    if not ok:
        raise StrategyRejected(
            f"{where}[{name!r}]['default'] 与 type={ptype!r} 不符（当前 {value!r}）"
        )


def coerce(spec: ParamSpec, value: object) -> int | float | bool:
    """单值归一：类型不符或越界即拒绝，**不做静默取整 / 夹取**。"""
    if spec.type == "bool":
        if not isinstance(value, bool):
            raise StrategyRejected(f"参数 {spec.name} 需要 true / false（当前 {value!r}）")
        return value
    if spec.type == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StrategyRejected(f"参数 {spec.name} 需要整数（当前 {value!r}）")
        if float(value) != int(value):
            raise StrategyRejected(f"参数 {spec.name} 需要整数（当前 {value!r}）")
        result: int | float = int(value)
    else:
        if not _is_number(value):
            raise StrategyRejected(f"参数 {spec.name} 需要数字（当前 {value!r}）")
        result = float(value)
    if spec.min is not None and result < spec.min:
        raise StrategyRejected(f"参数 {spec.name}={result} 小于下限 {spec.min:g}")
    if spec.max is not None and result > spec.max:
        raise StrategyRejected(f"参数 {spec.name}={result} 大于上限 {spec.max:g}")
    return result


def validate_params(
    schema: Mapping[str, ParamSpec], values: Mapping[str, Any]
) -> dict[str, int | float | bool]:
    """按 schema 校验并填满缺省值，返回**可直接传给策略**的完整参数字典。"""
    unknown = sorted(set(values) - set(schema))
    if unknown:
        allowed = "、".join(sorted(schema)) or "（无）"
        raise StrategyRejected(f"不接受参数 {unknown}；可用：{allowed}")
    return {
        name: coerce(spec, values[name] if name in values else spec.default)
        for name, spec in schema.items()
    }


__all__ = ["ParamSpec", "coerce", "parse_param_schema", "validate_params"]
