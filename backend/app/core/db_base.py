"""`Database` 的共享底座：池、跨进程锁与单条 SQL 辅助方法。

各域 mixin 只依赖这里的 `self.pool` / `_one` / `_all` / `_exec`，不互相 import。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg_pool import AsyncConnectionPool


class BaseRepo:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool
    @asynccontextmanager
    async def advisory_lock(self, key: str) -> AsyncIterator[None]:
        """跨进程会话锁（chat 同一 thread 不并发跑图）。

        **独立连接**而不是池里的连接：锁要持有到整轮图跑完（最长 180s），占池槽位会让
        其它请求排队。连接关闭即释放 session 级 advisory lock；进程被杀也会由内核断开释放。
        """
        import psycopg

        conn = await psycopg.AsyncConnection.connect(self.pool.conninfo, autocommit=True)
        try:
            await conn.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,)
            )
            yield
        finally:
            await conn.close()
    # ── 内部 ────────────────────────────────────────────────

    async def _one(self, sql: str, params: tuple) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(sql, params)
            return await cursor.fetchone()

    async def _all(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(sql, params)
            return list(await cursor.fetchall())

    async def _exec(self, sql: str, params: tuple) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(sql, params)
