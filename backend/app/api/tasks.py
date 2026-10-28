"""任务端点（P3-E1a 列表/详情/取消；E1b 加重连订阅与协作式中止）。

**提交不在这里**：回测的提交留在 `api/backtest.py`——那里才持有全部闸门（归属、静态检查、
参数、窗口解析），而闸门必须跑在入队**之前**（SPEC §2.2）。本模块只读任务表、改状态，
不碰执行（执行在 `app/tasks/worker.py`）。

取消的语义如实（SPEC §2.3/§2.8）：

* `queued` → 条件更新置 `cancelled`（真取消：worker 取不到它）；
* `running` 且 kind 支持协作式中止（`grid` / `batch`）→ 写 `cancel_requested_at` DB 标志
  （本进程有该任务时同时置本地事件），响应带 `cancelling: true`，任务由 worker 收尾成
  `cancelled`——**中止的结果不落库**。DB 标志让请求落到非 worker 进程也能生效；
* `running` 的单次回测与一切终态 → **409**（它跑在子线程/子进程里，中止不了；
  如实拒绝，不假装能停）。

`GET /{task_id}/events` 是**订阅**（E1b）：还在跑就回放已有格 + 续流；已终结就从
`optimization_runs` 回放 + 补一帧 `done`；失败/取消给一帧说明。帧与发起时的流逐字同形。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.api.backtest import normalize_run_id
from app.api.optimize import streaming_headers
from app.api.sse import sse_from, sse_response
from app.core.auth import require_db, require_user
from app.tasks import worker as task_worker
from app.tasks.kinds import COOPERATIVE_KINDS
from app.tasks.stream import task_frames

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


@router.get("")
async def list_tasks(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """我的任务（摘要），最近在前。不含整份 `request`（用户策略源码在里面）。"""
    return await require_db(request).list_tasks(int(user["id"]), limit)


@router.get("/{task_id}")
async def get_task(
    request: Request, task_id: str, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    """轮询与回看都走它：`status` / `progress` / `result.run_id` / `error`。越权与不存在同为 404。"""
    normalized = normalize_run_id(task_id)
    row = await require_db(request).get_task(int(user["id"]), normalized)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return row


@router.get("/{task_id}/events")
async def task_events(
    request: Request, task_id: str, user: dict[str, Any] = Depends(require_user)
) -> StreamingResponse:
    """订阅任务的事件流（E1b）：发起时的流与重连都用同一份帧。

    越权与不存在同为 404——订阅的归属判在这里（`task_frames` 里那条是兜底）。
    """
    db = require_db(request)
    user_id = int(user["id"])
    normalized = normalize_run_id(task_id)
    if await db.get_task(user_id, normalized) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return sse_response(sse_from(task_frames(db, user_id, normalized)), headers=streaming_headers())


@router.post("/{task_id}/cancel")
async def cancel_task(
    request: Request, task_id: str, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    """取消任务。排队中的直接取消；在跑的网格/批量**协作式中止**；其余 409。"""
    normalized = normalize_run_id(task_id)
    db = require_db(request)
    row = await db.cancel_task(int(user["id"]), normalized)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] == "cancelled":
        return row
    # running 的网格/批量：**DB 标志是权威**（收尾项 6）。本进程有该任务时顺手置本地
    # 事件，让它在 0.5s 轮询之前就停；请求落到非 worker 进程时，worker 自己会看到标志。
    if row["status"] == "running" and row["kind"] in COOPERATIVE_KINDS:
        task_worker.request_stop(normalized)
        latest = await db.request_task_cancel(int(user["id"]), normalized)
        if latest is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if latest["status"] == "running":
            return {**latest, "cancelling": True}
        raise HTTPException(status_code=409, detail=_why_not(latest["status"], latest["kind"]))
    raise HTTPException(status_code=409, detail=_why_not(row["status"], row["kind"]))


def _why_not(status: str, kind: str) -> str:
    """409 的文案分三档：跑着但停不下来 / 刚跑完的竞态 / 已经没有可取消的东西。"""
    if status == "running":
        if kind in COOPERATIVE_KINDS:
            # 走到这里 = 写标志时任务已经不在 running（典型是刚跑完）
            return "任务刚结束或状态已变化——刷新看状态"
        return "任务已在执行，无法中止（单次回测跑在子线程/子进程里，跑完会有结果）"
    return f"任务已结束（{status}），没有可取消的东西"
