"""T4 撮合单测：成交价 / 滑点方向 / 整手取整 / 现金约束 / 拒单语义。"""

from __future__ import annotations

from datetime import date

import pytest

from app.backtest.broker import Broker
from app.backtest.costs import CostModel
from app.backtest.portfolio import Portfolio
from app.backtest.types import Bar, Fill, Position, Side, Signal

DAY = date(2026, 8, 3)
NEXT = date(2026, 8, 4)


def make_bar(open_price: float = 100.0, day: date = NEXT) -> Bar:
    return Bar(
        symbol="600519",
        trade_date=day,
        open=open_price,
        high=open_price * 1.01,
        low=open_price * 0.99,
        close=open_price,
        volume=1e5,
    )


def funded(cash: float = 1_000_000.0) -> Portfolio:
    return Portfolio(cash)


def test_buy_fills_at_open_when_slippage_disabled() -> None:
    broker = Broker(CostModel().without_slippage())
    fill = broker.execute(Signal(Side.BUY, reason="r"), make_bar(100.0), funded())
    assert fill is not None
    assert fill.price == 100.0
    assert fill.ref_price == 100.0
    assert fill.trade_date == NEXT


def test_slippage_raises_buy_and_lowers_sell() -> None:
    costs = CostModel(slippage_bps=10.0)
    broker = Broker(costs)

    buy = broker.execute(Signal(Side.BUY), make_bar(100.0), funded())
    assert buy is not None and buy.price == pytest.approx(100.1)
    assert buy.slippage_cost == pytest.approx(0.1 * buy.qty)

    holding = funded()
    holding.settle(buy, bar_index=0)
    sell = broker.execute(Signal(Side.SELL), make_bar(100.0), holding)
    assert sell is not None and sell.price == pytest.approx(99.9)


def test_buy_quantity_is_floored_to_board_lot() -> None:
    broker = Broker(CostModel().without_slippage().without_fees())
    # 现金 10,050，价格 100 → 100.5 手 → 向下取整 100 股
    fill = broker.execute(Signal(Side.BUY), make_bar(100.0), funded(10_050.0))
    assert fill is not None
    assert fill.qty == 100


def test_buy_never_overdraws_cash_with_minimum_commission() -> None:
    """含最低 5 元佣金时，恰好买满的手数必须能被减手修正。"""
    broker = Broker(CostModel().without_slippage())
    cash = 1_000.0  # 价格 100 → 10 股 → 不足一手；若按 10 股算会超现金
    fill = broker.execute(Signal(Side.BUY), make_bar(100.0), funded(cash))
    if fill is not None:
        assert fill.qty * fill.price + fill.fees <= cash


def test_full_position_buy_exactly_consumes_cash_without_overdraw() -> None:
    broker = Broker(CostModel().without_slippage())
    cash = 10_005.0  # 100 股 × 100 元 + 佣金 5 元 = 10,005，正好买满
    fill = broker.execute(Signal(Side.BUY), make_bar(100.0), funded(cash))
    assert fill is not None
    assert fill.qty == 100
    assert fill.qty * fill.price + fill.fees <= cash


def test_sell_quantity_is_entire_holding() -> None:
    costs = CostModel().without_slippage()
    broker = Broker(costs)
    portfolio = funded()
    buy = broker.execute(Signal(Side.BUY), make_bar(100.0), portfolio)
    assert buy is not None
    portfolio.settle(buy, bar_index=0)

    sell = broker.execute(Signal(Side.SELL), make_bar(110.0), portfolio)
    assert sell is not None
    assert sell.qty == buy.qty
    assert portfolio.position.shares == buy.qty  # settle 前账本未变（broker 无状态）


def test_sell_charges_stamp_tax_but_buy_does_not() -> None:
    broker = Broker(CostModel().without_slippage())
    portfolio = funded()
    buy = broker.execute(Signal(Side.BUY), make_bar(100.0), portfolio)
    assert buy is not None and buy.stamp_tax == 0.0
    portfolio.settle(buy, bar_index=0)

    sell = broker.execute(Signal(Side.SELL), make_bar(100.0), portfolio)
    assert sell is not None
    assert sell.stamp_tax == pytest.approx(sell.qty * 100.0 * 0.0005)


