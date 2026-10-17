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
import hmac
import logging
import math
import threading
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.agent.graph import build_agent
from app.agent.tools import (
    list_paper_accounts,
    load_xiaoshi_tools,
    propose_paper_order,
    query_market_bars,
    search_events,
)
from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.backtest import router as backtest_router
from app.api.capabilities import router as capabilities_router
from app.api.chat import router as chat_router
from app.api.data import router as data_router
from app.api.etl import router as etl_router
from app.api.events import router as events_router
from app.api.factor import router as factor_router
from app.api.idempotency import IdempotencyConflict
from app.api.limits import RateLimitedError
from app.api.market import router as market_router
from app.api.optimize import router as optimize_router
from app.api.paper import router as paper_router
from app.api.reports import router as reports_router
from app.api.research import router as research_router
from app.api.strategies import router as strategies_router
from app.api.tasks import router as tasks_router
from app.api.watchlist import router as watchlist_router
from app.backtest.batch import BatchRequestError
from app.backtest.types import BacktestError, NoDataError
from app.core import llm
from app.core.body_limit import BodyLimitMiddleware
from app.core.checkpoint import open_checkpointer
from app.core.config import get_settings
from app.core.db import (
    Database,
    EmailTaken,
    StrategyNameTaken,
    StrategyVersionConflict,
    SymbolTracked,
    init_schema,
    open_pool,
)
from app.core.langfuse import build_langfuse_handler
from app.core.logging import setup_logging
from app.core.metrics import TASK_STATUSES, TASKS
from app.core.observability import ObservabilityMiddleware
from app.core.quota import QuotaExceeded
from app.core.security import SecurityHeadersMiddleware
from app.core.session import SessionRenewalMiddleware
from app.data.duckdb_client import DataNotReady
from app.etl.scheduler import start_scheduler
from app.memory.decision_store import MemoryHolder
from app.memory.scheduler import start_settlement_scheduler
from app.paper import PaperConflict, PaperError
from app.strategy import SandboxError, StrategyCheckFailed, StrategyRejected
from app.tasks import worker as task_worker

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


@dataclass(slots=True)
class _Background:
    """本进程承担的后台任务。

    `owner`：`None` = 没尝试竞选（业务库不可用或 worker 启动异常），
    `False` = 锁被别的进程持有，`True` = 本进程持锁。
    """

    owner: bool | None = None
    worker: task_worker.WorkerHandle | None = None
    scheduler: Any | None = None
    settle_scheduler: Any | None = None


async def _start_background(app: FastAPI, db: Any) -> _Background:
    """启动后台任务；只有拿到后台主实例锁的进程才启动 worker 与两个调度器。

    业务库不可用或 worker 启动异常时 `owner=None`，不启动调度器，但保留手动 ETL
    的逃生口；锁被占用时 `owner=False`，本进程只服务 HTTP。
    """
    background = _Background()
    if db is None:
        log.warning("业务库未就绪：后台任务与调度器均不启动")
        return background

    try:
        handle = await task_worker.start(db)
    except Exception as exc:  # noqa: BLE001 —— worker 起不来不阻断服务
        log.warning("任务 worker 未启动（%s），提交的任务将排队等待", type(exc).__name__)
        return background

    if handle is None:
        background.owner = False
        log.info(
            "未取得后台主实例锁（被占用或连接失败）：本进程只服务 HTTP"
            "（不跑 worker/调度器/手动全局数据操作）"
        )
        return background

    background.owner = True
    background.worker = handle
    app.state.task_worker = handle
    log.info("本进程为后台主实例：task worker + ETL/结算调度器")

    try:
        background.scheduler = start_scheduler()
        app.state.scheduler = background.scheduler
    except Exception as exc:  # noqa: BLE001
        log.warning("ETL 调度器未启动（%s），/api/v1/etl/run 仍可手动触发", type(exc).__name__)

    try:
        background.settle_scheduler = start_settlement_scheduler(app)
        app.state.settle_scheduler = background.settle_scheduler
    except Exception as exc:  # noqa: BLE001
        log.warning("结算调度器未启动（%s），结算端点仍可手动触发", type(exc).__name__)

    return background


