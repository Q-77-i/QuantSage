"""业务库：自建表的连接池、幂等建表与数据访问。

与 checkpointer 的池**分开**：`checkpoints` 系列表由 `langgraph-checkpoint-postgres`
自己的迁移管理（`saver.setup()`），本模块只管我们自己的表，两边互不牵扯。

三条约定：
  * 建表走幂等 DDL（`CREATE TABLE IF NOT EXISTS`），项目不引 Alembic——表结构变更一律写在这里，
    启动时执行；改列要另起迁移语句，不能只改这里的 DDL 就当生效（`IF NOT EXISTS` 不会改已存在的表）；
  * 连接 `autocommit=True` + `dict_row`（与 checkpointer 池同口径），单语句操作不需要显式事务；
  * 所有读写都带 `user_id`（见 M1 归属校验），命中 0 行由调用方翻成 404。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

#: 自选股的回落分组：删组时组内标的回落到它，它自己不可重命名 / 删除。
#: 建表默认值与它同源（下面的 DDL 由它拼出），避免两处各写一份字面量而漂移。
DEFAULT_GROUP = "默认分组"

# 幂等建表：按依赖顺序，users 在前（其余表挂它的外键）
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id            BIGSERIAL PRIMARY KEY,
        email         TEXT        NOT NULL UNIQUE,
        password_hash TEXT        NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_threads (
        thread_id      UUID PRIMARY KEY,
        user_id        BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_active_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS chat_threads_user_active_idx "
    "ON chat_threads (user_id, last_active_at DESC)",
    f"""
    CREATE TABLE IF NOT EXISTS watchlist (
        id          BIGSERIAL PRIMARY KEY,
        user_id     BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        symbol      TEXT        NOT NULL,
        group_name  TEXT        NOT NULL DEFAULT '{DEFAULT_GROUP}',
        added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        added_price NUMERIC(18, 4),
        UNIQUE (user_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backtest_runs (
        id         UUID PRIMARY KEY,
        user_id    BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        request    JSONB       NOT NULL,
        report     JSONB       NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS backtest_runs_user_created_idx "
    "ON backtest_runs (user_id, created_at DESC)",
    # M4c：用户策略。`params` 存最近一次保存的参数值（工作台回填用）
    """
    CREATE TABLE IF NOT EXISTS strategies (
        id         UUID PRIMARY KEY,
        user_id    BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name       TEXT        NOT NULL,
        code       TEXT        NOT NULL,
        params     JSONB       NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (user_id, name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS strategies_user_updated_idx "
    "ON strategies (user_id, updated_at DESC)",
    # M4c：`backtest_runs` 增列走**另起的迁移语句**——`CREATE TABLE IF NOT EXISTS` 对已存在的表
    # 什么都不做，只改上面的 DDL 是不会生效的（见模块 docstring 第二条约定）。
    # `strategy_id` **不设外键**：策略删了记录仍在（报告 JSONB 自足，见 SPEC §5 M4c）。
    "ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS strategy_id UUID",
    "ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS code_sha256 TEXT",
    # M5b：批量 / 网格的**汇总**（`request` + `summary`，**不落完整报告**）。
    # `summary.cells` 只有每格的 `params → metrics` 与收益矩，没有净值曲线与逐笔——
    # 一百格 × 上千点净值曲线会把这张表撑成 MB 级 JSONB，而要看某一格的完整报告，
    # 从热力图点进去重跑一次即可（单格 70–350ms）。
    """
    CREATE TABLE IF NOT EXISTS optimization_runs (
        id         UUID PRIMARY KEY,
        user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        request    JSONB NOT NULL,
        summary    JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS optimization_runs_user_created_idx "
    "ON optimization_runs (user_id, created_at DESC)",
)


class EmailTaken(RuntimeError):
    """邮箱已被注册（`users.email` 唯一约束）。"""


class SymbolTracked(RuntimeError):
    """该标的已在本人的自选股里（`watchlist` 的 `UNIQUE(user_id, symbol)`）。"""


class StrategyNameTaken(RuntimeError):
    """策略名已被本人占用（`strategies` 的 `UNIQUE(user_id, name)`）。"""


@asynccontextmanager
async def open_pool(
    conn_string: str, *, min_size: int = 1, max_size: int = 8
) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(
        conn_string,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True, timeout=10)
    try:
        yield pool
    finally:
        await pool.close()


async def init_schema(pool: AsyncConnectionPool) -> None:
    """建表（幂等）。重复执行安全，表已存在时不动。"""
    async with pool.connection() as conn:
        for statement in SCHEMA:
            await conn.execute(statement)


class Database:
    """业务表的全部读写出口。

    刻意只有一个类、方法粒度即业务动作：端点拿到的就是这些语义化调用，
    不在端点里拼 SQL（离线测试注入同形替身即可跑归属矩阵，见 `tests/fakes.py`）。
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    # ── 用户 ────────────────────────────────────────────────

    async def create_user(self, email: str, password_hash: str) -> dict[str, Any]:
        """建用户；邮箱重复抛 `EmailTaken`（不返回 None，避免调用方漏判）。"""
        try:
            async with self.pool.connection() as conn:
                cursor = await conn.execute(
                    "INSERT INTO users (email, password_hash) VALUES (%s, %s) "
                    "RETURNING id, email, created_at",
                    (email, password_hash),
                )
                row = await cursor.fetchone()
        except errors.UniqueViolation as exc:
            raise EmailTaken(email) from exc
        assert row is not None  # INSERT ... RETURNING 必然有行
        return row

    async def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        return await self._one(
            "SELECT id, email, password_hash FROM users WHERE email = %s", (email,)
        )

    async def find_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT id, email FROM users WHERE id = %s", (user_id,))

    # ── 会话归属 ────────────────────────────────────────────

    async def claim_thread(self, thread_id: str, user_id: int) -> None:
        """新会话落归属行。`DO NOTHING`：同一 thread_id 重复调用不报错也不改主人。"""
        await self._exec(
            "INSERT INTO chat_threads (thread_id, user_id) VALUES (%s::uuid, %s) "
            "ON CONFLICT (thread_id) DO NOTHING",
            (thread_id, user_id),
        )

    async def touch_thread(self, thread_id: str, user_id: int) -> None:
        """续聊时刷新活动时间（会话列表按它倒序）。"""
        await self._exec(
            "UPDATE chat_threads SET last_active_at = now() "
            "WHERE thread_id = %s::uuid AND user_id = %s",
            (thread_id, user_id),
        )

    async def thread_owner(self, thread_id: str) -> int | None:
        """归属查询：None 表示无主（或不存在）——调用方一律翻 404，不区分。"""
        row = await self._one(
            "SELECT user_id FROM chat_threads WHERE thread_id = %s::uuid", (thread_id,)
        )
        return int(row["user_id"]) if row is not None else None

    async def list_threads(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """按最近活动倒序的会话（号 + 活动时间）。纯自有表查询，不碰 checkpointer 内部表。

        带出 `last_active_at` 而不是只给号：个人空间的会话历史要显示「最近活动」，
        而它正是排序依据本身，没有额外查询。
        """
        return [
            {"thread_id": str(row["thread_id"]), "last_active_at": row["last_active_at"]}
            for row in await self._all(
                "SELECT thread_id, last_active_at FROM chat_threads WHERE user_id = %s "
                "ORDER BY last_active_at DESC LIMIT %s",
                (user_id, limit),
            )
        ]

    async def drop_thread(self, thread_id: str, user_id: int) -> None:
        await self._exec(
            "DELETE FROM chat_threads WHERE thread_id = %s::uuid AND user_id = %s",
            (thread_id, user_id),
        )

    # ── 自选股 ──────────────────────────────────────────────

    async def list_watchlist(self, user_id: int) -> list[dict[str, Any]]:
        """本人全部自选，按（分组, 加入时间）升序。分组视图由上层聚合，这里只给扁平事实。"""
        rows = await self._all(
            "SELECT symbol, group_name, added_at, added_price FROM watchlist "
            "WHERE user_id = %s ORDER BY group_name, added_at",
            (user_id,),
        )
        return [_watchlist_row(row) for row in rows]

    async def add_watchlist_item(
        self, user_id: int, symbol: str, group_name: str, added_price: float | None
    ) -> dict[str, Any]:
        """加自选；已在自选里抛 `SymbolTracked`（不返回 None，避免调用方漏判）。

        `added_price` 允许为 None —— 样例数据只覆盖少数标的，取不到价就如实留空。
        """
        try:
            row = await self._one(
                "INSERT INTO watchlist (user_id, symbol, group_name, added_price) "
                "VALUES (%s, %s, %s, %s) "
                "RETURNING symbol, group_name, added_at, added_price",
                (user_id, symbol, group_name, added_price),
            )
        except errors.UniqueViolation as exc:
            raise SymbolTracked(symbol) from exc
        assert row is not None  # INSERT ... RETURNING 必然有行
        return _watchlist_row(row)

    async def move_watchlist_item(self, user_id: int, symbol: str, group_name: str) -> bool:
        """改分组。返回是否命中（False 由调用方翻 404，不区分「不存在」与「不是本人」）。"""
        return (
            await self._one(
                "UPDATE watchlist SET group_name = %s "
                "WHERE user_id = %s AND symbol = %s RETURNING id",
                (group_name, user_id, symbol),
            )
            is not None
        )

    async def drop_watchlist_item(self, user_id: int, symbol: str) -> bool:
        return (
            await self._one(
                "DELETE FROM watchlist WHERE user_id = %s AND symbol = %s RETURNING id",
                (user_id, symbol),
            )
            is not None
        )

    async def rename_watchlist_group(self, user_id: int, old: str, new: str) -> bool:
        """重命名分组 = 一条 UPDATE。目标名已存在时两组自然合并（不设 409）。"""
        return await self._group_update(user_id, new, old)

    async def drop_watchlist_group(self, user_id: int, group: str) -> bool:
        """删组 = 组内标的回落 `DEFAULT_GROUP`，同样只是一条 UPDATE（无分组实体表）。"""
        return await self._group_update(user_id, DEFAULT_GROUP, group)

    async def _group_update(self, user_id: int, new: str, old: str) -> bool:
        return (
            await self._one(
                "UPDATE watchlist SET group_name = %s "
                "WHERE user_id = %s AND group_name = %s RETURNING id",
                (new, user_id, old),
            )
            is not None
        )

    # ── 用户策略（M4c）─────────────────────────────────────

    async def list_strategies(self, user_id: int) -> list[dict[str, Any]]:
        """本人全部策略的**摘要**（不带 code / params——列表不为每行拖一份源码），最近改的在前。

        摘要**不复用 `_strategy_row`**：那个函数吃的是整行（要 `params` 列），而这条 SELECT
        刻意没取它——复用会 KeyError（真库上踩过，离线替身自己拼 dict 反而看不出来）。
        """
        rows = await self._all(
            "SELECT id, name, created_at, updated_at FROM strategies "
            "WHERE user_id = %s ORDER BY updated_at DESC",
            (user_id,),
        )
        return [
            {
                "id": str(row["id"]),
                "name": row["name"],
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
        """建策略。撞名抛 `StrategyNameTaken`（不返回 None，避免调用方漏判）。"""
        try:
            row = await self._one(
                "INSERT INTO strategies (id, user_id, name, code, params) "
                "VALUES (%s::uuid, %s, %s, %s, %s) "
                "RETURNING id, name, code, params, created_at, updated_at",
                (strategy_id, user_id, name, code, Jsonb(params)),
            )
        except errors.UniqueViolation as exc:
            raise StrategyNameTaken(name) from exc
        assert row is not None  # INSERT ... RETURNING 必然有行
        return _strategy_row(row)

    async def get_strategy(self, user_id: int, strategy_id: str) -> dict[str, Any] | None:
        """单条（含 code）。带 `user_id` 过滤，越权与不存在同为 None（由调用方翻 404）。"""
        row = await self._one(
            "SELECT id, name, code, params, created_at, updated_at FROM strategies "
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
    ) -> dict[str, Any] | None:
        """改策略（部分更新：`None` = 不动该列）。

        一条 UPDATE + `COALESCE`，不做「先读再写」——那样两次请求之间会有竞态窗口。
        `params` 的 `Jsonb(None)` 是 SQL NULL，正好落进「不动该列」的语义。
        """
        try:
            row = await self._one(
                "UPDATE strategies SET name = COALESCE(%s, name), code = COALESCE(%s, code), "
                "params = COALESCE(%s, params), updated_at = now() "
                "WHERE id = %s::uuid AND user_id = %s "
                "RETURNING id, name, code, params, created_at, updated_at",
                (name, code, Jsonb(params) if params is not None else None, strategy_id, user_id),
            )
        except errors.UniqueViolation as exc:  # 改名撞上本人另一条策略
            raise StrategyNameTaken(name or "") from exc
        return _strategy_row(row) if row is not None else None

    async def drop_strategy(self, user_id: int, strategy_id: str) -> bool:
        """删策略。返回是否命中（回测记录**不级联删**：`strategy_id` 悬空即可）。"""
        return (
            await self._one(
                "DELETE FROM strategies WHERE id = %s::uuid AND user_id = %s RETURNING id",
                (strategy_id, user_id),
            )
            is not None
        )

    # ── 回测记录 ────────────────────────────────────────────

    async def save_backtest_run(
        self,
        run_id: str,
        user_id: int,
        request: dict[str, Any],
        report: dict[str, Any],
        *,
        strategy_id: str | None = None,
        code_sha256: str | None = None,
    ) -> None:
        """落一次回测。`request` 与 `report` 都是普通 dict，必须显式包 `Jsonb`——
        psycopg 不会把 dict 自动转 jsonb，而 `list` 会被适配成数组。

        `strategy_id` / `code_sha256` 只对用户策略有值：前者供「我的回测」认出策略，
        后者供前端比对「代码是否已改」（M4c 运行契约）。
        """
        await self._exec(
            "INSERT INTO backtest_runs (id, user_id, request, report, strategy_id, code_sha256) "
            "VALUES (%s::uuid, %s, %s, %s, %s::uuid, %s)",
            (run_id, user_id, Jsonb(request), Jsonb(report), strategy_id, code_sha256),
        )

    async def list_backtest_runs(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """摘要列表。**只抽 JSONB 子集**，不把整份报告拉回来。

        `metrics` 整块取（`->` 而非 `->>`）：逐键抽出来的是 text，数字会静默变成字符串，
        前端按比例格式化时不会报错、只会算错。

        `strategy_name` 走 `report->'meta'->>'strategy_name'`（M4c）：内置策略为 null，
        用户策略给名字——列表要能显示「用户策略 · 双均线」而不是一个 `user`。
        """
        rows = await self._all(
            "SELECT id, created_at, "
            "request->>'symbol' AS symbol, request->>'strategy' AS strategy, "
            "request->>'start' AS start, request->>'end' AS end, "
            "request->>'pit_mode' AS pit_mode, report->'metrics' AS metrics, "
            "report->'meta'->>'strategy_name' AS strategy_name "
            "FROM backtest_runs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )
        return [{**row, "id": str(row["id"])} for row in rows]

    async def get_backtest_run(self, user_id: int, run_id: str) -> dict[str, Any] | None:
        """按 id 取回完整报告。带 `user_id` 过滤，越权与不存在同为 None（由调用方翻 404）。

        `strategy_id` / `code_sha256` 一并带出（M4c）：前端拿它与策略**当前**的 hash 比对，
        得出「已非当次运行的代码」。服务端不做这个判断——事实给出去，结论由读取方下。
        """
        row = await self._one(
            "SELECT id, created_at, request, report, strategy_id, code_sha256 FROM backtest_runs "
            "WHERE id = %s::uuid AND user_id = %s",
            (run_id, user_id),
        )
        if row is None:
            return None
        return {
            **row,
            "id": str(row["id"]),
            "strategy_id": str(row["strategy_id"]) if row["strategy_id"] else None,
        }

    # ── 批量 / 网格汇总（M5b）────────────────────────────────

    async def save_optimization_run(
        self,
        run_id: str,
        user_id: int,
        request: dict[str, Any],
        summary: dict[str, Any],
    ) -> None:
        """落一次批处理汇总。与 `save_backtest_run` 同形：dict 必须显式包 `Jsonb`。

        与回测记录**分开存**是 SPEC §6 M5b 定的：网格落的是「每格一行摘要」，
        与「我的回测」混流会让列表里一半是没法打开的残缺报告。
        """
        await self._exec(
            "INSERT INTO optimization_runs (id, user_id, request, summary) "
            "VALUES (%s::uuid, %s, %s, %s)",
            (run_id, user_id, Jsonb(request), Jsonb(summary)),
        )

    async def list_optimization_runs(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        """摘要列表（最近在前）。**取 `summary - 'cells'`**：去掉最大的那一块（每格矩阵），
        其余整块原样带出。

        比逐键抽字段好在两点：① 不会踩「`->>` 把数字变成 text」那个坑（见
        `list_backtest_runs` 的注释）；② 新增汇总字段时列表页自动就有了，不必两处同步。
        """
        rows = await self._all(
            "SELECT id, created_at, request, summary - 'cells' AS summary "
            "FROM optimization_runs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        )
        return [{**row, "id": str(row["id"])} for row in rows]

    async def get_optimization_run(self, user_id: int, run_id: str) -> dict[str, Any] | None:
        """按 id 取回完整汇总（含每格矩阵）。越权与不存在同为 None（调用方翻 404）。"""
        row = await self._one(
            "SELECT id, created_at, request, summary FROM optimization_runs "
            "WHERE id = %s::uuid AND user_id = %s",
            (run_id, user_id),
        )
        return {**row, "id": str(row["id"])} if row is not None else None

    # ── 内部 ────────────────────────────────────────────────

    async def _one(self, sql: str, params: tuple) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(sql, params)
            return await cursor.fetchone()

    async def _all(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(sql, params)
            return list(await cursor.fetchall())

    async def _exec(self, sql: str, params: tuple) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(sql, params)


def _strategy_row(row: dict[str, Any]) -> dict[str, Any]:
    """`id` 是 UUID，`params` 是 Jsonb——在数据层就转成 JSON 契约里的形状。

    同 `_watchlist_row` 的理由：让 `UUID` / `Jsonb` 这类驱动类型止步于数据层，
    端点与离线替身拿到的都是普通 dict。
    """
    return {**row, "id": str(row["id"]), "params": dict(row["params"] or {})}


def _watchlist_row(row: dict[str, Any]) -> dict[str, Any]:
    """`added_price` 是 NUMERIC，psycopg 给的是 `Decimal`。

    在数据层就转成 float：JSON 契约里它是 number，而 `Decimal - float` 会直接 TypeError——
    让 Decimal 漂到端点里做算术是个只会炸在运行期的坑。
    """
    price = row["added_price"]
    return {**row, "added_price": float(price) if price is not None else None}
