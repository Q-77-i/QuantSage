"""M6 重放单测：闸门六态、**与回测逐笔等价**、顺延、重放不变量。

头号断言是 `test_all_approved_is_the_backtest`：单标的 + 全部批准 ⇒ 模拟盘的成交与
`run_backtest` 的 `fills` **逐笔相等**（含费用、滑点、股数、原因、event_id）。
它成立的前提是股数在成交时按**开盘价**现算，而不是在提案时定死——这正是 D3 的口径。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from app.backtest.a_share_rules import RejectCode
from app.backtest.costs import CostModel
from app.backtest.engine import BacktestConfig, run_backtest
from app.backtest.types import BarContext, Side, Signal
from app.data import calendar as cal
from app.paper.replay import replay
from app.paper.types import Decision, DecisionStatus, PaperConfig, decision_id
from tests.conftest import make_backtest_dir, write_bars_parquet

START = date(2026, 8, 3)
SYMBOL = "600519"


class Scripted:
    """按 bar 下标脚本化下单（同 `test_backtest_engine.ScriptedStrategy`：注入式、可控）。"""

    name = "scripted"

    def __init__(self, actions: dict[int, list[Signal]]) -> None:
        self._actions = actions

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        return list(self._actions.get(ctx.index, []))


def sessions(count: int, start: date = START) -> list[date]:
    """真实交易日序列。

    **不能用 `conftest.trading_days`**：那是连续日历日，而模拟盘的推进日来自冻结日历，
    周末会被跳过——两边的 bar 序列就对不上，parity 断言会以「引擎跑了 6 根、模拟盘只跑
    4 根」这种形式假红。
    """
    days = cal.sessions(start, start + timedelta(days=count * 3 + 10))
    assert len(days) >= count
    return days[:count]


def flat_rows(days: list[date], base: float = 100.0) -> list[dict]:
    return [
        {
            "trade_date": day,
            "open": base + i,
            "close": base + i,
            "high": base + i + 1,
            "low": base + i - 1,
        }
        for i, day in enumerate(days)
    ]


def build_dir(tmp_path: Path, rows: list[dict], symbol: str = SYMBOL) -> Path:
    """qfq 与 raw 各写一份：涨跌停价要 raw 前收（M5a 口径），只写 qfq 会让两边都降级成「无 band」。"""
    root = make_backtest_dir(tmp_path, rows, events=[], symbol=symbol)
    write_bars_parquet(root / "bars", symbol, rows, adjust="raw")
    return root


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return build_dir(tmp_path, flat_rows(sessions(6)))


def config(end: date, **over: object) -> PaperConfig:
    base: dict = {
        "initial_cash": 1_000_000.0,
        "symbols": (SYMBOL,),
        "strategy": "scripted",
        "start": START,
        "end": end,
        "costs": CostModel(),
    }
    base.update(over)
    return PaperConfig(**base)  # type: ignore[arg-type]


# ── 头号断言：全批 ⇒ 与回测逐笔相等 ──────────────────────────


def test_all_approved_is_the_backtest(data_dir: Path) -> None:
    days = sessions(6)
    actions = {1: [Signal(Side.BUY, reason="进场")], 3: [Signal(Side.SELL, reason="离场")]}
    costs = CostModel()  # 默认口径：佣金万 2.5 最低 5 元 + 印花税 + 滑点 5bps

    expected = run_backtest(
        BacktestConfig(
            symbol=SYMBOL, strategy="scripted", start=START, end=days[-1], costs=costs,
            data_dir=data_dir,
        ),
        strategy=Scripted(actions),
    )
    outcome = replay(
        config(days[-1], costs=costs),
        auto=DecisionStatus.APPROVED,
        strategy=Scripted(actions),
        data_dir=data_dir,
    )

    filled = [d.fill for d in outcome.decisions if d.fill is not None]
    assert filled == list(expected.fills)  # 逐笔相等：股数 / 价格 / 费用 / reason / event_id
    assert outcome.equity_curve[-1].equity == pytest.approx(expected.final_equity)
    assert len(outcome.equity_curve) == len(expected.equity_curve)


def test_est_qty_is_an_estimate_not_the_fill(data_dir: Path) -> None:
    """跳空高开日：提案按前一日收盘价预估的股数会大于实际成交股数——**两个数都留着**。"""
    days = sessions(3)
    rows = flat_rows(days)
    rows[1] = {**rows[1], "open": 120.0, "close": 120.0, "high": 121.0, "low": 119.0}
    root = build_dir(data_dir, rows)

    outcome = replay(
        config(days[-1]),
        auto=DecisionStatus.APPROVED,
        strategy=Scripted({0: [Signal(Side.BUY, reason="进场")]}),
        data_dir=root,
    )
    decision = outcome.decisions[0]
    assert decision.status is DecisionStatus.FILLED
    assert decision.est_price == pytest.approx(100.0 * (1 + 5 / 10_000))  # 决策日收盘 + 滑点
    assert decision.fill is not None and decision.fill.price == pytest.approx(120.0 * (1 + 5 / 10_000))
    assert decision.est_qty > decision.fill.qty  # 按 100 元预估买得起的股数 > 按 120 元成交的


# ── 闸门：未审批不成交 / 驳回改变轨迹 ────────────────────────


def test_unapproved_never_fills(data_dir: Path) -> None:
    outcome = replay(
        config(sessions(6)[-1]),
        strategy=Scripted({1: [Signal(Side.BUY, reason="进场")]}),
        data_dir=data_dir,
    )
    assert [d for d in outcome.decisions if d.fill is not None] == []
    assert all(d.status is DecisionStatus.EXPIRED for d in outcome.decisions)
    assert outcome.state.cash == pytest.approx(1_000_000.0)
    assert dict(outcome.state.positions) == {}


def test_rejection_changes_the_trajectory(data_dir: Path) -> None:
    """驳回一笔买入 ⇒ 后面的卖出落在空仓上（broker 拒单）⇒ 一笔都没成交。

    这条断言验的是「闸门真的改变了账本」——若把驳回当空气，两条轨迹会一模一样。
    两次重放只差**那张买入决策的状态**，其余（后续卖出信号）都由策略当场产生。
    """
    days = sessions(6)
    actions = {1: [Signal(Side.BUY, reason="进场")], 3: [Signal(Side.SELL, reason="离场")]}
    buy = Decision(
        id=decision_id("acc", days[1], SYMBOL, Side.BUY),
        account_id="acc",
        symbol=SYMBOL,
        trade_date=days[1],
        side=Side.BUY,
        est_qty=9_000,
        est_price=101.0,
        reason="进场",
    )

    approved = replay(
        config(days[-1], strategy="scripted"),
        [buy.replace(status=DecisionStatus.APPROVED)],
        auto=DecisionStatus.APPROVED,
        strategy=Scripted(actions),
        data_dir=data_dir,
        account_id="acc",
    )
    rejected = replay(
        config(days[-1], strategy="scripted"),
        [buy.replace(status=DecisionStatus.REJECTED)],
        auto=DecisionStatus.APPROVED,
        strategy=Scripted(actions),
        data_dir=data_dir,
        account_id="acc",
    )

    assert len([d for d in approved.decisions if d.fill is not None]) == 2  # 买 + 卖
    assert [d for d in rejected.decisions if d.fill is not None] == []
    assert (
        next(d for d in rejected.decisions if d.side is Side.BUY).status
        is DecisionStatus.REJECTED
    )
    # 卖出信号照常生成、照常被批准，但空仓卖不出——如实记 `unfilled` 而不是静默消失
    sell = next(d for d in rejected.decisions if d.side is Side.SELL)
    assert sell.status is DecisionStatus.UNFILLED
    assert dict(rejected.state.positions) == {}


def test_approved_but_one_word_limit_up_cannot_fill(tmp_path: Path) -> None:
    """一字涨停日：批了也成交不了（A 股规则），状态是 `unfilled` 而不是 `filled`。"""
    days = sessions(4)
    rows = flat_rows(days)
    rows[1] = {**rows[1], "open": 110.0, "close": 110.0, "high": 110.0, "low": 110.0}  # 前收 100 → 涨停 110
    root = build_dir(tmp_path, rows)

    outcome = replay(
        config(days[-1]),
        auto=DecisionStatus.APPROVED,
        strategy=Scripted({0: [Signal(Side.BUY, reason="进场")]}),
        data_dir=root,
    )
    decision = outcome.decisions[0]
    assert decision.status is DecisionStatus.UNFILLED
    assert decision.reject_code == RejectCode.LIMIT_UP.value
    assert decision.reject_reason is not None and "一字涨停" in decision.reject_reason
    assert outcome.state.cash == pytest.approx(1_000_000.0)


# ── 重放不变量与多标的 ──────────────────────────────────────


def test_replay_is_a_pure_function(data_dir: Path) -> None:
    """同一份（config, 决策日志, 数据）重放两次 ⇒ 账户状态与决策逐字段相等。"""
    days = sessions(6)
    actions = {1: [Signal(Side.BUY, reason="进场")], 3: [Signal(Side.SELL, reason="离场")]}
    first = replay(
        config(days[-1]), auto=DecisionStatus.APPROVED, strategy=Scripted(actions),
        data_dir=data_dir,
    )
    second = replay(
        config(days[-1]), first.decisions, strategy=Scripted(actions), data_dir=data_dir
    )
    assert second.decisions == first.decisions
    assert second.state == first.state
    assert second.equity_curve == first.equity_curve


def test_two_symbols_keep_their_own_books(tmp_path: Path) -> None:
    """两只标的各有配额与持仓：一只买入不影响另一只的额度，净值是两者之和。"""
    days = sessions(4)
    rows = flat_rows(days)
    build_dir(tmp_path, rows, symbol=SYMBOL)
    write_bars_parquet(tmp_path / "bars", "000001", rows, adjust="qfq")
    write_bars_parquet(tmp_path / "bars", "000001", rows, adjust="raw")

    outcome = replay(
        config(days[-1], symbols=(SYMBOL, "000001"), initial_cash=200_000.0),
        auto=DecisionStatus.APPROVED,
        strategy=Scripted({0: [Signal(Side.BUY, reason="两只都买")]}),
        data_dir=tmp_path,
    )
    positions = outcome.state.positions
    assert set(positions) == {SYMBOL, "000001"}
    # 每只配额 10 万、价格约 100 元 ⇒ 各 900 股（含费用后 1000 股买不起）
    assert positions[SYMBOL].shares == positions["000001"].shares == 900
    mark = outcome.equity_curve[-1]
    assert mark.market_value == pytest.approx(2 * 900 * 103.0)  # 第 4 个交易日收盘 103
    assert mark.equity == pytest.approx(outcome.state.cash + mark.market_value)


def test_decisions_are_unique_per_day_and_side(tmp_path: Path) -> None:
    """同一天同一方向的多条信号只落一张决策单（决策 id 是 `(日, 标的, 方向)` 的函数）。"""
    days = sessions(4)
    root = build_dir(tmp_path, flat_rows(days))
    outcome = replay(
        config(days[-1]),
        strategy=Scripted({0: [Signal(Side.BUY, reason="一"), Signal(Side.BUY, reason="二")]}),
        data_dir=root,
    )
    assert len([d for d in outcome.decisions if d.side is Side.BUY]) == 1


def test_only_filled_decisions_carry_a_fill(data_dir: Path) -> None:
    """日志里「已驳回却带成交明细」这类自相矛盾的组合，重放一律把成交清掉。

    账本不受影响（它只认 `filled`），但**读的人会没法判断到底成没成交**——
    驳回之后决策单旁边还挂着一笔成交价，是另一种形式的撒谎。
    """
    days = sessions(6)
    actions = {1: [Signal(Side.BUY, reason="进场")]}
    approved = replay(
        config(days[-1]), auto=DecisionStatus.APPROVED, strategy=Scripted(actions),
        data_dir=data_dir,
    )
    (filled,) = approved.decisions
    assert filled.fill is not None

    rejected = replay(
        config(days[-1]),
        [filled.replace(status=DecisionStatus.REJECTED, fill=None, reject_reason="不要")],
        strategy=Scripted(actions),
        data_dir=data_dir,
    )
    (decision,) = rejected.decisions
    assert decision.status is DecisionStatus.REJECTED
    assert decision.fill is None

    # 就算日志里硬塞一个带成交的「已驳回」，重放也不把它当成交带出去
    dirty = replay(
        config(days[-1]),
        [filled.replace(status=DecisionStatus.REJECTED)],
        strategy=Scripted(actions),
        data_dir=data_dir,
    )
    (cleaned,) = dirty.decisions
    assert cleaned.status is DecisionStatus.REJECTED
    assert cleaned.fill is None
    # 账本与这条清理无关：两种「已驳回」写法算出来的账户状态必须一样
    assert rejected.state == dirty.state
    assert dirty.state.cash == pytest.approx(1_000_000.0)
