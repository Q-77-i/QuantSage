"""会话续期中间件（E8a，SPEC §8.3）。

**为什么是中间件，而不是 `require_user` 的依赖注入**：本仓 chat / research 的 SSE 端点直接
`return StreamingResponse(...)`，而 FastAPI 在「端点直出 Response」这条路径上**不合并**注入
`Response` 的头（`fastapi/routing.py` 的 `isinstance(raw_response, Response)` 分支只带走
background）——续期 cookie 挂在依赖上会在三条流式路由上**静默失效**。中间件与端点形态无关。

**纯 ASGI**：理由同 `app/core/security.py`（不复述）。

三条判据写死：
  * **过半程才换**（`Session.renewal_due`）——每个响应都发 `Set-Cookie` 会让缓存与调试都变脏，
    而收益为零：半程判据已经保证活跃用户走不到过期；
  * **只在成功响应（<400）上换**——401/503 是「这次请求没成」，不该顺手续命；
  * **只给真正通过 `require_user` 的请求续**：公开端点不续。否则登出/改密之后，一次
    `/health` 就能把刚清掉的 cookie 用旧 token 再签回来（库不可用时尤其致命）。
    响应里已有 `Set-Cookie: qs_session=…` 时也一律不续——登出必须能真正清掉 cookie。

签发用的版本取自 token 自身：成功响应意味着 `require_user` 已比对过版本，所以这里**不读库**
（读库会把每个响应都拖上一次查询，而中间件看得到所有请求，包括不需要鉴权的那些）。
"""

from __future__ import annotations

from http.cookies import SimpleCookie

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.auth import COOKIE_NAME, decode_session, issue_token, session_cookie_value


def _cookie(scope: Scope, name: str) -> str | None:
    """从 ASGI scope 里取一个 cookie 的值（没有就是 None）。"""
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            jar = SimpleCookie()
            jar.load(value.decode("latin-1"))
            morsel = jar.get(name)
            return morsel.value if morsel else None
    return None


class SessionRenewalMiddleware:
    """活跃会话过半程即换新 cookie：**活跃即续、闲置满 TTL 失效**。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        renewal = _renewal_cookie(scope)

        async def send_with_cookie(message: Message) -> None:
            if (
                renewal is not None
                and message["type"] == "http.response.start"
                and int(message["status"]) < 400
                # 标志在端点里由 `require_user` 设置，必须在**响应头**这一刻读，
                # 不能在请求开始算 renewal 时读（那时鉴权还没跑）
                and scope.get("qs_session_verified")
                and not _sets_session_cookie(message)
            ):
                message = {
                    **message,
                    "headers": [*message.get("headers", []), (b"set-cookie", renewal)],
                }
            await send(message)

        await self.app(scope, receive, send_with_cookie)


def _sets_session_cookie(message: Message) -> bool:
    """响应已经自己写了 `qs_session` 的 Set-Cookie（登出/登录）——续期不许再覆盖它。"""
    marker = f"{COOKIE_NAME}=".encode("latin-1")
    return any(
        key.lower() == b"set-cookie" and marker in value
        for key, value in message.get("headers", [])
    )


def _renewal_cookie(scope: Scope) -> bytes | None:
    """该续就给出 Set-Cookie 的头值，否则 None。纯解码，不读库（理由见模块 docstring）。

    「这次请求是否真的通过了鉴权」在**响应头阶段**才由 `send_with_cookie` 判——`require_user`
    要等端点执行时才写 `qs_session_verified`，此处读不到。
    """
    token = _cookie(scope, COOKIE_NAME)
    if not token:
        return None

    session = decode_session(token)
    if session is None or not session.renewal_due():
        return None

    fresh = issue_token(
        session.user_id, version=session.version, remember=session.remember
    )
    return session_cookie_value(fresh, remember=session.remember).encode("latin-1")
