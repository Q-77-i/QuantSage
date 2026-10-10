"""M6 账户账本单测：等额配额、跨标的不串账、卖出释放额度、停牌估值不塌成 0。

账本是纯算术，故这里一律手搓 `Fill` 精确断言数字——不经过策略与数据层，
出问题时能立刻定位到「账算错了」而不是「策略没触发」。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.backtest.costs import CostModel
from app.backtest.types import Fill, Side
from app.paper.account import PaperAccount
from app.paper.types import PaperConfig

DAY = date(2026, 8, 3)


def config(symbols: tuple[str, ...] = ("600519", "000001"), cash: float = 200_000.0) -> PaperConfig:
    return PaperConfig(
        initial_cash=cash,
        symbols=symbols,
        strategy="ma_cross",
        start=DAY,
        end=DAY,
        costs=CostModel(),
    )


def fill(side: Side, qty: int, price: float, fees: float = 5.0) -> Fill:
    """一笔成交。`cash_delta` 按买卖方向手算（与 Broker 同口径）。"""
    delta = -(qty * price + fees) if side is Side.BUY else qty * price - fees
    return Fill(
        trade_date=DAY,
        side=side,
        qty=qty,
        price=price,
        ref_price=price,
        commission=fees,
        stamp_tax=0.0,
        cash_delta=delta,
    )


def test_quota_is_initial_cash_split_evenly() -> None:
    account = PaperAccount(config(cash=200_000.0))
    assert account.budget("600519") == pytest.approx(100_000.0)
    assert account.budget("000001") == pytest.approx(100_000.0)


def test_view_caps_cash_at_the_symbols_quota() -> None:
    """配额是硬顶：账户有 20 万，单只也只准用 10 万。"""
    account = PaperAccount(config(cash=200_000.0))
    assert account.view("600519").cash == pytest.approx(100_000.0)


def test_view_takes_the_smaller_of_cash_and_quota() -> None:
    """费用从现金出、配额只记持仓占用 —— 两者会错开，故取小。"""
    account = PaperAccount(config(cash=200_000.0))
    account.settle("600519", fill(Side.BUY, 900, 100.0, fees=5.0), bar_index=0)
    # 现金 = 200000 - 90005 = 109995；600519 剩余额度 = 100000 - 90005 = 9995
    assert account.cash == pytest.approx(109_995.0)
    assert account.budget("600519") == pytest.approx(9_995.0)
    assert account.view("600519").cash == pytest.approx(9_995.0)
    # 另一只没动过：额度仍是全额，但受账户现金约束（109995 > 100000，故取配额）
    assert account.view("000001").cash == pytest.approx(100_000.0)


def test_symbols_do_not_share_budgets() -> None:
    account = PaperAccount(config(cash=200_000.0))
    account.settle("600519", fill(Side.BUY, 900, 100.0), bar_index=0)
    assert account.budget("000001") == pytest.approx(100_000.0)
    assert account.position("600519").shares == 900
    assert account.position("000001").is_flat


def test_sell_releases_the_quota_and_books_realized_pnl() -> None:
    account = PaperAccount(config(cash=200_000.0))
    account.settle("600519", fill(Side.BUY, 900, 100.0, fees=5.0), bar_index=0)
    account.settle("600519", fill(Side.SELL, 900, 110.0, fees=50.0), bar_index=3)

    assert account.position("600519").is_flat
    assert account.budget("600519") == pytest.approx(100_000.0)  # 额度回到全额
    # 已实现盈亏 = (900×110 − 50) − (900×100 + 5) = 98950 − 90005
    assert account.snapshot().realized_pnl == pytest.approx(8_945.0)
    # 盈利落进现金，不滚入额度
    assert account.cash == pytest.approx(208_945.0)


def test_mark_uses_last_known_close_on_suspension() -> None:
    """停牌日按最近一次已知收盘价估值 —— 不塌成 0（SPEC §7）。"""
    account = PaperAccount(config(cash=200_000.0))
    account.settle("600519", fill(Side.BUY, 900, 100.0), bar_index=0)

    first = account.mark(DAY, {"600519": 120.0, "000001": 10.0})
    assert first.market_value == pytest.approx(108_000.0)

    # 次日 600519 停牌（当日无 bar）：沿用 120.0，而不是 0
    second = account.mark(date(2026, 8, 4), {})
    assert second.market_value == pytest.approx(108_000.0)
    assert second.equity == pytest.approx(account.cash + 108_000.0)


def test_positions_snapshot_only_lists_open_ones() -> None:
    account = PaperAccount(config(cash=200_000.0))
    account.settle("600519", fill(Side.BUY, 900, 100.0), bar_index=0)
    account.settle("600519", fill(Side.SELL, 900, 110.0), bar_index=1)
    assert dict(account.positions) == {}
