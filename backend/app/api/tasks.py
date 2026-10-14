"""任务端点（P3-E1a）：列表 / 详情 / 取消。

**提交不在这里**：回测的提交留在 `api/backtest.py`——那里才持有全部闸门（归属、静态检查、
参数、窗口解析），而闸门必须跑在入队**之前**（SPEC §2.2）。本模块只读任务表、改状态，
不碰执行（执行在 `app/tasks/worker.py`）。

取消的语义如实（SPEC §2.3）：`queued` 能取消；`running` 与终态一律 **409**——单次回测跑在
子线程 + 沙箱子进程里，中止不了，不假装能停（E1b 的网格才用得上 `run_cells(stop=)` 的
协作式中止）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.backtest import normalize_run_id
from app.core.auth import require_db, require_user

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


@router.post("/{task_id}/cancel")
async def cancel_task(
    request: Request, task_id: str, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    """取消排队中的任务。已在跑或已结束 → 409（请求本身没写错，是这一刻的状态不允许）。"""
    normalized = normalize_run_id(task_id)
    row = await require_db(request).cancel_task(int(user["id"]), normalized)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] != "cancelled":
        raise HTTPException(status_code=409, detail=_why_not(row["status"]))
    return row


def _why_not(status: str) -> str:
    """409 的文案分两档：能等一等 vs 已经没有可取消的东西。"""
    if status == "running":
        return "任务已在执行，无法中止（跑完会有结果）"
    return f"任务已结束（{status}），没有可取消的东西"