async def _stop_background(background: _Background | None) -> None:
    """停止本进程的后台任务。幂等：重复调用不会二次 shutdown。

    **不等正在跑的那一件**：取消不了线程/子进程，硬等只会拖长停机；它的行留在
    `running`，下次启动由持锁进程扫成 failed/interrupted。
    """
    if background is None:
        return
    if background.worker is not None:
        await task_worker.stop(background.worker)
        background.worker = None
    for name in ("scheduler", "settle_scheduler"):
        scheduler = getattr(background, name)
        if scheduler is not None:
            with suppress(Exception):
                scheduler.shutdown(wait=False)
            setattr(background, name, None)


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
    app.state.memory = None
    app.state.settle_scheduler = None
    app.state.task_worker = None
    app.state.background_owner = None

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

        tools = [query_market_bars, search_events, list_paper_accounts, propose_paper_order]
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
                llm.build_chat_model(), tools, checkpointer=app.state.checkpointer
            )
            app.state.langfuse_handler = build_langfuse_handler()
            log.info("Agent 就绪，共 %d 个工具", len(tools))
        except Exception as exc:  # noqa: BLE001 —— 缺 key 时不要让整个服务起不来
            log.warning("Agent 未就绪（%s），/api/v1/chat 将返回 503", type(exc).__name__)

        # 决策记忆（M7b）：LangGraph Store 首用——与 checkpointer 同库不同表。
        # **按需建**（`MemoryHolder`）：`AsyncBatchedBaseStore` 在构造时捕获事件循环，
        # 在 lifespan 的 loop 里建、在请求的 loop 上用会炸 `Future attached to a different loop`
        # （实测；uvicorn 单 loop 侥幸不炸，那是运气）。这里只放持有者，建库建表在首次使用时。
        app.state.memory = MemoryHolder(settings.postgres_dsn)

        # 异步任务 worker + ETL/结算调度器：队列实体就是 `tasks` 表，业务库不可用就不起 worker；
        # 生命周期收进局部句柄——退出时只停本进程启动的那些，嵌套 lifespan 不会互相 shutdown。
        background = await _start_background(app, app.state.db)
        app.state.background_owner = background.owner

        if settings.rag_warmup_on_start:
            # 后台线程预热 RAG 模型：把首次提问要付的 7–15s 载入挪到启动期。
            # 默认关——集成测试也跑 lifespan，默认开会白等十余秒并占 4GB。
            _start_rag_warmup()

        try:
            yield
        finally:
            # 用本地句柄停：不读 app.state，避免嵌套 lifespan 时把别人的调度器 shutdown 两次
            await _stop_background(background)
            app.state.background_owner = None
            app.state.task_worker = None
            app.state.scheduler = None
            app.state.settle_scheduler = None
            holder = app.state.memory
            if holder is not None and hasattr(holder, "aclose"):
                await holder.aclose()  # 按需建的 Store 连接在这里收


app = FastAPI(title="QuantSage API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    # PATCH 是自选股改分组用的：漏了它浏览器直接拦掉且报错难懂（T6b 漏 DELETE 同款）
    # 每加一个跨源方法都要同步这里：漏掉时浏览器直接拦掉、报错难懂，而后端用例全绿也发现不了
    # （T6b 漏过 DELETE，M4c 漏过 PUT——策略更新走 PUT）
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    # `Idempotency-Key` 是回测/网格提交必带的头（E4b）：漏了它，跨源预检直接 400
    # "Disallowed CORS headers"，浏览器不会发出真实请求——后端用例不跑预检，发现不了。
    allow_headers=["Content-Type", "Idempotency-Key"],
    # 会话 cookie 要跨源发送（前端 3001 ↔ API 8000，同 site 不同 origin）。
    # 凭据模式下 allow_origins 不能是 "*"，上面已是显式列表。
    allow_credentials=True,
    # 兜底通道靠响应头回传（SPEC §6 会话号 / M8 运行号）；**跨源下浏览器读不到未暴露的响应头**
    # ——这是同类坑的第三次（前两次：CORS 漏放行 PUT、缺 X-Thread-Id），新增一个要读的头就回这里加
    expose_headers=["X-Thread-Id", "X-Run-Id", "X-Request-Id"],
)

# Host 头白名单：默认 `["*"]`（本地/测试）；公网部署在 .env 填真实域名，
# 让 Host 注入（能力卡回显、绝对 URL 生成）在入口就失败。
if set(get_settings().trusted_hosts) != {"*"}:
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=get_settings().trusted_hosts
    )

# 请求体上限放在路由之前、安全头之内：413 也带安全响应头。它先读后回放（纯 ASGI），
# 不改变 SSE 的响应路径。
app.add_middleware(BodyLimitMiddleware, max_bytes=get_settings().max_request_bytes)

# E8a / E7e 的三个纯 ASGI 中间件（为什么不用 `BaseHTTPMiddleware` 见各自模块的 docstring）。
# `add_middleware` 的**后加者在更外层**：续期 → 安全头 → 可观测性。可观测性在最外层，
# 看到的是最终状态码与完整耗时（含 SSE 流）；安全头覆盖续期自己的响应；续期只管「成功响应
# 上换 cookie」，谁在外层都不改它的判据。
app.add_middleware(SessionRenewalMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(ObservabilityMiddleware)

for router in (
    admin_router,
    auth_router,
    chat_router,
    backtest_router,
    data_router,
    market_router,
    events_router,
    watchlist_router,
    strategies_router,
    optimize_router,
    factor_router,
    etl_router,
    paper_router,
    reports_router,
    research_router,
    tasks_router,
    capabilities_router,
):
    app.include_router(router)


def _finite_json(value: Any) -> Any:
    """把 NaN / Infinity 转成字符串，供 **422 错误体**序列化。

    `json.loads` 接受 `NaN`，Pydantic 会拒绝它；但 FastAPI 默认的错误体里带着
    `input: nan`，Starlette 的 JSONResponse 用 `allow_nan=False` 序列化 ⇒ 客户端拿到的
    不是 422，而是序列化失败的 500。这里只改错误体，不碰业务响应。
    """
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(item) for item in value]
    return value


