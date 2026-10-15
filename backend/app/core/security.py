"""安全响应头中间件（E8a，SPEC §8.2）。

**纯 ASGI，不用 `BaseHTTPMiddleware`**：后者会包装响应体迭代器、引入额外任务与缓冲，
SSE 的首字节延迟与断连语义都会被它改掉——而 chat / research / optimize 三条核心路径都是 SSE。
这里只做一件事：在 `http.response.start` 上追加响应头。

**位置**：在 `main.py` 里**最后** `add_middleware`（Starlette 的后加者在更外层），于是它覆盖
CORS 与续期中间件的全部响应。唯一的例外是 `ServerErrorMiddleware` 层的未捕获 500——那在一切
用户中间件之外（SPEC §8.8 已记）。
"""

from __future__ import annotations

from collections.abc import Sequence

from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: 只对 JSON API 成立的组合：无子资源 ⇒ CSP 一行即成立；`frame-ancestors 'none'` 挡点击劫持。
#:
#: **HSTS 不在这里**——本地是 http，加上去没有任何浏览器会理它；上线置 https 时与
#: `session_cookie_value` 的 `Secure` 一起改（SPEC §8.2 已把这两处绑成一句口径）。
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"geolocation=(), camera=(), microphone=()"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
)


class SecurityHeadersMiddleware:
    """给每个 HTTP 响应补上 `SECURITY_HEADERS`（只追加，不覆盖同名头）。"""

    def __init__(
        self, app: ASGIApp, headers: Sequence[tuple[bytes, bytes]] = SECURITY_HEADERS
    ) -> None:
        self.app = app
        self.headers = tuple(headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *self.headers]}
            await send(message)

        await self.app(scope, receive, send_with_headers)
