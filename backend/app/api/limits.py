"""限流的 HTTP 面（E4b / L0）：把 policy 层的令牌桶翻成 429 + `Retry-After`。

**必须在开流之前拒**——chat 与 research 是 SSE，首字节一旦发出就改不了状态码
（与 `api/chat.py` 开头那条注释同源）。`Depends` 在 handler 体之前跑，正好卡在那个位置，
不需要每个端点自己记得「先判再开流」。

拒绝体与全仓的既有信封同形：`{"detail": <一句人话>}` 打底，附加 `kind` / `retry_after`
两个结构化键（与 422 带 `findings` 同一手法，见 `main.py` 的处理器注册）。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, Request

from app.core import reliability
from app.core.auth import require_user


class RateLimitedError(Exception):
    """请求过于频繁。`retry_after` 是建议等待的整秒数（进 `Retry-After` 头）。"""

    kind = "rate_limited"

    def __init__(self, bucket: str, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.bucket = bucket
        self.retry_after = retry_after


def _reject_if_limited(bucket: str, wait: float | None) -> None:
    if wait is None:
        return
    seconds = max(1, math.ceil(wait))
    raise RateLimitedError(bucket, f"请求过于频繁，请 {seconds} 秒后再试", seconds)


def limit(bucket: str) -> Callable[..., Awaitable[None]]:
    """按**用户**造一个依赖。用法：`_: None = Depends(limit("llm-chat"))`。

    桶名见 `core/reliability._bucket_of`（`llm-chat` / `llm-research` / `task-submit` /
    `paper-run` / `pdf`）；额度在 settings 里（`rate_limit_*`），默认取「演示友好」档。
    """

    async def dependency(user: dict[str, Any] = Depends(require_user)) -> None:
        _reject_if_limited(bucket, reliability.take(bucket, int(user["id"])))

    return dependency


def limit_ip(bucket: str) -> Callable[..., Awaitable[None]]:
    """按**客户端地址**造一个依赖，给匿名端点用（`/public/reports/{token}/pdf`）。

    `request.client.host` 是直连对端地址；部署在反代后面时它会是代理地址，需要按部署
    形态显式信任 `X-Forwarded-For`（本项目默认单机直连，不猜代理头，避免伪造绕过）。
    键用 sha256 截断而不是 `hash()`：后者每个进程随机化，重启即失效。
    """

    async def dependency(request: Request) -> None:
        host = request.client.host if request.client is not None else "unknown"
        digest = hashlib.sha256(host.encode("utf-8")).digest()
        key = int.from_bytes(digest[:8], "big")
        _reject_if_limited(bucket, reliability.take(bucket, key))

    return dependency
