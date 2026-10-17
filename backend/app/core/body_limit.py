"""请求体硬上限（纯 ASGI）。

**为什么需要**：uvicorn 不限制请求体大小，FastAPI/Pydantic 会把 JSON 完整读进内存再解析。
`POST /optimize/grid` 的 `axes[].values` 与 `params` 在没有字段上限时，一个几百 MB 的请求
就能在解析阶段把单机打穿；`limit()` / 配额都排在解析之后，救不了。

实现刻意选择「先读、再回放」：这样 chunked（无 `Content-Length`）也受同一条上限约束。
上限默认 1MB——本仓最大的合法请求体是 64KB 的策略源码，余量足够。
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyLimitMiddleware:
    """超过 `max_bytes` 的 HTTP 请求体直接 413，不进入应用层。"""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max(1, int(max_bytes))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 有 Content-Length 时先走快路径：不把大包读进来再拒
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._reject(send)
                    return
            except ValueError:
                pass  # 交给下面的实际读取兜底

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body") or b"")
            if len(body) > self.max_bytes:
                await self._reject(send)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, send: Send) -> None:
        limit_kb = self.max_bytes // 1024
        payload = json.dumps(
            {"detail": f"请求体超过 {limit_kb} KB 上限"}, ensure_ascii=False
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


__all__ = ["BodyLimitMiddleware"]
