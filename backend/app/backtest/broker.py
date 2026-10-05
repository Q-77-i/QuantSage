"""T4 撮合：市价单以**下一根 bar 的开盘价**成交（由 engine 保证调用时机）。

无状态纯撮合：给定同一 `Signal + Bar + Portfolio`，结果恒定，便于用手搓 bar 精确断言。
A 股整手 100 股向下取整；买入按可用现金精确递减，保证跳空高开也不打穿现金。
"""

from __future__ import annotations

import logging

from app.backtest.costs import CostModel
from app.backtest.portfolio import Portfolio
from app.backtest.types import Bar, Fill, Side, Signal

log = logging.getLogger(__name__)

LOT_SIZE = 100


class Broker:
    def __init__(self, costs: CostModel, lot_size: int = LOT_SIZE) -> None:
        self._costs = costs
        self._lot = lot_size

    @property
    def costs(self) -> CostModel:
        return self._costs

    def execute(self, signal: Signal, bar: Bar, portfolio: Portfolio) -> Fill | None:
        """撮合一根 bar 的开盘价；返回 None 表示拒单（已记日志，不抛异常）。"""
        if signal.side is Side.BUY:
            return self._buy(signal, bar, portfolio)
        return self._sell(signal, bar, portfolio)

    def _buy(self, signal: Signal, bar: Bar, portfolio: Portfolio) -> Fill | None:
        if not portfolio.position.is_flat:
            log.info("拒单：已持仓，忽略买入信号（%s）", signal.reason or "无原因")
            return None

        price = self._costs.fill_price(Side.BUY, bar.open)
        qty = self._affordable_qty(portfolio.cash, price)
        if qty <= 0:
            log.info("拒单：现金不足以买入一手（现金 %.2f，价格 %.4f）", portfolio.cash, price)
            return None

        commission, stamp_tax = self._costs.fees(Side.BUY, qty, price)
        return Fill(
            trade_date=bar.trade_date,
            side=Side.BUY,
            qty=qty,
            price=price,
            ref_price=bar.open,
            commission=commission,
            stamp_tax=stamp_tax,
            cash_delta=-(qty * price + commission + stamp_tax),
            reason=signal.reason,
            event_id=signal.event_id,
        )

    def _sell(self, signal: Signal, bar: Bar, portfolio: Portfolio) -> Fill | None:
        holding = portfolio.position
        if holding.is_flat:
            log.info("拒单：空仓，忽略卖出信号（%s）", signal.reason or "无原因")
            return None

        # 全额卖出不取整：持仓恒为整手买入的结果，不存在零股
        qty = holding.shares
        price = self._costs.fill_price(Side.SELL, bar.open)
        commission, stamp_tax = self._costs.fees(Side.SELL, qty, price)
        return Fill(
            trade_date=bar.trade_date,
            side=Side.SELL,
            qty=qty,
            price=price,
            ref_price=bar.open,
            commission=commission,
            stamp_tax=stamp_tax,
            cash_delta=qty * price - commission - stamp_tax,
            reason=signal.reason,
            event_id=signal.event_id,
        )

    def _affordable_qty(self, cash: float, price: float) -> int:
        """按整手向下取整，再逐步减手直到「成交金额 + 费用」不超现金。

        含最低佣金时可能需减 1~2 手，故用循环而非一次估算（跳空高开也不打穿现金）。
        """
        qty = int(cash // price) // self._lot * self._lot
        while qty > 0:
            commission, stamp_tax = self._costs.fees(Side.BUY, qty, price)
            if qty * price + commission + stamp_tax <= cash:
                return qty
            qty -= self._lot
        return 0
