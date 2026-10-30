"""`Database` 的共享底座：池、跨进程锁与单条 SQL 辅助方法。

各域 mixin 只依赖这里的 `self.pool` / `_one` / `_all` / `_exec`，不互相 import。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg_pool import AsyncConnectionPool

from app.core.config import get_settings


class ChatLockBusy(RuntimeError):
    """chat 的池外锁连接槽位已满（P2 A8）。调用方应在开流前翻成 503。"""

    kind = "chat_lock_busy"


class BaseRepo:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool
        settings = get_settings()
        # 每进程一池许可：锁连接要持有到整轮结束，不能占业务池槽位，也不能无上限。
        self._chat_lock_slots = asyncio.BoundedSemaphore(
            max(1, int(settings.chat_lock_max_connections))
        )
        self._chat_lock_timeout = max(
            0.1, float(settings.chat_lock_acquire_timeout_seconds)
        )

    async def acquire_chat_lock_slot(self) -> None:
        """占用一条 chat 锁连接许可；满时在 `CHAT_LOCK_ACQUIRE_TIMEOUT` 内抛 `ChatLockBusy`。"""
        try:
            await asyncio.wait_for(
                self._chat_lock_slots.acquire(), timeout=self._chat_lock_timeout
            )
        except TimeoutError as exc:
            raise ChatLockBusy("会话并发已满，请稍后再试") from exc

    def release_chat_lock_slot(self) -> None:
        self._chat_lock_slots.release()

    @asynccontextmanager
    async def advisory_lock(self, key: str) -> AsyncIterator[None]:
        """跨进程会话锁（chat 同一 thread 不并发跑图）。

        **独立连接**而不是池里的连接：锁要持有到整轮图跑完（最长 180s），占池槽位会让
        其它请求排队。连接关闭即释放 session 级 advisory lock；进程被杀也会由内核断开释放。

        **调用方必须先 `acquire_chat_lock_slot()`**（当前唯一调用方是 chat 路由）：
        许可负责“最多几条池外连接”，本方法只管锁；两件事分开才能把 503 放在开流前。
        """
        import psycopg

        conn = await asyncio.wait_for(
            psycopg.AsyncConnection.connect(self.pool.conninfo, autocommit=True),
            timeout=self._chat_lock_timeout,
        )
        try:
            await conn.execute("SET application_name = 'quantsage-chat-lock'")
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
