"""认证 API：注册 / 登录 / 退出 / 当前用户。

两条安全口径（SPEC §2）：
  * 登录失败对「邮箱不存在」与「密码错」返回**同一响应**，且都跑一次 bcrypt 校验，
    耗时不泄露邮箱是否注册过；
  * bcrypt 是 CPU 密集（约百毫秒），一律 `asyncio.to_thread` 卸载，不阻塞事件循环。

邮箱唯一冲突（`EmailTaken`）到 409 的映射在 `main.py` 统一注册，这里不写 try/except。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator

from app.core.auth import (
    DUMMY_HASH,
    PASSWORD_MAX_BYTES,
    PASSWORD_MIN_BYTES,
    clear_session_cookie,
    hash_password,
    issue_token,
    password_bytes,
    require_db,
    require_user,
    set_session_cookie,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

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


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    request: Request, response: Response, body: Credentials
) -> UserOut:
    """注册即登录：与登录共用签发逻辑，前端少一次往返。"""
    db = require_db(request)
    password_hash = await asyncio.to_thread(hash_password, body.password)
    user = await db.create_user(body.email, password_hash)
    set_session_cookie(response, issue_token(int(user["id"])))
    return UserOut(id=int(user["id"]), email=user["email"])


@router.post("/login")
async def login(request: Request, response: Response, body: LoginRequest) -> UserOut:
    db = require_db(request)
    user = await db.find_user_by_email(body.email)

    # 用户不存在时也跑一次校验（拿占位哈希），让两条失败路径耗时接近
    password_hash = user["password_hash"] if user else DUMMY_HASH
    matched = await asyncio.to_thread(verify_password, body.password, password_hash)
    if user is None or not matched:
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")

    set_session_cookie(response, issue_token(int(user["id"])), remember=body.remember)
    return UserOut(id=int(user["id"]), email=user["email"])


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    """幂等：没登录也返回成功，不做鉴权（清一个不存在的 cookie 没有副作用）。"""
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
async def me(user: dict[str, Any] = Depends(require_user)) -> UserOut:
    return UserOut(id=int(user["id"]), email=user["email"])
