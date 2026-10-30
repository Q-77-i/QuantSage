"""任务事件流（P3-E1b / S5）：`(事件名, 载荷)` 的异步迭代器，喂给 SSE 编码。

两个消费者共用它：`POST /optimize/{grid,batch}`（发起时的流）与
`GET /api/v1/tasks/{id}/events`（断线重连）。两侧的帧**逐字同形**——前端的消费器因此
只需要一份（这是 E1b 把 SSE 从「执行载体」降为「进度订阅」之后必须保住的性质）。

终态的权威是**数据库**：任务已终结就从 `optimization_runs.summary.cells` 回放 + 补一帧
`done`（没记录 = 失败或取消，给 `error` / `cancelled`）。缓冲只在任务还在跑时有意义。

**先订阅、再查状态**（反了就有空档：任务在两次调用之间结束，那一格落进缝隙里，
客户端什么都收不到还查不出原因）。

**S5 跨进程**：`grid`/`batch` 的逐格进度以 `tasks.progress.cells` 为权威。订阅后先补
库里的格，之后每 `poll_seconds`（1s）查一次轻量进度；bus 里的实时帧照常优先，按
`index` 去重。非 master worker 的 SSE 因此也能长出完整热力图。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from app.core.metrics import TASK_PROGRESS_DB_CELLS
from app.tasks import bus
from app.tasks.kinds import COOPERATIVE_KINDS

log = logging.getLogger(__name__)

#: 静默超过这个秒数发一个保活帧（`None`）。逐格推送密集得多，这里是兜底。
KEEPALIVE_SECONDS = 15.0

#: 跨进程逐格轮询间隔（S5，SPEC §6.6）。只对 grid/batch 生效。
POLL_SECONDS = 1.0

#: 无新格时的退避上限（P2 A9）：1→2→4→5s，有新帧/新格立即回 1s。
POLL_MAX_SECONDS = 5.0


def next_poll_interval(
    current: float,
    *,
    new_cells: bool,
    base: float = POLL_SECONDS,
    maximum: float = POLL_MAX_SECONDS,
) -> float:
    """协作式任务的轮询间隔。纯函数，便于用假时钟/直接断言测退避。"""
    if new_cells:
        return base
    return min(maximum, max(base, current * 2))

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
    poll_seconds: float = POLL_SECONDS,
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

        seen: set[int] = set()
        cooperative = str(task.get("kind") or "") in COOPERATIVE_KINDS
        interval = poll_seconds if cooperative else keepalive_seconds

        # 订阅前已经落库的格（断线重连/非 master 的常见路径）
        if cooperative:
            for frame in _db_cells(task, seen):
                yield frame

        # 再把订阅时拿到的回放（订阅之前已发布的格）吐出去。
        while not queue.empty():
            frame = queue.get_nowait()
            if _is_duplicate_cell(frame, seen):
                continue
            _mark_seen(frame, seen)
            yield frame
            if frame[0] in _CLOSING:
                return

        last_keepalive = time.monotonic()
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), interval)
            except TimeoutError:
                if cooperative:
                    latest = await db.get_task_progress(user_id, task_id)
                    if latest is None:
                        yield ("error", {"code": "not_found", "message": "任务不存在"})
                        return
                    if latest["status"] in TERMINAL:
                        async for replay in _replay(db, user_id, latest, skip=seen):
                            yield replay
                        return
                    new_cells = _db_cells(latest, seen)
                    for progress_frame in new_cells:
                        yield progress_frame
                    interval = next_poll_interval(
                        interval, new_cells=bool(new_cells), base=poll_seconds
                    )
                    if time.monotonic() - last_keepalive >= keepalive_seconds:
                        last_keepalive = time.monotonic()
                        yield None
                else:
                    # 非逐格任务保持原语义：保活超时查一次终态。
                    latest = await db.get_task(user_id, task_id)
                    if latest is None:
                        yield ("error", {"code": "not_found", "message": "任务不存在"})
                        return
                    if latest["status"] in TERMINAL:
                        async for replay in _replay(db, user_id, latest, skip=seen):
                            yield replay
                        return
                    yield None
                continue
            if _is_duplicate_cell(frame, seen):
                continue
            if cooperative:
                # 有新帧说明任务还在动，回到最密的轮询间隔
                interval = poll_seconds
            _mark_seen(frame, seen)
            yield frame
            if frame[0] in _CLOSING:
                return


def _progress_cells(task: dict[str, Any]) -> list[dict[str, Any]]:
    cells = (task.get("progress") or {}).get("cells")
    if not isinstance(cells, list):
        return []
    return [cell for cell in cells if isinstance(cell, dict)]


def _cell_index(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    index = value.get("index")
    return index if isinstance(index, int) else None


def _mark_seen(frame: tuple[str, object], seen: set[int]) -> None:
    if frame[0] != "cell":
        return
    index = _cell_index(frame[1])
    if index is not None:
        seen.add(index)


def _is_duplicate_cell(frame: tuple[str, object], seen: set[int]) -> bool:
    if frame[0] != "cell":
        return False
    index = _cell_index(frame[1])
    return index is not None and index in seen


def _db_cells(
    task: dict[str, Any], seen: set[int]
) -> list[tuple[str, object]]:
    """取库里尚未发过的格，并记入 `seen`（bus 与 DB 同格只发一次）。"""
    frames: list[tuple[str, object]] = []
    for cell in _progress_cells(task):
        index = _cell_index(cell)
        if index is None or index in seen:
            continue
        seen.add(index)
        TASK_PROGRESS_DB_CELLS.inc()
        frames.append(("cell", cell))
    return frames


def _start_payload(task: dict[str, Any]) -> dict[str, Any] | None:
    """发起时冻结在任务行里的 `start` 帧。回测任务没有它（单次回测不流向 SSE）。"""
    exec_payload = (task.get("request") or {}).get("exec") or {}
    return exec_payload.get("start")


async def _replay(
    db: Any,
    user_id: int,
    task: dict[str, Any],
    *,
    skip: set[int] | None = None,
) -> AsyncIterator[tuple[str, object]]:
    """终态回放：成功从库里取 cells + 摘要；失败/取消给一帧说明。

    `skip` 是本次连接已经发过的 index（S5）——终态回放不重复发同一格。
    """
    status = task["status"]
    run_id = (task.get("result") or {}).get("run_id")

    if status == "done" and run_id:
        row = await db.get_optimization_run(user_id, str(run_id))
        if row is not None:
            summary = row["summary"]
            for cell in summary.get("cells") or []:
                index = _cell_index(cell)
                if skip is not None and index is not None and index in skip:
                    continue
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


__all__ = ["KEEPALIVE_SECONDS", "POLL_SECONDS", "TERMINAL", "done_payload", "task_frames"]
