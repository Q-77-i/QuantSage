"""集成用例：任务表与 worker（P3-E1a）。

离线用例用内存替身驱动 `run_once`，验的是**逻辑**；这里验替身照不出来的那些：真 JSONB、
真 `UPDATE ... RETURNING` 取件、真 lifespan 里的 worker、真外键级联。

运行方式（README）：`uv run pytest -m integration`（需容器与真实密钥）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.integration.conftest import purge_rows, run_backtest, sign_up, wait_for_task

pytestmark = pytest.mark.integration

SYMBOL = "600519"


def fresh_emails(prefix: str, count: int) -> list[str]:
    suffix = uuid.uuid4().hex[:8]
    return [f"e1-{prefix}-{suffix}-{index}@example.com" for index in range(count)]



MA_CROSS = """PARAMS = {"fast": {"type": "int", "default": 5, "min": 1, "max": 60, "label": "快线"}}
USES_EVENTS = False


def on_bar(ctx):
    return []
"""


def test_submitted_task_runs_and_is_listed(real_stack: TestClient) -> None:
    """提交 → 202；worker 取件跑完 → 任务可查、报告可取回、回测记录落库。"""
    email = fresh_emails("task", 1)[0]
    try:
        member = sign_up(email)
        response = member.post(
            "/api/v1/backtest", json={"strategy": "ma_cross", "symbol": SYMBOL}
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]

        # 提交那一刻**不该**已经有报告（异步的意义就在这里）
        assert member.get(f"/api/v1/tasks/{task_id}").json()["status"] in {"queued", "running"}

        task = wait_for_task(member, task_id)
        assert task["status"] == "done", task
        assert task["progress"] == {"done": 1, "total": 1}
        run_id = task["result"]["run_id"]

        detail = member.get(f"/api/v1/backtest/runs/{run_id}").json()
        assert detail["report"]["meta"]["symbol"] == SYMBOL
        # 摘要列表里能看到这条任务（`request->>'symbol'` 那两列在真库上抽得对不对）
        summary = member.get("/api/v1/tasks").json()[0]
        assert summary["id"] == task_id and summary["symbol"] == SYMBOL
        assert summary["strategy"] == "ma_cross"
    finally:
        purge_rows([email], [])


def test_user_strategy_task_round_trips_frozen_source(real_stack: TestClient) -> None:
    """用户策略走队列：源码随任务行落 JSONB、执行体读回来跑（`Jsonb` 包装错就炸在这里）。"""
    email = fresh_emails("taskuser", 1)[0]
    try:
        member = sign_up(email)
        saved = member.post("/api/v1/strategies", json={"name": "队列里的", "code": MA_CROSS})
        assert saved.status_code == 201, saved.text
        strategy_id = saved.json()["id"]

        task, envelope = run_backtest(
            member, {"strategy": "user", "strategy_id": strategy_id, "symbol": SYMBOL}
        )
        assert task["status"] == "done", task
        assert envelope["report"]["meta"]["strategy_name"] == "队列里的"

        detail = member.get(f"/api/v1/backtest/runs/{envelope['run_id']}").json()
        assert detail["strategy_id"] == strategy_id
        assert detail["code_sha256"] == saved.json()["code_sha256"]
        # 执行字段不进回测记录表（源码只留 hash）
        assert "source" not in detail["request"]
    finally:
        purge_rows([email], [])


def test_cancel_then_worker_skips_it(real_stack: TestClient) -> None:
    """排队中被取消的任务不会被执行——真库的**条件更新**是不是真的只命中 `queued`。"""
    email = fresh_emails("taskdrop", 1)[0]
    try:
        member = sign_up(email)
        task_id = member.post(
            "/api/v1/backtest", json={"strategy": "ma_cross", "symbol": SYMBOL}
        ).json()["task_id"]

        response = member.post(f"/api/v1/tasks/{task_id}/cancel")
        # worker 取件间隔 200ms：极少数情况下它抢先一步，那时如实 409
        assert response.status_code in {200, 409}, response.text
        if response.status_code == 200:
            assert response.json()["status"] == "cancelled"
            assert member.get("/api/v1/backtest/runs").json() == []
        else:
            assert wait_for_task(member, task_id)["status"] == "done"
    finally:
        purge_rows([email], [])


def test_reaper_requeues_expired_lease_then_fails_at_max_attempts(jwt_env: Any) -> None:
    """reaper 在真库上的三种去向（P4-S2）：过期租约回 `queued`；attempt 用尽终态。

    **不重跑 lifespan**（那会撞 ETL 调度器的 `SchedulerNotRunningError`，见
    `test_strategies_m4c.py` 的同款注释）：自己开一个连接池直接打这个方法——
    验的是同一条 SQL；造行走同步 psycopg（应用侧的异步池绑在别的循环上，测试线程碰不得）。
    """
    import asyncio

    import psycopg

    from app.core.db import Database, open_pool

    email = fresh_emails("reap", 1)[0]
    task_id = str(uuid.uuid4())
    try:
        with psycopg.connect(get_settings().postgres_dsn) as conn:
            # 账号也直接插：本用例不碰 HTTP（`sign_up` 的裸 TestClient 不跑 lifespan，
            # 会拿到上一个用例关掉的池），而 `tasks.user_id` 只是要个有的外键
            user_id = conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
                (email, "not-a-real-hash"),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO tasks (id, user_id, kind, status, request, progress, "
                " attempts, max_attempts, worker_id, lease_expires_at) "
                "VALUES (%s::uuid, %s, 'backtest', 'running', '{}'::jsonb, "
                "'{\"done\": 0, \"total\": 1}'::jsonb, 1, 2, 'dead-worker', "
                "now() - interval '1 second')",
                (task_id, user_id),
            )

        async def reap(*, expired_only: bool = True) -> dict[str, int]:
            async with open_pool(get_settings().postgres_dsn) as pool:
                return await Database(pool).reap_running_tasks(
                    backoff_seconds=0, expired_only=expired_only
                )

        assert asyncio.run(reap()) == {"queued": 1}

        with psycopg.connect(get_settings().postgres_dsn) as conn:
            status, error, worker = conn.execute(
                "SELECT status, error, worker_id FROM tasks WHERE id = %s::uuid", (task_id,)
            ).fetchone()
        assert status == "queued"
        assert error is None  # 还没终态，不写 error
        assert worker is None
        # 幂等：再扫一次不该重复计入（`WHERE status='running'` 已经不匹配了）
        assert asyncio.run(reap()) == {}

        # 第二次执行也过期 ⇒ attempts 用尽 → failed/lease_expired
        with psycopg.connect(get_settings().postgres_dsn) as conn:
            conn.execute(
                "UPDATE tasks SET status='running', attempts=2, worker_id='dead-worker', "
                "lease_expires_at=now() - interval '1 second' WHERE id=%s::uuid",
                (task_id,),
            )
        assert asyncio.run(reap()) == {"failed": 1}
        with psycopg.connect(get_settings().postgres_dsn) as conn:
            status, kind = conn.execute(
                "SELECT status, error->>'kind' FROM tasks WHERE id = %s::uuid", (task_id,)
            ).fetchone()
        assert status == "failed"
        assert kind == "lease_expired"
    finally:
        purge_rows([email], [])
