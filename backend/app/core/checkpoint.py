"""LangGraph Postgres checkpointer：连接池 + 建表。

三个硬要求（缺一不可）：
  1. `autocommit=True` —— `.setup()` 用 CREATE INDEX CONCURRENTLY，事务块里跑不了
  2. `row_factory=dict_row` —— checkpointer 内部按列名取数，默认 tuple 会 TypeError
  3. 必须在**运行中的事件循环**里构造（`AsyncPostgresSaver.__init__` 会取 running loop），
     所以不要在模块顶层建实例，放进 FastAPI lifespan 或 `asyncio.run()` 的脚本里
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


@asynccontextmanager
async def open_checkpointer(
    conn_string: str,
    *,
    min_size: int = 1,
    max_size: int = 8,
    statement_timeout_ms: int | None = None,
) -> AsyncIterator[AsyncPostgresSaver]:
    """checkpointer 池。建表脚本（`setup_checkpoint_tables`）不传超时——`.setup()` 里
    可能有 `CREATE INDEX CONCURRENTLY`；只有应用运行期池加 30s（S4）。"""
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
        kwargs=kwargs,
    )
    await pool.open(wait=True, timeout=10)
    try:
        yield AsyncPostgresSaver(pool)
    finally:
        await pool.close()


async def setup_checkpoint_tables(conn_string: str) -> None:
    """建表（幂等）：checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations。"""
    async with open_checkpointer(conn_string) as saver:
        await saver.setup()
