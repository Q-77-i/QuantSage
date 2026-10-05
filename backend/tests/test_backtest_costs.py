"""T4 成本模型单测：佣金（含最低 5 元）/ 印花税（仅卖出）/ 滑点方向与幅度 / 两个开关独立生效。"""

from __future__ import annotations

import pytest

from app.backtest.costs import CostModel
from app.backtest.types import Side


def test_commission_hits_minimum_on_small_order() -> None:
    """小额单按费率算不足 5 元，应抬到 5 元。"""
    model = CostModel()
    commission, _ = model.fees(Side.BUY, 100, 10.0)  # 1000 元 × 万 2.5 = 0.25 元
    assert commission == pytest.approx(5.0)


def test_commission_uses_rate_on_large_order() -> None:
    """大额单按万 2.5 计费，且高于最低值。"""
    model = CostModel()
    commission, _ = model.fees(Side.BUY, 10_000, 100.0)  # 100 万元 × 万 2.5 = 250 元
    assert commission == pytest.approx(250.0)


def test_stamp_tax_only_charged_on_sell() -> None:
    model = CostModel()
    _, buy_tax = model.fees(Side.BUY, 10_000, 100.0)
    _, sell_tax = model.fees(Side.SELL, 10_000, 100.0)
    assert buy_tax == 0.0
    assert sell_tax == pytest.approx(500.0)  # 100 万元 × 0.05%


def test_fee_toggle_zeroes_both_commission_and_stamp_tax() -> None:
    model = CostModel().without_fees()
    buy = model.fees(Side.BUY, 10_000, 100.0)
    sell = model.fees(Side.SELL, 10_000, 100.0)
    assert buy == (0.0, 0.0)
    assert sell == (0.0, 0.0)


def test_zero_qty_charges_nothing() -> None:
    """拒单路径（qty=0）不应产生最低佣金。"""
    assert CostModel().fees(Side.BUY, 0, 100.0) == (0.0, 0.0)


def test_slippage_raises_buy_price_and_lowers_sell_price() -> None:
    model = CostModel(slippage_bps=10.0)
    assert model.fill_price(Side.BUY, 100.0) == pytest.approx(100.1)
    assert model.fill_price(Side.SELL, 100.0) == pytest.approx(99.9)


def test_slippage_disabled_returns_reference_price() -> None:
    model = CostModel(slippage_bps=10.0).without_slippage()
    assert model.fill_price(Side.BUY, 100.0) == 100.0
    assert model.fill_price(Side.SELL, 100.0) == 100.0


def test_slippage_bps_zero_is_a_noop() -> None:
    model = CostModel(slippage_bps=0.0)
    assert model.fill_price(Side.BUY, 100.0) == 100.0


def test_disabled_turns_off_everything() -> None:
    model = CostModel.disabled()
    assert model.fees(Side.SELL, 10_000, 100.0) == (0.0, 0.0)
    assert model.fill_price(Side.BUY, 100.0) == 100.0
    # 费率本身仍保留，便于报告展示「本来是万 2.5」
    assert model.commission_rate == CostModel().commission_rate


def test_fee_and_slippage_toggles_are_independent() -> None:
    """关费用不应关掉滑点，反之亦然——这是验收判据②的前提。"""
    only_slippage = CostModel().without_fees()
    assert only_slippage.fill_price(Side.BUY, 100.0) == pytest.approx(100.05)

    only_fees = CostModel().without_slippage()
    assert only_fees.fees(Side.SELL, 10_000, 100.0) != (0.0, 0.0)
