"""T4 成本模型：佣金 / 印花税 / 滑点。

纯数据 + 纯函数，不依赖 portfolio/broker，便于单测直接断言数字。
默认值即 SPEC §4 的口径：佣金双边万 2.5、印花税卖出 0.05%、滑点单边 5 bps；
额外的「单笔佣金最低 5 元」是 A 股实盘规则（经用户确认加入）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.backtest.types import Side

BPS = 10_000.0


@dataclass(frozen=True, slots=True)
class CostModel:
    """费用与滑点。两个开关独立，便于验收「手续费/滑点各有可见影响」。"""

    commission_rate: float = 0.00025  # 万 2.5，买卖双边
    commission_min: float = 5.0  # 单笔佣金下限（元）
    stamp_tax_rate: float = 0.0005  # 0.05%，仅卖出
    slippage_bps: float = 5.0  # 单边滑点（基点）
    fee_enabled: bool = True  # 关掉佣金 + 印花税
    slippage_enabled: bool = True  # 单独关滑点

    def fees(self, side: Side, qty: int, price: float) -> tuple[float, float]:
        """返回 `(佣金, 印花税)`。关闭或数量为 0 时均为 0。"""
        if not self.fee_enabled or qty <= 0:
            return (0.0, 0.0)
        notional = qty * price
        commission = max(notional * self.commission_rate, self.commission_min)
        stamp_tax = notional * self.stamp_tax_rate if side is Side.SELL else 0.0
        return (commission, stamp_tax)

    def fill_price(self, side: Side, ref_price: float) -> float:
        """滑点作用于成交价：买入抬价、卖出压价。"""
        if not self.slippage_enabled or self.slippage_bps == 0:
            return ref_price
        ratio = self.slippage_bps / BPS
        return ref_price * (1 + ratio) if side is Side.BUY else ref_price * (1 - ratio)

    @classmethod
    def disabled(cls) -> CostModel:
        """费用与滑点全关。"""
        return cls(fee_enabled=False, slippage_enabled=False)

    def without_slippage(self) -> CostModel:
        return replace(self, slippage_enabled=False)

    def without_fees(self) -> CostModel:
        return replace(self, fee_enabled=False)

    def describe(self) -> str:
        """报告表格里的一格。"""
        fee = (
            f"佣金万{self.commission_rate * BPS:g}(最低{self.commission_min:g}元)"
            f"+印花税{self.stamp_tax_rate:.2%}卖出"
            if self.fee_enabled
            else "费用关"
        )
        slip = f"滑点{self.slippage_bps:g}bps" if self.slippage_enabled else "滑点关"
        return f"{fee} / {slip}"
