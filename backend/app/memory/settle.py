"""决策结算（M7）：从决策日志配对买卖回合。**纯函数、不碰数据库、不调模型。**

口径与 `paper/account.py::settle` **同源**（那是账本累计已实现盈亏的地方）：
    pnl = (卖价 × 卖量 − 卖费用) − (买价 × 买量 + 买费用)
账户账本「一标的一份持仓、卖出即清仓」，所以配对规则照着它来：

* **买入即开仓**；持仓未平时又来一笔买入 ⇒ **后买覆盖前买**（账本 `settle()` 就是无条件
  替换 `_positions[symbol]`；不照抄这条，重算之和就对不上 `realized_pnl`）；
* **卖出即平仓**，空仓卖出只动现金、不计盈亏（账本走的是同一个防御分支）；
* 只有 `filled` 且有 `fill` 的决策进入配对；其余四态（被驳回 / 过期 / 未成交 / 待审）
  **不做反事实收益**（SPEC §8 D2），由复盘层单列状态。

两条已知边界，写在这里免得后来人 debugging：

1. **舍入**：落库成交价与费用是 `NUMERIC(18,4)`，而账本用全精度浮点算过一遍——
   重算之和与 `realized_pnl` 有**元级以下**差异（真实数据实测 0.01 / 0.53 元）。
   故对账断言用容差，界面「已实现盈亏」取账本数，回合明细只用于归因与复盘。
2. **同日同标的的买卖**：库里没有成交序号，唯一能恢复的确定性口径是「**先买后卖**」
   （同日一笔买一笔卖时，账本会先建仓再平掉；反序则是空仓卖出 + 开仓）。
   真实数据实测 0 例（规划期探针）；一旦出现，按此口径配对。

未平仓回合的 `pnl` 一律为 `None`——估值是行情的事（`mark_open_trips` 需要收盘价），
本模块不做 I/O、也就不编价格。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date

from app.backtest.types import Side
from app.paper.types import Decision, DecisionStatus


@dataclass(frozen=True, slots=True)
class Trip:
    """一次「买 → 卖」回合（未平仓时 `exit_*` 与 `pnl` 为 `None`）。

    不复用回测的 `Trade`：那个是引擎产物、**必已平仓**、也没有决策 id；而模拟盘里
    「期末仍未平仓」是**多数形态**（实测 14 笔买单里 9 笔落在数据末端那天），
    拿一个必平仓的类型去装它，只会逼出一个假的 exit_date。
    """

    symbol: str
    entry_date: date
    entry_decision_id: str
    qty: int
    entry_price: float
    entry_fees: float
    entry_reason: str = ""
    exit_date: date | None = None
    exit_decision_id: str | None = None
    exit_price: float | None = None
    exit_fees: float | None = None
    exit_reason: str = ""
    pnl: float | None = None
    return_pct: float | None = None

    @property
    def is_open(self) -> bool:
        return self.exit_date is None

    @property
    def cost(self) -> float:
        """建仓成本（含买入费用）——账本 `budget()` 与平仓成本用的是同一个口径。"""
        return self.entry_price * self.qty + self.entry_fees


def _sorted_fills(decisions: Iterable[Decision]) -> list[Decision]:
    """成交决策按（成交日，先买后卖，id）排序——见模块 docstring 的第 2 条边界。"""
    filled = [d for d in decisions if d.status is DecisionStatus.FILLED and d.fill is not None]
    return sorted(
        filled,
        key=lambda d: (d.fill.trade_date, 0 if d.side is Side.BUY else 1, d.id),  # type: ignore[union-attr]
    )


def pair_trips(decisions: Sequence[Decision]) -> list[Trip]:
    """把决策日志配成回合列表：已平仓的在前（按建仓日），未平仓的在后（按建仓日）。

    排序只是**展示稳定**（两次调用逐字段相等），配对本身按成交日推进。
    """
    open_lots: dict[str, Trip] = {}
    closed: list[Trip] = []

    for decision in _sorted_fills(decisions):
        fill = decision.fill
        assert fill is not None  # `_sorted_fills` 已保证
        if fill.side is Side.BUY:
            # 后买覆盖前买（账本同款语义）：被覆盖的那笔就此消失，与账本一致
            open_lots[decision.symbol] = Trip(
                symbol=decision.symbol,
                entry_date=fill.trade_date,
                entry_decision_id=decision.id,
                qty=fill.qty,
                entry_price=fill.price,
                entry_fees=fill.fees,
                entry_reason=decision.reason,
            )
            continue

        lot = open_lots.pop(decision.symbol, None)
        if lot is None:
            continue  # 空仓卖出：账本只动现金、不计盈亏，这里同样不成一个回合
        proceeds = fill.price * fill.qty - fill.fees
        pnl = proceeds - lot.cost
        closed.append(
            replace(
                lot,
                exit_date=fill.trade_date,
                exit_decision_id=decision.id,
                exit_price=fill.price,
                exit_fees=fill.fees,
                exit_reason=decision.reason,
                pnl=pnl,
                return_pct=(pnl / lot.cost) if lot.cost else None,
            )
        )

    closed.sort(key=lambda t: (t.entry_date, t.entry_decision_id))
    opened = sorted(open_lots.values(), key=lambda t: (t.entry_date, t.entry_decision_id))
    return [*closed, *opened]


def mark_open_trips(trips: Sequence[Trip], closes: Mapping[str, float]) -> list[Trip]:
    """给未平仓回合按给定收盘价回填浮动盈亏；已平仓的原样返回。

    `closes` 由调用方按**账户 `as_of` 那天（或之前最近一根有价 bar）**取好再传进来——
    本模块不做 I/O；缺价的标的**保持 `pnl=None`**，不编价（同自选股「取不到即留空」口径）。
    """
    out: list[Trip] = []
    for trip in trips:
        if not trip.is_open:
            out.append(trip)
            continue
        price = closes.get(trip.symbol)
        if price is None or price <= 0:
            out.append(trip)
            continue
        pnl = price * trip.qty - trip.entry_fees - trip.entry_price * trip.qty
        out.append(
            replace(trip, pnl=pnl, return_pct=(pnl / trip.cost) if trip.cost else None)
        )
    return out
