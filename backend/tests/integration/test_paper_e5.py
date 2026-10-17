"""E5 集成：真库上的审计表 / 风控快照 / 重放对账 / 级联删除。

离线替身证明不了 `decision_audit` 的 DDL、JSONB 往返与级联——这条用例打真 Postgres
与真实 `data/`（fast=1/slow=2 保证 60 天窗口内一定出信号，审计不会空跑）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import Database, init_schema, open_pool
from app.data import duckdb_client as dc
from app.paper.replay import replay
from app.paper.store import config_from_payload, decision_from_row
from tests.integration.conftest import purge_rows, sign_up

pytestmark = pytest.mark.integration

SYMBOL = "600519"
RISK = {
    "max_gross_exposure": 95.0,
    "max_position": 30.0,
    "stop_loss": 6.0,
    "max_drawdown": 20.0,
}


def _window() -> tuple[date, date]:
    latest = date.fromisoformat(str(dc.latest_dates()["latest_trade_date"]))
    return latest - timedelta(days=60), latest


def _create(client, name: str, **over: object):
    start, end = _window()
    payload: dict = {
        "name": name,
        "initial_cash": 1_000_000.0,
        "symbols": [SYMBOL],
        "strategy": "ma_cross",
        "params": {"fast": 1, "slow": 2},
        "start": start.isoformat(),
        "end": end.isoformat(),
        "risk": RISK,
    }
    payload.update(over)
    return client.post("/api/v1/paper/accounts", json=payload)


def _run(coro):
    return asyncio.run(coro)


async def _with_db(work):
    async with open_pool(get_settings().postgres_dsn) as pool:
        return await work(Database(pool))


def test_schema_has_e5_columns_and_audit_table() -> None:
    """`risk_state` / `origin` 两列与 `decision_audit` 表都幂等就位（连跑两次 init_schema）。"""

    async def check() -> None:
        async with open_pool(get_settings().postgres_dsn) as pool:
            await init_schema(pool)
            await init_schema(pool)
            db = Database(pool)
            rows = await db._all(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_name IN ('paper_accounts', 'paper_decisions') "
                "AND column_name IN ('risk_state', 'origin')",
                (),
            )
            names = {(row["table_name"], row["column_name"]) for row in rows}
            assert ("paper_accounts", "risk_state") in names
            assert ("paper_decisions", "origin") in names
            audit = await db._all("SELECT to_regclass('decision_audit') AS reg", ())
            assert audit[0]["reg"] is not None

    _run(check())


def test_audit_row_with_null_decision_id_inserts(real_stack: TestClient) -> None:
    """回撤状态切换的审计行没有决策 id（`NULL::uuid`）——真库上必须能落。"""
    emails = [f"e5-null-{uuid.uuid4().hex[:8]}@example.com"]
    account_id = ""
    try:
        client = sign_up(emails[0])
        user_id = int(client.get("/api/v1/auth/me").json()["id"])
        detail = _create(client, "空决策 id 审计").json()
        account_id = detail["account"]["id"]
        event_id = str(uuid.uuid4())
        trade_date = date.fromisoformat(detail["account"]["as_of"])

        async def scenario(db: Database) -> None:
            row = await db.get_paper_account(user_id, account_id)
            assert row is not None
            ok = await db.paper_advance(
                account_id=account_id,
                user_id=user_id,
                expected_as_of=row["as_of"],
                cash=float(row["cash"]),
                realized_pnl=float(row["realized_pnl"]),
                as_of=row["as_of"] + timedelta(days=1),
                status="active",
                rules={},
                risk_state={},
                positions=[],
                decisions=[],
                equity=[],
                risk_events=[
                    {
                        "id": event_id,
                        "account_id": account_id,
                        "decision_id": None,
                        "trade_date": trade_date,
                        "symbol": None,
                        "side": None,
                        "origin": "strategy",
                        "kind": "drawdown_state",
                        "verdict": "breach",
                        "gate": "drawdown",
                        "metrics": {"drawdown_pct": 12.0},
                        "detail": "测试行",
                    }
                ],
            )
            assert ok
            rows = await db.paper_audit(account_id, 10)
            assert [str(item["id"]) for item in rows] == [event_id]
            assert rows[0]["decision_id"] is None

        _run(_with_db(scenario))
    finally:
        purge_rows(emails, [])


def test_risk_audit_and_state_survive_real_postgres(real_stack: TestClient) -> None:
    emails = [f"e5-{uuid.uuid4().hex[:8]}@example.com"]
    account_id = ""
    try:
        client = sign_up(emails[0])
        user_id = int(client.get("/api/v1/auth/me").json()["id"])
        created = _create(client, "真库风控会话")
        assert created.status_code == 201, created.text
        detail = created.json()
        account_id = detail["account"]["id"]
        created_as_of = detail["account"]["as_of"]
        assert detail["account"]["config"]["risk"]["max_position"] == 30.0
        assert detail["risk_state"]["as_of"] == created_as_of

        finished = client.post(
            f"/api/v1/paper/accounts/{account_id}/run", json={"approve": "all"}
        )
        assert finished.status_code == 200, finished.text
        final = finished.json()
        assert final["risk_events"], "真数据上 fast=1/slow=2 应当至少产生一次提案审计"
        assert final["risk_state"]["equity"] > 0

        async def check(db: Database) -> None:
            row = await db.get_paper_account(user_id, account_id)
            assert row is not None
            assert row["risk_state"]["as_of"] == row["as_of"].isoformat()
            audit = await db.paper_audit(account_id, 500)
            assert audit, "审计行必须真的落库"
            config = config_from_payload(row["config"])
            log = tuple(
                decision_from_row(r) for r in await db.paper_decisions(account_id, 500)
            )
            rebuilt = replay(config, log, account_id=account_id)
            # 提案审计是**生成期事实**：决策日志里没有「被拦截的提案」，而已落日志的信号日
            # 在重放里会被 `logged` 跳过——所以不能要求「从日志重放 == 原始审计」。审计表
            # 是权威记录；重放只负责账户状态与 `as_of` 的风险快照。
            assert any(event["trade_date"].isoformat() > created_as_of for event in audit)
            assert rebuilt.risk_state is not None
            stored_state = row["risk_state"]
            assert rebuilt.risk_state.as_of.isoformat() == stored_state["as_of"]
            assert rebuilt.risk_state.equity == pytest.approx(
                float(stored_state["equity"]), abs=0.01
            )
            assert (
                rebuilt.risk_state.blocked_by_drawdown
                == stored_state["blocked_by_drawdown"]
            )
            assert rebuilt.state.cash == pytest.approx(float(row["cash"]), abs=0.01)

        _run(_with_db(check))
    finally:
        purge_rows(emails, [])
    if account_id:
        leftover = _run(
            _with_db(
                lambda db: db._all(
                    "SELECT count(*) AS n FROM decision_audit WHERE account_id = %s::uuid",
                    (account_id,),
                )
            )
        )
        assert int(leftover[0]["n"]) == 0
