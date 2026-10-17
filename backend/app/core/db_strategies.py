"""用户策略与 append-only 版本（M4c / 5d）。"""

from __future__ import annotations

from typing import Any

from psycopg import errors
from psycopg.types.json import Jsonb

from app.core.db_errors import StrategyNameTaken, StrategyVersionConflict


class StrategiesMixin:
    # ── 用户策略（M4c）─────────────────────────────────────

    async def list_strategies(self, user_id: int) -> list[dict[str, Any]]:
        """本人全部策略的**摘要**（不带 code / params——列表不为每行拖一份源码），最近改的在前。

        摘要**不复用 `_strategy_row`**：那个函数吃的是整行（要 `params` 列），而这条 SELECT
        刻意没取它——复用会 KeyError（真库上踩过，离线替身自己拼 dict 反而看不出来）。
        """
        rows = await self._all(
            "SELECT id, name, version, created_at, updated_at FROM strategies "
            "WHERE user_id = %s ORDER BY updated_at DESC",
            (user_id,),
        )
        return [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "version": int(row["version"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    async def create_strategy(
        self,
        strategy_id: str,
        user_id: int,
        name: str,
        code: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """建策略（策略 + 版本 1 同一事务）。撞名抛 `StrategyNameTaken`。

        `INSERT ... RETURNING` 必然有行；版本行的 id 交给 `gen_random_uuid()`（PG13+），
        不需要把 UUID 生成搬到 Python。
        """
        try:
            async with self.pool.connection() as conn, conn.transaction():
                cur = await conn.execute(
                    "INSERT INTO strategies (id, user_id, name, code, params, version) "
                    "VALUES (%s::uuid, %s, %s, %s, %s, 1) "
                    "RETURNING id, name, code, params, version, created_at, updated_at",
                    (strategy_id, user_id, name, code, Jsonb(params)),
                )
                row = await cur.fetchone()
                assert row is not None
                await conn.execute(
                    "INSERT INTO strategy_versions "
                    "(id, strategy_id, version_no, name, code, params) "
                    "VALUES (gen_random_uuid(), %s::uuid, 1, %s, %s, %s)",
                    (strategy_id, name, code, Jsonb(params)),
                )
        except errors.UniqueViolation as exc:
            raise StrategyNameTaken(name) from exc
        return _strategy_row(row)

    async def get_strategy(self, user_id: int, strategy_id: str) -> dict[str, Any] | None:
        """单条（含 code）。带 `user_id` 过滤，越权与不存在同为 None（由调用方翻 404）。"""
        row = await self._one(
            "SELECT id, name, code, params, version, created_at, updated_at FROM strategies "
            "WHERE id = %s::uuid AND user_id = %s",
            (strategy_id, user_id),
        )
        return _strategy_row(row) if row is not None else None

    async def update_strategy(
        self,
        user_id: int,
        strategy_id: str,
        *,
        name: str | None = None,
        code: str | None = None,
        params: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        """改策略（部分更新：`None` = 不动该列）。

        **版本口径**（收尾项 5d）：`SELECT ... FOR UPDATE` 锁住当前行；`expected_version`
        给了且与库里不一致 → `StrategyVersionConflict`（409，不覆盖别人的保存）。
        只有 `code` 或 `params` **实际变化**才 `version+1` 并追加版本行；只改名不增版
        （名字快照留在版本行里，当前名以 `strategies.name` 为准）。

        与旧实现的一条 UPDATE + COALESCE 相比，事务 + 行锁换来的是「版本号连续」与
        「两个标签页同时保存时后写者不静默覆盖」——这正是版本管理要解决的问题。
        """
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT id, name, code, params, version FROM strategies "
                "WHERE id = %s::uuid AND user_id = %s FOR UPDATE",
                (strategy_id, user_id),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            current_version = int(row["version"])
            if expected_version is not None and expected_version != current_version:
                raise StrategyVersionConflict(
                    f"策略已在别处更新（当前 v{current_version}）——请重新打开后再保存"
                )

            next_name = name if name is not None else row["name"]
            next_code = code if code is not None else row["code"]
            next_params = dict(params) if params is not None else dict(row["params"] or {})
            changed = (code is not None and code != row["code"]) or (
                params is not None and dict(params) != dict(row["params"] or {})
            )
            new_version = current_version + 1 if changed else current_version
            try:
                cur = await conn.execute(
                    "UPDATE strategies SET name = %s, code = %s, params = %s, "
                    "version = %s, updated_at = now() "
                    "WHERE id = %s::uuid AND user_id = %s "
                    "RETURNING id, name, code, params, version, created_at, updated_at",
                    (next_name, next_code, Jsonb(next_params), new_version, strategy_id, user_id),
                )
                updated = await cur.fetchone()
            except errors.UniqueViolation as exc:  # 改名撞上本人另一条策略
                raise StrategyNameTaken(name or "") from exc
            assert updated is not None
            if changed:
                await conn.execute(
                    "INSERT INTO strategy_versions "
                    "(id, strategy_id, version_no, name, code, params) "
                    "VALUES (gen_random_uuid(), %s::uuid, %s, %s, %s, %s)",
                    (strategy_id, new_version, next_name, next_code, Jsonb(next_params)),
                )
        return _strategy_row(updated)

    async def list_strategy_versions(
        self, user_id: int, strategy_id: str
    ) -> list[dict[str, Any]] | None:
        """版本摘要（不带 code）；策略不存在/越权返回 None（端点翻 404）。"""
        own = await self._one(
            "SELECT version FROM strategies WHERE id = %s::uuid AND user_id = %s",
            (strategy_id, user_id),
        )
        if own is None:
            return None
        rows = await self._all(
            "SELECT id, version_no, name, created_at FROM strategy_versions "
            "WHERE strategy_id = %s::uuid ORDER BY version_no DESC",
            (strategy_id,),
        )
        current = int(own["version"])
        return [
            {
                "id": str(row["id"]),
                "version_no": int(row["version_no"]),
                "name": row["name"],
                "created_at": row["created_at"],
                "current": int(row["version_no"]) == current,
            }
            for row in rows
        ]

    async def get_strategy_version(
        self, user_id: int, strategy_id: str, version_no: int
    ) -> dict[str, Any] | None:
        """某一版的完整内容（含 code/params）；归属经 `strategies` 判。"""
        row = await self._one(
            "SELECT v.id, v.version_no, v.name, v.code, v.params, v.created_at "
            "FROM strategy_versions v JOIN strategies s ON s.id = v.strategy_id "
            "WHERE v.strategy_id = %s::uuid AND v.version_no = %s AND s.user_id = %s",
            (strategy_id, version_no, user_id),
        )
        return _strategy_row(row) if row is not None else None

    async def rollback_strategy(
        self,
        user_id: int,
        strategy_id: str,
        version_no: int,
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any] | None:
        """把目标版本的内容作为**新版本**追加（append-only，不物理回退）。

        只回滚 `code` / `params`，**保留当前名字**（改名可能与本人另一条策略撞唯一约束，
        且「版本」按内容计）；目标内容与当前一致时不新建版本。策略/版本不存在或越权 → None。
        """
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT id, name, code, params, version FROM strategies "
                "WHERE id = %s::uuid AND user_id = %s FOR UPDATE",
                (strategy_id, user_id),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            current_version = int(row["version"])
            if expected_version is not None and expected_version != current_version:
                raise StrategyVersionConflict(
                    f"策略已在别处更新（当前 v{current_version}）——请重新打开后再回滚"
                )
            cur = await conn.execute(
                "SELECT name, code, params FROM strategy_versions "
                "WHERE strategy_id = %s::uuid AND version_no = %s",
                (strategy_id, version_no),
            )
            target = await cur.fetchone()
            if target is None:
                return None
            same = target["code"] == row["code"] and dict(target["params"] or {}) == dict(
                row["params"] or {}
            )
            if same:
                cur = await conn.execute(
                    "SELECT id, name, code, params, version, created_at, updated_at "
                    "FROM strategies WHERE id = %s::uuid",
                    (strategy_id,),
                )
                unchanged = await cur.fetchone()
                assert unchanged is not None
                return _strategy_row(unchanged)

            new_version = current_version + 1
            cur = await conn.execute(
                "UPDATE strategies SET code = %s, params = %s, version = %s, "
                "updated_at = now() WHERE id = %s::uuid AND user_id = %s "
                "RETURNING id, name, code, params, version, created_at, updated_at",
                (target["code"], Jsonb(dict(target["params"] or {})), new_version,
                 strategy_id, user_id),
            )
            updated = await cur.fetchone()
            assert updated is not None
            await conn.execute(
                "INSERT INTO strategy_versions "
                "(id, strategy_id, version_no, name, code, params) "
                "VALUES (gen_random_uuid(), %s::uuid, %s, %s, %s, %s)",
                (strategy_id, new_version, updated["name"], target["code"],
                 Jsonb(dict(target["params"] or {}))),
            )
        return _strategy_row(updated)

    async def drop_strategy(self, user_id: int, strategy_id: str) -> bool:
        """删策略。返回是否命中（回测记录**不级联删**：`strategy_id` 悬空即可）。"""
        return (
            await self._one(
                "DELETE FROM strategies WHERE id = %s::uuid AND user_id = %s RETURNING id",
                (strategy_id, user_id),
            )
            is not None
        )



def _strategy_row(row: dict[str, Any]) -> dict[str, Any]:
    """`id` 是 UUID，`params` 是 Jsonb——在数据层就转成 JSON 契约里的形状。

    同 `_watchlist_row` 的理由：让 `UUID` / `Jsonb` 这类驱动类型止步于数据层，
    端点与离线替身拿到的都是普通 dict。
    """
    return {**row, "id": str(row["id"]), "params": dict(row["params"] or {})}
