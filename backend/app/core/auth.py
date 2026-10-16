"""鉴权核心：密码哈希、JWT 会话、`require_user` 依赖。

四处刻意的选择：
  * `bcrypt` 直用，不引 passlib（对 bcrypt 4.x 有 `__about__` 告警且维护停滞）；
  * 密码在 **schema 层**限长 8–72 字节——bcrypt 只取前 72 字节，超长必须拒绝而非静默截断；
  * 本模块的函数都是**纯同步**的，bcrypt 是 CPU 密集（默认 cost 下约百毫秒），
    调用方（端点）负责 `asyncio.to_thread` 卸载，别在事件循环里直接跑；
  * 会话有效期两端也进 payload（E8a）：`ver` 与 `users.token_version` 比对，让「登出」
    对服务端也生效；`Session.renewal_due()` 给滑动续期用（过半程才换，见 `app/core/session.py`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, Response

from app.core.config import get_settings

COOKIE_NAME = "qs_session"
ALGORITHM = "HS256"

#: 角色（E2b）：`users.role` 的两个取值。**只在这里定义一份**——`core/quota.py` 的
#: 「admin 不限额」也引用它，免得两处各写一个字面量而漂移。
ROLE_USER = "user"
ROLE_ADMIN = "admin"

#: bcrypt 的硬上限：超过 72 字节的部分不参与计算，必须显式拒绝
PASSWORD_MAX_BYTES = 72
PASSWORD_MIN_BYTES = 8

#: 邮箱不存在时拿它跑一次校验，让失败耗时不泄露「该邮箱是否注册过」。
#: 明文是随机串、未落任何地方，仅作占位（改它无风险，只需是合法 bcrypt 哈希）
DUMMY_HASH = "$2b$12$/6qMm1Df5h7aZnQ0X5X7nONWhJODVYx.tXJkScVs/.UdfAn7z4w2K"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        # 库里的哈希格式不对（手改过库/迁移残留）时按「验证不通过」处理，不抛到端点
        return False


def password_bytes(password: str) -> int:
    return len(password.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class Session:
    """解出来的会话：谁、撤销版本、有效期两端。

    `version` 是签发时的 `users.token_version`。两者不等即视为已撤销（登出/改密会加一）。
    """

    user_id: int
    version: int
    issued_at: datetime
    expires_at: datetime

    @property
    def lifetime(self) -> timedelta:
        return self.expires_at - self.issued_at

    def renewal_due(self, *, now: datetime | None = None) -> bool:
        """该换一张新 cookie 了吗：**剩余寿命不足一半**才换（E8a 的滑动续期）。

        为什么不是「每个响应都换」：那样每个响应都带 `Set-Cookie`，缓存与调试都变脏，
        而收益为零——半程判据已经保证活跃用户永远不会走到过期。
        """
        moment = now or datetime.now(UTC)
        return self.expires_at - moment < self.lifetime / 2


def issue_token(user_id: int, *, now: datetime | None = None, version: int = 0) -> str:
    """签发 access token。`now` 只给测试注入用，正常走系统时间。

    `version` 是用户的撤销版本（`users.token_version`）：登出会把它加一，于是**此前签发的
    所有 token 在 `require_user` 处一并失效**——没有服务端会话表也能做到「登出即失效」。
    """
    settings = get_settings()
    issued = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "ver": int(version),
        "iat": issued,
        "exp": issued + timedelta(seconds=settings.jwt_ttl_seconds),
    }
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)


def decode_session(token: str) -> Session | None:
    """解出会话；过期、签名不对、格式坏都返回 None（调用方一律 401）。

    **无 `ver` 的历史 token 按 0 处理**——与 `users.token_version` 的列默认值一致，
    本次改动不会把既有会话全部打断（它们本来也没被撤销过）。
    """
    try:
        payload = jwt.decode(
            token, get_settings().jwt_secret.get_secret_value(), algorithms=[ALGORITHM]
        )
        return Session(
            user_id=int(payload["sub"]),
            version=int(payload.get("ver", 0)),
            issued_at=datetime.fromtimestamp(float(payload["iat"]), UTC),
            expires_at=datetime.fromtimestamp(float(payload["exp"]), UTC),
        )
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        return None


def session_cookie_value(token: str, *, remember: bool = True) -> str:
    """会话 cookie 的完整头值——**属性只有这一份**。

    端点写 cookie（`set_session_cookie`）与续期中间件（纯 ASGI，手里没有 `Response` 对象）
    共用它，免得两处各写一遍属性而漂移。

    `remember` 决定它是**持久 cookie** 还是**会话 cookie**：前者带 `Max-Age`（关掉浏览器
    仍在），后者不带（浏览器关掉即失效）。这是「记住我」的全部实现——没有服务端会话表，
    token 本身的有效期仍由 JWT 的 `exp` 兜底。

    `HttpOnly`：JS 读不到 → 防 XSS 窃取。`SameSite=lax`：跨站请求不带 cookie，顺带挡掉
    表单类 CSRF；本项目前后端同 site（页面 host 与 API host 相同，只差端口），Lax 不影响
    正常调用。**`secure` 缺省即不写**——本地是 http；上线必须补上 `Secure`（与 HSTS
    同一处口径，见 SPEC §8.2）。
    """
    parts = [f"{COOKIE_NAME}={token}", "Path=/", "SameSite=lax", "HttpOnly"]
    if remember:
        parts.append(f"Max-Age={get_settings().jwt_ttl_seconds}")
    return "; ".join(parts)


def set_session_cookie(response: Response, token: str, *, remember: bool = True) -> None:
    """写会话 cookie（属性见 `session_cookie_value`）。"""
    response.headers.append("set-cookie", session_cookie_value(token, remember=remember))


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


def require_db(request: Request) -> Any:
    """受保护端点的业务库出口。库没起来（降级启动）时 503，与 checkpointer 同姿态。"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="数据库不可用")
    return db


async def require_user(request: Request) -> dict[str, Any]:
    """受保护端点的统一入口：未登录 401、会话失效/被撤销 401、库不可用 503。

    越权（资源不属于本人）不在这里判——那是各端点带 `user_id` 查库的事，
    命中 0 行一律 404（见 `app/api/chat.py`）。
    """
    if not get_settings().jwt_secret.get_secret_value():
        raise HTTPException(status_code=503, detail="鉴权未配置（JWT_SECRET 缺失）")

    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")

    session = decode_session(token)
    if session is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")

    user = await require_db(request).find_user_by_id(session.user_id)
    if user is None:
        # 用户被删/库被重置后，旧 token 仍能解出 id——这里兜住
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")

    # 撤销（E8a）：登出/改密把 `users.token_version` 加一，此前签发的 token 到此为止。
    # 复用上面这次查询（`find_user_by_id` 已带该列），不额外读库。
    if int(user.get("token_version") or 0) != session.version:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")

    return user


async def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """admin 面的入口（E2b）：角色不足一律 **403**。

    **为什么是全仓第一处 403**（此前越权一律与「不存在」同响应 404）：那条 404 口径保护的是
    「**别人的资源**是否存在」——防的是同侪之间互相探测。admin 面不属任何用户，缺的是**角色**，
    而且它的存在本来就不是秘密（PRD 写明了要有 admin 视图）。语义不同，姿态就不同，
    也与「超配额被拒并给出明确提示」这条验收的精神一致。

    角色**不进 JWT**：`require_user` 每次都查 users 整行，角色变更立即生效；
    塞进 payload 会引入「撤销角色要 bump token_version」的耦合，而那个字段的语义是会话撤销。
    """
    if str(user.get("role") or ROLE_USER) != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
