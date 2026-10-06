"""M1c 集成测试：自选股与回测落库打真实 Postgres + 真实 `data/` 行情。

离线那份（`tests/test_watchlist_api.py` / `test_backtest_runs.py`）注入的是内存业务库，
**证明不了 SQL 里的 `user_id` 过滤真的生效**——过滤写在 SQL 里，所以必须再打一次真库
（SPEC §12 的双跑口径）。另外两件只有真库能验的事：

  * `report` / `request` 经 **JSONB** 往返后仍是原样（数字不能变成字符串）；
  * 删账号时 `watchlist` 与 `backtest_runs` 随外键级联清掉——teardown 全靠它。

前置条件：`docker compose up -d --wait`（postgres 必需）+ `data/` 已落盘。
默认不收集；用 `uv run pytest -m integration` 触发。
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.integration.conftest import purge_rows, sign_up

pytestmark = pytest.mark.integration

SYMBOL = "600519"


def owned_rows(email: str, table: str) -> int:
    """这个账号在 `table` 里还剩几行（`watchlist` / `backtest_runs` 都挂 users 外键）。"""
    with psycopg.connect(get_settings().postgres_dsn) as conn:
        row = conn.execute(
            f"SELECT count(*) FROM {table} t JOIN users u ON u.id = t.user_id "
            "WHERE u.email = %s",
            (email,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def fresh_emails(prefix: str, count: int) -> list[str]:
    suffix = uuid.uuid4().hex[:8]
    return [f"m1c-{prefix}-{suffix}-{index}@example.com" for index in range(count)]


def test_watchlist_ownership_and_group_semantics_on_real_sql(
    real_stack: TestClient,
) -> None:
    alice_email, bob_email = fresh_emails("wl", 2)
    try:
        alice, bob = sign_up(alice_email), sign_up(bob_email)

        created = alice.post("/api/v1/watchlist", json={"symbol": SYMBOL, "group_name": "核心"})
        assert created.status_code == 201, created.text
        # 真行情：加入时要记下最近可得收盘价
        assert created.json()["added_price"] is not None
        assert created.json()["change_pct"] == pytest.approx(0.0)

        assert [item["symbol"] for item in alice.get("/api/v1/watchlist").json()] == [SYMBOL]
        assert bob.get("/api/v1/watchlist").json() == []

        # 越权：改 / 删 / 改组一律 404，且真的没动到别人的数据
        assert bob.patch(f"/api/v1/watchlist/{SYMBOL}", json={"group_name": "x"}).status_code == 404
        assert bob.delete(f"/api/v1/watchlist/{SYMBOL}").status_code == 404
        assert bob.patch("/api/v1/watchlist/groups/核心", json={"name": "y"}).status_code == 404
        assert alice.get("/api/v1/watchlist").json()[0]["group_name"] == "核心"

        # 分组语义（重命名 / 回落）落在真 SQL 上
        assert alice.patch("/api/v1/watchlist/groups/核心", json={"name": "长线"}).status_code == 200
        assert alice.get("/api/v1/watchlist").json()[0]["group_name"] == "长线"
        assert alice.delete("/api/v1/watchlist/groups/长线").status_code == 200
        assert alice.get("/api/v1/watchlist").json()[0]["group_name"] == "默认分组"

        # 同一个号只能加一次（真库的唯一约束，替身照抄的那条）
        assert alice.post("/api/v1/watchlist", json={"symbol": SYMBOL}).status_code == 409
    finally:
        purge_rows([alice_email, bob_email], [])


def test_backtest_run_round_trips_through_jsonb(real_stack: TestClient) -> None:
    """整份报告要能原样取回（重开靠它），且摘要里的指标仍是数字。

    `->>` 会把 JSONB 数字抽成 text —— 那样前端按比例格式化不会报错、只会算错，
    所以这里专门断类型。
    """
    email = fresh_emails("run", 1)[0]
    try:
        member = sign_up(email)
        response = member.post(
            "/api/v1/backtest",
            json={"strategy": "ma_cross", "symbol": SYMBOL, "pit_mode": "both"},
        )
        assert response.status_code == 200, response.text
        created = response.json()
        run_id = created["run_id"]

        summary = member.get("/api/v1/backtest/runs").json()[0]
        assert summary["id"] == run_id
        assert summary["symbol"] == SYMBOL
        assert summary["strategy"] == "ma_cross"
        assert summary["pit_mode"] == "both"
        assert isinstance(summary["metrics"]["total_return"], float)
        assert summary["metrics"] == created["report"]["metrics"]

        detail = member.get(f"/api/v1/backtest/runs/{run_id}").json()
        assert detail["report"] == created["report"]
        assert detail["request"]["adjust"] == "qfq"
        assert detail["request"]["pit_mode"] == "both"
        assert detail["request"]["start"] == created["report"]["meta"]["start"]
    finally:
        purge_rows([email], [])


def test_runs_and_watchlist_are_invisible_across_accounts(real_stack: TestClient) -> None:
    alice_email, bob_email = fresh_emails("iso", 2)
    try:
        alice, bob = sign_up(alice_email), sign_up(bob_email)
        assert (
            alice.post("/api/v1/backtest", json={"strategy": "ma_cross", "symbol": SYMBOL}).status_code
            == 200
        )
        run_id = alice.get("/api/v1/backtest/runs").json()[0]["id"]

        assert bob.get("/api/v1/backtest/runs").json() == []
        assert bob.get(f"/api/v1/backtest/runs/{run_id}").status_code == 404
    finally:
        purge_rows([alice_email, bob_email], [])


def test_deleting_the_account_cascades_to_owned_rows(real_stack: TestClient) -> None:
    """teardown 全靠外键级联：留垃圾的话开发库会被集成用例慢慢喂胖。"""
    email = fresh_emails("cascade", 1)[0]
    member = sign_up(email)
    assert member.post("/api/v1/watchlist", json={"symbol": SYMBOL}).status_code == 201
    assert (
        member.post("/api/v1/backtest", json={"strategy": "ma_cross", "symbol": SYMBOL}).status_code
        == 200
    )
    assert owned_rows(email, "watchlist") == 1
    assert owned_rows(email, "backtest_runs") == 1

    purge_rows([email], [])

    assert owned_rows(email, "watchlist") == 0
    assert owned_rows(email, "backtest_runs") == 0
