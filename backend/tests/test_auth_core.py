"""离线单测：密码哈希、JWT 会话、cookie 属性。全部是纯函数，不碰数据库。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import Response

from app.core.auth import (
    COOKIE_NAME,
    PASSWORD_MAX_BYTES,
    clear_session_cookie,
    decode_token,
    hash_password,
    issue_token,
    password_bytes,
    set_session_cookie,
    verify_password,
)

pytestmark = pytest.mark.usefixtures("jwt_secret")


def test_hash_and_verify_round_trip() -> None:
    stored = hash_password("correct horse battery")
    assert stored != "correct horse battery"  # 明文不入库
    assert stored.startswith("$2b$")  # bcrypt 标识
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong password", stored)


def test_hash_is_salted() -> None:
    """同一口令两次哈希不同：盐随机，防彩虹表。"""
    assert hash_password("same-secret") != hash_password("same-secret")


def test_malformed_hash_is_a_failed_verification_not_a_crash() -> None:
    """库里哈希格式不对（手改库/迁移残留）时返回 False，不把异常抛到端点。"""
    assert verify_password("whatever", "not-a-bcrypt-hash") is False


def test_password_length_counts_bytes_not_characters() -> None:
    """一个汉字 3 字节：按字符数判长会放过超 72 字节的口令。"""
    assert password_bytes("密码密码") == 12
    assert password_bytes("a" * PASSWORD_MAX_BYTES) == PASSWORD_MAX_BYTES
    assert password_bytes("密" * 25) > PASSWORD_MAX_BYTES


def test_token_round_trip_carries_user_id() -> None:
    assert decode_token(issue_token(42)) == 42


def test_expired_token_decodes_to_none() -> None:
    """过期的 token 与伪造的同等对待：一律 None，调用方翻 401。"""
    stale = issue_token(1, now=datetime.now(UTC) - timedelta(days=8))
    assert decode_token(stale) is None


def test_tampered_token_decodes_to_none() -> None:
    token = issue_token(1)
    head, payload, signature = token.split(".")
    assert decode_token(f"{head}.{payload}.{signature[:-2]}xx") is None


def test_token_signed_with_another_secret_decodes_to_none() -> None:
    """密钥一换，旧 token 立即失效——这是 JWT_SECRET 不轮换的代价与保证。"""
    foreign = jwt.encode(
        {"sub": "1"}, "another-secret-but-long-enough-for-hs256-ok", algorithm="HS256"
    )
    assert decode_token(foreign) is None


def test_session_cookie_is_hardened() -> None:
    response = Response()
    set_session_cookie(response, "token-value")

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=token-value")
    assert "HttpOnly" in cookie  # JS 读不到 → 防 XSS 窃取
    assert "SameSite=lax" in cookie  # 跨站请求不带 → 挡表单类 CSRF
    assert "Path=/" in cookie
    assert "Max-Age=" in cookie


def test_clear_cookie_expires_it() -> None:
    response = Response()
    clear_session_cookie(response)

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "Max-Age=0" in cookie
