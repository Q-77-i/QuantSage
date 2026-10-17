"""请求级可观测性（E7e）：request id + HTTP 指标 + 一行结构化访问日志。

**纯 ASGI**（同 `security.py` / `session.py`）：不包装响应体迭代器，SSE 不会被缓冲。
`X-Request-Id` 回写到响应头（CORS 的 `expose_headers` 已放行），客户端可凭它把一次
报错对到服务端日志——这也是结构化日志里 `request_id` 的来源。

**路由标签用模板不用原始路径**：`/api/v1/paper/accounts/{account_id}` 而不是带 UUID 的
实际路径，否则 Prometheus 的时间序列会被每个账户/策略 id 撑爆。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from app.core.logging import request_id_var
from app.core.metrics import HTTP_DURATION, HTTP_REQUESTS

log = logging.getLogger("quantsage.http")

#: 客户端传的 `X-Request-Id` 只接受这种形状；其余一律重新生成（防日志注入/超长）
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class ObservabilityMiddleware:
    """放在中间件栈最外层：看到的是最终状态码与完整耗时（含 SSE 流）。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        request_id = supplied if _REQUEST_ID.match(supplied) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        method = str(scope.get("method", "GET"))
        path = str(scope.get("path", ""))
        status = 500
        started = time.perf_counter()

        async def send_with_id(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            duration = time.perf_counter() - started
            # 404 等未匹配路由统一记 `unmatched`，不把任意路径写进标签
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
            HTTP_DURATION.labels(method=method, route=route).observe(duration)
            log.info(
                "http_request method=%s path=%s status=%s duration_ms=%.2f",
                method,
                path,
                status,
                duration * 1000,
                extra={
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "status": status,
                    "duration_ms": round(duration * 1000, 2),
                },
            )
            request_id_var.reset(token)
