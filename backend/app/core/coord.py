"""共享限流（S5）：redis-coord + Lua 令牌桶，coord 不可用时降级为进程内。

**为什么需要**：`core.reliability.Limiter` 是进程内 dict；2 个 API worker 各有一份，
同一用户的实际额度会变成 2×。S0 实测 `RATE_LIMIT_CHAT=5` 时 2 workers 放行 10。

**生命周期**：coord 是软依赖。任何异常（连不上/超时/脚本错）都只让**本进程**退回到
`floor(capacity/N)` 的进程内额度，并记 `quantsage_rate_limit_degraded_total`；
连续 3 次失败熔断 30s，期间不再碰 Redis。**不默认 fail-closed**——Redis 抖动不该把
chat/research 直接打成 503；降级后的总额度仍不超过原额度（N 个进程各 floor(capacity/N)）。

**Lua 返回整数毫秒**：Redis 的 Lua 数字回包是整数，返回浮点秒会被截断成 0。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core import reliability
from app.core.config import get_settings
from app.core.metrics import RATE_LIMIT_DEGRADED

log = logging.getLogger(__name__)

#: 令牌桶：`KEYS[1]` 是 `rl:{bucket}:{user_id}`，ARGV 是容量与窗口毫秒。
#: 用 Redis 自己的 `TIME` 结算，避免两个 worker 的墙钟差；每次调用续 TTL=窗口。
LUA_TAKE = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
local rate = capacity / window_ms
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local wait_ms = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait_ms = math.ceil((1 - tokens) / rate)
end
redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', key, window_ms)
return wait_ms
"""

#: 懒建连接：单进程/未启用 shared 时完全不碰 Redis。
_client_obj: Any = None
#: coord 降级用的进程内桶：按 (桶名, 降级容量, 窗口) 复用
_fallback: dict[tuple[str, int, float], reliability.Limiter] = {}


def _client() -> Any:
    global _client_obj
    if _client_obj is None:
        import redis.asyncio as aioredis

        _client_obj = aioredis.Redis.from_url(
            get_settings().redis_coord_url,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
            health_check_interval=30,
        )
    return _client_obj


def reset() -> None:
    """丢连接与降级桶——**测试用**；运行时只在 lifespan 收尾走 `aclose`。"""
    global _client_obj
    _client_obj = None
    _fallback.clear()


async def aclose() -> None:
    """关闭懒建连接。异常不影响停机。"""
    global _client_obj
    client, _client_obj = _client_obj, None
    if client is not None:
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 —— 停机收尾不该再抛
            log.debug("关闭 redis-coord 连接失败（%s）", type(exc).__name__)


def _window_ms(window: float) -> int:
    return max(1, int(window * 1000))


def _degraded(
    name: str,
    key: int,
    bucket: reliability.Bucket,
    workers: int,
    reason: str,
) -> float | None:
    """进程内降级：额度按 worker 数收紧，N 个进程合计不超过原额度。"""
    RATE_LIMIT_DEGRADED.labels(reason=reason).inc()
    capacity = max(1, bucket.capacity // max(1, workers))
    cache_key = (name, capacity, bucket.window)
    limiter = _fallback.get(cache_key)
    if limiter is None:
        limiter = reliability.Limiter(
            name=f"coord-fallback:{name}",
            bucket=reliability.Bucket(capacity=capacity, window=bucket.window),
        )
        _fallback[cache_key] = limiter
    return limiter.take(key)


async def take(name: str, key: int) -> float | None:
    """取一发。放行返回 None，否则是建议等待秒数（与 `reliability.take` 同形）。"""
    settings = get_settings()
    if not (settings.rate_limit_shared and settings.api_workers > 1):
        return reliability.take(name, key)

    bucket = reliability.bucket_of(name)
    dep = reliability.get_dep("redis-coord")
    wait = dep.allow(dep.clock())
    if wait is not None:
        return _degraded(name, key, bucket, settings.api_workers, "circuit_open")

    try:
        wait_ms = await _client().eval(
            LUA_TAKE,
            1,
            f"rl:{name}:{key}",
            bucket.capacity,
            _window_ms(bucket.window),
        )
    except Exception as exc:  # noqa: BLE001 —— coord 是软依赖，任何失败都降级
        dep.record_failure(dep.clock())
        if dep.failures <= 1:
            log.warning(
                "redis-coord 限流不可用（%s），本进程降级为进程内 floor(capacity/N)",
                type(exc).__name__,
            )
        else:
            log.debug("redis-coord 限流失败（%s），本进程降级", type(exc).__name__)
        return _degraded(name, key, bucket, settings.api_workers, "unavailable")

    dep.record_success()
    value = float(wait_ms or 0)
    return None if value <= 0 else value / 1000.0
