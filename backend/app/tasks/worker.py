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
from typing import Any

from app.backtest.batch import error_of
from app.tasks.executors import EXECUTORS

log = logging.getLogger(__name__)

#: 取件间隔（秒）。单次回测 0.3–2s，这点启动延迟可忽略，换来的是零跨 loop 状态。
POLL_SECONDS = 0.2


async def run_once(db: Any) -> bool:
    """取一件、跑一件；没有可取的返 `False`。取件是原子的（`claim_next_task` 一条语句）。"""
    task = await db.claim_next_task()
    if task is None:
        return False

    executor = EXECUTORS.get(str(task["kind"]))
    if executor is None:  # pragma: no cover - 端点只建已注册的 kind（E1b 加 grid / batch）
        await db.fail_task(
            task["id"], error={"kind": "internal", "message": f"未知任务类型 {task['kind']!r}"}
        )
        return True

    try:
        result = await executor(db, task)
    except Exception as exc:  # noqa: BLE001 —— 任务的失败要**落库可查**，不能只进日志
        error = error_of(exc)
        log.warning("任务 %s（%s）失败：%s", task["id"], task["kind"], error["message"])
        await db.fail_task(task["id"], error=error)
        return True

    # 成功 ⇒ 进度满格。E1b 的网格逐格推进，这里是收尾那一下（invariant：done 的必满格）
    total = int((task.get("progress") or {}).get("total") or 1)
    await db.update_task_progress(task["id"], done=total, total=total)
    await db.finish_task(task["id"], result=result)
    return True


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


async def start(db: Any) -> asyncio.Task[None]:
    """启动：先扫尾上一条进程留下的 `running`（标 `failed/interrupted`），再起常驻循环。

    **扫尾不自动重跑**（SPEC §2.4）：静默重跑一个跑到一半的任务，比明说失败更让人困惑。
    `queued` 的那些不用管——worker 起来自然接着取。

    扫尾失败**不拖住 worker**：它只是一条 UPDATE，失败最多留下几条陈旧的 `running` 行；
    为此把整个队列停摆（提交照收、永远没人跑）才是更糟的降级。
    """
    try:
        interrupted = await db.sweep_interrupted_tasks()
    except Exception:  # noqa: BLE001
        log.exception("任务扫尾失败（worker 照常启动）")
    else:
        if interrupted:
            log.warning("任务扫尾：%d 条因服务重启被标为 failed/interrupted", interrupted)
    return asyncio.create_task(serve(db), name="task-worker")


async def stop(task: asyncio.Task[None]) -> None:
    """收工：取消常驻循环并等它退出。

    **不 await 正在跑的那一件**（与 `api/optimize.py` 的断线姿态同源：取消不了线程/
    子进程，硬等只会把停机拖长）；它会随进程一起消失，行留在 `running`，
    下次启动由 `start()` 扫成 `failed/interrupted`——如实，不粉饰。
    """
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


__all__ = ["POLL_SECONDS", "run_once", "serve", "start", "stop"]
