"""多标的账户账本：现金共享、每标的一份持仓、每标的一份等额配额。

与 `backtest.portfolio.Portfolio` 的关系值得写清楚，免得后来人以为这里该复用它：
那个是**单标的、单持仓、只做多、不加仓**的极简账本（P1 范围），现金是它自己的；
这里是「一个账户的若干标的」，现金共享、持仓每标的各一份、每只还有一份配额。
语义上不是同一件东西——硬套会让 `Portfolio` 多出一个它不该知道的维度。

但**撮合与费用一行都不复制**：`Broker` 只读 `cash` 与 `position` 两个属性，故用 `SymbolView`
把「该标的的配额现金 + 该标的的持仓」喂给同一个 `Broker` 即可。这是「模拟盘与回测同口径」
在代码结构上的落点（SPEC §7），不是一句口径约定。

不记 `Trade` 开平配对（回测的 `Portfolio` 有）：模拟盘的账本条目是**决策**，复盘看的是决策流水
与已实现盈亏；开平配对留给 M7 按需从决策日志算——本轮不提前造一份可能与它不一致的副本。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from app.backtest.types import Fill, Position, Side
from app.paper.types import AccountState, EquityMark, PaperConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SymbolView:
    """一只标的眼里的账户：**该标的的配额现金**与**该标的的持仓**（撮合那一刻的快照）。

    做成快照而不是活引用：`Broker` 本就是无状态纯撮合（同一 `Signal + Bar + View` 必得同一
    `Fill`），快照让这条性质在多标的账户上照样成立。
    """

    cash: float
    position: Position


class PaperAccount:
    """账户账本。变动只经 `settle()`（成交）与 `mark()`（估值）两个入口。"""

    def __init__(self, config: PaperConfig) -> None:
        if config.initial_cash <= 0:
            raise ValueError("初始资金必须为正")
        if not config.symbols:
            raise ValueError("标的池不能为空")
        self._config = config
        self._cash = config.initial_cash
        self._positions: dict[str, Position] = {}
        self._realized_pnl = 0.0
        #: 每只标的最近一次已知收盘价——停牌日估值沿用它，**不塌成 0**（SPEC §7）
        self._last_close: dict[str, float] = {}

    # ── 读 ──────────────────────────────────────────────────

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def positions(self) -> Mapping[str, Position]:
        """非空持仓（按标的名排序，便于逐字段比对与界面稳定展示）。"""
        return {s: p for s, p in sorted(self._positions.items()) if not p.is_flat}

    def position(self, symbol: str) -> Position:
        return self._positions.get(symbol, Position())

    def budget(self, symbol: str) -> float:
        """该标的还剩多少**配额**可用：配额 − 现持仓成本（含买入费用）。

        持仓成本口径与 `Portfolio` 算平仓成本的那一处同源（`entry_price × shares + entry_fees`）。
        卖出后持仓清空，额度自动回到全额——**盈利不滚入额度**，它落进账户现金。
        """
        position = self._positions.get(symbol)
        if position is None or position.is_flat:
            return self._config.quota
        cost = position.entry_price * position.shares + position.entry_fees
        return max(self._config.quota - cost, 0.0)

    def view(self, symbol: str) -> SymbolView:
        """喂给 `Broker` 的那一刻快照。

        `min(账户现金, 该标的配额)` 里的 `min` 是必要的：费用从**现金**出、配额只记**持仓占用**，
        两者会错开（多笔买入的手续费累积后，现金可能低于某只标的的剩余配额）。
        """
        return SymbolView(cash=min(self._cash, self.budget(symbol)), position=self.position(symbol))

    # ── 写 ──────────────────────────────────────────────────

    def settle(self, symbol: str, fill: Fill, bar_index: int) -> None:
        """按成交更新账本：买入开仓（记成本与费用）、卖出平仓（结转已实现盈亏）。"""
        self._cash += fill.cash_delta
        if fill.side is Side.BUY:
            self._positions[symbol] = Position(
                shares=fill.qty,
                entry_price=fill.price,
                entry_fees=fill.fees,
                entry_date=fill.trade_date,
                entry_index=bar_index,
                entry_reason=fill.reason,
            )
            return

        entry = self._positions.get(symbol)
        if entry is None or entry.is_flat:
            # broker 已拦空仓卖出；此处是防御性分支，不应在生产路径触发
            log.warning("空仓卖出落到账本上（%s）：只动了现金", symbol)
            return
        cost = entry.entry_price * entry.shares + entry.entry_fees
        self._realized_pnl += (fill.price * fill.qty - fill.fees) - cost
        self._positions.pop(symbol, None)

    def mark(self, trade_date: date, closes: Mapping[str, float]) -> EquityMark:
        """按收盘价估值并返回当日估值点。

        `closes` 只带**当日有 bar** 的标的；其余持仓沿用最近一次已知收盘价——
        「当天没成交」不是「一文不值」，把它当 0 会让净值在那个缺口上凭空塌下去。
        """
        for symbol, close in closes.items():
            if close > 0:
                self._last_close[symbol] = close

        market_value = 0.0
        for symbol, position in self._positions.items():
            if position.is_flat:
                continue
            price = self._last_close.get(symbol)
            if price is None:
                # 结构上不该发生（建仓那天必然写过 last_close）。真拿到脏数据时按成本价估值
                # 并把事实记下来——静默当 0 会把净值砸出一个假坑。
                price = position.entry_price
                log.warning("标的 %s 无已知收盘价，按成本价 %.4f 估值", symbol, price)
            market_value += position.shares * price

        return EquityMark(
            trade_date=trade_date,
            cash=self._cash,
            market_value=market_value,
            equity=self._cash + market_value,
        )

    def snapshot(self) -> AccountState:
        return AccountState(
            cash=self._cash, positions=self.positions, realized_pnl=self._realized_pnl
        )
