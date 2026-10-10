"""M6 集成测试：四张表 + 事务推进 + 乐观并发 + **重放对账**，打真实 Postgres 与真实 `data/`。

离线那份（`tests/test_paper_api.py`）注入内存业务库，**证明不了 SQL 里的事务与守卫真的生效**
（同 M1c/M4c 的双跑口径）。四件只有真库能验：

  * 四张表**幂等建表**（连起两次 `init_schema`）；
  * **推进是一个事务**——中途失败时 `as_of` 与现金**一个都不许变**；
  * **乐观并发守卫**在 SQL 里（拿旧的 `as_of` 再推必然返 False）；
  * **重放对账**：从库里的决策日志重建账户状态 == 库里存的账户状态。

前置条件：`docker compose up -d --wait`（postgres 必需）+ `data/` 已落盘。
默认不收集；用 `uv run pytest -m integration` 触发。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.backtest.types import Side
from app.core.config import get_settings
from app.core.db import Database, init_schema, open_pool
from app.data import duckdb_client as dc
from app.paper.replay import replay
from app.paper.store import config_from_payload, decision_from_row, decision_to_row
from app.paper.types import Decision
from tests.integration.conftest import purge_rows, sign_up

pytestmark = pytest.mark.integration

SYMBOL = "600519"


def fresh_emails(prefix: str, count: int) -> list[str]:
    suffix = uuid.uuid4().hex[:8]
    return [f"m6-{prefix}-{suffix}-{index}@example.com" for index in range(count)]


def _window() -> tuple[date, date]:
    latest = date.fromisoformat(str(dc.latest_dates()["latest_trade_date"]))
    return latest - timedelta(days=60), latest


def _create(client, name: str, **over: object):
    start, end = _window()
    payload: dict = {
        "name": name,
        "initial_cash": 300_000.0,
        "symbols": [SYMBOL],
        "strategy": "ma_cross",
        "params": {"fast": 5, "slow": 20},
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    payload.update(over)
    return client.post("/api/v1/paper/accounts", json=payload)


def _run(coro):
    return asyncio.run(coro)


async def _with_db(work):
    async with open_pool(get_settings().postgres_dsn) as pool:
        return await work(Database(pool))


def test_schema_setup_is_idempotent() -> None:
    """四张新表的 DDL 幂等：连跑两次不能报错（否则每次重启都在赌）。"""

    async def setup_twice() -> None:
        async with open_pool(get_settings().postgres_dsn) as pool:
            await init_schema(pool)
            await init_schema(pool)

    _run(setup_twice())


def test_full_lifecycle_on_real_postgres(real_stack: TestClient) -> None:
    """建会话 → 批准 → 推进 → 跑到结束，全程真库；顺带验归属与级联清理。"""
    emails = fresh_emails("life", 2)
    account_id = ""
    try:
        client = sign_up(emails[0])
        user_id = int(client.get("/api/v1/auth/me").json()["id"])
        created = _create(client, "真库会话")
        assert created.status_code == 201, created.text
        detail = created.json()
        account_id = detail["account"]["id"]

        for decision in detail["pending"]:
            approved = client.post(f"/api/v1/paper/decisions/{decision['id']}/approve")
            assert approved.status_code == 200, approved.text

        stepped = client.post(f"/api/v1/paper/accounts/{account_id}/step")
        assert stepped.status_code == 200, stepped.text

        finished = client.post(f"/api/v1/paper/accounts/{account_id}/run", json={"approve": "all"})
        assert finished.status_code == 200, finished.text
        final = finished.json()
        assert final["account"]["status"] == "finished"
        assert final["account"]["as_of"] == final["progress"]["end"]
        assert len(final["equity_curve"]) == final["progress"]["days_total"]

        # 归属：另一个账号看不到它（越权与不存在同为 404），列表里也没有
        other = sign_up(emails[1])
        assert other.get(f"/api/v1/paper/accounts/{account_id}").status_code == 404
        assert other.get("/api/v1/paper/accounts").json() == []
        assert client.get("/api/v1/paper/accounts").json()[0]["id"] == account_id

        # ── 重放对账：拿库里的决策日志重建账户状态，必须与库里存的逐字段相等 ──
        async def reconcile(db: Database) -> None:
            row = await db.get_paper_account(user_id, account_id)
            assert row is not None
            config = config_from_payload(row["config"])
            log = tuple(decision_from_row(r) for r in await db.paper_decisions(account_id, 500))
            rebuilt = replay(config, log, account_id=account_id)
            assert rebuilt.state.cash == pytest.approx(float(row["cash"]))
            assert rebuilt.as_of == row["as_of"]
            stored = {p["symbol"] for p in await db.paper_positions(account_id)}
            assert set(rebuilt.state.positions) == stored

        _run(_with_db(reconcile))
    finally:
        purge_rows(emails, [])
    # 级联：删账号后会话与决策一并消失（teardown 全靠它）
    leftover = _run(
        _with_db(
            lambda db: db._all(
                "SELECT (SELECT count(*) FROM paper_accounts WHERE id = %s::uuid) AS accounts, "
                "(SELECT count(*) FROM paper_decisions WHERE account_id = %s::uuid) AS decisions",
                (account_id, account_id),
            )
        )
    )
    assert (int(leftover[0]["accounts"]), int(leftover[0]["decisions"])) == (0, 0)


def test_advance_guards_on_as_of_and_rolls_back_on_failure(real_stack: TestClient) -> None:
    """守卫与事务两件一起验（都用真库、都不经过端点）。"""
    emails = fresh_emails("advance", 1)
    try:
        client = sign_up(emails[0])
        user_id = int(client.get("/api/v1/auth/me").json()["id"])
        detail = _create(client, "推进会话").json()
        account_id = detail["account"]["id"]

        async def scenario(db: Database) -> None:
            row = await db.get_paper_account(user_id, account_id)
            assert row is not None
            as_of, cash = row["as_of"], float(row["cash"])

            # ① 事务：决策列表里塞两条**同键**的行（`(account, date, symbol, side)` 唯一约束）
            #    → executemany 中途抛 → 整笔回滚 → as_of 与现金都不许变
            clash = Decision(
                id=str(uuid.uuid4()),
                account_id=account_id,
                symbol=SYMBOL,
                trade_date=as_of,
                side=Side.BUY,
                est_qty=100,
                est_price=1.0,
            )
            twin = clash.replace(id=str(uuid.uuid4()))
            with pytest.raises(Exception):  # noqa: B017 - 驱动层的唯一约束异常，类型不必钉
                await db.paper_advance(
                    account_id=account_id,
                    user_id=user_id,
                    expected_as_of=as_of,
                    cash=1.0,  # 故意写个假数：回滚后它不该留在库里
                    realized_pnl=0.0,
                    as_of=as_of + timedelta(days=1),
                    status="active",
                    rules={},
                    positions=[],
                    decisions=[decision_to_row(clash), decision_to_row(twin)],
                    equity=[],
                )
            after = await db.get_paper_account(user_id, account_id)
            assert after is not None
            assert after["as_of"] == as_of, "事务回滚后 as_of 不许变"
            assert float(after["cash"]) == pytest.approx(cash), "现金也不许变"

            # ② 乐观并发：正常推进一步之后，拿**旧 as_of** 再推必然 False
            staged = dict(
                account_id=account_id,
                user_id=user_id,
                cash=cash,
                realized_pnl=0.0,
                status="active",
                rules={},
                positions=[],
                decisions=[],
                equity=[],
            )
            assert await db.paper_advance(
                **staged, expected_as_of=as_of, as_of=as_of + timedelta(days=1)
            )
            assert not await db.paper_advance(
                **staged, expected_as_of=as_of, as_of=as_of + timedelta(days=2)
            ), "守卫必须挡住拿着旧 as_of 的请求"

        _run(_with_db(scenario))
    finally:
        purge_rows(emails, [])
