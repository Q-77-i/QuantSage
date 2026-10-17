"""任务 worker（P3-E1a）：单协程、轮询任务表、按 `kind` 派发。

**队列实体就是 `tasks` 表**（SPEC §2.2）：不引 `asyncio.Queue`——它绑事件循环，而本仓
离线用例是模块级 `TestClient`（每请求一个 loop），跨 loop 的队列在那种用法下直接坏掉。
表即队列还顺带把「重启续跑」变成零代码：worker 起来接着取 `queued` 行即可。

单 worker 串行（PRD 定）。E1a 只有亚秒级的单次回测，排队延迟可忽略；E1b 迁入网格后
才有「满格网格挡住单次回测」的代价，届时再评估 worker 数可配。

测试形态：离线用例**不起常驻循环**，直接 `asyncio.run(run_once(db))` 驱动（见 §10 E1）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from app.backtest.batch import error_of
from app.tasks import bus
from app.tasks.executors import EXECUTORS, RunContext

log = logging.getLogger(__name__)

#: 取件间隔（秒）。单次回测 0.3–2s，这点启动延迟可忽略，换来的是零跨 loop 状态。
POLL_SECONDS = 0.2

#: 进程级 worker 单实例锁（Postgres advisory lock 的 key，取 ASCII 常量便于排查）。
#: 多进程/多副本部署时只有一个进程能拿到它；只有持锁者扫尾与取件，避免 B 启动时把
#: A 正在跑的 `running` 标成 failed（旧行为会静默丢结果）。
WORKER_LOCK_KEY = int.from_bytes(b"QS-TASKS", "big")

#: 离线替身没有 `pool`：区分「没有跨进程概念」与「锁被别的进程持有」
_NO_LOCK = object()


@dataclass(slots=True)
class WorkerHandle:
    """常驻循环 + 可选的单实例锁连接。`lock_conn=None` 只在离线替身/无池场景出现。"""

    task: asyncio.Task[None]
    lock_conn: Any = None


#: 正在跑的任务 → 中止信号。**协作式中止的投递口**：`POST /tasks/{id}/cancel` 在这里置位，
#: 执行体（`run_cells`）看到就停止派发新格。进程内——单实例假设同 E1a（见 SPEC §2.4）。
_RUNNING: dict[str, asyncio.Event] = {}


def request_stop(task_id: str) -> bool:
    """请求中止一个正在跑的任务。返回是否真投递到了（同进程、且确实在跑）。

    `False` 不是错误：可能它刚跑完，也可能跑在别的进程里（本项目单实例，如实返回即可）。
    """
    event = _RUNNING.get(task_id)
    if event is None:
        return False
    event.set()
    return True


async def run_once(db: Any) -> bool:
    """取一件、跑一件；没有可取的返 `False`。取件是原子的（`claim_next_task` 一条语句）。"""
    task = await db.claim_next_task()
    if task is None:
        return False

    task_id = str(task["id"])
    executor = EXECUTORS.get(str(task["kind"]))
    if executor is None:  # pragma: no cover - 端点只建已注册的 kind
        message = f"未知任务类型 {task['kind']!r}"
        await db.fail_task(task_id, error={"kind": "internal", "message": message})
        bus.publish(task_id, "error", {"code": "internal", "message": message})
        return True

    stop = asyncio.Event()
    _RUNNING[task_id] = stop
    total = int((task.get("progress") or {}).get("total") or 1)

    async def progress(done: int) -> None:
        await db.update_task_progress(task_id, done=done, total=total)

    ctx = RunContext(
        task=task,
        stop=stop,
        publish=lambda event, payload: bus.publish(task_id, event, payload),
        progress=progress,
    )

    try:
        try:
            result = await executor(db, task, ctx)
        except Exception as exc:  # noqa: BLE001 —— 任务的失败要**落库可查**，不能只进日志
            error = error_of(exc)
            log.warning("任务 %s（%s）失败：%s", task_id, task["kind"], error["message"])
            await db.fail_task(task_id, error=error)
            ctx.publish("error", {"code": error.get("kind", "internal"), "message": error["message"]})
            return True

        if result.get("cancelled"):
            # 协作式中止：执行体已按约定**不落库**，这里只把状态收干净
            log.info("任务 %s（%s）已中止", task_id, task["kind"])
            await db.finish_task_cancelled(task_id)
            ctx.publish("cancelled", {"message": "任务已取消（中止的结果不落库）"})
            return True

        # 成功 ⇒ 进度满格。网格逐格推进，这里是收尾那一下（invariant：done 的必满格）
        await progress(total)
        await db.finish_task(task_id, result=result)
        # **收尾帧必须广播**：在线订阅者靠它收流（没有它，页面会一直停在「运行中」——
        # 重连那条路是 `stream._replay` 从库里拼的，所以只有「在线跑完」才暴露这个缺口）
        ctx.publish("done", result)
        return True
    finally:
        _RUNNING.pop(task_id, None)
        bus.release(task_id)


async def serve(db: Any, *, interval: float = POLL_SECONDS) -> None:
    """常驻取件循环。取件出错**不退出**——一次数据库抖动不该让队列停摆到重启。"""
    log.info("任务 worker 就绪（取件间隔 %.2fs）", interval)
    while True:
        try:
            worked = await run_once(db)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("任务 worker 取件出错，跳过本轮")
            worked = False
        if not worked:
            await asyncio.sleep(interval)


async def _acquire_instance_lock(db: Any) -> Any:
    """拿进程级 worker 锁；拿不到返回 None。没有连接池（离线替身）时按不锁处理。

    **单独成函数**是为了让「锁被别的进程持有」这条分支可以离线注入；真库路径用一条
    **独立连接**持有 session 级 advisory lock——不能用池里的连接，那会白占一个槽位直到进程退出。
    """
    pool = getattr(db, "pool", None)
    if pool is None:
        return _NO_LOCK  # 替身：没有跨进程概念，按已持有处理
    import psycopg

    try:
        conn = await psycopg.AsyncConnection.connect(pool.conninfo, autocommit=True)
    except Exception:  # noqa: BLE001 —— 连不上库时降级启动，worker 不阻塞服务
        log.exception("任务 worker 锁连接失败，本次不启动 worker")
        return None
    try:
        cursor = await conn.execute("SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK_KEY,))
        row = await cursor.fetchone()
    except Exception:
        await conn.close()
        raise
    if not row or not row[0]:
        await conn.close()
        return None
    return conn


async def start(db: Any) -> WorkerHandle | None:
    """启动：先抢单实例锁，再扫尾上一条进程留下的 `running`，最后起常驻循环。

    **扫尾不自动重跑**（SPEC §2.4）：静默重跑一个跑到一半的任务，比明说失败更让人困惑。
    `queued` 的那些不用管——worker 起来自然接着取。

    **多进程下的关键**：扫尾只能在持锁者里做。旧实现每个进程都扫，进程 B 启动时会把
    进程 A 正在跑的 `running` 标成 failed，A 的 `finish_task` 又因 `WHERE status='running'`
    静默不写——任务结果就这么丢了。拿不到锁的进程直接返回 None（不扫、不取件）。

    扫尾失败**不拖住 worker**：它只是一条 UPDATE，失败最多留下几条陈旧的 `running` 行；
    为此把整个队列停摆（提交照收、永远没人跑）才是更糟的降级。
    """
    lock_conn = await _acquire_instance_lock(db)
    if lock_conn is None:
        log.info("任务 worker 单实例锁被其它进程持有，本进程不启动 worker")
        return None
    try:
        interrupted = await db.sweep_interrupted_tasks()
    except Exception:  # noqa: BLE001
        log.exception("任务扫尾失败（worker 照常启动）")
    else:
        if interrupted:
            log.warning("任务扫尾：%d 条因服务重启被标为 failed/interrupted", interrupted)
    return WorkerHandle(
        task=asyncio.create_task(serve(db), name="task-worker"),
        lock_conn=lock_conn,
    )


async def stop(handle: WorkerHandle | asyncio.Task[None] | None) -> None:
    """收工：取消常驻循环、释放单实例锁。

    **不 await 正在跑的那一件**（与 `api/optimize.py` 的断线姿态同源：取消不了线程/
    子进程，硬等只会把停机拖长）；它会随进程一起消失，行留在 `running`，
    下次启动由持锁进程的 `start()` 扫成 `failed/interrupted`——如实，不粉饰。
    """
    if handle is None:
        return
    task = handle.task if isinstance(handle, WorkerHandle) else handle
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    if (
        isinstance(handle, WorkerHandle)
        and handle.lock_conn is not None
        and handle.lock_conn is not _NO_LOCK
    ):
        with suppress(Exception):
            await handle.lock_conn.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK_KEY,))
            await handle.lock_conn.close()


__all__ = [
    "POLL_SECONDS",
    "WORKER_LOCK_KEY",
    "WorkerHandle",
    "request_stop",
    "run_once",
    "serve",
    "start",
    "stop",
]
