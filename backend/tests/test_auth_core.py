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
    decode_session,
    hash_password,
    issue_token,
    password_bytes,
    session_cookie_value,
    set_session_cookie,
    verify_password,
)
from app.core.config import get_settings

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
    session = decode_session(issue_token(42))

    assert session is not None
    assert session.user_id == 42


def test_expired_token_decodes_to_none() -> None:
    """过期的 token 与伪造的同等对待：一律 None，调用方翻 401。"""
    stale = issue_token(1, now=datetime.now(UTC) - timedelta(days=8))
    assert decode_session(stale) is None


def test_tampered_token_decodes_to_none() -> None:
    token = issue_token(1)
    head, payload, signature = token.split(".")
    assert decode_session(f"{head}.{payload}.{signature[:-2]}xx") is None


def test_token_signed_with_another_secret_decodes_to_none() -> None:
    """密钥一换，旧 token 立即失效——这是 JWT_SECRET 不轮换的代价与保证。"""
    foreign = jwt.encode(
        {"sub": "1"}, "another-secret-but-long-enough-for-hs256-ok", algorithm="HS256"
    )
    assert decode_session(foreign) is None


def test_token_carries_revocation_version() -> None:
    """`ver` 进 payload：登出把它加一，旧票在 `require_user` 处被比对出来（E8a）。"""
    session = decode_session(issue_token(7, version=3))

    assert session is not None
    assert session.version == 3


def test_token_without_version_is_treated_as_zero() -> None:
    """**兼容口径**：本次改动前签发的 token 没有 `ver`——按 0 处理，既有会话不被打断。

    签一张「老格式」的票（只有 sub / iat / exp）来验，而不是拿今天的 `issue_token` 硬凑。
    """
    issued = datetime.now(UTC)
    legacy = jwt.encode(
        {
            "sub": "5",
            "iat": issued,
            "exp": issued + timedelta(days=7),
        },
        get_settings().jwt_secret.get_secret_value(),
        algorithm="HS256",
    )

    session = decode_session(legacy)
    assert session is not None
    assert session.version == 0


def test_renewal_is_due_only_after_half_life() -> None:
    """滑动续期的判据：**过半程才换**（不是每响应都换，也不是快过期才换）。"""
    now = datetime.now(UTC)
    ttl = timedelta(days=7)

    fresh = decode_session(issue_token(1, now=now))
    assert fresh is not None
    assert not fresh.renewal_due(now=now + timedelta(days=1))

    old = decode_session(issue_token(1, now=now - ttl / 2 - timedelta(hours=1)))
    assert old is not None
    assert old.renewal_due(now=now)


def test_expired_token_is_never_eligible_for_renewal() -> None:
    """过期票解不出会话 ⇒ 中间件不会给它续命（`decode_session` 返回 None 是唯一前提）。"""
    assert decode_session(issue_token(1, now=datetime.now(UTC) - timedelta(days=8))) is None


def test_session_cookie_is_hardened() -> None:
    response = Response()
    set_session_cookie(response, "token-value")

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=token-value")
    assert "HttpOnly" in cookie  # JS 读不到 → 防 XSS 窃取
    assert "SameSite=lax" in cookie  # 跨站请求不带 → 挡表单类 CSRF
    assert "Path=/" in cookie
    assert "Max-Age=" in cookie


def test_renewal_and_login_write_the_same_cookie_attributes() -> None:
    """端点写 cookie 与续期中间件写 cookie **必须逐字一致**（E8a）。

    两处共用 `session_cookie_value`，这条用例钉住「共用」这件事——哪天有人只改了一边
    （比如给续期那侧补 Secure 而忘了登录那侧），这里立刻红。
    """
    response = Response()
    set_session_cookie(response, "token-value")

    assert response.headers["set-cookie"] == session_cookie_value("token-value")
    assert "Max-Age=" not in session_cookie_value("token-value", remember=False)


def test_clear_cookie_expires_it() -> None:
    response = Response()
    clear_session_cookie(response)

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "Max-Age=0" in cookie
