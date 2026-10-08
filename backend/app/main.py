"""FastAPI 入口。

健康探针 + 七组业务路由：auth（M1）、watchlist（M1c）、chat / threads（T3）、
backtest / market / events（T6）、etl（M2b）。

lifespan 里按「能降级就降级」的姿态装配：checkpointer → 业务库 → 工具 → Agent，
任一环不可用都不阻断服务启动，由具体的 API 返回 503。

业务异常到 HTTP 状态的映射统一在这里注册（见文件末），端点里不写 try/except：
数据类异常按「依赖未就绪 / 没数据 / 配置不对」三分，分别给 503 / 404 / 400。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agent.graph import build_agent
from app.agent.tools import load_xiaoshi_tools, query_market_bars, search_events
from app.api.auth import router as auth_router
from app.api.backtest import router as backtest_router
from app.api.chat import router as chat_router
from app.api.etl import router as etl_router
from app.api.events import router as events_router
from app.api.market import router as market_router
from app.api.strategies import router as strategies_router
from app.api.watchlist import router as watchlist_router
from app.backtest.types import BacktestError, NoDataError
from app.core.checkpoint import open_checkpointer
from app.core.config import get_settings
from app.core.db import (
    Database,
    EmailTaken,
    StrategyNameTaken,
    SymbolTracked,
    init_schema,
    open_pool,
)
from app.strategy import SandboxError, StrategyCheckFailed, StrategyRejected
from app.core.langfuse import build_langfuse_handler
from app.core.llm import build_chat_model
from app.core.logging import setup_logging
from app.etl.scheduler import start_scheduler
from app.data.duckdb_client import DataNotReady

log = logging.getLogger(__name__)

# 小石 MCP 首次握手要拉起子进程（实测约 2.8s）；给足余量但不无限等
MCP_BOOT_TIMEOUT = 30.0


def _start_rag_warmup() -> None:
    """后台线程预热 RAG 模型。**不阻塞启动、失败只记日志**。"""

    def run() -> None:
        from app.rag.encoder import configure_hf_env, warmup

        configure_hf_env()
        ok, detail = warmup()
        if ok:
            log.info("RAG 模型预热完成：%s", detail)
        else:
            log.warning("RAG 模型预热失败（%s），首次检索会现场载入", detail)

    threading.Thread(target=run, name="rag-warmup", daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()

    # 先立默认值：路由在任何阶段都能安全地 getattr，而不是撞 AttributeError
    app.state.checkpointer = None
    app.state.db = None
    app.state.db_ready = False
    app.state.agent = None
    app.state.langfuse_handler = None
    app.state.scheduler = None

    if not settings.jwt_secret.get_secret_value():
        # 不静默降级成无鉴权：/api/v1/auth/* 会返回 503，缺的是配置不是代码
        log.warning("JWT_SECRET 未配置，鉴权相关端点将返回 503（见 .env.example）")

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

        # 业务库（自建表）与 checkpointer 用两个独立的池：各管各的表，互不牵扯
        try:
            pool = await stack.enter_async_context(open_pool(settings.postgres_dsn))
            await init_schema(pool)
            app.state.db = Database(pool)
            log.info("业务库就绪（%s）", settings.postgres_summary)
        except Exception as exc:  # noqa: BLE001
            if settings.startup_require_db:
                raise
            log.warning(
                "业务库不可用（%s），登录鉴权与会话归属将返回 503", type(exc).__name__
            )

        tools = [query_market_bars, search_events]
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

        # 事件语料定时（M2b）：默认不启用；启用了才起，起不来也不阻断服务
        try:
            app.state.scheduler = start_scheduler()
        except Exception as exc:  # noqa: BLE001
            log.warning("ETL 调度器未启动（%s），/api/v1/etl/run 仍可手动触发", type(exc).__name__)

        if settings.rag_warmup_on_start:
            # 后台线程预热 RAG 模型：把首次提问要付的 7–15s 载入挪到启动期。
            # 默认关——集成测试也跑 lifespan，默认开会白等十余秒并占 4GB。
            _start_rag_warmup()

        try:
            yield
        finally:
            if app.state.scheduler is not None:
                app.state.scheduler.shutdown(wait=False)


app = FastAPI(title="QuantSage API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    # PATCH 是自选股改分组用的：漏了它浏览器直接拦掉且报错难懂（T6b 漏 DELETE 同款）
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
    # 会话 cookie 要跨源发送（前端 3001 ↔ API 8000，同 site 不同 origin）。
    # 凭据模式下 allow_origins 不能是 "*"，上面已是显式列表。
    allow_credentials=True,
    # 会话号在断连时靠响应头兜底回传（SPEC §6）；跨源下浏览器读不到未暴露的响应头
    expose_headers=["X-Thread-Id"],
)

for router in (
    auth_router,
    chat_router,
    backtest_router,
    market_router,
    events_router,
    watchlist_router,
    strategies_router,
    etl_router,
):
    app.include_router(router)


@app.exception_handler(EmailTaken)
async def _email_taken(_: Request, exc: EmailTaken) -> JSONResponse:
    """邮箱唯一约束冲突：请求本身没写错，是「这个邮箱已被占用」，与 422 区分开。"""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT, content={"detail": "该邮箱已被注册"}
    )


@app.exception_handler(SymbolTracked)
async def _symbol_tracked(_: Request, exc: SymbolTracked) -> JSONResponse:
    """自选股唯一约束冲突：与邮箱冲突同姿态，是「已经在里面了」而不是请求写错了。"""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT, content={"detail": f"{exc.args[0]} 已在自选股中"}
    )


@app.exception_handler(StrategyNameTaken)
async def _strategy_name_taken(_: Request, exc: StrategyNameTaken) -> JSONResponse:
    """策略重名：是「这个名字已在你名下」，与请求写错（422）区分开，同邮箱 / 自选股姿态。"""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": f"已有一条名为「{exc.args[0]}」的策略，换个名字"},
    )


@app.exception_handler(StrategyCheckFailed)
async def _strategy_check_failed(_: Request, exc: StrategyCheckFailed) -> JSONResponse:
    """运行前的静态检查闸门：422 + findings（编辑器标注与检查面板直接吃这份）。"""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": str(exc), "findings": [item.to_dict() for item in exc.findings]},
    )


@app.exception_handler(StrategyRejected)
async def _strategy_rejected(_: Request, exc: StrategyRejected) -> JSONResponse:
    """用户要改的问题（参数越界 / 运行期异常 / 返回值形态）：422，带行号供编辑器定位。"""
    content: dict[str, Any] = {"detail": str(exc)}
    if exc.line:
        content["line"] = exc.line
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content=content)


@app.exception_handler(SandboxError)
async def _sandbox_error(_: Request, exc: SandboxError) -> JSONResponse:
    """沙箱终止（cpu / wall / memory / output / crash）：**400**——请求本身合法，是这次运行
    没能完成（与既有 `BacktestError → 400` 同档）。`kind` 交出去供 UI 分档显示。"""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc), "kind": exc.kind},
    )


@app.exception_handler(DataNotReady)
async def _data_not_ready(_: Request, exc: DataNotReady) -> JSONResponse:
    """样例数据未落盘：是依赖没就绪，不是请求写错了。"""
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": str(exc)})


@app.exception_handler(NoDataError)
async def _no_data(_: Request, exc: NoDataError) -> JSONResponse:
    """标的不存在或区间内无 bar。"""
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})


@app.exception_handler(BacktestError)
async def _backtest_error(_: Request, exc: BacktestError) -> JSONResponse:
    """其余回测配置类错误。"""
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


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
