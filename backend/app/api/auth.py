"""认证 API：注册 / 登录 / 退出 / 当前用户。

两条安全口径（SPEC §2）：
  * 登录失败对「邮箱不存在」与「密码错」返回**同一响应**，且都跑一次 bcrypt 校验，
    耗时不泄露邮箱是否注册过；
  * bcrypt 是 CPU 密集（约百毫秒），一律 `asyncio.to_thread` 卸载，不阻塞事件循环。

邮箱唯一冲突（`EmailTaken`）到 409 的映射在 `main.py` 统一注册，这里不写 try/except。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator

from app.api.limits import limit_ip
from app.core.auth import (
    COOKIE_NAME,
    DUMMY_HASH,
    PASSWORD_MAX_BYTES,
    PASSWORD_MIN_BYTES,
    clear_session_cookie,
    decode_session,
    hash_password,
    issue_token,
    password_bytes,
    require_db,
    require_user,
    set_session_cookie,
    verify_password,
)
from app.core.config import get_settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

log = logging.getLogger(__name__)


def _require_auth_configured() -> None:
    """未配置 `JWT_SECRET` 时在**写任何用户数据之前**返回 503（与 `require_user` 同口径）。

    只在 `decode_session` 里兜住空密钥还不够：注册会先 `create_user`（已落库）再
    `issue_token` 抛错，重试就变成 409「邮箱已被注册」——用户数据被半途写坏。
    """
    if not get_settings().jwt_secret.get_secret_value():
        raise HTTPException(status_code=503, detail="鉴权未配置（JWT_SECRET 缺失）")

#: 刻意不引 email-validator：本项目只要「形如邮箱」，多一个依赖不值当
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Credentials(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not EMAIL_PATTERN.match(normalized):
            raise ValueError("邮箱格式不正确")
        return normalized

    @field_validator("password")
    @classmethod
    def _check_password_bytes(cls, value: str) -> str:
        # 按**字节**而非字符数判：一个汉字 3 字节，字符数上限会放过 72 字节以上的口令
        size = password_bytes(value)
        if size < PASSWORD_MIN_BYTES:
            raise ValueError(f"密码至少 {PASSWORD_MIN_BYTES} 个字节")
        if size > PASSWORD_MAX_BYTES:
            raise ValueError(f"密码不得超过 {PASSWORD_MAX_BYTES} 个字节（bcrypt 上限）")
        return value


class LoginRequest(Credentials):
    """登录请求。`remember` 只改 cookie 的存活方式（持久 / 会话），不改 token 有效期。"""

    remember: bool = True


class UserOut(BaseModel):
    id: int
    email: str
    #: 角色（E2b）：前端据此决定要不要显示 admin 入口——**那不是门**，门在服务端（403）
    role: str


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    request: Request,
    response: Response,
    body: Credentials,
    _: None = Depends(limit_ip("auth")),
) -> UserOut:
    """注册即登录：与登录共用签发逻辑，前端少一次往返。

    按**客户端地址**限流：每次注册都要跑一次 bcrypt，不挡就是 CPU 放大入口。
    """
    _require_auth_configured()
    db = require_db(request)
    password_hash = await asyncio.to_thread(hash_password, body.password)
    user = await db.create_user(body.email, password_hash)
    set_session_cookie(response, issue_token(int(user["id"])))
    return UserOut(id=int(user["id"]), email=user["email"], role=user.get("role") or "user")


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    _: None = Depends(limit_ip("auth")),
) -> UserOut:
    """登录。按客户端地址限流，挡密码喷洒与 bcrypt CPU 放大。"""
    _require_auth_configured()
    db = require_db(request)
    user = await db.find_user_by_email(body.email)

    # 用户不存在时也跑一次校验（拿占位哈希），让两条失败路径耗时接近
    password_hash = user["password_hash"] if user else DUMMY_HASH
    matched = await asyncio.to_thread(verify_password, body.password, password_hash)
    if user is None or not matched:
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")

    # **按库里的撤销版本签发**：登出过（版本已加一）的用户再登录，拿到的票才与库对得上
    set_session_cookie(
        response,
        issue_token(
            int(user["id"]),
            version=int(user.get("token_version") or 0),
            remember=body.remember,
        ),
        remember=body.remember,
    )
    return UserOut(id=int(user["id"]), email=user["email"], role=user.get("role") or "user")


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    """幂等：没登录也返回成功，不做鉴权（清一个不存在的 cookie 没有副作用）。

    E8a 起顺带**撤销**：cookie 能解出会话就把它那一版作废（`users.token_version` 加一），
    于是「登出」对服务端也生效，而不是只把 cookie 从浏览器抹掉（那之前旧票还能用满 7 天）。

    两条刻意的让步：**库不可用时不拦**（登出因为库挂了而失败最没道理，这一次退化成旧语义：
    仅清 cookie）；**不看 token 是否被撤销过**（重复登出照样成功，语义仍是幂等）。
    """
    token = request.cookies.get(COOKIE_NAME)
    session = decode_session(token) if token else None
    db = getattr(request.app.state, "db", None)
    if session is not None and db is not None:
        try:
            # **只撤销「当前有效」的那一版**：一张早已过期的旧 token 若也能 bump，
            # 攻击者就能拿它反复把用户刚登录的会话踢下线（版本号被无限加一）。
            user = await db.find_user_by_id(session.user_id)
            if user is not None and int(user.get("token_version") or 0) == session.version:
                await db.bump_token_version(session.user_id, expected=session.version)
        except Exception as exc:  # noqa: BLE001 —— 撤销失败也必须清 cookie
            # 退化成旧语义（只清 cookie），但不能让「库抖一下」把登出整成 500：
            # 用户视角只有一句话——点了登出，浏览器里的会话必须消失。
            log.warning("登出撤销失败（%s），本次仅清 cookie", type(exc).__name__)

    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
async def me(user: dict[str, Any] = Depends(require_user)) -> UserOut:
    return UserOut(id=int(user["id"]), email=user["email"], role=user.get("role") or "user")
