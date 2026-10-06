"""鉴权核心：密码哈希、JWT 会话、`require_user` 依赖。

三处刻意的选择：
  * `bcrypt` 直用，不引 passlib（对 bcrypt 4.x 有 `__about__` 告警且维护停滞）；
  * 密码在 **schema 层**限长 8–72 字节——bcrypt 只取前 72 字节，超长必须拒绝而非静默截断；
  * 本模块的函数都是**纯同步**的，bcrypt 是 CPU 密集（默认 cost 下约百毫秒），
    调用方（端点）负责 `asyncio.to_thread` 卸载，别在事件循环里直接跑。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt
from fastapi import HTTPException, Request, Response

from app.core.config import get_settings

COOKIE_NAME = "qs_session"
ALGORITHM = "HS256"

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


def issue_token(user_id: int, *, now: datetime | None = None) -> str:
    """签发 access token。`now` 只给测试注入用，正常走系统时间。"""
    settings = get_settings()
    issued = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": issued,
        "exp": issued + timedelta(seconds=settings.jwt_ttl_seconds),
    }
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)


def decode_token(token: str) -> int | None:
    """解出 user id；过期、签名不对、格式坏都返回 None（调用方一律 401）。"""
    try:
        payload = jwt.decode(
            token, get_settings().jwt_secret.get_secret_value(), algorithms=[ALGORITHM]
        )
        return int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        return None


def set_session_cookie(response: Response, token: str) -> None:
    """写会话 cookie。

    `secure=False` 只因为本地是 http；上线必须置 True（SPEC §2 已记）。
    `samesite="lax"`：跨站请求不带 cookie，顺带挡掉表单类 CSRF；
    本项目前后端同 site（127.0.0.1 的 3001 ↔ 8000），Lax 不影响正常调用。
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=get_settings().jwt_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


def require_db(request: Request) -> Any:
    """受保护端点的业务库出口。库没起来（降级启动）时 503，与 checkpointer 同姿态。"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="数据库不可用")
    return db


async def require_user(request: Request) -> dict[str, Any]:
    """受保护端点的统一入口：未登录 401、无会话失效 401、库不可用 503。

    越权（资源不属于本人）不在这里判——那是各端点带 `user_id` 查库的事，
    命中 0 行一律 404（见 `app/api/chat.py`）。
    """
    if not get_settings().jwt_secret.get_secret_value():
        raise HTTPException(status_code=503, detail="鉴权未配置（JWT_SECRET 缺失）")

    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")

    user_id = decode_token(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")

    user = await require_db(request).find_user_by_id(user_id)
    if user is None:
        # 用户被删/库被重置后，旧 token 仍能解出 id——这里兜住
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")

    return user
