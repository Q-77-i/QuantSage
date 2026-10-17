"""E5 风控纯函数单测：四闸门判据、缩量取整、止损成本口径、回撤快照。

这些函数不读库、不碰 replay——出问题时能立刻分清是「判据错了」还是「接线错了」。
数字一律手算：成本/权益/市值都是整数或 .05 级别的滑点，期望值不依赖被测函数。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.backtest.costs import CostModel
from app.backtest.types import Position
from app.paper.risk import (
    drawdown_pct,
    evaluate_buy,
    exposure,
    risk_state,
    stop_loss_hits,
)
from app.paper.types import RiskLimits

DAY = date(2026, 8, 3)
COSTS = CostModel()


def limits(**over: float) -> RiskLimits:
    base = {
        "max_gross_exposure": 95.0,
        "max_position": 60.0,
        "stop_loss": 6.0,
        "max_drawdown": 10.0,
    }
    base.update(over)
    return RiskLimits(**base)  # type: ignore[arg-type]


def position(shares: int = 1000, entry: float = 100.0, fees: float = 5.0) -> Position:
    return Position(shares=shares, entry_price=entry, entry_fees=fees, entry_date=DAY)


def buy(**over: object):
    base: dict = {
        "limits": limits(),
        "costs": COSTS,
        "requested_qty": 4900,
        "budget": 500_000.0,
        "est_price": 100.05,
        "equity": 1_000_000.0,
        "market_value": 0.0,
        "symbol_value": 0.0,
        "pending_notional": 0.0,
        "drawdown": 0.0,
        "has_position": False,
        "stop_loss_pending": False,
    }
    base.update(over)
    return evaluate_buy(**base)  # type: ignore[arg-type]


# ── 配置与快照 ──────────────────────────────────────────────


def test_risk_limits_must_be_percentages_in_range() -> None:
    for bad in (0.0, -1.0, 100.1):
        with pytest.raises(ValueError):
            RiskLimits(bad, 30.0, 6.0, 10.0)


def test_exposure_ignores_flat_and_missing_prices() -> None:
    positions = {
        "600519": position(1000),
        "000001": Position(),  # 空仓不占市值
        "300750": position(500, entry=200.0),  # 没有价格 → 不计入
    }
    market_value, equity, values = exposure(positions, {"600519": 110.0}, 50_000.0)
    assert market_value == pytest.approx(110_000.0)
    assert equity == pytest.approx(160_000.0)
    assert values == {"600519": pytest.approx(110_000.0)}


def test_risk_state_reports_percentages_and_drawdown() -> None:
    state = risk_state(
        as_of=DAY,
        positions={"600519": position(1000)},
        prices={"600519": 110.0},
        cash=500_000.0,
        peak=1_000_000.0,
        limits=limits(max_drawdown=10.0),
    )
    assert state.equity == pytest.approx(610_000.0)
    assert state.drawdown_pct == pytest.approx(39.0)
    assert state.gross_exposure_pct == pytest.approx(110_000 / 610_000 * 100)
    assert state.max_position_pct == pytest.approx(110_000 / 610_000 * 100)
    assert state.blocked_by_drawdown is True


def test_risk_state_is_not_blocked_below_the_drawdown_limit() -> None:
    state = risk_state(
        as_of=DAY,
        positions={},
        prices={},
        cash=1_000_000.0,
        peak=1_000_000.0,
        limits=limits(max_drawdown=10.0),
    )
    assert state.drawdown_pct == pytest.approx(0.0)
    assert state.gross_exposure_pct == pytest.approx(0.0)
    assert state.max_position_pct == pytest.approx(0.0)
    assert state.blocked_by_drawdown is False


def test_risk_state_with_non_positive_equity_has_no_ratios() -> None:
    state = risk_state(
        as_of=DAY,
        positions={},
        prices={},
        cash=0.0,
        peak=1_000_000.0,
        limits=limits(),
    )
    assert state.equity == 0.0
    assert state.drawdown_pct is None
    assert state.gross_exposure_pct is None
    assert state.max_position_pct is None
    assert state.blocked_by_drawdown is True


def test_drawdown_peak_zero_is_undefined() -> None:
    assert drawdown_pct(0.0, 0.0) is None


# ── 止损 ────────────────────────────────────────────────────


def test_stop_loss_uses_cost_basis_including_entry_fees() -> None:
    # 成本 = 100 × 1000 + 5 = 100,005；收盘 90 → 亏损 10.0045% ≥ 10%
    hits = stop_loss_hits({"600519": position()}, {"600519": 90.0}, 10.0)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.symbol == "600519"
    assert hit.cost_basis == pytest.approx(100_005.0)
    assert hit.market_value == pytest.approx(90_000.0)
    assert hit.loss_pct == pytest.approx(10.0045, abs=1e-4)


def test_stop_loss_does_not_fire_below_the_threshold() -> None:
    assert stop_loss_hits({"600519": position()}, {"600519": 95.0}, 10.0) == ()


def test_stop_loss_skips_flat_positions_and_missing_prices() -> None:
    positions = {"000001": Position(), "600519": position()}
    assert stop_loss_hits(positions, {"600519": 50.0}, 1.0)[0].symbol == "600519"
    assert stop_loss_hits({"600519": position()}, {}, 1.0) == ()


def test_stop_loss_is_deterministic_by_symbol() -> None:
    positions = {"600519": position(), "000001": position()}
    hits = stop_loss_hits(positions, {"600519": 90.0, "000001": 90.0}, 10.0)
    assert [hit.symbol for hit in hits] == ["000001", "600519"]


# ── 买入闸门 ────────────────────────────────────────────────


def test_buy_passes_when_the_requested_size_is_under_all_limits() -> None:
    check = buy(limits=limits(max_position=60.0, max_gross_exposure=95.0))
    assert check.verdict == "pass"
    assert check.gate is None
    assert check.qty == 4900


def test_buy_is_capped_by_single_name_concentration() -> None:
    # 权益 100 万、上限 30% ⇒ 可买 30 万；价格 100.05 ⇒ 2,900 股（3,000 股会超过 30 万）
    check = buy(limits=limits(max_position=30.0))
    assert check.verdict == "cap"
    assert check.gate == "concentration"
    assert check.qty == 2900
    assert check.metrics["requested_qty"] == 4900
    assert check.metrics["cap_position"] == pytest.approx(300_000.0)


def test_buy_is_capped_by_gross_exposure() -> None:
    # 已持仓 90 万、权益 100 万、总仓位上限 95% ⇒ 只剩 5 万；价格 100.05 ⇒ 400 股
    check = buy(
        limits=limits(max_position=100.0, max_gross_exposure=95.0),
        market_value=900_000.0,
    )
    assert check.verdict == "cap"
    assert check.gate == "gross_exposure"
    assert check.qty == 400
    assert check.metrics["cap_gross"] == pytest.approx(50_000.0)


def test_buy_is_blocked_when_the_cap_buys_less_than_one_lot() -> None:
    check = buy(limits=limits(max_position=100.0, max_gross_exposure=95.0), market_value=995_000.0)
    assert check.verdict == "block"
    assert check.gate == "gross_exposure"
    assert check.qty == 0
    assert "不足一手" in check.detail


def test_buy_is_blocked_when_already_holding() -> None:
    check = buy(has_position=True)
    assert check.verdict == "block"
    assert check.gate == "position_open"


def test_buy_is_blocked_when_drawdown_is_at_or_above_the_limit() -> None:
    check = buy(drawdown=10.0)
    assert check.verdict == "block"
    assert check.gate == "drawdown"
    assert "回撤" in check.detail


def test_buy_is_blocked_when_a_stop_loss_exit_is_pending() -> None:
    check = buy(stop_loss_pending=True)
    assert check.verdict == "block"
    assert check.gate == "stop_loss_pending"


def test_buy_is_blocked_when_equity_is_non_positive() -> None:
    check = buy(equity=0.0)
    assert check.verdict == "block"
    assert check.gate == "drawdown"
    assert "权益" in check.detail


def test_buy_metrics_carry_the_inputs_needed_to_recompute_the_verdict() -> None:
    check = buy(limits=limits(max_position=30.0), pending_notional=100_000.0)
    metrics = check.metrics
    assert metrics["pending_notional"] == pytest.approx(100_000.0)
    assert metrics["projected_gross_pct"] == pytest.approx(
        (100_000.0 + 4900 * 100.05) / 1_000_000.0 * 100
    )
    assert metrics["projected_position_pct"] == pytest.approx(4900 * 100.05 / 1_000_000.0 * 100)
