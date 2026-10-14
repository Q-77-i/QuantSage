"""任务事件流（P3-E1b）：`(事件名, 载荷)` 的异步迭代器，喂给 SSE 编码。

两个消费者共用它：`POST /optimize/{grid,batch}`（发起时的流）与
`GET /api/v1/tasks/{id}/events`（断线重连）。两侧的帧**逐字同形**——前端的消费器因此
只需要一份（这是 E1b 把 SSE 从「执行载体」降为「进度订阅」之后必须保住的性质）。

终态的权威是**数据库**：任务已终结就从 `optimization_runs.summary.cells` 回放 + 补一帧
`done`（没记录 = 失败或取消，给 `error` / `cancelled`）。缓冲只在任务还在跑时有意义。

**先订阅、再查状态**（反了就有空档：任务在两次调用之间结束，那一格落进缝隙里，
客户端什么都收不到还查不出原因）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from app.tasks import bus

log = logging.getLogger(__name__)

#: 静默超过这个秒数发一个保活帧（`None`）。逐格推送密集得多，这里是兜底。
KEEPALIVE_SECONDS = 15.0

#: 任务状态机的终态（与 `api/tasks.py` 的 409 判据同一套取值）
TERMINAL = frozenset({"done", "failed", "cancelled"})

#: 收到这几帧就该收流了
_CLOSING = frozenset({"done", "error", "cancelled"})


async def task_frames(
    db: Any,
    user_id: int,
    task_id: str,
    *,
    keepalive_seconds: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[tuple[str, object] | None]:
    """任务的事件流。`None` = 保活（由编码层翻成注释帧）。"""
    async with bus.subscribe(task_id) as queue:
        task = await db.get_task(user_id, task_id)
        if task is None:  # 越权与不存在同姿态（调用方通常已判过，这里是兜底）
            yield ("error", {"code": "not_found", "message": "任务不存在"})
            return

        start = _start_payload(task)
        if start is not None:
            yield ("start", start)

        if task["status"] in TERMINAL:
            async for frame in _replay(db, user_id, task):
                yield frame
            return

        # 还在跑：先把订阅时拿到的回放（订阅之前已发布的格）吐出去，再续流。
        # 这一步不能省——断线重连时用户要看到「已经长出这么多」的热力图。
        while not queue.empty():
            frame = queue.get_nowait()
            yield frame
            if frame[0] in _CLOSING:
                return

        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), keepalive_seconds)
            except TimeoutError:
                yield None
                continue
            yield frame
            if frame[0] in _CLOSING:
                return


def _start_payload(task: dict[str, Any]) -> dict[str, Any] | None:
    """发起时冻结在任务行里的 `start` 帧。回测任务没有它（单次回测不流向 SSE）。"""
    exec_payload = (task.get("request") or {}).get("exec") or {}
    return exec_payload.get("start")


async def _replay(
    db: Any, user_id: int, task: dict[str, Any]
) -> AsyncIterator[tuple[str, object]]:
    """终态回放：成功从库里取 cells + 摘要；失败/取消给一帧说明。"""
    status = task["status"]
    run_id = (task.get("result") or {}).get("run_id")

    if status == "done" and run_id:
        row = await db.get_optimization_run(user_id, str(run_id))
        if row is not None:
            summary = row["summary"]
            for cell in summary.get("cells") or []:
                yield ("cell", cell)
            yield ("done", done_payload(str(run_id), summary))
            return

    if status == "cancelled":
        yield ("cancelled", {"message": "任务已取消（中止的结果不落库）"})
        return
    error = task.get("error") or {}
    yield ("error", {"code": error.get("kind") or "failed", "message": error.get("message") or "任务失败"})


def done_payload(run_id: str, summary: dict[str, Any]) -> dict[str, Any]:
    """`done` 帧的载荷：与 M5b 的 `_stream` 逐字同形（前端只认这一份）。

    **执行体与回放共用它**：在线时由执行体发（它手里就有 summary），重连时由 `_replay`
    从库里拼——两处各写一份的话，帧会悄悄漂移（前端只认其中一份，另一份白写）。
    """
    return {
        "run_id": run_id,
        "kind": summary.get("kind"),
        "cells_total": summary.get("cells_total"),
        "cells_ok": summary.get("cells_ok"),
        "best_index": summary.get("best_index"),
        "overfit": summary.get("overfit"),
        "duration_s": summary.get("duration_s"),
    }


__all__ = ["KEEPALIVE_SECONDS", "TERMINAL", "done_payload", "task_frames"]
