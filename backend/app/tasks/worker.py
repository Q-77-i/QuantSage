"""任务 worker（P3-E1a）：轮询任务表、按 `kind` 派发；P4-S7 起单进程内按 kind 多协程。

**队列实体就是 `tasks` 表**（SPEC §2.2）：不引 `asyncio.Queue`——它绑事件循环，而本仓
离线用例是模块级 `TestClient`（每请求一个 loop），跨 loop 的队列在那种用法下直接坏掉。
表即队列还顺带把「重启续跑」变成零代码：worker 起来接着取 `queued` 行即可。

S1–S6 是单 worker 串行（PRD 定）；E1b 迁入网格后「满格网格挡住单次回测」的代价在 S6
实测为 20 回测 queue p95 2.4s。P4-S7 在**同一个持锁进程**里按 kind 起多条 `serve()`，
不是多进程 worker 池；`TASK_WORKER_POOL_ENABLED=false` 一键回单循环。

测试形态：离线用例**不起常驻循环**，直接 `asyncio.run(run_once(db))` 驱动（见 §10 E1）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.backtest.batch import error_of
from app.core.config import get_settings
from app.core.db_tasks import RETRY_BACKOFF_SECONDS
from app.core.metrics import TASK_QUEUE_SECONDS
from app.tasks import bus
from app.tasks.executors import EXECUTORS, RunContext
from app.tasks.kinds import COOPERATIVE_KINDS

log = logging.getLogger(__name__)

#: 取件间隔（秒）。单次回测 0.3–2s，这点启动延迟可忽略，换来的是零跨 loop 状态。
POLL_SECONDS = 0.2

#: 本进程的 worker 标识（P4-S2）。只作诊断与取件归属；**执行期栅栏是每次取件的
#: `claim_token`**（P1），不是这个进程级常量——同一进程重取同一任务后 token 也会变。
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

#: 租约 / 心跳 / reaper（SPEC §3.3）。心跳必须显著小于租约：事件循环卡顿一轮不至于丢租约。
LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 10
REAPER_SECONDS = 30


def _lease_seconds() -> int:
    """租约秒数；0 = `TASK_LEASE_ENABLED=false`（回滚形态：只保留 `SKIP LOCKED`）。"""
    return LEASE_SECONDS if get_settings().task_lease_enabled else 0

#: 执行期查取消标志的间隔（收尾项 6）。取消是协作式的：这个间隔决定「多快停止派发新格」，
#: 不打断已经在跑的格。0.5s 对 100 格的任务是足够细的粒度。
CANCEL_POLL_SECONDS = 0.5

#: 后台主实例锁（Postgres advisory lock 的 key，取 ASCII 常量便于排查）。
#: 多进程/多副本部署时只有一个进程能拿到它：持锁者跑 task worker，并由
#: `main._start_background` 一并启动 ETL/结算调度器；非持锁进程只服务 HTTP。
#: **数值不可改**（滚动升级期间新旧进程必须互斥）；改名只是让语义与职责一致。
BACKGROUND_LOCK_KEY = int.from_bytes(b"QS-TASKS", "big")

#: S5：多 worker 时主实例被 kill，uvicorn 会补一个新进程；旧进程的 PG 后端可能还没
#: 完全释放 session lock，新进程一次性抢锁会输给这个几毫秒的窗口。初始 + 3 次重试
#: （每次 0.25s）覆盖它。
LOCK_RETRY_ATTEMPTS = 4
LOCK_RETRY_SECONDS = 0.25

#: 离线替身没有 `pool`：区分「没有跨进程概念」与「锁被别的进程持有」
_NO_LOCK = object()


@dataclass(slots=True)
class WorkerHandle:
    """常驻循环 + 可选的单实例锁连接。`lock_conn=None` 只在离线替身/无池场景出现。

    `db` 用于停机释放租约（S4）：`stop()` 不 await 在飞任务，但把它的行放回 `queued`；
    释放用的是 `_RUNNING` 里那一件的 **claim token**，不是进程级 `worker_id`。
    """

    task: asyncio.Task[None]
    lock_conn: Any = None
    db: Any = None
    worker_id: str = WORKER_ID


#: 正在跑的任务 → (中止事件, 本次取件的 claim token)。**本地加速口**：同进程的
#: `POST /cancel` 立即置位；跨进程的权威是 `tasks.cancel_requested_at`（收尾项 6），
#: 由执行期的轮询读取。claim token 供停机 release 使用——不能再用进程级 `worker_id`。
_RUNNING: dict[str, tuple[asyncio.Event, str]] = {}


def request_stop(task_id: str) -> bool:
    """请求中止一个正在跑的任务。返回是否真投递到了（同进程、且确实在跑）。

    `False` 不是错误：可能它刚跑完，也可能跑在别的进程里——后者由 DB 标志
    `cancel_requested_at` 兜底（收尾项 6），调用方不必据此判失败。
    """
    entry = _RUNNING.get(task_id)
    if entry is None:
        return False
    entry[0].set()
    return True


async def _cancel_requested(db: Any, task_id: str) -> bool:
    """查 DB 取消标志。替身没有这个方法时按未请求处理；查询失败不拖垮任务。"""
    checker = getattr(db, "task_cancel_requested", None)
    if checker is None:
        return False
    try:
        return bool(await checker(task_id))
    except Exception:  # noqa: BLE001 —— 一次抖动不该让任务失败
        log.warning("任务 %s 取消标志查询失败（继续执行）", task_id, exc_info=True)
        return False


def _retryable(exc: BaseException) -> bool:
    """基础设施瞬时错误才重排（SPEC §3.4）。**按异常类型判**，不按 `error.kind`——
    `internal` 是个大口袋，照 kind 重试等于把代码 bug 跑两遍。确定性错误（无数据、参数、
    沙箱拒绝/超限、策略运行期异常）都不在这里。"""
    import psycopg
    import psycopg_pool

    return isinstance(
        exc,
        (
            psycopg.OperationalError,
            psycopg.InterfaceError,
            psycopg_pool.PoolTimeout,
            ConnectionError,
            TimeoutError,
        ),
    )


async def _heartbeat(
    db: Any,
    task_id: str,
    claim_token: str,
    lease_seconds: int,
    stop: asyncio.Event,
) -> None:
    """每 10s 续租（SPEC §3.3）。异常只记日志——一次数据库抖动不该把执行拖垮；
    返回 `False`（行不再属于本次 claim）就停，并置 `stop` 让执行体在格边界收尾。
    **只停续租是不够的**：旧执行会继续跑完整段，迟到写入还可能命中。"""
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            alive = await db.heartbeat_task(
                task_id, claim_token=claim_token, lease_seconds=lease_seconds
            )
        except Exception:  # noqa: BLE001
            log.warning("任务 %s 心跳失败（继续执行）", task_id, exc_info=True)
            continue
        if not alive:
            log.warning("任务 %s 的租约已被回收（claim token 不匹配），停止执行", task_id)
            stop.set()
            return


async def _reaper_loop(db: Any) -> None:
    """周期 reaper：每 30s 回收过期租约（SPEC §3.3）。

    独立协程而不是塞在 `serve()` 里：`serve()` 在长任务执行期间不会回到循环顶部，
    塞进去等于「有任务在跑就不回收」。
    """
    while True:
        await asyncio.sleep(REAPER_SECONDS)
        try:
            counts = await db.reap_running_tasks(
                backoff_seconds=RETRY_BACKOFF_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("reaper 出错，跳过本轮")
            continue
        if counts:
            log.warning("reaper 回收 running 任务：%s", counts)


def _observe_queue_wait(task: dict[str, Any]) -> None:
    """S8：取件后记排队时长（`started_at - created_at`）。

    替身缺列、或 naive/aware 混用时只跳过——**指标不能拖垮任务执行**。
    """
    created, started = task.get("created_at"), task.get("started_at")
    if not isinstance(created, datetime) or not isinstance(started, datetime):
        return
    try:
        seconds = max(0.0, (started - created).total_seconds())
    except TypeError:  # naive vs aware：口径不成立，宁可不记
        return
    TASK_QUEUE_SECONDS.labels(kind=str(task.get("kind"))).observe(seconds)


async def run_once(
    db: Any,
    *,
    worker_id: str = WORKER_ID,
    kinds: tuple[str, ...] | None = None,
) -> bool:
    """取一件、跑一件；没有可取的返 `False`。

    取件是原子的（`FOR UPDATE SKIP LOCKED`）；执行期间心跳协程续租；可重排的瞬时错误
    按 `attempts < max_attempts` 回队列（SPEC §3.3/§3.4）。`kinds` 透传给
    `claim_next_task`（P4-S7 的按 kind 池化；`None` = 任何 kind）。
    """
    lease_seconds = _lease_seconds()
    task = await db.claim_next_task(
        worker_id, lease_seconds=lease_seconds, kinds=kinds
    )
    if task is None:
        return False

    task_id = str(task["id"])
    # 每次取件的 claim token；执行期所有写入都以它做栅栏（不是进程级 worker_id）。
    claim_token = str(task["claim_token"])
    _observe_queue_wait(task)
    executor = EXECUTORS.get(str(task["kind"]))
    if executor is None:  # pragma: no cover - 端点只建已注册的 kind
        message = f"未知任务类型 {task['kind']!r}"
        try:
            await db.fail_task(
                task_id, claim_token=claim_token, error={"kind": "internal", "message": message}
            )
            bus.publish(task_id, "error", {"code": "internal", "message": message})
        finally:
            # 这条分支在外层 try/finally 之前 return，必须自己释放；否则该 task_id 的
            # 缓冲会按任务数永久留存（在线订阅者已拿到帧，不依赖缓冲）
            bus.release(task_id)
        return True

    # 取件前就已经有取消标志（例如请求落在别的进程、标志先写库）⇒ 直接收尾，不执行
    if task.get("cancel_requested_at") is not None:
        try:
            log.info("任务 %s（%s）取件前已请求取消，直接收尾", task_id, task["kind"])
            await db.finish_task_cancelled(task_id, claim_token=claim_token)
            bus.publish(task_id, "cancelled", {"message": "任务已取消（中止的结果不落库）"})
        finally:
            bus.release(task_id)
        return True

    stop = asyncio.Event()
    _RUNNING[task_id] = (stop, claim_token)
    total = int((task.get("progress") or {}).get("total") or 1)
    heartbeat: asyncio.Task[None] | None = None
    if lease_seconds > 0:
        heartbeat = asyncio.create_task(
            _heartbeat(db, task_id, claim_token, lease_seconds, stop),
            name=f"heartbeat-{task_id}",
        )

    # 协作式任务在执行期轮询 DB 标志：请求可能来自别的进程，本地事件只是加速
    watcher: asyncio.Task[None] | None = None
    if str(task["kind"]) in COOPERATIVE_KINDS:

        async def watch_cancel() -> None:
            while not stop.is_set():
                await asyncio.sleep(CANCEL_POLL_SECONDS)
                if await _cancel_requested(db, task_id):
                    stop.set()
                    return

        watcher = asyncio.create_task(watch_cancel(), name=f"cancel-watch-{task_id}")

    async def progress(done: int) -> None:
        await db.update_task_progress(
            task_id, claim_token=claim_token, done=done, total=total
        )

    async def record_cell(done: int, cell: dict[str, Any]) -> None:
        # S5：逐格同时落 done/total 与 cells；任意 API worker 的 SSE 都能从库里读到。
        await db.append_task_cell(
            task_id, claim_token=claim_token, done=done, total=total, cell=cell
        )

    ctx = RunContext(
        task=task,
        stop=stop,
        publish=lambda event, payload: bus.publish(task_id, event, payload),
        progress=progress,
        record_cell=record_cell,
    )

    try:
        try:
            result = await executor(db, task, ctx)
        except Exception as exc:  # noqa: BLE001 —— 任务的失败要**落库可查**，不能只进日志
            error = error_of(exc)
            # 取消是权威：用户已请求中止时，异常不再翻成 failed（SPEC §3.4）
            if await _cancel_requested(db, task_id):
                log.info("任务 %s（%s）执行出错但已请求取消，收尾为 cancelled", task_id, task["kind"])
                await db.finish_task_cancelled(task_id, claim_token=claim_token)
                ctx.publish("cancelled", {"message": "任务已取消（中止的结果不落库）"})
                return True
            # 基础设施瞬时错误且还有 attempt：回队列，不写 error（还没终态）
            if _retryable(exc) and int(task["attempts"]) < int(task["max_attempts"]):
                requeued = await db.requeue_task(
                    task_id,
                    claim_token=claim_token,
                    backoff_seconds=RETRY_BACKOFF_SECONDS,
                )
                if requeued:
                    log.warning(
                        "任务 %s（%s）遇到瞬时错误，重排（attempt %s/%s）：%s",
                        task_id,
                        task["kind"],
                        task["attempts"],
                        task["max_attempts"],
                        error["message"],
                    )
                    return True
            log.warning("任务 %s（%s）失败：%s", task_id, task["kind"], error["message"])
            await db.fail_task(task_id, claim_token=claim_token, error=error)
            ctx.publish("error", {"code": error.get("kind", "internal"), "message": error["message"]})
            return True

        if result.get("cancelled"):
            # 协作式中止：执行体已按约定**不落库**，这里只把状态收干净
            log.info("任务 %s（%s）已中止", task_id, task["kind"])
            await db.finish_task_cancelled(task_id, claim_token=claim_token)
            ctx.publish("cancelled", {"message": "任务已取消（中止的结果不落库）"})
            return True

        # 成功 ⇒ 进度满格 + 终态一次写完（P4-S9；invariant：done 的必满格）
        await db.finish_task(
            task_id,
            claim_token=claim_token,
            result=result,
            progress_done=total,
            progress_total=total,
        )
        # **收尾帧必须广播**：在线订阅者靠它收流（没有它，页面会一直停在「运行中」——
        # 重连那条路是 `stream._replay` 从库里拼的，所以只有「在线跑完」才暴露这个缺口）
        ctx.publish("done", result)
        return True
    finally:
        if watcher is not None:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        _RUNNING.pop(task_id, None)
        bus.release(task_id)


async def serve(
    db: Any,
    *,
    interval: float = POLL_SECONDS,
    kinds: tuple[str, ...] | None = None,
) -> None:
    """常驻取件循环。取件出错**不退出**——一次数据库抖动不该让队列停摆到重启。"""
    log.info(
        "任务 worker 就绪（取件间隔 %.2fs，kinds=%s）",
        interval,
        "any" if kinds is None else ",".join(kinds),
    )
    while True:
        try:
            worked = await run_once(db, kinds=kinds)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("任务 worker 取件出错，跳过本轮")
            worked = False
        if not worked:
            await asyncio.sleep(interval)


def worker_slots() -> list[tuple[str | None, int]]:
    """S7 池化的道数：`(kind, 道数)`；`(None, 1)` = 单循环所有 kind。

    纯函数（只读 settings / EXECUTORS），便于离线用例直接断言，不用起常驻循环。
    未注册的 kind 不配；`EXECUTORS` 里新增但配置没写的 kind 默认 1 道；全 0 回退单循环。
    """
    settings = get_settings()
    if not settings.task_worker_pool_enabled:
        return [(None, 1)]
    counts: dict[str, int] = {
        "backtest": settings.task_workers_backtest,
        "grid": settings.task_workers_grid,
        "batch": settings.task_workers_batch,
    }
    known = set(EXECUTORS)
    for kind in sorted(known - set(counts)):
        counts[kind] = 1
        log.warning("任务 kind %r 未配置道数，默认 1 道", kind)
    for kind in list(counts):
        if kind not in known:
            log.warning("配置了未注册的任务 kind %r，忽略", kind)
            counts.pop(kind)
    slots = [(kind, count) for kind, count in counts.items() if count > 0]
    if not slots:
        log.warning("TASK_WORKERS_* 全为 0，回退单循环（所有 kind）")
        return [(None, 1)]
    return slots


def _serve_coros(db: Any) -> list[Any]:
    slots = worker_slots()
    log.info(
        "任务 worker 道数：%s（单进程内多协程）",
        "、".join(f"{kind or 'all'}×{count}" for kind, count in slots),
    )
    return [
        serve(db, kinds=None if kind is None else (kind,))
        for kind, count in slots
        for _ in range(count)
    ]


async def _acquire_instance_lock(db: Any) -> Any:
    """拿后台主实例锁；拿不到返回 None。没有连接池（离线替身）时按不锁处理。

    **单独成函数**是为了让「锁被别的进程持有」这条分支可以离线注入；真库路径用一条
    **独立连接**持有 session 级 advisory lock——不能用池里的连接，那会白占一个槽位直到进程退出。

    S5：`API_WORKERS>1` 时重试 3×0.25s。主实例被 kill 后，uvicorn 补的新进程启动时
    去抢锁，旧连接的释放可能晚几毫秒；一次性抢锁会让新进程永远不跑后台。
    """
    pool = getattr(db, "pool", None)
    if pool is None:
        return _NO_LOCK  # 替身：没有跨进程概念，按已持有处理
    import psycopg

    attempts = LOCK_RETRY_ATTEMPTS if get_settings().api_workers > 1 else 1
    for attempt in range(attempts):
        try:
            conn = await psycopg.AsyncConnection.connect(pool.conninfo, autocommit=True)
        except Exception:  # noqa: BLE001 —— 连不上库时降级启动，worker 不阻塞服务
            log.exception("任务 worker 锁连接失败，本次不启动 worker")
            return None
        try:
            cursor = await conn.execute("SELECT pg_try_advisory_lock(%s)", (BACKGROUND_LOCK_KEY,))
            row = await cursor.fetchone()
        except Exception:
            await conn.close()
            raise
        if row and row[0]:
            return conn
        await conn.close()
        if attempt + 1 < attempts:
            log.debug(
                "后台主实例锁被占用，%.2fs 后重试（%d/%d）",
                LOCK_RETRY_SECONDS,
                attempt + 1,
                attempts,
            )
            await asyncio.sleep(LOCK_RETRY_SECONDS)
    return None


async def start(db: Any) -> WorkerHandle | None:
    """启动：先抢单实例锁，再扫尾上一条进程留下的 `running`，最后起常驻循环。

    **扫尾语义（P4-S2）**：租约启用时，持锁者扫**全部** `running`——拿到 advisory lock
    即证明没有第二个 worker，这些行都是上一条进程的孤儿，有限次重排（`attempts < max_attempts`
    回 `queued`，否则 `failed/lease_expired`）。这是 `kill -9` 后秒级恢复的来源。
    `TASK_LEASE_ENABLED=false` 时退回旧语义：全部标 `failed/interrupted`，不重排。

    **多进程下的关键**：扫尾只能在持锁者里做。拿不到锁的进程直接返回 None（不扫、不取件），
    否则会把别的进程正在跑的行标掉。

    扫尾失败**不拖住 worker**：它只是一条 UPDATE，失败最多留下几条陈旧的 `running` 行；
    为此把整个队列停摆（提交照收、永远没人跑）才是更糟的降级。
    """
    lock_conn = await _acquire_instance_lock(db)
    if lock_conn is None:
        # 由 `main._start_background` 统一打“本进程只服务 HTTP”的日志，避免两处重复
        log.debug("未取得后台主实例锁，本进程不启动 worker")
        return None
    lease_seconds = _lease_seconds()
    counts: dict[str, int] = {}
    interrupted = 0
    try:
        if lease_seconds > 0:
            counts = await db.reap_running_tasks(
                backoff_seconds=RETRY_BACKOFF_SECONDS, expired_only=False
            )
        else:
            interrupted = await db.sweep_interrupted_tasks()
    except Exception:  # noqa: BLE001
        log.exception("任务扫尾失败（worker 照常启动）")
    else:
        if lease_seconds > 0:
            if counts:
                log.warning("任务启动扫尾：%s", counts)
        elif interrupted:
            log.warning("任务扫尾：%d 条因服务重启被标为 failed/interrupted", interrupted)

    async def run_worker() -> None:
        coros: list[Any] = _serve_coros(db)
        if lease_seconds > 0:
            coros.append(_reaper_loop(db))
        await asyncio.gather(*coros)

    return WorkerHandle(
        task=asyncio.create_task(run_worker(), name="task-worker"),
        lock_conn=lock_conn,
        db=db,
    )


async def stop(handle: WorkerHandle | asyncio.Task[None] | None) -> None:
    """收工：停止取件、释放在飞任务的租约、释放单实例锁。

    **不 await 正在跑的那一件**（与 `api/optimize.py` 的断线姿态同源：取消不了线程/
    子进程，硬等只会把停机拖长）；它随进程一起消失，但退出前对快照里的任务调用
    `release_task`：未取消的回到 `queued`（attempts−1，不算一次失败），已取消的收成
    `cancelled`。这样不重启也能让下一次启动/另一个持锁进程立刻捡起来；kill -9 没有
    这段收尾，仍由 `start()` 的全量扫尾重排（S2）。
    """
    if handle is None:
        return
    task = handle.task if isinstance(handle, WorkerHandle) else handle
    # 快照必须在 cancel 之前：run_once 的 finally 会把 _RUNNING 清空
    pending = list(_RUNNING.items())
    db = getattr(handle, "db", None) if isinstance(handle, WorkerHandle) else None
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    release = getattr(db, "release_task", None)
    if pending and release is not None:
        for task_id, (_event, claim_token) in pending:
            try:
                status = await release(task_id, claim_token=claim_token)
            except Exception:  # noqa: BLE001 —— 收尾失败不能拖住停机；启动扫尾兜底
                log.warning("停机释放任务 %s 租约失败（启动扫尾兜底）", task_id, exc_info=True)
                continue
            if status:
                log.info("停机释放任务 %s：%s", task_id, status)

    if (
        isinstance(handle, WorkerHandle)
        and handle.lock_conn is not None
        and handle.lock_conn is not _NO_LOCK
    ):
        with suppress(Exception):
            await handle.lock_conn.execute("SELECT pg_advisory_unlock(%s)", (BACKGROUND_LOCK_KEY,))
            await handle.lock_conn.close()


__all__ = [
    "POLL_SECONDS",
    "BACKGROUND_LOCK_KEY",
    "HEARTBEAT_SECONDS",
    "LEASE_SECONDS",
    "REAPER_SECONDS",
    "WORKER_ID",
    "WorkerHandle",
    "request_stop",
    "run_once",
    "serve",
    "start",
    "stop",
]
