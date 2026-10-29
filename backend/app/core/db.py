"""业务库：连接池、幂等建表与数据访问门面。

与 checkpointer 的池**分开**：`checkpoints` 系列表由 `langgraph-checkpoint-postgres`
自己的迁移管理（`saver.setup()`），本模块只管我们自己的表，两边互不牵扯。

三条约定：
  * 建表走幂等 DDL（`CREATE TABLE IF NOT EXISTS`），项目不引 Alembic——表结构变更一律写
    `app/core/schema.py`，启动时执行；改列要另起迁移语句，不能只改这里的 DDL 就当生效；
  * 连接 `autocommit=True` + `dict_row`（与 checkpointer 池同口径），单语句操作不需要显式事务；
  * 所有读写都带 `user_id`（见 M1 归属校验），命中 0 行由调用方翻成 404。

数据访问按业务域拆成 mixin（`db_*.py`），`Database` 仍是对外唯一门面：端点拿到的
就是这些语义化调用，不在端点里拼 SQL（离线测试注入同形替身即可跑归属矩阵）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.db_backtest import BacktestMixin
from app.core.db_base import BaseRepo
from app.core.db_chat import ChatMixin
from app.core.db_errors import (
    EmailTaken,
    StrategyNameTaken,
    StrategyVersionConflict,
    SymbolTracked,
)
from app.core.db_paper import PaperMixin
from app.core.db_research import ResearchMixin
from app.core.db_strategies import StrategiesMixin
from app.core.db_tasks import TasksMixin
from app.core.db_users import UsersMixin
from app.core.db_watchlist import WatchlistMixin
from app.core.schema import DEFAULT_GROUP, SCHEMA

__all__ = [
    "DEFAULT_GROUP",
    "Database",
    "EmailTaken",
    "StrategyNameTaken",
    "StrategyVersionConflict",
    "SymbolTracked",
    "init_schema",
    "open_pool",
]


@asynccontextmanager
async def open_pool(
    conn_string: str,
    *,
    min_size: int = 1,
    max_size: int = 8,
    statement_timeout_ms: int | None = None,
) -> AsyncIterator[AsyncConnectionPool]:
    """业务库连接池。`statement_timeout_ms` 只作用于池里的连接；独立 advisory 连接
    （`BaseRepo.advisory_lock` 用 `pool.conninfo`）不受影响——`conninfo` 不含这里
    的 kwargs，这是有意保留的逃生口（Chat 等锁可能超过 5s）。"""
    kwargs: dict[str, Any] = {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
    }
    if statement_timeout_ms and statement_timeout_ms > 0:
        kwargs["options"] = f"-c statement_timeout={int(statement_timeout_ms)}"
    pool = AsyncConnectionPool(
        conn_string,
        min_size=min_size,
        max_size=max_size,
        open=False,
        # S6：PG 重启后池里的空闲连接已被掐断；取件时校验并重连，避免第一个请求 500。
        check=AsyncConnectionPool.check_connection,
        kwargs=kwargs,
    )
    await pool.open(wait=True, timeout=10)
    try:
        yield pool
    finally:
        await pool.close()


#: 建表串行锁（S5）。与 `tasks.worker.BACKGROUND_LOCK_KEY` 不同 key：前者只管 DDL，
#: 后者管后台主实例，互不阻塞。取 **8 字节** ASCII 常量（int64 内，psycopg 才会推断 bigint；
#: 9 字节会变成 numeric，`pg_advisory_xact_lock(numeric)` 不存在）。
SCHEMA_LOCK_KEY = int.from_bytes(b"QS-SCHMA", "big")


async def init_schema(pool: AsyncConnectionPool) -> None:
    """建表（幂等）。重复执行安全，表已存在时不动。

    S5：多个 API worker 会同时启动；同一批 DDL 放进一个事务并用 advisory xact lock
    串行，避免并发 `CREATE TABLE/INDEX IF NOT EXISTS` 在系统表上撞车。
    """
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)", (SCHEMA_LOCK_KEY,)
        )
        for statement in SCHEMA:
            await conn.execute(statement)


class Database(
    UsersMixin,
    ChatMixin,
    WatchlistMixin,
    StrategiesMixin,
    BacktestMixin,
    PaperMixin,
    ResearchMixin,
    TasksMixin,
    BaseRepo,
):
    """业务表的全部读写出口；方法按域拆在 `db_*.py`，公开面仍是这一个类。"""
