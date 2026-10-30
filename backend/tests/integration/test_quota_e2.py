"""集成用例：配额的两条 SQL 语义走**真库**（P3-E2a）。

离线用例用内存替身，验的是**逻辑**；这里的真相在 SQL 里：

  * `ON CONFLICT DO UPDATE … WHERE used < limit` 的**原子性**——并发下恰好放行上限个。
    替身是「读 → 判 → 写」三步、天然会超发，这条它证明不了（与 E4b 幂等键同一条理由）；
  * gauge 的 `count(*) … IN ('queued','running')` 打在真表上：**排队即占额、终态不计**。

运行方式（README）：`uv run pytest -m integration`（需容器与真实密钥）。
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import Database, open_pool
from tests.integration.conftest import purge_rows, sign_up

pytestmark = pytest.mark.integration

METRIC = "agent_calls"


def email(prefix: str) -> str:
    return f"e2-{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def user_id_of(address: str) -> int:
    """同步 psycopg 直连：应用侧的异步池绑在 TestClient 的事件循环上，测试线程碰不得。"""
    with psycopg.connect(get_settings().postgres_dsn) as conn:
        row = conn.execute("SELECT id FROM users WHERE email = %s", (address,)).fetchone()
    assert row is not None, address
    return int(row[0])


def counter_value(address: str, period_key: str) -> int | None:
    with psycopg.connect(get_settings().postgres_dsn) as conn:
        row = conn.execute(
            "SELECT c.used FROM usage_counters c JOIN users u ON u.id = c.user_id "
            "WHERE u.email = %s AND c.metric = %s AND c.period_key = %s",
            (address, METRIC, period_key),
        ).fetchone()
    return None if row is None else int(row[0])


def rejection_count(address: str) -> int:
    with psycopg.connect(get_settings().postgres_dsn) as conn:
        row = conn.execute(
            "SELECT count(*) FROM quota_rejections r JOIN users u ON u.id = r.user_id "
            "WHERE u.email = %s",
            (address,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_counter_admits_exactly_the_limit_under_concurrency(real_stack: TestClient) -> None:
    """判据落在真表上：**恰好**放行 `limit` 个，序号 1..limit 不重不漏。"""
    address = email("atomic")
    sign_up(address)
    uid = user_id_of(address)
    limit, shots, day = 3, 12, "2026-10-16"

    async def hammer() -> list[int | None]:
        async with open_pool(get_settings().postgres_dsn) as pool:
            db = Database(pool)
            return list(
                await asyncio.gather(
                    *(
                        db.bump_usage_counter(uid, METRIC, day, limit)
                        for _ in range(shots)
                    )
                )
            )

    try:
        results = asyncio.run(hammer())

        granted = sorted(item for item in results if item is not None)
        assert granted == [1, 2, 3], results
        assert results.count(None) == shots - limit
        assert counter_value(address, day) == limit
    finally:
        purge_rows([address], [])


def test_counter_is_per_day_on_the_real_table(real_stack: TestClient) -> None:
    """跨日不是清理出来的，是 `period_key` 换了个键——真表上两行互不影响。"""
    address = email("days")
    sign_up(address)
    uid = user_id_of(address)

    async def bump(day: str) -> int | None:
        async with open_pool(get_settings().postgres_dsn) as pool:
            return await Database(pool).bump_usage_counter(uid, METRIC, day, 1)

    try:
        assert asyncio.run(bump("2026-10-16")) == 1
        assert asyncio.run(bump("2026-10-16")) is None  # 当天已满
        assert asyncio.run(bump("2026-10-17")) == 1  # 换日即归零
        assert counter_value(address, "2026-10-16") == 1
        assert counter_value(address, "2026-10-17") == 1
    finally:
        purge_rows([address], [])


def test_gauge_counts_in_flight_only(real_stack: TestClient) -> None:
    """在飞 = `queued` + `running`；终态两种都不计。"""
    address = email("gauge")
    sign_up(address)
    uid = user_id_of(address)

    async def count() -> int:
        async with open_pool(get_settings().postgres_dsn) as pool:
            return await Database(pool).count_active_tasks(uid)

    try:
        assert asyncio.run(count()) == 0
        with psycopg.connect(get_settings().postgres_dsn) as conn:
            for status in ("queued", "running", "done", "failed", "cancelled"):
                conn.execute(
                    "INSERT INTO tasks (id, user_id, kind, status, request) "
                    "VALUES (%s::uuid, %s, 'backtest', %s, '{}'::jsonb)",
                    (str(uuid.uuid4()), uid, status),
                )

        assert asyncio.run(count()) == 2
    finally:
        purge_rows([address], [])


def test_gauge_admits_exactly_the_limit_under_concurrency(real_stack: TestClient) -> None:
    """并发提交：**恰好**放行 `limit` 个（try-lock 重试路径的真库证据）。

    抢不到锁的请求先归还连接再退避，count+insert 仍在同一事务里——配额不会因为并发
    而超发，也不会因为重试而多建行。
    """
    address = email("gauge-atomic")
    sign_up(address)
    uid = user_id_of(address)
    limit, shots = 3, 12

    async def hammer() -> list[dict | None]:
        async with open_pool(get_settings().postgres_dsn) as pool:
            db = Database(pool)
            return list(
                await asyncio.gather(
                    *(
                        db.create_task(
                            str(uuid.uuid4()),
                            uid,
                            # worker 只认 backtest/grid/batch；这个 kind 不会被取件，
                            # 保证「在飞」计数在整个用例里稳定
                            "quota-probe",
                            {"symbol": "600519"},
                            active_limit=limit,
                        )
                        for _ in range(shots)
                    )
                )
            )

    try:
        results = asyncio.run(hammer())
        granted = [row for row in results if row is not None]
        assert len(granted) == limit, results
        with psycopg.connect(get_settings().postgres_dsn) as conn:
            total = conn.execute(
                "SELECT count(*) FROM tasks WHERE user_id = %s", (uid,)
            ).fetchone()[0]
            slots = sorted(
                row[0]
                for row in conn.execute(
                    "SELECT quota_slot FROM tasks WHERE user_id = %s", (uid,)
                )
            )
        assert total == limit
        assert slots == list(range(limit))  # 槽位 0..limit-1，一个不多一个不少
    finally:
        purge_rows([address], [])


def test_slot_is_released_when_task_reaches_terminal(real_stack: TestClient) -> None:
    """槽位由部分唯一索引维护：任务进终态即释放，不需要减计数。"""
    address = email("slot-release")
    sign_up(address)
    uid = user_id_of(address)

    async def create() -> dict | None:
        async with open_pool(get_settings().postgres_dsn) as pool:
            return await Database(pool).create_task(
                str(uuid.uuid4()),
                uid,
                "quota-probe",
                {"symbol": "600519"},
                active_limit=1,
            )

    try:
        first = asyncio.run(create())
        assert first is not None
        assert asyncio.run(create()) is None  # 唯一槽被占用

        with psycopg.connect(get_settings().postgres_dsn) as conn:
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (first["id"],))

        assert asyncio.run(create()) is not None  # 终态释放槽位
    finally:
        purge_rows([address], [])


def test_idempotent_replay_returns_the_same_row_under_quota_contention(
    real_stack: TestClient,
) -> None:
    """同一幂等键并发重放：两个请求拿到**同一行**、只建一行（配额只剩 1 个也放行）。"""
    address = email("gauge-replay")
    sign_up(address)
    uid = user_id_of(address)
    key = f"same-{uuid.uuid4().hex[:8]}"

    async def hammer() -> list[dict | None]:
        async with open_pool(get_settings().postgres_dsn) as pool:
            db = Database(pool)
            return list(
                await asyncio.gather(
                    *(
                        db.create_task(
                            str(uuid.uuid4()),
                            uid,
                            "quota-probe",
                            {"symbol": "600519"},
                            idempotency_key=key,
                            active_limit=1,
                        )
                        for _ in range(2)
                    )
                )
            )

    try:
        results = asyncio.run(hammer())
        assert all(row is not None for row in results), results
        assert results[0]["id"] == results[1]["id"]
        with psycopg.connect(get_settings().postgres_dsn) as conn:
            total = conn.execute(
                "SELECT count(*) FROM tasks WHERE user_id = %s", (uid,)
            ).fetchone()[0]
        assert total == 1
    finally:
        purge_rows([address], [])


def test_rejection_lands_on_the_real_table(real_stack: TestClient) -> None:
    """拒绝记录进真表——admin 的拒绝事件列表与验收证据都读它。"""
    address = email("reject")
    sign_up(address)
    uid = user_id_of(address)

    async def record() -> None:
        async with open_pool(get_settings().postgres_dsn) as pool:
            await Database(pool).record_quota_rejection(uid, METRIC, 3, 3)

    try:
        asyncio.run(record())

        assert rejection_count(address) == 1
    finally:
        purge_rows([address], [])


def test_cascade_keeps_quota_tables_clean(real_stack: TestClient) -> None:
    """账号删掉，配额两表随外键级联——不留孤儿（集成用例打完自己收干净的前提）。"""
    address = email("cascade")
    sign_up(address)
    uid = user_id_of(address)
    purge_rows([address], [])

    with psycopg.connect(get_settings().postgres_dsn) as conn:
        counters = conn.execute(
            "SELECT count(*) FROM usage_counters WHERE user_id = %s", (uid,)
        ).fetchone()
        rejections = conn.execute(
            "SELECT count(*) FROM quota_rejections WHERE user_id = %s", (uid,)
        ).fetchone()

    assert (counters, rejections) == ((0,), (0,))
