"""M7a 决策结算：从决策日志配对买卖回合（`app.memory.settle.pair_trips`）。

口径与 `paper/account.py::settle` **同源**——重算出的盈亏之和必须落在账户 `realized_pnl` 上
（真实数据上的容差边界见 SPEC §8：落库价格是 NUMERIC(18,4)，有元级以下舍入差）。
本文件的期望值全部手工算好、写成字面量，不用被测函数反推。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.backtest.types import Fill, Side
from app.memory.settle import Trip, pair_trips  # noqa: F401  （Trip 供类型断言引用）
from app.paper.types import Decision, DecisionStatus


def buy(
    day: date,
    *,
    symbol: str = "600519",
    qty: int = 1000,
    price: float = 10.0,
    commission: float = 5.0,
    did: str = "d-buy",
) -> Decision:
    return Decision(
        id=did,
        account_id="acct",
        symbol=symbol,
        trade_date=day,
        side=Side.BUY,
        est_qty=qty,
        est_price=price,
        reason="MA 金叉",
        status=DecisionStatus.FILLED,
        fill=Fill(
            trade_date=day,
            side=Side.BUY,
            qty=qty,
            price=price,
            ref_price=price,
            commission=commission,
            stamp_tax=0.0,
            cash_delta=-(qty * price + commission),
        ),
    )


def sell(
    day: date,
    *,
    symbol: str = "600519",
    qty: int = 1000,
    price: float = 11.0,
    commission: float = 5.0,
    stamp_tax: float = 5.5,
    did: str = "d-sell",
) -> Decision:
    return Decision(
        id=did,
        account_id="acct",
        symbol=symbol,
        trade_date=day,
        side=Side.SELL,
        est_qty=qty,
        est_price=price,
        reason="持有到期",
        status=DecisionStatus.FILLED,
        fill=Fill(
            trade_date=day,
            side=Side.SELL,
            qty=qty,
            price=price,
            ref_price=price,
            commission=commission,
            stamp_tax=stamp_tax,
            cash_delta=qty * price - commission - stamp_tax,
        ),
    )


def test_open_trip_has_no_pnl_and_reports_open() -> None:
    """未平仓的买入：`exit_*` 与 `pnl` 都是 None——估值要收盘价，配对这一步不编价。"""
    trips = pair_trips([buy(date(2026, 9, 30), did="d-open")])

    assert len(trips) == 1
    trip = trips[0]
    assert trip.is_open
    assert trip.exit_date is None
    assert trip.exit_decision_id is None
    assert trip.pnl is None
    assert trip.return_pct is None
    assert trip.entry_decision_id == "d-open"


def test_second_buy_replaces_the_pending_lot_like_the_ledger_does() -> None:
    """连续两笔买入：**后买覆盖前买**（账本 `settle()` 就是无条件替换持仓）。

    买1：1000 × 10.00 + 5 = 10,005；买2：500 × 20.00 + 5 = 10,005；
    卖：500 × 22.00 − 5 − 5.5 = 10,989.5 → pnl = 984.50（用**买2**的成本）
    """
    trips = pair_trips(
        [
            buy(date(2026, 8, 3), qty=1000, price=10.0, did="d-buy-1"),
            buy(date(2026, 8, 4), qty=500, price=20.0, did="d-buy-2"),
            sell(date(2026, 8, 10), qty=500, price=22.0, did="d-sell"),
        ]
    )

    assert len(trips) == 1
    trip = trips[0]
    assert trip.entry_decision_id == "d-buy-2"
    assert trip.qty == 500
    assert trip.pnl == pytest.approx(984.50)


def test_sell_without_a_lot_makes_no_trip() -> None:
    """空仓卖出只动现金、不计盈亏（账本的防御分支），不成回合。"""
    assert pair_trips([sell(date(2026, 8, 10))]) == []


def test_unfilled_decisions_never_enter_pairing() -> None:
    """只有 `filled` 进配对；被驳回 / 过期 / 未成交一律不结算（SPEC §8 D2）。"""
    rejected = buy(date(2026, 8, 3), did="d-rej").replace(
        status=DecisionStatus.REJECTED, fill=None
    )
    expired = buy(date(2026, 8, 4), did="d-exp").replace(
        status=DecisionStatus.EXPIRED, fill=None
    )

    assert pair_trips([rejected, expired]) == []


def test_symbols_do_not_cross_contaminate() -> None:
    """多标的各配各的：A 的卖出不该平掉 B 的仓。"""
    trips = pair_trips(
        [
            buy(date(2026, 8, 3), symbol="600519", did="d-mt-buy"),
            sell(date(2026, 8, 10), symbol="000001", qty=100, price=11.0, did="d-pa-sell"),
        ]
    )

    assert len(trips) == 1
    assert trips[0].symbol == "600519"
    assert trips[0].is_open


def test_recomputed_pnl_sum_equals_the_account_ledger() -> None:
    """**头号不变量**：回合重算之和 == 账本 `realized_pnl`（同一把尺子）。

    真值来自**账本本身**（`PaperAccount.settle`，独立于本模块的实现），不是手算常数——
    配对规则一旦漂移（比如改成 FIFO），这条立刻红。
    """
    from app.backtest.types import Position
    from app.paper.account import PaperAccount
    from app.paper.types import PaperConfig

    decisions = [
        buy(date(2026, 8, 3), qty=1000, price=10.0, did="d1"),
        sell(date(2026, 8, 10), qty=1000, price=11.0, did="d2"),
        buy(date(2026, 8, 12), qty=500, price=20.0, did="d3"),
        buy(date(2026, 8, 13), qty=300, price=21.0, did="d4"),  # 覆盖 d3
        sell(date(2026, 8, 20), qty=300, price=19.0, did="d5"),
    ]
    account = PaperAccount(
        PaperConfig(initial_cash=100_000.0, symbols=("600519",), strategy="event_driven",
                    start=date(2026, 8, 1), end=date(2026, 8, 31))
    )
    for index, decision in enumerate(decisions):
        assert decision.fill is not None
        account.settle("600519", decision.fill, index)

    trips = pair_trips(decisions)
    recomputed = sum(t.pnl for t in trips if t.pnl is not None)

    assert recomputed == pytest.approx(account.snapshot().realized_pnl)
    assert isinstance(account.snapshot().positions.get("600519", Position()), Position)


def test_mark_open_trips_fills_float_pnl_and_leaves_closed_alone() -> None:
    """未平仓按给定收盘价回填浮盈；缺价的标的保持 None（不编价）；已平仓原样。"""
    from app.memory.settle import mark_open_trips

    closed = pair_trips([buy(date(2026, 8, 3)), sell(date(2026, 8, 10))])
    opened = pair_trips(
        [
            buy(date(2026, 9, 29), symbol="600519", did="d-open"),
            buy(date(2026, 9, 29), symbol="000001", qty=100, price=11.0, did="d-open-2"),
        ]
    )
    marked = mark_open_trips([*closed, *opened], {"600519": 12.0})

    # 已平仓的不动
    assert marked[0].pnl == pytest.approx(984.50)
    # 未平仓、有价：12.00 × 1000 − 5 − 10.00 × 1000 = 1,995.00
    assert marked[1].pnl == pytest.approx(1995.00)
    assert marked[1].return_pct == pytest.approx(1995.0 / 10005.0)
    # 未平仓、缺价：留 None
    assert marked[2].pnl is None


def test_settle_closed_trip_computes_window_alpha() -> None:
    """已到期回合：窗口 = [买入成交日, 卖出成交日]，alpha = 回合收益 − 同窗口全市场等权。

    手算：return = 984.50 / 10,005 = 0.0984008；基准给 0.02 ⇒ alpha = (0.0984008 − 0.02)×100
    = 7.84008 pp。窗口只含区间内的交易日（8/3–8/7、8/10 共 6 天）。
    """
    from app.memory.settle import settle_trips

    trips = pair_trips([buy(date(2026, 8, 3)), sell(date(2026, 8, 10))])
    days = [date(2026, 8, day) for day in (3, 4, 5, 6, 7, 10)]
    calls: list[list[date]] = []

    def benchmark(window: object) -> float:
        calls.append(list(window))  # type: ignore[arg-type]
        return 0.02

    settled = settle_trips(
        trips, as_of=date(2026, 9, 30), market_days=days, benchmark_return=benchmark
    )

    item = settled[0]
    assert item.settled and item.decision_id == "d-buy" and item.symbol == "600519"
    assert item.entry_date == date(2026, 8, 3) and item.exit_date == date(2026, 8, 10)
    assert item.window_days == 6
    assert item.pnl == pytest.approx(984.50)
    assert item.return_pct == pytest.approx(0.0984008, rel=1e-6)
    assert item.benchmark_pct == pytest.approx(0.02)
    assert item.alpha_pp == pytest.approx(7.84008, rel=1e-6)
    assert item.entry_reason == "MA 金叉" and item.exit_reason == "持有到期"
    assert calls == [days]


def test_open_trip_windows_to_as_of_not_to_the_data_end() -> None:
    """未平仓：窗口算到**账户 as_of**——不看到账户还没走过的日子（那是前视）。

    手算：浮盈 1,995.00 / 10,005 = 0.1994003；基准 0.01 ⇒ alpha = 18.94003 pp。
    """
    from app.memory.settle import mark_open_trips, settle_trips

    trips = mark_open_trips(
        pair_trips([buy(date(2026, 9, 29), did="d-open")]), {"600519": 12.0}
    )
    days = [date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 8)]
    seen: list[list[date]] = []

    def benchmark(window: object) -> float:
        seen.append(list(window))  # type: ignore[arg-type]
        return 0.01

    settled = settle_trips(
        trips, as_of=date(2026, 9, 30), market_days=days, benchmark_return=benchmark
    )

    item = settled[0]
    assert not item.settled
    assert item.exit_date is None and item.exit_reason == ""
    assert item.window_days == 2
    assert seen == [days[:2]]
    assert item.pnl == pytest.approx(1995.0)
    assert item.return_pct == pytest.approx(0.1994003, rel=1e-6)
    assert item.alpha_pp == pytest.approx(18.94003, rel=1e-6)


def test_trip_without_mark_keeps_money_fields_none() -> None:
    """缺价的未平仓：pnl / return / alpha 全 None（不编），到期状态与窗口照记。"""
    from app.memory.settle import settle_trips

    trips = pair_trips([buy(date(2026, 9, 29), did="d-open")])
    settled = settle_trips(
        trips,
        as_of=date(2026, 9, 30),
        market_days=[date(2026, 9, 29), date(2026, 9, 30)],
        benchmark_return=lambda window: 0.01,
    )

    item = settled[0]
    assert not item.settled
    assert item.pnl is None and item.return_pct is None and item.alpha_pp is None
    assert item.window_days == 2


def test_empty_window_skips_the_benchmark_call() -> None:
    """窗口内没有交易日（如成交日在市场日历之外）⇒ 不调基准，两个基准字段都是 None。"""
    from app.memory.settle import settle_trips

    trips = pair_trips([buy(date(2026, 8, 3)), sell(date(2026, 8, 10))])
    calls: list[object] = []

    def benchmark(window: object) -> float:
        calls.append(window)
        return 0.02

    settled = settle_trips(
        trips, as_of=date(2026, 9, 30), market_days=[], benchmark_return=benchmark
    )

    assert calls == []
    assert settled[0].window_days == 0
    assert settled[0].benchmark_pct is None and settled[0].alpha_pp is None


def test_benchmark_absence_leaves_alpha_none() -> None:
    """基准取不到（窗口内无行情样本）时 benchmark_pct 是 None ⇒ alpha 也只能是 None。"""
    from app.memory.settle import settle_trips

    trips = pair_trips([buy(date(2026, 8, 3)), sell(date(2026, 8, 10))])
    settled = settle_trips(
        trips,
        as_of=date(2026, 9, 30),
        market_days=[date(2026, 8, 3), date(2026, 8, 10)],
        benchmark_return=lambda window: None,
    )

    assert settled[0].benchmark_pct is None
    assert settled[0].alpha_pp is None
    assert settled[0].return_pct is not None


def test_closed_trip_matches_hand_computed_ledger_arithmetic() -> None:
    """一笔买 + 一笔卖 = 一个已平仓回合，pnl 按账本公式手算。

    买：1000 × 10.00 + 佣金 5.00           = 成本 10,005.00
    卖：1000 × 11.00 − 佣金 5.00 − 印花 5.50 = 净得 10,989.50
    pnl = 984.50；return = 984.50 / 10,005.00 = 0.0984008
    """
    trips = pair_trips([buy(date(2026, 8, 3)), sell(date(2026, 8, 10))])

    assert len(trips) == 1
    trip = trips[0]
    assert trip.symbol == "600519"
    assert trip.entry_date == date(2026, 8, 3)
    assert trip.exit_date == date(2026, 8, 10)
    assert trip.qty == 1000
    assert trip.entry_decision_id == "d-buy"
    assert trip.exit_decision_id == "d-sell"
    assert trip.pnl == pytest.approx(984.50)
    assert trip.return_pct == pytest.approx(0.0984008, rel=1e-6)
    assert not trip.is_open
