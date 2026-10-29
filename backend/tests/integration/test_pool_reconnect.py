"""S6：PG 重启后，池里的**空闲**旧连接不能把第一个请求打成 500。

`AsyncConnectionPool` 默认不检查连接（`check=None`）；`putconn` 只在归还时看状态，
空闲在池里的连接被 PG 重启掐断后，下一次 `getconn` 会把它直接发出去。业务池加
`check=AsyncConnectionPool.check_connection` 后，取件时发现坏连接就丢弃并重连。
"""

from __future__ import annotations

import psycopg
import pytest

from app.core.config import get_settings
from app.core.db import open_pool


@pytest.mark.integration
async def test_open_pool_replaces_an_idle_stale_connection() -> None:
    dsn = get_settings().postgres_dsn
    async with open_pool(dsn, min_size=1, max_size=1) as pool:
        async with pool.connection() as conn:
            cursor = await conn.execute("SELECT pg_backend_pid() AS pid")
            row = await cursor.fetchone()
            assert row is not None
            pid = row["pid"]

        # 连接已归还到池里（空闲）；从外部掐断它
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as admin:
            await admin.execute("SELECT pg_terminate_backend(%s)", (pid,))

        async with pool.connection() as conn:
            cursor = await conn.execute("SELECT 1 AS ok")
            row = await cursor.fetchone()
            assert row is not None and row["ok"] == 1
