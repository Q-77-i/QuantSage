"""Agent 的模拟盘工具（收尾项 5a）：上下文、文本口径与「只提案」边界。

工具是给模型看的文本，不是程序契约；这里断言的是模型能不能据此说对话：
有没有会话、提案是否待审批、失败原因是不是人话。
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.agent import tools as agent_tools
from app.agent.context import AgentContext, reset_agent_context, set_agent_context
from app.paper import PaperError
from app.paper.proposal import ProposalOutcome
from tests.fakes import FakeDatabase


def context(db: FakeDatabase) -> object:
    return set_agent_context(AgentContext(user_id=1, db=db))


@pytest.mark.asyncio
async def test_paper_tools_refuse_without_request_context() -> None:
    listed = await agent_tools.list_paper_accounts.ainvoke({})
    proposed = await agent_tools.propose_paper_order.ainvoke(
        {"account_id": str(uuid.uuid4()), "symbol": "600519", "side": "buy", "reason": "x"}
    )
    assert "不可用" in listed
    assert "不可用" in proposed


@pytest.mark.asyncio
async def test_list_accounts_is_readable() -> None:
    db = FakeDatabase()
    from app.paper.store import config_to_payload
    from app.paper.types import PaperConfig

    account_id = str(uuid.uuid4())
    config = PaperConfig(
        initial_cash=200_000.0,
        symbols=("600519", "300750"),
        strategy="ma_cross",
        start=date(2026, 8, 3),
        end=date(2026, 8, 7),
    )
    await db.create_paper_account(
        account_id=account_id,
        user_id=1,
        name="我的会话",
        config=config_to_payload(config),
        cash=200_000.0,
        as_of=date(2026, 8, 3),
        rules={},
        risk_state={},
        positions=[],
        decisions=[],
        equity=[],
        risk_events=[],
    )
    token = context(db)
    try:
        text = await agent_tools.list_paper_accounts.ainvoke({})
    finally:
        reset_agent_context(token)

    assert "我的会话" in text
    assert account_id in text
    assert "600519、300750" in text
    assert "200,000" in text


@pytest.mark.asyncio
async def test_proposal_tool_never_claims_a_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_propose(*args, **kwargs) -> ProposalOutcome:
        return ProposalOutcome(
            account_id=kwargs["account_id"],
            as_of=date(2026, 8, 3),
            decision={
                "id": str(uuid.uuid4()),
                "symbol": "600519",
                "side": "buy",
                "est_qty": 900,
                "trade_date": "2026-08-03",
                "status": "pending",
                "status_label": "待审批",
                "origin": "agent",
            },
        )

    monkeypatch.setattr("app.paper.proposal.propose_agent_decision", fake_propose)
    token = context(FakeDatabase())
    try:
        text = await agent_tools.propose_paper_order.ainvoke(
            {"account_id": "acct-1", "symbol": "600519", "side": "buy", "reason": "测试"}
        )
    finally:
        reset_agent_context(token)

    assert "待审批" in text
    assert "不能批准或成交" in text
    assert "已成交" not in text
    assert "已买入" not in text
    assert "900" in text


@pytest.mark.asyncio
async def test_proposal_tool_reports_gate_block(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_propose(*args, **kwargs) -> ProposalOutcome:
        return ProposalOutcome(
            account_id="acct-1",
            as_of=date(2026, 8, 3),
            decision=None,
            blocked=True,
            detail="单标的集中度超限，缩量后不足一手",
        )

    monkeypatch.setattr("app.paper.proposal.propose_agent_decision", fake_propose)
    token = context(FakeDatabase())
    try:
        text = await agent_tools.propose_paper_order.ainvoke(
            {"account_id": "acct-1", "symbol": "600519", "side": "buy", "reason": "测试"}
        )
    finally:
        reset_agent_context(token)

    assert "风控拦截" in text
    assert "未生成决策" in text


@pytest.mark.asyncio
async def test_proposal_tool_rejects_unknown_side() -> None:
    token = context(FakeDatabase())
    try:
        text = await agent_tools.propose_paper_order.ainvoke(
            {"account_id": "acct-1", "symbol": "600519", "side": "hold", "reason": "x"}
        )
    finally:
        reset_agent_context(token)
    assert "buy 或 sell" in text


@pytest.mark.asyncio
async def test_proposal_tool_turns_domain_error_into_text(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*args, **kwargs):
        raise PaperError("600519 不在本会话的标的池里")

    monkeypatch.setattr("app.paper.proposal.propose_agent_decision", boom)
    token = context(FakeDatabase())
    try:
        text = await agent_tools.propose_paper_order.ainvoke(
            {"account_id": "acct-1", "symbol": "600519", "side": "buy", "reason": "测试"}
        )
    finally:
        reset_agent_context(token)
    assert "提案未创建" in text
    assert "标的池" in text
