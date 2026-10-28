"""S5 真机集成：redis-coord 的 Lua 令牌桶（跨连接共享 + TTL）。

离线用例只验证了 ARGV/降级路径；Lua 的补令牌与 TTL 必须用真 Redis 跑一遍。
coord 未启动时 skip（集成套件本来按需依赖本机基础设施）。
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress

import pytest

from app.core import coord
from app.core.config import get_settings

pytestmark = pytest.mark.integration


def test_lua_token_bucket_is_shared_and_expires() -> None:
    async def scenario() -> None:
        import redis.asyncio as aioredis

        url = get_settings().redis_coord_url
        first = aioredis.Redis.from_url(url, socket_connect_timeout=0.5)
        second = aioredis.Redis.from_url(url, socket_connect_timeout=0.5)
        key = f"rl:test:{uuid.uuid4().hex}"
        try:
            try:
                await first.ping()
            except Exception:  # noqa: BLE001 —— 没起 coord 就跳过
                pytest.skip("redis-coord 未运行")

            # 容量 2：前两发放行，第三发必须等待（跨两个连接共享同一个键）
            waits = [
                await first.eval(coord.LUA_TAKE, 1, key, 2, 60_000),
                await second.eval(coord.LUA_TAKE, 1, key, 2, 60_000),
                await second.eval(coord.LUA_TAKE, 1, key, 2, 60_000),
            ]
            ttl = await first.pttl(key)
        finally:
            with suppress(Exception):
                await first.delete(key)
            await first.aclose()
            await second.aclose()
        assert waits[0] == 0 and waits[1] == 0
        assert waits[2] > 0
        assert 0 < ttl <= 60_000

    asyncio.run(scenario())