@app.exception_handler(RequestValidationError)
async def _request_validation_error(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    """与 FastAPI 默认同形（`{"detail": [...]}`），只是把非有限浮点字符串化。"""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": _finite_json(jsonable_encoder(exc.errors()))},
    )


@app.exception_handler(PaperConflict)
async def _paper_conflict(_: Request, exc: PaperConflict) -> JSONResponse:
    """模拟盘的状态冲突（决策不是待审批 / 会话已到末端 / 并发推进）：**409**。

    与 422 分开：请求本身没写错，是「这一刻的会话状态不允许这个动作」——
    用户该做的是刷新看看现在是什么状态，而不是改请求。
    """
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


@app.exception_handler(PaperError)
async def _paper_error(_: Request, exc: PaperError) -> JSONResponse:
    """模拟盘的配置/数据前提不满足（区间内没数据、交易日不足、策略已删等）：**400**。"""
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


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


@app.exception_handler(StrategyVersionConflict)
async def _strategy_version_conflict(_: Request, exc: StrategyVersionConflict) -> JSONResponse:
    """5d：`expected_version` 与库里不一致（并发保存/回滚）→ 409，不覆盖别人的改动。"""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc.args[0]) if exc.args else "策略版本已变化，请重新打开"},
    )


@app.exception_handler(IdempotencyConflict)
async def _idempotency_conflict(_: Request, exc: IdempotencyConflict) -> JSONResponse:
    """同一个 `Idempotency-Key` 换了另一份请求体（E4b / L7）：**409**。

    与 422 分开：请求本身没写错，是「这个键已经代表过另一份请求」——改键或改回原请求。
    """
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})


@app.exception_handler(RateLimitedError)
async def _rate_limited(_: Request, exc: RateLimitedError) -> JSONResponse:
    """限额用尽（E4b / L0）：**429 + `Retry-After`**。

    装在依赖里而不是各个端点里，为的是「**在开流之前拒**」——chat / research 是 SSE，
    首字节发出就改不了状态码（与 `api/chat.py` 开头那条注释同源）。
    """
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": str(exc), "kind": exc.kind, "retry_after": exc.retry_after},
        headers={"Retry-After": str(exc.retry_after)},
    )


@app.exception_handler(QuotaExceeded)
async def _quota_exceeded(_: Request, exc: QuotaExceeded) -> JSONResponse:
    """用户配额用尽（E2）：**429**，与 L0 限流同码、靠 `kind` 区分（SPEC §3.3）。

    不给 `Retry-After` 头：日配额的等待可能是几小时，那个头是给「过几秒再来」用的；
    计数器把归零时刻放在体内（`reset_at`），gauge 干脆没有——它不由时钟释放。
    """
    content: dict[str, Any] = {
        "detail": str(exc),
        "kind": exc.kind,
        "metric": exc.metric,
        "limit": exc.limit,
        "used": exc.used,
    }
    if exc.reset_at:
        content["reset_at"] = exc.reset_at
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS, content=content
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


@app.exception_handler(BatchRequestError)
async def _batch_request_error(_: Request, exc: BatchRequestError) -> JSONResponse:
    """批处理的请求级错误（M5b）：**422**——是请求本身不成立，且必须在开流之前判死。

    典型是「网格里有一格参数非法」：SPEC §6 M5b 写的「任一格非法即整单 422」。
    流一旦开始，状态码就改不了了（与 chat 同一条约束），所以这类判断全在端点的
    `StreamingResponse` **之前**完成。
    """
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(exc)}
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


def _metrics_allowed(request: Request) -> bool:
    """配置了 `METRICS_TOKEN` 就验头；未配置只放行回环客户端（本地 scrape / 测试）。"""
    token = get_settings().metrics_token.get_secret_value()
    if token:
        supplied = request.headers.get("x-metrics-token", "")
        return hmac.compare_digest(supplied, token)
    host = request.client.host if request.client is not None else ""
    return host in {"127.0.0.1", "::1", "testclient"}


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Prometheus 文本端点（E7e）。任务队列是**scrape 时现查**，不靠进程内缓存猜。

    访问控制：`METRICS_TOKEN` 存在时要求 `X-Metrics-Token`；否则只允许回环地址。
    只暴露路由模板 / 状态 / 计数，不含用户数据。LLM 的 trace/token/成本仍在 Langfuse。
    """
    if not _metrics_allowed(request):
        raise HTTPException(status_code=403, detail="metrics 未授权")
    db = getattr(app.state, "db", None)
    if db is not None:
        try:
            counts = await db.task_status_counts()
        except Exception as exc:  # noqa: BLE001 —— 指标读不到不该让整个 scrape 失败
            log.warning("任务队列指标读取失败（%s），本次跳过", type(exc).__name__)
        else:
            for task_status in TASK_STATUSES:
                TASKS.labels(status=task_status).set(counts.get(task_status, 0))
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
