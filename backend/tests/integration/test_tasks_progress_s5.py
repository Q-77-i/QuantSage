"""S5 真库集成：逐格进度落 JSONB + 轻量轮询视图。"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from psycopg.types.json import Jsonb

from app.core.config import get_settings
from app.core.db import Database, init_schema, open_pool
from app.core.db_tasks import MAX_PROGRESS_CELLS
from tests.integration.conftest import purge_rows

pytestmark = pytest.mark.integration


def test_append_task_cell_writes_cells_and_respects_worker_fence() -> None:
    email = f"s5-progress-{uuid.uuid4().hex[:8]}@example.com"

    async def scenario() -> None:
        async with open_pool(get_settings().postgres_dsn) as pool:
            await init_schema(pool)
            db = Database(pool)
            user = await db.create_user(email, "x")
            task_id = str(uuid.uuid4())
            token = str(uuid.uuid4())
            wrong = str(uuid.uuid4())
            await db.create_task(task_id, user["id"], "grid", {}, total=3)
            # 直接置 running：不经过 claim_next_task，避免和开发库里的其它排队任务抢件
            await db._exec(
                "UPDATE tasks SET status = 'running', worker_id = 'w1', "
                "claim_token = %s::uuid, attempts = 1 WHERE id = %s::uuid",
                (token, task_id),
            )

            await db.append_task_cell(
                task_id,
                claim_token=token,
                done=1,
                total=3,
                cell={"index": 0, "ok": True},
            )
            # 栅栏：别的 claim 的迟到写入不生效
            await db.append_task_cell(
                task_id,
                claim_token=wrong,
                done=2,
                total=3,
                cell={"index": 1, "ok": True},
            )

            row = await db.get_task_progress(user["id"], task_id)
            assert row is not None
            assert row["progress"]["done"] == 1
            assert row["progress"]["total"] == 3
            assert [cell["index"] for cell in row["progress"]["cells"]] == [0]
            # 轻量视图不带 request（网格的格规格很大）
            assert "request" not in row
            # 越权/不存在同姿态
            assert await db.get_task_progress(user["id"] + 100_000, task_id) is None

            # 重排后旧 cells 必须清掉：下一次执行从 0 开始，不重复追加
            assert await db.requeue_task(task_id, claim_token=token, backoff_seconds=0)
            row = await db.get_task_progress(user["id"], task_id)
            assert row is not None and row["status"] == "queued"
            assert row["progress"]["done"] == 0
            assert "cells" not in row["progress"]

            # 1000 格硬顶：再追加不增长
            cells = [{"index": i} for i in range(MAX_PROGRESS_CELLS)]
            await db._exec(
                "UPDATE tasks SET status = 'running', worker_id = 'w1', "
                "claim_token = %s::uuid, progress = %s WHERE id = %s::uuid",
                (token, Jsonb({"done": 0, "total": 3, "cells": cells}), task_id),
            )
            await db.append_task_cell(
                task_id,
                claim_token=token,
                done=1,
                total=3,
                cell={"index": 9999},
            )
            row = await db.get_task_progress(user["id"], task_id)
            assert row is not None
            assert len(row["progress"]["cells"]) == MAX_PROGRESS_CELLS

            # 终态（取消）后迟到写入 no-op
            await db.finish_task_cancelled(task_id, claim_token=token)
            before = len(row["progress"]["cells"])
            await db.append_task_cell(
                task_id,
                claim_token=token,
                done=2,
                total=3,
                cell={"index": 10000},
            )
            row = await db.get_task_progress(user["id"], task_id)
            assert row is not None and row["status"] == "cancelled"
            assert len(row["progress"]["cells"]) == before

    try:
        asyncio.run(scenario())
    finally:
        purge_rows([email], [])


def test_init_schema_serializes_concurrent_workers() -> None:
    """S5：两个 worker 同时启动，DDL 由 advisory xact lock 串行，不互相撞车。"""

    async def scenario() -> None:
        async with open_pool(get_settings().postgres_dsn) as p1, open_pool(
            get_settings().postgres_dsn
        ) as p2:
            await asyncio.gather(init_schema(p1), init_schema(p2))

    asyncio.run(scenario())
