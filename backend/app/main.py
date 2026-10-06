"""FastAPI 入口。

健康探针 + 对话路由（T3 起）。业务路由按阶段挂载：T3 挂 chat，
backtest / market 端点随 T6 前端一起补。

lifespan 里按「能降级就降级」的姿态装配：checkpointer → 工具 → Agent，
任一环不可用都不阻断服务启动，由具体的 API 返回 503。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Response, status

from app.agent.graph import build_agent
from app.agent.tools import load_xiaoshi_tools, query_market_bars
from app.api.chat import router as chat_router
from app.core.checkpoint import open_checkpointer
from app.core.config import get_settings
from app.core.langfuse import build_langfuse_handler
from app.core.llm import build_chat_model
from app.core.logging import setup_logging

log = logging.getLogger(__name__)

# 小石 MCP 首次握手要拉起子进程（实测约 2.8s）；给足余量但不无限等
MCP_BOOT_TIMEOUT = 30.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()

    # 先立默认值：路由在任何阶段都能安全地 getattr，而不是撞 AttributeError
    app.state.checkpointer = None
    app.state.db_ready = False
    app.state.agent = None
    app.state.langfuse_handler = None

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
            log.warning(
                "Postgres 不可用（%s），降级启动；先跑 docker compose up -d --wait",
                type(exc).__name__,
            )

        tools = [query_market_bars]
        try:
            async with asyncio.timeout(MCP_BOOT_TIMEOUT):
                xiaoshi_tools, missing = await load_xiaoshi_tools()
            tools.extend(xiaoshi_tools)
            if missing:
                # 白名单是按 tools/list 实际结果校验的：缺项说明工具改名或下架，要显式暴露
                log.warning("小石工具白名单缺项：%s", sorted(missing))
            log.info("小石 MCP 工具已加载 %d 个（白名单外不暴露）", len(xiaoshi_tools))
        except Exception as exc:  # noqa: BLE001 —— MCP 不可用时只挂本地工具
            log.warning(
                "小石 MCP 不可用（%s），降级为仅本地行情工具", type(exc).__name__
            )

        try:
            # 图必须在事件循环内、checkpointer 就绪后构建：saver 在 compile 期固化
            app.state.agent = build_agent(
                build_chat_model(), tools, checkpointer=app.state.checkpointer
            )
            app.state.langfuse_handler = build_langfuse_handler()
            log.info("Agent 就绪，共 %d 个工具", len(tools))
        except Exception as exc:  # noqa: BLE001 —— 缺 key 时不要让整个服务起不来
            log.warning("Agent 未就绪（%s），/api/v1/chat 将返回 503", type(exc).__name__)

        yield


app = FastAPI(title="QuantSage API", version="0.1.0", lifespan=lifespan)
app.include_router(chat_router)


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
