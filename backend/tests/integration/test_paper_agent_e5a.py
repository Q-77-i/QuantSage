"""收尾项 5a 真库集成：单条 Agent 决策 + 审计、`as_of` 守卫、批量排除。

离线替身证不了 `add_paper_decision` 的 `INSERT ... SELECT ... FOR UPDATE`、
`ON CONFLICT DO NOTHING` 与 `origin='agent'` 的批量排除——这些只有真 Postgres 能验。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.backtest.types import Side
from app.core.config import get_settings
from app.core.db import Database, open_pool
from app.data import duckdb_client as dc
from app.paper.store import decision_to_row, risk_event_to_row
from app.paper.types import Decision, RiskEvent, decision_id
from tests.integration.conftest import purge_rows, sign_up

pytestmark = pytest.mark.integration

SYMBOL = "600519"


def _window() -> tuple[date, date]:
    latest = date.fromisoformat(str(dc.latest_dates()["latest_trade_date"]))
    return latest - timedelta(days=60), latest


async def _with_db(work):
    async with open_pool(get_settings().postgres_dsn) as pool:
        return await work(Database(pool))


def _create(client: TestClient) -> tuple[str, date, Side]:
    start, end = _window()
    response = client.post(
        "/api/v1/paper/accounts",
        json={
            "name": "5a 集成会话",
            "initial_cash": 1_000_000.0,
            "symbols": [SYMBOL],
            "strategy": "ma_cross",
            "params": {"fast": 1, "slow": 2},
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    detail = response.json()
    account_id = detail["account"]["id"]
    as_of = date.fromisoformat(detail["account"]["as_of"])

    async def pick() -> Side:
        async with open_pool(get_settings().postgres_dsn) as pool:
            db = Database(pool)
            rows = await db.paper_decisions(account_id, 500)
        used = {row["side"] for row in rows if row["trade_date"] == as_of and row["symbol"] == SYMBOL}
        return Side.SELL if "buy" in used else Side.BUY

    side = asyncio.run(pick())
    return account_id, as_of, side


def test_add_agent_decision_guards_as_of_and_excludes_bulk(
    real_stack: TestClient,
) -> None:
    email = f"e5a-agent-{uuid.uuid4().hex[:8]}@example.com"
    client = sign_up(email)
    account_id, as_of, side = _create(client)

    async def run() -> None:
        async with open_pool(get_settings().postgres_dsn) as pool:
            db = Database(pool)
            account = await db.get_paper_account(999999, account_id)
            assert account is None  # 错误用户/不存在同返回 None
            row = await db._one("SELECT id FROM users WHERE email = %s", (email,))
            assert row is not None
            user_id = int(row["id"])
            account = await db.get_paper_account(user_id, account_id)
            assert account is not None
            assert account["as_of"] == as_of

            decision = Decision(
                id=decision_id(account_id, as_of, SYMBOL, side),
                account_id=account_id,
                symbol=SYMBOL,
                trade_date=as_of,
                side=side,
                est_qty=100,
                est_price=100.0,
                reason="集成测试 Agent 提案",
                origin="agent",
            )
            event = RiskEvent(
                id=str(uuid.uuid4()),
                account_id=account_id,
                trade_date=as_of,
                kind="proposal",
                verdict="pass",
                decision_id=decision.id,
                symbol=SYMBOL,
                side=side,
                origin="agent",
                metrics={},
                detail="Agent 提案放行（集成）",
            )
            assert await db.add_paper_decision(
                account_id=account_id,
                user_id=user_id,
                expected_as_of=as_of,
                decision=decision_to_row(decision),
                risk_events=[risk_event_to_row(event)],
            )
            assert await db.paper_pending_agent_count(account_id) == 1
            audits = await db.paper_audit(account_id, 10)
            assert any(a["origin"] == "agent" and a["verdict"] == "pass" for a in audits)

            # 守卫：as_of 不对（模拟并发推进）→ 0 行，不写
            later = as_of + timedelta(days=1)
            assert not await db.add_paper_decision(
                account_id=account_id,
                user_id=user_id,
                expected_as_of=later,
                decision=None,
                risk_events=[],
            )

            # 批量排除 agent：再插一条策略 pending（反方向，避免 UNIQUE 冲突），
            # 批量批准后 agent 仍是 pending，策略单变成 approved。
            other_side = Side.SELL if side is Side.BUY else Side.BUY
            strategy = Decision(
                id=decision_id(account_id, as_of, SYMBOL, other_side),
                account_id=account_id,
                symbol=SYMBOL,
                trade_date=as_of,
                side=other_side,
                est_qty=100,
                est_price=100.0,
                reason="策略单（集成）",
                origin="strategy",
            )
            assert await db.add_paper_decision(
                account_id=account_id,
                user_id=user_id,
                expected_as_of=as_of,
                decision=decision_to_row(strategy),
            )
            changed = await db.set_paper_decisions_bulk(account_id, "approved")
            assert changed == 1
            rows = {str(r["id"]): r for r in await db.paper_decisions(account_id, 500)}
            assert rows[decision.id]["status"] == "pending"
            assert rows[strategy.id]["status"] == "approved"

    try:
        asyncio.run(run())
    finally:
        purge_rows([email], [])
