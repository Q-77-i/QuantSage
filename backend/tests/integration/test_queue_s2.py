"""集成用例：队列 v2 的取件并发与结果幂等（P4-S2）。

离线替身验的是逻辑；这里验真库才有的性质：`FOR UPDATE SKIP LOCKED` 的并发取件、
`ON CONFLICT (task_id)` 的唯一索引。运行方式：`uv run pytest -m integration`。

**取件并发跑在独立的临时 schema 上**（`CREATE TABLE … (LIKE public.tasks INCLUDING ALL)`）：
本机常驻的 dev backend 有自己的 worker，会与测试抢 `public.tasks` 里的 queued 行。
`LIKE … INCLUDING ALL` 把列/默认值/索引一并复制，跑的仍是 `claim_next_task` 同一条 SQL。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import get_settings
from tests.integration.conftest import purge_rows

pytestmark = pytest.mark.integration


def _seed_user_and_task(email: str) -> tuple[int, str]:
    """造一个用户 + 一条 `running` 任务（不排 `queued`，避免 dev worker 抢走）。"""
    task_id = str(uuid.uuid4())
    with psycopg.connect(get_settings().postgres_dsn) as conn:
        user_id = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
            (email, "not-a-real-hash"),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO tasks (id, user_id, kind, status, request, progress, "
            " attempts, max_attempts, worker_id, lease_expires_at) "
            "VALUES (%s::uuid, %s, 'backtest', 'running', '{}'::jsonb, "
            "'{\"done\": 0, \"total\": 1}'::jsonb, 1, 2, 'test-worker', "
            "now() + interval '1 hour')",
            (task_id, user_id),
        )
    return user_id, task_id


def test_concurrent_claims_never_duplicate(jwt_env: Any) -> None:
    """4 路并发 × 50 件：每件恰好被取走一次（`SKIP LOCKED` 的直接回归）。"""
    from app.core.db import Database

    dsn = get_settings().postgres_dsn
    schema = f"probe_s2_{uuid.uuid4().hex[:8]}"
    task_ids = [str(uuid.uuid4()) for _ in range(50)]
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'CREATE TABLE "{schema}".tasks (LIKE public.tasks INCLUDING ALL)')
        for task_id in task_ids:
            conn.execute(
                f'INSERT INTO "{schema}".tasks '
                "(id, user_id, kind, status, request, progress) "
                "VALUES (%s::uuid, 1, 'backtest', 'queued', '{}'::jsonb, "
                "'{\"done\": 0, \"total\": 1}'::jsonb)",
                (task_id,),
            )

    try:
        async def scenario() -> list[str]:
            pool = AsyncConnectionPool(
                dsn,
                min_size=1,
                max_size=8,
                open=False,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                    "options": f"-c search_path={schema},public",
                },
            )
            await pool.open(wait=True, timeout=10)
            try:
                db = Database(pool)

                async def claim_all(worker_id: str) -> list[str]:
                    got: list[str] = []
                    while True:
                        task = await db.claim_next_task(worker_id, lease_seconds=60)
                        if task is None:
                            return got
                        got.append(str(task["id"]))

                batches = await asyncio.gather(
                    *(claim_all(f"probe-{index}") for index in range(4))
                )
            finally:
                await pool.close()
            return [task_id for batch in batches for task_id in batch]

        claimed = asyncio.run(scenario())
        assert len(claimed) == len(task_ids)
        assert set(claimed) == set(task_ids)  # 0 重复、0 遗漏
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_release_task_requeues_and_cancels(jwt_env: Any) -> None:
    """S4 优雅停机：未取消的回 queued 且 attempts−1；已取消的收 cancelled。"""
    from app.core.db import Database

    dsn = get_settings().postgres_dsn
    schema = f"probe_s4_{uuid.uuid4().hex[:8]}"
    queued_id = str(uuid.uuid4())
    cancelled_id = str(uuid.uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'CREATE TABLE "{schema}".tasks (LIKE public.tasks INCLUDING ALL)')
        for task_id, cancel in ((queued_id, None), (cancelled_id, True)):
            conn.execute(
                f'INSERT INTO "{schema}".tasks '
                "(id, user_id, kind, status, request, progress, attempts, max_attempts, "
                " worker_id, lease_expires_at, cancel_requested_at) "
                "VALUES (%s::uuid, 1, 'backtest', 'running', '{}'::jsonb, "
                "'{\"done\": 40, \"total\": 100}'::jsonb, 1, 2, 'release-worker', "
                "now() + interval '1 hour', CASE WHEN %s THEN now() END)",
                (task_id, cancel),
            )

    try:
        async def scenario() -> tuple[str | None, str | None, str | None]:
            pool = AsyncConnectionPool(
                dsn,
                min_size=1,
                max_size=4,
                open=False,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                    "options": f"-c search_path={schema},public",
                },
            )
            await pool.open(wait=True, timeout=10)
            try:
                db = Database(pool)
                queued = await db.release_task(queued_id, worker_id="release-worker")
                cancelled = await db.release_task(cancelled_id, worker_id="release-worker")
                wrong = await db.release_task(queued_id, worker_id="other-worker")
                return queued, cancelled, wrong
            finally:
                await pool.close()

        queued, cancelled, wrong = asyncio.run(scenario())
        assert (queued, cancelled, wrong) == ("queued", "cancelled", None)

        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                f'SELECT status, attempts, worker_id, lease_expires_at, progress, finished_at '
                f'FROM "{schema}".tasks WHERE id = %s::uuid',
                (queued_id,),
            ).fetchone()
            assert row is not None
            assert row[0] == "queued" and row[1] == 0
            assert row[2] is None and row[3] is None
            assert row[4]["done"] == 0 and row[5] is None

            row = conn.execute(
                f'SELECT status, attempts, finished_at FROM "{schema}".tasks WHERE id = %s::uuid',
                (cancelled_id,),
            ).fetchone()
            assert row is not None
            assert row[0] == "cancelled" and row[1] == 1 and row[2] is not None
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_backtest_run_is_idempotent_by_task_id(jwt_env: Any) -> None:
    """同一 `task_id` 第二次落库不新增行，返回既有 `run_id`。"""
    from app.core.db import Database, open_pool

    email = f"s2-idem-{uuid.uuid4().hex[:8]}@example.com"
    try:
        user_id, task_id = _seed_user_and_task(email)
        first = str(uuid.uuid4())
        second = str(uuid.uuid4())

        async def scenario() -> tuple[str, str, str | None]:
            async with open_pool(get_settings().postgres_dsn) as pool:
                db = Database(pool)
                run_a = await db.save_backtest_run(
                    first,
                    user_id,
                    {"symbol": "600519"},
                    {"meta": {}, "metrics": {}},
                    task_id=task_id,
                )
                run_b = await db.save_backtest_run(
                    second,
                    user_id,
                    {"symbol": "600519"},
                    {"meta": {}, "metrics": {}},
                    task_id=task_id,
                )
                found = await db.backtest_run_id_by_task(task_id)
                return run_a, run_b, found

        run_a, run_b, found = asyncio.run(scenario())
        assert run_a == first
        assert run_b == first  # 冲突时回既有行，不是新 run_id
        assert found == first
    finally:
        purge_rows([email], [])


def test_claim_next_task_filters_by_kind_s7(jwt_env: Any) -> None:
    """S7：真库上 `kinds` 过滤只取本 kind；取空后返回 None。"""
    from app.core.db import Database

    dsn = get_settings().postgres_dsn
    schema = f"probe_s7_{uuid.uuid4().hex[:8]}"
    backtest_id = str(uuid.uuid4())
    grid_id = str(uuid.uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'CREATE TABLE "{schema}".tasks (LIKE public.tasks INCLUDING ALL)')
        for task_id, kind in ((grid_id, "grid"), (backtest_id, "backtest")):
            conn.execute(
                f'INSERT INTO "{schema}".tasks '
                "(id, user_id, kind, status, request, progress) "
                "VALUES (%s::uuid, 1, %s, 'queued', '{}'::jsonb, "
                "'{\"done\": 0, \"total\": 1}'::jsonb)",
                (task_id, kind),
            )

    try:

        async def scenario() -> tuple[Any, Any, Any]:
            pool = AsyncConnectionPool(
                dsn,
                min_size=1,
                max_size=2,
                open=False,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                    "options": f"-c search_path={schema},public",
                },
            )
            await pool.open(wait=True, timeout=10)
            try:
                db = Database(pool)
                grid = await db.claim_next_task(
                    "kind-worker", lease_seconds=60, kinds=("grid",)
                )
                backtest = await db.claim_next_task(
                    "kind-worker", lease_seconds=60, kinds=("backtest",)
                )
                empty = await db.claim_next_task(
                    "kind-worker", lease_seconds=60, kinds=("grid",)
                )
                return grid, backtest, empty
            finally:
                await pool.close()

        grid, backtest, empty = asyncio.run(scenario())
        assert grid is not None and str(grid["id"]) == grid_id
        assert backtest is not None and str(backtest["id"]) == backtest_id
        assert empty is None
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_finish_task_merges_full_progress_in_one_update(jwt_env: Any) -> None:
    """S9：真库上一次 UPDATE 同时写终态与满格进度，`progress.cells` 不丢。"""
    from app.core.db import Database

    dsn = get_settings().postgres_dsn
    schema = f"probe_s9_{uuid.uuid4().hex[:8]}"
    task_id = str(uuid.uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f'CREATE TABLE "{schema}".tasks (LIKE public.tasks INCLUDING ALL)')
        conn.execute(
            f'INSERT INTO "{schema}".tasks '
            "(id, user_id, kind, status, request, progress, attempts, max_attempts, "
            " worker_id, lease_expires_at) "
            "VALUES (%s::uuid, 1, 'batch', 'running', '{}'::jsonb, "
            "'{\"done\": 1, \"total\": 3, \"cells\": [{\"i\": 0}]}'::jsonb, "
            "1, 2, 'finish-worker', now() + interval '1 hour')",
            (task_id,),
        )

    try:

        async def scenario() -> None:
            pool = AsyncConnectionPool(
                dsn,
                min_size=1,
                max_size=2,
                open=False,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                    "options": f"-c search_path={schema},public",
                },
            )
            await pool.open(wait=True, timeout=10)
            try:
                db = Database(pool)
                await db.finish_task(
                    task_id,
                    worker_id="finish-worker",
                    result={"run_id": "r"},
                    progress_done=3,
                    progress_total=3,
                )
            finally:
                await pool.close()

        asyncio.run(scenario())
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                f'SELECT status, progress FROM "{schema}".tasks WHERE id = %s::uuid',
                (task_id,),
            ).fetchone()
            assert row is not None
            assert row[0] == "done"
            assert row[1]["done"] == 3 and row[1]["total"] == 3
            assert row[1]["cells"] == [{"i": 0}]
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
