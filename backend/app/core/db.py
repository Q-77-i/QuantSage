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
