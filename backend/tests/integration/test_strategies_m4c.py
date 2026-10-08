"""M4c 集成测试：策略 CRUD 与用户策略回测打真实 Postgres + 真实 `data/` 行情。

离线那份（`tests/test_strategies_api.py` / `test_backtest_user.py`）注入的是内存业务库，
**证明不了 SQL 里的 `user_id` 过滤真的生效**（SPEC §12 的双跑口径）。另有三件只有真库能验：

  * **建表与增列的幂等**——`strategies` 是新表，`backtest_runs` 走的是
    `ADD COLUMN IF NOT EXISTS`；连起两次 lifespan 必须不报错（否则每次重启都在赌）；
  * `params` 经 **JSONB** 往返仍是原样；
  * 删账号时 `strategies` 随外键级联清掉（teardown 全靠它），而**删策略不删回测记录**。

前置条件：`docker compose up -d --wait`（postgres 必需）+ `data/` 已落盘。
默认不收集；用 `uv run pytest -m integration` 触发。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.strategy.templates import template_source
from tests.integration.conftest import purge_rows, sign_up

pytestmark = pytest.mark.integration

SYMBOL = "600519"
MA_CROSS = template_source("ma_cross")


def fresh_emails(prefix: str, count: int) -> list[str]:
    suffix = uuid.uuid4().hex[:8]
    return [f"m4c-{prefix}-{suffix}-{index}@example.com" for index in range(count)]


def test_schema_setup_is_idempotent() -> None:
    """建表与增列都幂等：同一批 DDL 连跑两次不能报错。

    直接打 `init_schema` 而不再起一个 `TestClient`：嵌套跑两遍 lifespan 会让
    ETL 调度器在第二次退出时撞 `SchedulerNotRunningError`（与本用例要验的事无关）。
    """
    import asyncio

    from app.core.config import get_settings
    from app.core.db import init_schema, open_pool

    async def setup_twice() -> None:
        async with open_pool(get_settings().postgres_dsn) as pool:
            await init_schema(pool)
            await init_schema(pool)

    asyncio.run(setup_twice())


def test_strategy_crud_ownership_on_real_sql(real_stack: TestClient) -> None:
    alice_email, bob_email = fresh_emails("crud", 2)
    try:
        alice, bob = sign_up(alice_email), sign_up(bob_email)

        created = alice.post(
            "/api/v1/strategies",
            json={"name": "我的双均线", "code": MA_CROSS, "params": {"fast": 7, "slow": 30}},
        )
        assert created.status_code == 201, created.text
        strategy_id = created.json()["id"]

        # JSONB 往返：params 还是数字，没有被转成字符串
        detail = alice.get(f"/api/v1/strategies/{strategy_id}").json()
        assert detail["params"] == {"fast": 7, "slow": 30}
        assert detail["code"] == MA_CROSS

        # 撞名 409 落在真唯一约束上
        assert (
            alice.post("/api/v1/strategies", json={"name": "我的双均线", "code": MA_CROSS}).status_code
            == 409
        )

        # 归属：越权一律 404，且真没动到别人的数据
        assert bob.get("/api/v1/strategies").json() == []
        assert bob.get(f"/api/v1/strategies/{strategy_id}").status_code == 404
        assert bob.put(f"/api/v1/strategies/{strategy_id}", json={"name": "抢"}).status_code == 404
        assert bob.delete(f"/api/v1/strategies/{strategy_id}").status_code == 404
        assert alice.get(f"/api/v1/strategies/{strategy_id}").json()["name"] == "我的双均线"

        # 改名后列表按 updated_at 倒序
        assert (
            alice.put(f"/api/v1/strategies/{strategy_id}", json={"name": "改名了"}).status_code == 200
        )
        assert alice.get("/api/v1/strategies").json()[0]["name"] == "改名了"

        assert alice.delete(f"/api/v1/strategies/{strategy_id}").status_code == 200
        assert alice.get("/api/v1/strategies").json() == []
    finally:
        purge_rows([alice_email, bob_email], [])


def test_save_check_run_persist_reopen(real_stack: TestClient) -> None:
    """端到端一条：保存 → 检查 → 沙箱回测 → 落库 → 重开（SPEC §5 验收 ①）。"""
    email = fresh_emails("e2e", 1)[0]
    try:
        member = sign_up(email)

        saved = member.post("/api/v1/strategies", json={"name": "端到端", "code": MA_CROSS})
        assert saved.status_code == 201, saved.text
        strategy_id = saved.json()["id"]

        checked = member.post("/api/v1/strategies/check", json={"code": MA_CROSS}).json()
        assert checked["findings"] == []
        assert set(checked["meta"]["params"]) == {"fast", "slow"}

        run = member.post(
            "/api/v1/backtest",
            json={"strategy": "user", "strategy_id": strategy_id, "symbol": SYMBOL},
        )
        assert run.status_code == 200, run.text
        report = run.json()["report"]
        assert report["meta"]["strategy_kind"] == "user"
        assert report["meta"]["strategy_name"] == "端到端"

        detail = member.get(f"/api/v1/backtest/runs/{run.json()['run_id']}").json()
        assert detail["strategy_id"] == strategy_id
        assert detail["code_sha256"] == saved.json()["code_sha256"]
        assert detail["report"]["metrics"] == report["metrics"]

        # 改码之后 hash 不再相等（前端据此标注「已非当次运行的代码」）
        changed = member.put(
            f"/api/v1/strategies/{strategy_id}", json={"code": MA_CROSS + "\n# 改了一行\n"}
        ).json()
        assert changed["code_sha256"] != detail["code_sha256"]
    finally:
        purge_rows([email], [])


def test_deleting_strategy_keeps_runs_and_account_cascade(real_stack: TestClient) -> None:
    """删策略**不**级联删回测记录；删账号才级联清策略（teardown 靠它）。"""
    email = fresh_emails("cascade", 1)[0]
    try:
        member = sign_up(email)
        strategy_id = member.post(
            "/api/v1/strategies", json={"name": "要删的", "code": MA_CROSS}
        ).json()["id"]
        run_id = member.post(
            "/api/v1/backtest",
            json={"strategy": "user", "strategy_id": strategy_id, "symbol": SYMBOL},
        ).json()["run_id"]

        assert member.delete(f"/api/v1/strategies/{strategy_id}").status_code == 200
        # 记录还在，报告自足（`strategy_id` 悬空）
        assert member.get(f"/api/v1/backtest/runs/{run_id}").status_code == 200
    finally:
        purge_rows([email], [])
