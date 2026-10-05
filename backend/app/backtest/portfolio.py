"""T4 有状态账本：现金、持仓、权益曲线、开平配对。

与 broker 分离是为了让 broker 保持无状态纯函数：撮合只做「信号 → 成交」的映射，
账本变动一律经 `settle()` 单点发生。
"""

from __future__ import annotations

from app.backtest.types import (
    Bar,
    EquityPoint,
    Fill,
    Position,
    Side,
    Trade,
)


class Portfolio:
    """单标的、只做多、不加仓的极简账本（P1 范围）。"""

    def __init__(self, initial_cash: float) -> None:
        if initial_cash <= 0:
            raise ValueError("初始资金必须为正")
        self._cash = initial_cash
        self._position = Position()
        self._trades: list[Trade] = []
        self._curve: list[EquityPoint] = []

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def position(self) -> Position:
        return self._position

    @property
    def trades(self) -> tuple[Trade, ...]:
        """仅已平仓交易（T5 算胜率时不被半截交易污染）。"""
        return tuple(self._trades)

    @property
    def equity_curve(self) -> tuple[EquityPoint, ...]:
        return tuple(self._curve)

    def settle(self, fill: Fill, bar_index: int) -> None:
        """按成交更新账本：空仓买入 → 开仓；满仓卖出 → 平仓并配对 `Trade`。"""
        self._cash += fill.cash_delta
        if fill.side is Side.BUY:
            self._position = Position(
                shares=fill.qty,
                entry_price=fill.price,
                entry_fees=fill.fees,
                entry_date=fill.trade_date,
                entry_index=bar_index,
                entry_reason=fill.reason,
            )
            return

        entry = self._position
        if entry.is_flat or entry.entry_index is None or entry.entry_date is None:
            # broker 已拦截空仓卖出；此处是防御性分支，不应在生产路径触发
            return
        cost = entry.entry_price * entry.shares + entry.entry_fees
        proceeds = fill.price * fill.qty - fill.fees
        self._trades.append(
            Trade(
                entry_date=entry.entry_date,
                entry_price=entry.entry_price,
                qty=entry.shares,
                entry_fees=entry.entry_fees,
                entry_reason=entry.entry_reason,
                exit_date=fill.trade_date,
                exit_price=fill.price,
                exit_fees=fill.fees,
                exit_reason=fill.reason,
                pnl=proceeds - cost,
                return_pct=(proceeds - cost) / cost if cost else 0.0,
                hold_bars=bar_index - entry.entry_index,
            )
        )
        self._position = Position()

    def mark(self, bar: Bar) -> EquityPoint:
        """按收盘价估值并追加一个权益点（每根 bar 一点）。"""
        market_value = self._position.shares * bar.close
        point = EquityPoint(
            trade_date=bar.trade_date,
            cash=self._cash,
            market_value=market_value,
            equity=self._cash + market_value,
            close=bar.close,
        )
        self._curve.append(point)
        return point
