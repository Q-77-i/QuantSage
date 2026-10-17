"""收尾项 5d 真库集成：版本表 DDL、启动回填、事务化保存/回滚与 409 并发。

离线替身证不了 `gen_random_uuid()`、`FOR UPDATE`、`UNIQUE(strategy_id, version_no)`
与启动回填——这些只在真 Postgres 上成立。
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
from tests.conftest import submit_and_run
from tests.integration.conftest import purge_rows, sign_up

pytestmark = pytest.mark.integration

CODE_V1 = """\
PARAMS = {"window": {"type": "int", "default": 20, "min": 2, "max": 250, "label": "窗口"}}
USES_EVENTS = False


def on_bar(ctx, p):
    return []
"""


def test_version_table_backfill_and_rollback(real_stack: TestClient) -> None:
    email = f"5d-version-{uuid.uuid4().hex[:8]}@example.com"
    client = sign_up(email)

    created = client.post(
        "/api/v1/strategies", json={"name": "版本测试", "code": CODE_V1, "params": {}}
    )
    assert created.status_code == 201, created.text
    row = created.json()
    strategy_id = row["id"]
    assert row["version"] == 1

    updated = client.put(
        f"/api/v1/strategies/{strategy_id}",
        json={"code": CODE_V1 + "\n# v2\n", "expected_version": 1},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2

    stale = client.put(
        f"/api/v1/strategies/{strategy_id}",
        json={"code": CODE_V1 + "\n# stale\n", "expected_version": 1},
    )
    assert stale.status_code == 409

    versions = client.get(f"/api/v1/strategies/{strategy_id}/versions").json()
    assert [v["version_no"] for v in versions] == [2, 1]

    diff = client.get(f"/api/v1/strategies/{strategy_id}/versions/1/diff").json()
    assert diff["from"] == 2 and diff["to"] == 1
    assert "# v2" in diff["diff"]

    rolled = client.post(
        f"/api/v1/strategies/{strategy_id}/rollback",
        json={"version_no": 1, "expected_version": 2},
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["version"] == 3
    assert rolled.json()["code"] == CODE_V1

    async def check_backfill() -> None:
        async with open_pool(get_settings().postgres_dsn) as pool:
            db = Database(pool)
            await db._exec(
                "DELETE FROM strategy_versions WHERE strategy_id = %s::uuid",
                (strategy_id,),
            )
            await init_schema(pool)
            await init_schema(pool)  # 幂等：第二次不能再插
            rows = await db._all(
                "SELECT version_no, code FROM strategy_versions "
                "WHERE strategy_id = %s::uuid ORDER BY version_no",
                (strategy_id,),
            )
        assert len(rows) == 1
        assert rows[0]["version_no"] == 3
        assert rows[0]["code"] == CODE_V1

    try:
        asyncio.run(check_backfill())
    finally:
        purge_rows([email], [])


def test_backtest_run_records_the_frozen_version(real_stack: TestClient) -> None:
    email = f"5d-run-{uuid.uuid4().hex[:8]}@example.com"
    client = sign_up(email)
    created = client.post(
        "/api/v1/strategies", json={"name": "版本绑定", "code": CODE_V1, "params": {}}
    ).json()
    strategy_id = created["id"]
    assert created["version"] == 1
    updated = client.put(
        f"/api/v1/strategies/{strategy_id}",
        json={"code": CODE_V1 + "\n# v2\n", "expected_version": 1},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2

    latest = date.fromisoformat(str(dc.latest_dates()["latest_trade_date"]))
    start = latest - timedelta(days=60)
    submit_and_run(
        client,
        {
            "strategy": "user",
            "strategy_id": strategy_id,
            "symbol": "600519",
            "start": start.isoformat(),
            "end": latest.isoformat(),
            "pit_mode": "pit",
            "params": {},
        },
    )

    async def check() -> None:
        async with open_pool(get_settings().postgres_dsn) as pool:
            db = Database(pool)
            row = await db._one(
                "SELECT strategy_version, report FROM backtest_runs "
                "WHERE strategy_id = %s::uuid ORDER BY created_at DESC LIMIT 1",
                (strategy_id,),
            )
        assert row is not None
        assert row["strategy_version"] == 2
        assert row["report"]["meta"]["strategy_version"] == 2

    try:
        asyncio.run(check())
    finally:
        purge_rows([email], [])
