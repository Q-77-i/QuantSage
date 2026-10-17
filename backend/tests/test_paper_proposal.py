"""Agent 下单提案（收尾项 5a）：只写待审批决策，不推进账户、不成交。

用真实合成 Parquet + FakeDatabase 验接线：生成走 replay 的同一套定价/风控，
审批后下一次推进按 T+1 开盘成交；策略优先、池外/末端/重复提案都被拒绝。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.backtest.costs import CostModel
from app.backtest.types import Side
from app.data import calendar as cal
from app.paper import PaperConflict, PaperError
from app.paper.proposal import propose_agent_decision
from app.paper.replay import replay
from app.paper.store import config_to_payload, decision_from_row
from app.paper.types import DecisionStatus, PaperConfig, RiskLimits, decision_id
from tests.conftest import make_backtest_dir, write_bars_parquet
from tests.fakes import FakeDatabase

START = date(2026, 8, 3)
SYMBOL = "600519"
USER_ID = 1


class NoSignals:
    name = "none"

    def on_bar(self, ctx):
        return []


def sessions(count: int) -> list[date]:
    days = cal.sessions(START, START + timedelta(days=count * 3 + 10))
    assert len(days) >= count
    return days[:count]


def build_dir(tmp_path: Path, days: list[date]) -> Path:
    rows = [
        {"trade_date": day, "open": 100.0, "close": 100.0, "high": 101.0, "low": 99.0}
        for day in days
    ]
    root = make_backtest_dir(tmp_path, rows, events=[], symbol=SYMBOL)
    write_bars_parquet(root / "bars", SYMBOL, rows, adjust="raw")
    return root


def make_config(days: list[date], risk: RiskLimits | None = None) -> PaperConfig:
    return PaperConfig(
        initial_cash=1_000_000.0,
        symbols=(SYMBOL,),
        strategy="scripted",
        start=days[0],
        end=days[-1],
        costs=CostModel(),
        risk=risk,
    )


async def make_account(db: FakeDatabase, config: PaperConfig, as_of: date) -> str:
    account_id = str(uuid.uuid4())
    await db.create_paper_account(
        account_id=account_id,
        user_id=USER_ID,
        name="提案会话",
        config=config_to_payload(config),
        cash=config.initial_cash,
        as_of=as_of,
        rules={},
        risk_state={},
        positions=[],
        decisions=[],
        equity=[],
        risk_events=[],
    )
    return account_id


@pytest.mark.asyncio
async def test_proposal_is_pending_and_does_not_touch_the_account(tmp_path: Path) -> None:
    days = sessions(3)
    data_dir = build_dir(tmp_path, days)
    config = make_config(days)
    db = FakeDatabase()
    account_id = await make_account(db, config, days[0])

    outcome = await propose_agent_decision(
        db,
        user_id=USER_ID,
        account_id=account_id,
        symbol=SYMBOL,
        side=Side.BUY,
        reason="测试提案",
        data_dir=data_dir,
    )

    assert outcome.decision is not None
    assert outcome.decision["origin"] == "agent"
    assert outcome.decision["status"] == "pending"
    assert outcome.decision["side"] == "buy"
    assert outcome.decision["est_qty"] > 0
    # 账户没有被推进/扣款/建仓
    account = db.paper_accounts[account_id]
    assert account["as_of"] == days[0]
    assert account["cash"] == config.initial_cash
    assert db.paper_positions_rows[account_id] == []
    # 决策真的落进日志（且只有这一条）
    assert len(db.paper_decision_rows) == 1
    stored = next(iter(db.paper_decision_rows.values()))
    assert stored["origin"] == "agent"
    # 审批走既有端点语义（只改状态）
    assert await db.set_paper_decision(outcome.decision["id"], "approved")


@pytest.mark.asyncio
async def test_approved_proposal_fills_at_next_open(tmp_path: Path) -> None:
    days = sessions(3)
    data_dir = build_dir(tmp_path, days)
    config = make_config(days)
    db = FakeDatabase()
    account_id = await make_account(db, config, days[0])

    outcome = await propose_agent_decision(
        db, user_id=USER_ID, account_id=account_id, symbol=SYMBOL,
        side=Side.BUY, reason="测试提案", data_dir=data_dir,
    )
    assert outcome.decision is not None
    await db.set_paper_decision(outcome.decision["id"], "approved")

    stored = tuple(decision_from_row(r) for r in await db.paper_decisions(account_id, 500))
    result = replay(
        config,
        stored,
        through=days[1],
        strategy=NoSignals(),
        account_id=account_id,
        data_dir=data_dir,
    )
    target = next(d for d in result.decisions if d.id == outcome.decision["id"])
    assert target.status is DecisionStatus.FILLED
    assert target.fill is not None
    assert target.fill.qty > 0
    assert result.state.positions[SYMBOL].shares == target.fill.qty


@pytest.mark.asyncio
async def test_risk_gate_blocks_and_records_an_agent_audit(tmp_path: Path) -> None:
    days = sessions(3)
    data_dir = build_dir(tmp_path, days)
    config = make_config(
        days,
        RiskLimits(
            max_gross_exposure=100.0,
            max_position=0.1,  # 预算 1000 元，买不起一手
            stop_loss=100.0,
            max_drawdown=100.0,
        ),
    )
    db = FakeDatabase()
    account_id = await make_account(db, config, days[0])

    outcome = await propose_agent_decision(
        db, user_id=USER_ID, account_id=account_id, symbol=SYMBOL,
        side=Side.BUY, reason="测试提案", data_dir=data_dir,
    )

    assert outcome.decision is None
    assert outcome.blocked is True
    assert "集中度" in outcome.detail or "concentration" in outcome.detail
    assert db.paper_decision_rows == {}
    audits = list(db.paper_audit_rows.values())
    assert len(audits) == 1
    assert audits[0]["origin"] == "agent"
    assert audits[0]["verdict"] == "block"
    assert audits[0]["gate"] == "concentration"


@pytest.mark.asyncio
async def test_same_symbol_same_day_is_rejected_after_the_first(tmp_path: Path) -> None:
    days = sessions(3)
    data_dir = build_dir(tmp_path, days)
    config = make_config(days)
    db = FakeDatabase()
    account_id = await make_account(db, config, days[0])

    await propose_agent_decision(
        db, user_id=USER_ID, account_id=account_id, symbol=SYMBOL,
        side=Side.BUY, reason="第一张", data_dir=data_dir,
    )
    with pytest.raises(PaperConflict):
        await propose_agent_decision(
            db, user_id=USER_ID, account_id=account_id, symbol=SYMBOL,
            side=Side.SELL, reason="第二张", data_dir=data_dir,
        )


@pytest.mark.asyncio
async def test_symbol_outside_the_pool_is_rejected(tmp_path: Path) -> None:
    days = sessions(3)
    data_dir = build_dir(tmp_path, days)
    config = make_config(days)
    db = FakeDatabase()
    account_id = await make_account(db, config, days[0])

    with pytest.raises(PaperError, match="不在本会话的标的池"):
        await propose_agent_decision(
            db, user_id=USER_ID, account_id=account_id, symbol="000001",
            side=Side.BUY, reason="池外", data_dir=data_dir,
        )


@pytest.mark.asyncio
async def test_account_at_end_has_no_t_plus_one(tmp_path: Path) -> None:
    days = sessions(3)
    data_dir = build_dir(tmp_path, days)
    config = make_config(days)
    db = FakeDatabase()
    account_id = await make_account(db, config, days[-1])

    with pytest.raises(PaperConflict, match="T\\+1"):
        await propose_agent_decision(
            db, user_id=USER_ID, account_id=account_id, symbol=SYMBOL,
            side=Side.BUY, reason="末端", data_dir=data_dir,
        )


def test_decision_id_is_the_existing_natural_key() -> None:
    """提案必须用既有确定性 id：否则落库会撞 UNIQUE，整次推进事务回滚。"""
    target = decision_id("acct", date(2026, 8, 3), "600519", Side.BUY)
    assert target == decision_id("acct", date(2026, 8, 3), "600519", Side.BUY)
