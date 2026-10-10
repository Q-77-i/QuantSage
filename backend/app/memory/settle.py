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

from collections.abc import Callable, Iterable, Mapping, Sequence
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


@dataclass(frozen=True, slots=True)
class SettledTrip:
    """一条**结算后的回合**（决策记忆的事实层，SPEC §8 D2）。

    `settled=False` 表示**未到期**：期末仍未平仓，窗口只算到账户 `as_of`、不看到账户还没走
    过的日子（那是前视）。未到期的**不给反思**——教训要从已了结的结果里长出来。
    """

    decision_id: str
    symbol: str
    entry_date: date
    exit_date: date | None
    settled: bool
    pnl: float | None
    return_pct: float | None
    benchmark_pct: float | None
    alpha_pp: float | None
    window_days: int
    entry_reason: str = ""
    exit_reason: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "entry_date": self.entry_date.isoformat(),
            "exit_date": self.exit_date.isoformat() if self.exit_date else None,
            "settled": self.settled,
            "pnl": self.pnl,
            "return_pct": self.return_pct,
            "benchmark_pct": self.benchmark_pct,
            "alpha_pp": self.alpha_pp,
            "window_days": self.window_days,
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
        }


#: 基准收益的取数口：给一段交易日，回该窗口的全市场等权收益（取不到给 None）。
#: 做成回调是为了让本模块保持纯函数——真实实现是 `market_benchmark(...).total_return`。
BenchmarkReturn = Callable[[Sequence[date]], "float | None"]


def settle_trips(
    trips: Sequence[Trip],
    *,
    as_of: date,
    market_days: Sequence[date],
    benchmark_return: BenchmarkReturn,
) -> list[SettledTrip]:
    """回合 → 结算结果（含窗口 alpha）。**纯函数**：I/O 全在注入的 `benchmark_return` 里。

    窗口 = `[买入成交日, 卖出成交日]`（未平仓则到 `as_of`）∩ `market_days`；窗口为空
    （成交日不在市场日历里）或基准取不到时，`benchmark_pct` / `alpha_pp` 如实为 `None`。
    """
    ordered = sorted(market_days)
    out: list[SettledTrip] = []
    for trip in trips:
        end = trip.exit_date or as_of
        window = [day for day in ordered if trip.entry_date <= day <= end]
        benchmark_pct = benchmark_return(window) if window else None
        alpha_pp = (
            (trip.return_pct - benchmark_pct) * 100.0
            if trip.return_pct is not None and benchmark_pct is not None
            else None
        )
        out.append(
            SettledTrip(
                decision_id=trip.entry_decision_id,
                symbol=trip.symbol,
                entry_date=trip.entry_date,
                exit_date=trip.exit_date,
                settled=not trip.is_open,
                pnl=trip.pnl,
                return_pct=trip.return_pct,
                benchmark_pct=benchmark_pct,
                alpha_pp=alpha_pp,
                window_days=len(window),
                entry_reason=trip.entry_reason,
                exit_reason=trip.exit_reason,
            )
        )
    return out
