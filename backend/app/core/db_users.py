"""用户与令牌版本（M1 / E8a）。"""

from __future__ import annotations

from typing import Any

from psycopg import errors

from app.core.db_errors import EmailTaken


class UsersMixin:
    # ── 用户 ────────────────────────────────────────────────

    async def create_user(self, email: str, password_hash: str) -> dict[str, Any]:
        """建用户；邮箱重复抛 `EmailTaken`（不返回 None，避免调用方漏判）。"""
        try:
            async with self.pool.connection() as conn:
                cursor = await conn.execute(
                    "INSERT INTO users (email, password_hash) VALUES (%s, %s) "
                    "RETURNING id, email, created_at, role",
                    (email, password_hash),
                )
                row = await cursor.fetchone()
        except errors.UniqueViolation as exc:
            raise EmailTaken(email) from exc
        assert row is not None  # INSERT ... RETURNING 必然有行
        return row

    async def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        # `token_version` 一并取出：登录要按它签发（否则登出后再登录会签出一张立刻失效的票）
        # `role` 也随行返回：登录响应要它（SPEC §3.4）；缺了它 admin 会被回成普通用户。
        return await self._one(
            "SELECT id, email, password_hash, token_version, role FROM users WHERE email = %s",
            (email,),
        )

    async def find_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        # `token_version` 随行返回：`require_user` 用它判撤销，省一次读库（E8a）
        # `role` 同理：`require_admin` 与配额豁免都吃这一行（E2b）
        return await self._one(
            "SELECT id, email, token_version, role FROM users WHERE id = %s", (user_id,)
        )

    async def bump_token_version(self, user_id: int, *, expected: int | None = None) -> bool:
        """把用户的撤销版本加一：此前签发的所有 token 立即失效（登出/改密用，E8a）。

        **`expected` 是条件更新**：只有库里的版本仍等于调用方手里那张票的版本时才加一。
        否则一张早已被撤销的旧 token 可以反复调用登出，把用户刚登录的新会话无限踢下线。
        返回是否真的加了一（`expected=None` 时恒 True，保留旧语义）。
        """
        if expected is None:
            await self._exec(
                "UPDATE users SET token_version = token_version + 1 WHERE id = %s", (user_id,)
            )
            return True
        row = await self._one(
            "UPDATE users SET token_version = token_version + 1 "
            "WHERE id = %s AND token_version = %s RETURNING id",
            (user_id, expected),
        )
        return row is not None
