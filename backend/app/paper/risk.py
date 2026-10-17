"""E5 风控：**纯函数闸门**。不读库、不调模型、不写状态——判据只在这里，处置只在 replay。

四个闸门（SPEC §6.1）：

| 闸门 | 判据 | 处置 |
|---|---|---|
| 总仓位 | `(MV + N + 当日待成交买入) / E > max_gross_exposure` | 缩量到不越线；不足一手 → 拦截 |
| 单标的集中度 | `(P_i + N) / E > max_position` | 缩量到不越线；不足一手 → 拦截 |
| 止损 | `(成本 - 市值) / 成本 ≥ stop_loss` | 生成 `approved` 风控卖出（由 replay 做） |
| 回撤 | `(峰值 - E) / 峰值 ≥ max_drawdown` | 禁新开仓；恢复到阈值下自动解禁 |

配置一律是**百分比**（0–100），内部换算成小数；`RiskState` 也回百分比，界面不再换算。
「数量由策略决定」在 M6 不成立——策略只给方向，股数由 `affordable_qty` 算——所以
仓位/集中度只有**缩量**才有意义；硬拦截只用于已持仓、止损挂单中、回撤超限这三类
「无论买多少都不该开仓」的条件。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from app.backtest.a_share_rules import LOT_SIZE
from app.backtest.broker import affordable_qty
from app.backtest.costs import CostModel
from app.backtest.types import Position
from app.paper.types import RiskLimits, RiskState

#: 闸门 → 中文（`detail` 由服务端出，前端不自己拼一句——同 M5b 的 `overfit.reason_text` 规矩）
GATE_LABELS: dict[str, str] = {
    "gross_exposure": "总仓位上限",
    "concentration": "单标的集中度上限",
    "position_open": "已持仓不加仓",
    "stop_loss_pending": "止损挂单中",
    "drawdown": "回撤超限",
}


@dataclass(frozen=True, slots=True)
class BuyCheck:
    """一次买入提案的风控结论。`qty=0` 且 `verdict=block` 表示不生成决策。"""

    qty: int
    verdict: str  # pass | cap | block
    gate: str | None = None
    metrics: Mapping[str, float | int | None] = field(default_factory=dict)
    detail: str = ""

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"


@dataclass(frozen=True, slots=True)
class StopLossHit:
    """一只持仓触发止损时的事实（replay 据此生成风控卖出与审计行）。"""

    symbol: str
    shares: int
    entry_price: float
    entry_fees: float
    cost_basis: float
    market_value: float
    loss_pct: float


def pct(value: float) -> float:
    """百分比 → 小数。"""
    return value / 100.0


def exposure(
    positions: Mapping[str, Position], prices: Mapping[str, float], cash: float
) -> tuple[float, float, dict[str, float]]:
    """返回 `(持仓市值, 权益, 每只标的的市值)`。缺价格的持仓不计入市值（与 mark 同口径，调用方先兜底）。"""
    values = {
        symbol: position.shares * prices[symbol]
        for symbol, position in positions.items()
        if not position.is_flat and symbol in prices
    }
    market_value = sum(values.values())
    return market_value, cash + market_value, values


def drawdown_pct(peak: float, equity: float) -> float | None:
    """峰值回撤（百分比）。峰值 ≤ 0 时无定义，返回 None。"""
    if peak <= 0:
        return None
    return max(0.0, (peak - equity) / peak * 100.0)


def risk_state(
    *,
    as_of: date,
    positions: Mapping[str, Position],
    prices: Mapping[str, float],
    cash: float,
    peak: float,
    limits: RiskLimits | None,
) -> RiskState:
    """收盘快照。权益 ≤ 0 时三个比率都是 None（界面显示「—」），并按「禁新开仓」处理。"""
    market_value, equity, values = exposure(positions, prices, cash)
    if equity <= 0:
        return RiskState(
            as_of=as_of,
            peak_equity=peak,
            equity=equity,
            drawdown_pct=None,
            gross_exposure_pct=None,
            max_position_pct=None,
            blocked_by_drawdown=limits is not None,
        )
    drawdown = drawdown_pct(peak, equity)
    blocked = bool(limits is not None and drawdown is not None and drawdown >= limits.max_drawdown)
    return RiskState(
        as_of=as_of,
        peak_equity=peak,
        equity=equity,
        drawdown_pct=drawdown,
        gross_exposure_pct=market_value / equity * 100.0,
        max_position_pct=(max(values.values()) / equity * 100.0) if values else 0.0,
        blocked_by_drawdown=blocked,
    )


def stop_loss_hits(
    positions: Mapping[str, Position],
    prices: Mapping[str, float],
    stop_loss: float,
) -> tuple[StopLossHit, ...]:
    """逐持仓判定止损（按标的排序，结果确定）。

    成本口径 = `entry_price × shares + entry_fees`（含买入费用）；亏损率 = `(成本 - 市值) / 成本`。
    缺价格或价格为非正时**不判**（调用方应先用 `mark` 的兜底价填好 `prices`）——静默当成
    「没触发」会把一个数据缺口伪装成安全。
    """
    threshold = pct(stop_loss)
    hits: list[StopLossHit] = []
    for symbol in sorted(positions):
        position = positions[symbol]
        if position.is_flat:
            continue
        price = prices.get(symbol)
        if price is None or price <= 0:
            continue
        cost = position.entry_price * position.shares + position.entry_fees
        if cost <= 0:
            continue
        value = position.shares * price
        loss = (cost - value) / cost
        if loss >= threshold:
            hits.append(
                StopLossHit(
                    symbol=symbol,
                    shares=position.shares,
                    entry_price=position.entry_price,
                    entry_fees=position.entry_fees,
                    cost_basis=cost,
                    market_value=value,
                    loss_pct=loss * 100.0,
                )
            )
    return tuple(hits)


def evaluate_buy(
    *,
    limits: RiskLimits,
    costs: CostModel,
    requested_qty: int,
    budget: float,
    est_price: float,
    equity: float,
    market_value: float,
    symbol_value: float,
    pending_notional: float,
    drawdown: float | None,
    has_position: bool,
    stop_loss_pending: bool,
) -> BuyCheck:
    """对一次买入提案跑四个闸门。`requested_qty > 0` 由调用方保证（预算不足一手不进来）。

    处置优先级：硬拦截（已持仓 / 止损挂单 / 回撤 / 权益 ≤ 0）→ 缩量 → 放行。
    先判硬拦截再算缩量，是为了让「已经不该开仓」的提案不去算一个没有意义的股数。
    """
    base: dict[str, float | int | None] = {
        "requested_qty": requested_qty,
        "final_qty": 0,
        "budget": budget,
        "est_price": est_price,
        "equity": equity,
        "market_value": market_value,
        "symbol_value": symbol_value,
        "pending_notional": pending_notional,
        "drawdown_pct": drawdown,
        "max_gross_exposure": limits.max_gross_exposure,
        "max_position": limits.max_position,
        "stop_loss": limits.stop_loss,
        "max_drawdown": limits.max_drawdown,
    }

    if has_position:
        return BuyCheck(0, "block", "position_open", base, GATE_LABELS["position_open"])
    if stop_loss_pending:
        return BuyCheck(0, "block", "stop_loss_pending", base, GATE_LABELS["stop_loss_pending"])
    if equity <= 0:
        return BuyCheck(0, "block", "drawdown", base, "权益 ≤ 0，禁止开新仓")
    if drawdown is not None and drawdown >= limits.max_drawdown:
        return BuyCheck(
            0,
            "block",
            "drawdown",
            base,
            f"回撤 {drawdown:.2f}% ≥ 上限 {limits.max_drawdown:.2f}%，暂停开新仓",
        )

    cap_position = max(equity * pct(limits.max_position) - symbol_value, 0.0)
    cap_gross = max(equity * pct(limits.max_gross_exposure) - market_value - pending_notional, 0.0)
    risk_budget = min(cap_position, cap_gross)
    gate = "concentration" if cap_position <= cap_gross else "gross_exposure"
    final_qty = affordable_qty(costs, min(budget, risk_budget), est_price)

    projected_position = symbol_value + requested_qty * est_price
    projected_gross = market_value + pending_notional + requested_qty * est_price
    base.update(
        {
            "final_qty": final_qty,
            "cap_position": cap_position,
            "cap_gross": cap_gross,
            "risk_budget": risk_budget,
            "gross_exposure_pct": market_value / equity * 100.0,
            "position_pct": symbol_value / equity * 100.0,
            "projected_gross_pct": projected_gross / equity * 100.0,
            "projected_position_pct": projected_position / equity * 100.0,
        }
    )

    if final_qty < LOT_SIZE:
        return BuyCheck(
            0,
            "block",
            gate,
            base,
            f"{GATE_LABELS[gate]}下可买预算不足一手（{risk_budget:,.0f} 元）",
        )
    if final_qty < requested_qty:
        return BuyCheck(
            final_qty,
            "cap",
            gate,
            base,
            f"{GATE_LABELS[gate]}缩量：{requested_qty} → {final_qty} 股",
        )
    return BuyCheck(final_qty, "pass", None, base, "")


__all__ = [
    "GATE_LABELS",
    "BuyCheck",
    "StopLossHit",
    "drawdown_pct",
    "evaluate_buy",
    "exposure",
    "pct",
    "risk_state",
    "stop_loss_hits",
]
