"""RAG 推理服务（S3）：单进程、单 MPS 闸门，只绑 127.0.0.1。

**职责边界**：只做模型推理（embed / rerank），不碰 Qdrant、不做检索管线。检索仍在
API/MCP 进程里（`retrieve.search`），通过 `remote.RemoteEmbedder/RemoteReranker` 调过来。

排队模型（SPEC §4.3）：

* 精排 `max_inflight=1`（MPS 不许并发），由单 worker executor + 信号量保证；
* 按请求分级（interactive / research / batch）分别计**等待者**，到顶立即 503 `queue_full`；
* 等待超时 → 503 `timeout`；不下沉到模型调用，已开始的推理跑完即弃（无法中断 MPS）；
* 不做抢占——交互请求排在研究后面时同样按自己的超时降级。

鉴权（SPEC §4.7）：`RAG_SERVICE_TOKEN` 非空时 `/v1/*` 与 `/metrics` 要 Bearer；
为空时只允许回环地址（与 API `/metrics` 同姿态）。`/health` 只允许回环。

启动：`cd backend && .venv/bin/python -m app.rag.service`
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.rag import RagNotReady, RagOverloaded
from app.rag import metrics as rag_metrics
from app.rag.protocols import Embedder, EncodedDocs, Reranker

log = logging.getLogger(__name__)

CLASS_INTERACTIVE = "interactive"
CLASS_RESEARCH = "research"
CLASS_BATCH = "batch"

#: 端点接受的分级（rerank 不接受 batch：索引任务只做嵌入）
EmbedClass = Literal["interactive", "research", "batch"]
RerankClass = Literal["interactive", "research"]

_MAX_TEXTS = 512
_MAX_DOCUMENTS = 256


def _limits(kind: str) -> dict[str, tuple[int, float]]:
    """`class → (max_queue, wait_seconds)`。每次现读配置，改了无需重启。

    **嵌入自成一档**：查询嵌入是硬依赖（没有 RRF 可降级），不能套用精排的短超时；
    批量索引仍用长等待。
    """
    settings = get_settings()
    if kind == "embed":
        return {
            CLASS_INTERACTIVE: (
                settings.rag_interactive_max_queue,
                settings.rag_embed_wait_seconds,
            ),
            CLASS_RESEARCH: (
                settings.rag_research_max_queue,
                settings.rag_embed_wait_seconds,
            ),
            CLASS_BATCH: (settings.rag_batch_max_queue, settings.rag_batch_wait_seconds),
        }
    return {
        CLASS_INTERACTIVE: (
            settings.rag_interactive_max_queue,
            settings.rag_interactive_wait_seconds,
        ),
        CLASS_RESEARCH: (
            settings.rag_research_max_queue,
            settings.rag_research_wait_seconds,
        ),
        CLASS_BATCH: (settings.rag_batch_max_queue, settings.rag_batch_wait_seconds),
    }


class Runtime:
    """执行器、信号量、模型与队列计数。测试可注入假模型，不载真权重。"""

    def __init__(
        self, *, embedder: Embedder | None = None, reranker: Reranker | None = None
    ) -> None:
        self.embedder = embedder
        self.reranker = reranker
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._waiting: dict[str, dict[str, int]] = {
            "rerank": {CLASS_INTERACTIVE: 0, CLASS_RESEARCH: 0, CLASS_BATCH: 0},
            "embed": {CLASS_INTERACTIVE: 0, CLASS_RESEARCH: 0, CLASS_BATCH: 0},
        }
        self._inflight = {"rerank": 0, "embed": 0}
        # 计数用**线程锁**（不进事件循环等待）：取消路径里也要能同步清理
        self._state_lock = threading.Lock()
        self._load_lock = threading.Lock()

    # ── 生命周期 ────────────────────────────────────────────
    def start(self) -> None:
        settings = get_settings()
        self._executors["embed"] = ThreadPoolExecutor(
            max_workers=settings.rag_embed_inflight, thread_name_prefix="rag-embed"
        )
        self._executors["rerank"] = ThreadPoolExecutor(
            max_workers=settings.rag_rerank_inflight, thread_name_prefix="rag-rerank"
        )
        self._sems["embed"] = asyncio.Semaphore(settings.rag_embed_inflight)
        self._sems["rerank"] = asyncio.Semaphore(settings.rag_rerank_inflight)
        if settings.rag_preload:
            threading.Thread(target=self._preload, name="rag-preload", daemon=True).start()

    def stop(self) -> None:
        for executor in self._executors.values():
            executor.shutdown(wait=False)

    def _preload(self) -> None:
        try:
            self.model("embed").encode_query("预热")
            self.model("rerank").rerank("预热", ["预热"])
            log.info("RAG 服务预热完成")
        except Exception:  # noqa: BLE001 —— 预热失败只让首次请求慢，不该杀服务
            log.exception("RAG 服务预热失败（首次请求会现场载入）")

    # ── 模型 ────────────────────────────────────────────────
    def model(self, kind: str) -> Any:
        """取模型（首次在**工作线程**里载入，别在事件循环里阻塞）。"""
        with self._load_lock:
            if kind == "embed" and self.embedder is None:
                from app.rag.encoder import get_embedder

                self.embedder = get_embedder()
            if kind == "rerank" and self.reranker is None:
                from app.rag.encoder import get_reranker

                self.reranker = get_reranker()
        return self.embedder if kind == "embed" else self.reranker

    def loaded(self, kind: str) -> bool:
        model = self.embedder if kind == "embed" else self.reranker
        if model is None:
            return False
        return getattr(model, "_model", True) is not None

    def device(self, kind: str) -> str:
        model = self.embedder if kind == "embed" else self.reranker
        return str(getattr(model, "_device", "unknown"))

    # ── 排队 ────────────────────────────────────────────────
    def _refresh_metrics(self) -> None:
        rag_metrics.RERANK_QUEUE_DEPTH.set(sum(self._waiting["rerank"].values()))
        rag_metrics.RERANK_INFLIGHT.set(self._inflight["rerank"])

    async def admit(self, kind: str, klass: str) -> None:
        """等到推理槽位；队列满/等待超时抛 `RagOverloaded`。成功即占用一个 inflight。"""
        limits = _limits(kind)
        max_queue, wait = limits.get(klass, limits[CLASS_INTERACTIVE])
        with self._state_lock:
            if self._waiting[kind][klass] >= max_queue:
                if kind == "rerank":
                    rag_metrics.RERANK_DEGRADED.labels(
                        **{"class": klass, "reason": "queue_full"}
                    ).inc()
                raise RagOverloaded("queue_full")
            self._waiting[kind][klass] += 1
            if kind == "rerank":
                self._refresh_metrics()
        started = time.perf_counter()
        try:
            await asyncio.wait_for(self._sems[kind].acquire(), wait)
        except TimeoutError as exc:
            if kind == "rerank":
                rag_metrics.RERANK_DEGRADED.labels(
                    **{"class": klass, "reason": "timeout"}
                ).inc()
            raise RagOverloaded("timeout") from exc
        finally:
            with self._state_lock:
                self._waiting[kind][klass] -= 1
                if kind == "rerank":
                    self._refresh_metrics()
            if kind == "rerank":
                rag_metrics.RERANK_WAIT.labels(**{"class": klass}).observe(
                    time.perf_counter() - started
                )
        with self._state_lock:
            self._inflight[kind] += 1
            if kind == "rerank":
                self._refresh_metrics()

    def release(self, kind: str) -> None:
        self._sems[kind].release()
        with self._state_lock:
            self._inflight[kind] -= 1
            if kind == "rerank":
                self._refresh_metrics()

    def queue(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "waiting": {klass: sum(w.values()) for klass, w in self._waiting.items()},
                "inflight": dict(self._inflight),
                "limits": {
                    klass: {"max_queue": max_queue, "wait_seconds": wait}
                    for klass, (max_queue, wait) in _limits("rerank").items()
                },
            }

    async def run(self, kind: str, klass: str, call: Callable[[], Any]) -> Any:
        """排队 → 在工作线程执行 → 释放。`call` 里再取模型，保证载入不阻塞事件循环。

        **取消不释放槽位**：客户端断线时推理可能已经进了 MPS，必须等它跑完再 release；
        否则下一条请求会与它并发进 Metal（M3 的 abort 事故）。
        """
        await self.admit(kind, klass)
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executors[kind], call)
        try:
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await asyncio.shield(future)
                raise
        finally:
            self.release(kind)


# ── HTTP 契约 ──────────────────────────────────────────────


class EmbedRequest(BaseModel):
    texts: list[str] = Field(default_factory=list, max_length=_MAX_TEXTS)
    batch_size: int | None = Field(default=None, ge=1, le=256)
    request_class: EmbedClass = CLASS_INTERACTIVE


class EmbedResponse(BaseModel):
    dense: list[list[float]]
    sparse: list[dict[str, float]]
    dim: int


class RerankRequest(BaseModel):
    query: str
    documents: list[str] = Field(default_factory=list, max_length=_MAX_DOCUMENTS)
    request_class: RerankClass = CLASS_INTERACTIVE


class RerankResponse(BaseModel):
    scores: list[float]


def _loopback(request: Request) -> bool:
    host = request.client.host if request.client is not None else ""
    return host in {"127.0.0.1", "::1", "testclient"}


def require_auth(request: Request, authorization: str | None = Header(None)) -> None:
    """token 非空 → 强制 Bearer；为空 → 只允许回环（SPEC §4.7）。"""
    token = get_settings().rag_service_token.get_secret_value()
    if token:
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="RAG 服务鉴权失败")
        return
    if not _loopback(request):
        raise HTTPException(status_code=403, detail="未配置 RAG_SERVICE_TOKEN：只允许回环访问")


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def create_app(
    *, embedder: Embedder | None = None, reranker: Reranker | None = None
) -> FastAPI:
    """构造服务。测试注入假模型，**离线用例不载真权重**。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime.start()
        try:
            yield
        finally:
            app.state.runtime.stop()

    app = FastAPI(title="QuantSage RAG Service", lifespan=lifespan)
    app.state.runtime = Runtime(embedder=embedder, reranker=reranker)

    @app.exception_handler(RagOverloaded)
    async def _overloaded(_request: Request, exc: RagOverloaded) -> JSONResponse:
        return JSONResponse(status_code=503, content={"reason": exc.reason})

    @app.exception_handler(RagNotReady)
    async def _not_ready(_request: Request, exc: RagNotReady) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"reason": "unavailable", "message": str(exc)},
        )

    @app.post("/v1/embed", response_model=EmbedResponse, dependencies=[Depends(require_auth)])
    async def embed(body: EmbedRequest, runtime: Runtime = Depends(_runtime)) -> EmbedResponse:
        klass = body.request_class

        def call_embed() -> EncodedDocs:
            # 模型在**工作线程**里取：首次载入 ~10s，不能阻塞事件循环
            started = time.perf_counter()
            try:
                return runtime.model("embed").encode_documents(body.texts)
            finally:
                rag_metrics.EMBED_DURATION.labels(**{"class": klass}).observe(
                    time.perf_counter() - started
                )

        encoded = await runtime.run("embed", klass, call_embed)
        return EmbedResponse(
            dense=encoded.dense,
            sparse=[{str(key): value for key, value in item.items()} for item in encoded.sparse],
            dim=runtime.model("embed").dim,
        )

    @app.post("/v1/rerank", response_model=RerankResponse, dependencies=[Depends(require_auth)])
    async def rerank(body: RerankRequest, runtime: Runtime = Depends(_runtime)) -> RerankResponse:
        def call_rerank() -> list[float]:
            return runtime.model("rerank").rerank(
                body.query, body.documents, request_class=body.request_class
            )

        scores = await runtime.run("rerank", body.request_class, call_rerank)
        return RerankResponse(scores=[float(score) for score in scores])

    @app.get("/health")
    async def health(request: Request, runtime: Runtime = Depends(_runtime)) -> dict[str, Any]:
        if not _loopback(request):
            raise HTTPException(status_code=403, detail="只允许回环地址")
        return {
            "status": "ok",
            "models": {"embedder": runtime.loaded("embed"), "reranker": runtime.loaded("rerank")},
            "device": {"embed": runtime.device("embed"), "rerank": runtime.device("rerank")},
            "queue": runtime.queue(),
        }

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        require_auth(request)
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=settings.rag_service_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