def test_rejects_buy_when_already_holding() -> None:
    """不做金字塔加仓：持仓中再收买入信号一律拒单。"""
    broker = Broker(CostModel())
    portfolio = funded()
    first = broker.execute(Signal(Side.BUY), make_bar(100.0), portfolio)
    assert first is not None
    portfolio.settle(first, bar_index=0)

    assert broker.execute(Signal(Side.BUY), make_bar(101.0), portfolio) is None


def test_rejects_sell_when_flat() -> None:
    broker = Broker(CostModel())
    assert broker.execute(Signal(Side.SELL), make_bar(100.0), funded()) is None


def test_rejects_buy_when_cash_below_one_lot() -> None:
    broker = Broker(CostModel().without_slippage())
    assert broker.execute(Signal(Side.BUY), make_bar(100.0), funded(50.0)) is None


def test_fill_carries_event_id_and_reason_for_traceability() -> None:
    """event_id 是无前视断言与前端 K 线标注的抓手。"""
    broker = Broker(CostModel())
    fill = broker.execute(
        Signal(Side.BUY, reason="event_driven:news:1", event_id="news:1"),
        make_bar(100.0),
        funded(),
    )
    assert fill is not None
    assert fill.event_id == "news:1"
    assert fill.reason == "event_driven:news:1"


def test_costs_disabled_yields_zero_fees_and_reference_price() -> None:
    broker = Broker(CostModel.disabled())
    fill = broker.execute(Signal(Side.BUY), make_bar(100.0), funded())
    assert fill is not None
    assert fill.fees == 0.0
    assert fill.price == 100.0
    assert fill.slippage_cost == 0.0


def test_broker_is_stateless_across_calls() -> None:
    """同一输入重复撮合结果一致（broker 不持有状态）。"""
    broker = Broker(CostModel())
    bar = make_bar(100.0)
    a = broker.execute(Signal(Side.BUY), bar, funded())
    b = broker.execute(Signal(Side.BUY), bar, funded())
    assert isinstance(a, Fill) and isinstance(b, Fill)
    assert (a.qty, a.price, a.fees) == (b.qty, b.price, b.fees)


def test_settle_updates_cash_and_position() -> None:
    broker = Broker(CostModel().without_slippage())
    portfolio = funded(10_005.0)
    fill = broker.execute(Signal(Side.BUY), make_bar(100.0), portfolio)
    assert fill is not None
    portfolio.settle(fill, bar_index=3)

    assert portfolio.cash == pytest.approx(0.0)
    assert portfolio.position.shares == 100
    assert portfolio.position.entry_index == 3
    assert portfolio.position.entry_date == NEXT


def test_settle_pairs_trade_with_net_pnl() -> None:
    """平仓后 Trade.pnl 应是扣掉双边费用的净值。"""
    costs = CostModel().without_slippage()
    broker = Broker(costs)
    portfolio = funded(10_005.0)

    buy = broker.execute(Signal(Side.BUY, reason="入场"), make_bar(100.0), portfolio)
    assert buy is not None
    portfolio.settle(buy, bar_index=0)

    sell = broker.execute(
        Signal(Side.SELL, reason="出场"), make_bar(110.0), portfolio
    )
    assert sell is not None
    portfolio.settle(sell, bar_index=5)

    assert portfolio.position.is_flat
    trade = portfolio.trades[0]
    expected = (110.0 * 100 - sell.fees) - (100.0 * 100 + buy.fees)
    assert trade.pnl == pytest.approx(expected)
    assert trade.hold_bars == 5
    assert trade.entry_reason == "入场"
    assert trade.exit_reason == "出场"


def test_mark_uses_close_price_for_valuation() -> None:
    broker = Broker(CostModel().without_slippage())
    portfolio = funded(10_005.0)
    fill = broker.execute(Signal(Side.BUY), make_bar(100.0), portfolio)
    assert fill is not None
    portfolio.settle(fill, bar_index=0)

    bar = Bar("600519", NEXT, 100.0, 120.0, 90.0, 115.0, 1e5)
    point = portfolio.mark(bar)
    assert point.market_value == pytest.approx(100 * 115.0)
    assert point.equity == pytest.approx(portfolio.cash + 100 * 115.0)


def test_position_is_flat_default() -> None:
    assert Position().is_flat
    assert Portfolio(1_000.0).position.is_flat
