"""双均线策略：MA5 上穿 MA20 金叉全仓买入，下穿死叉全仓卖出。

交叉判定用「前一根 vs 当前根」四值比较，只在**穿越发生的那一根**触发一次；
下一根即使仍在同侧也不会重复触发（无需额外的状态记忆）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import ClassVar

from app.backtest.types import BarContext, Side, Signal

DEFAULT_FAST = 5
DEFAULT_SLOW = 20


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


@dataclass(frozen=True, slots=True)
class MaCross:
    # name 是 ClassVar 而非字段：slots=True 会把普通字段变成类级 slot 描述符，
    # 使 `MaCross.name` 拿不到字符串；ClassVar 同时满足 Protocol 与注册表取键。
    name: ClassVar[str] = "ma_cross"

    fast: int = DEFAULT_FAST
    slow: int = DEFAULT_SLOW

    @classmethod
    def from_params(cls, params: Mapping[str, float | int]) -> MaCross:
        known = {f.name for f in fields(cls)}
        kwargs = {k: int(v) for k, v in params.items() if k in known}
        return cls(**kwargs)

    @classmethod
    def validate_params(cls, params: Mapping[str, float | int]) -> list[str]:
        """值域规则与 T6c 前端表单同源：`fast ≥ 1`、`slow ≥ 2`、`fast < slow`。

        没有这条，`fast=20 / slow=5` 会被 `from_params` 照单全收，静默跑出没有意义的交叉结果。
        """
        fast, slow = params.get("fast"), params.get("slow")
        errors: list[str] = []
        if fast is not None and fast < 1:
            errors.append(f"fast 至少为 1（当前 {fast:g}）")
        if slow is not None and slow < 2:
            errors.append(f"slow 至少为 2（当前 {slow:g}）")
        if fast is not None and slow is not None and fast >= slow:
            errors.append(f"fast({fast:g}) 必须小于 slow({slow:g})，否则双均线交叉没有意义")
        return errors

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        closes = [bar.close for bar in ctx.history]
        index = len(closes) - 1
        # 需要 slow+1 根：算「前一根」与「当前根」两套慢线
        if index < self.slow:
            return []

        fast_now = _mean(closes[index - self.fast + 1 : index + 1])
        fast_prev = _mean(closes[index - self.fast : index])
        slow_now = _mean(closes[index - self.slow + 1 : index + 1])
        slow_prev = _mean(closes[index - self.slow : index])

        golden = fast_prev <= slow_prev and fast_now > slow_now
        death = fast_prev >= slow_prev and fast_now < slow_now

        if golden and ctx.position.is_flat:
            return [Signal(Side.BUY, reason=f"ma_cross:golden MA{self.fast}/MA{self.slow}")]
        if death and not ctx.position.is_flat:
            return [Signal(Side.SELL, reason=f"ma_cross:death MA{self.fast}/MA{self.slow}")]
        return []
