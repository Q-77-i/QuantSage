"""FastAPI 入口。

T1 只有健康探针；业务路由（chat / backtest / market）从 T3、T4 开始挂。
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Response, status

from app.core.checkpoint import open_checkpointer
from app.core.config import get_settings
from app.core.logging import setup_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    async with AsyncExitStack() as stack:
        try:
            app.state.checkpointer = await stack.enter_async_context(
                open_checkpointer(settings.postgres_dsn)
            )
            app.state.db_ready = True
            log.info("checkpointer 就绪（%s）", settings.postgres_summary)
        except Exception as exc:  # noqa: BLE001 —— 连不上库时按配置决定是否降级
            if settings.startup_require_db:
                raise
            app.state.checkpointer = None
            app.state.db_ready = False
            log.warning(
                "Postgres 不可用（%s），降级启动；先跑 docker compose up -d --wait",
                type(exc).__name__,
            )
        yield


app = FastAPI(title="QuantSage API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    """存活探针：不碰任何外部依赖。"""
    settings = get_settings()
    return {"status": "ok", "service": settings.app_name, "version": app.version}


@app.get("/health/ready")
async def ready(response: Response) -> dict:
    """就绪探针：依赖（Postgres checkpointer）不可用时返回 503。"""
    db_ready = bool(getattr(app.state, "db_ready", False))
    if not db_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if db_ready else "degraded", "postgres": db_ready}
