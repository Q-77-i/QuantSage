"""数据面看板（E3a）：体检结果 + 数据新鲜度，给 admin 的一块只读视图。

门是 `require_admin`（403 的理由写在 `core/auth.py`）——这是**平台运维**视图，不是用户资产，
与 E2b 的 ETL 两端点同一道门。普通用户的「数据截至 X」已由 `/api/v1/market/freshness` +
页头 tooltip 承载，**不在这里重复**。

三个读数各有各的家，别混：

* `last` —— 最近一次**落档**的体检（`_meta/health_last.json`，全文含 detail）
* `history` —— 台账尾部（`_meta/health_runs.jsonl`，只有摘要级 checks）
* `freshness` / `stale_source` —— **现算**的（前者复用 `dc.latest_dates`，后者直接用 X7
  这项检查本身当读数源——判据只有一处，不另写一份）

两条口径：

* **「还没跑过」是答案不是错误**：`last` 回 `null` 而不是 404（同 `/market/{symbol}/probe`
  的 `has_data: false`）。
* **红灯由服务端算**（`check_status`）：前端不重算——重算就会有两套口径，改一处漏一处。
  体检本身跑不起来（数据目录空的）时 `freshness` 回 `null`，但端点照常 200：
  那种情况下「体检报了什么」正是看板最该显示的东西。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.auth import require_admin
from app.core.config import get_settings
from app.data import data_health as dh
from app.data import duckdb_client as dc
from app.data import health_log, rollback, snapshots
from app.data.data_lock import Busy
from app.etl import scheduler

router = APIRouter(prefix="/api/v1/data", tags=["data"])


def _decorate(row: dict) -> dict:
    """给一行台账补上 `check_status`。跳过的轮次（没有计数）保持 `None`——不假装它有结论。"""
    if "error" not in row:
        return {**row, "check_status": None}
    return {
        **row,
        "check_status": dh.status_from_counts(error=row.get("error", 0), warn=row.get("warn", 0)),
    }


def _freshness() -> dict | None:
    """本地数据的最新时点；数据没落盘时回 `None`（看板照常打开，由 `DATA` 那条 error 说话）。"""
    try:
        return dict(dc.latest_dates())
    except dc.DataNotReady:
        return None


@router.get("/health")
async def get_health(request: Request, _: Annotated[dict[str, Any], Depends(require_admin)]) -> dict[str, Any]:
    """看板首屏：最近一次体检 + 现算的新鲜度与源侧边界 + 下一次体检的时刻。"""
    settings = get_settings()
    data_dir = settings.data_dir
    last = await asyncio.to_thread(health_log.read_last, data_dir)
    stale = await asyncio.to_thread(_source_stall, data_dir)
    return {
        "last": _decorate(last) if last is not None else None,
        "freshness": await asyncio.to_thread(_freshness),
        "stale_source": stale,
        "delisting": (last or {}).get("delisting"),
        "scheduler": {
            "enabled": settings.data_health_enabled,
            "hour": settings.data_health_hour,
            "minute": settings.data_health_minute,
            "next_run_at": scheduler.next_run_time(
                getattr(request.app.state, "scheduler", None), scheduler.HEALTH_JOB_ID
            ),
        },
    }


@router.get("/health/history")
async def get_history(
    _: Annotated[dict[str, Any], Depends(require_admin)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """台账尾部，**新的在前**（服务端分页：视图规模随时间走，分页是底线，E2c 已立此约定）。"""
    total, runs = await asyncio.to_thread(
        health_log.read_runs, get_settings().data_dir, limit=limit, offset=offset
    )
    return {"total": total, "offset": offset, "limit": limit, "runs": [_decorate(r) for r in runs]}


def _source_stall(data_dir) -> dict:
    """源侧行情边界与停滞交易日数——**直接用 X7 这项检查当读数源**。

    不另写一份读数逻辑：判据只有一处（`check_x7_source_stall`），API 与体检报告因此永远同值。
    `con` 那条参数它不用（本项走的是 `bars_sync.json`，不碰 DuckDB）。
    """
    return dict(dh.check_x7_source_stall(None, data_dir).data)


# ── 快照与回滚（E3b）────────────────────────────────────────────────────────


class RollbackRequest(BaseModel):
    """回滚请求。**`confirm` 必须与 `snapshot` 逐字相等**——防手滑，比弹窗确认硬。"""

    snapshot: str = Field(min_length=1, max_length=64)
    confirm: str = Field(min_length=1, max_length=64)
    channels: list[Literal["bars", "events"]] = Field(default_factory=lambda: ["bars", "events"])


@router.get("/snapshots")
async def list_snapshots(_: Annotated[dict[str, Any], Depends(require_admin)]) -> dict[str, Any]:
    """可选的回滚目标，**新的在前**（要回滚就得先知道有哪些 ts 可选）。"""
    rows = await asyncio.to_thread(snapshots.read_index, get_settings().data_dir)
    return {"total": len(rows), "snapshots": rows}


@router.post("/rollback")
async def do_rollback(
    payload: RollbackRequest, _: Annotated[dict[str, Any], Depends(require_admin)]
) -> dict[str, Any]:
    """按快照把 `data/` 换回历史状态（SPEC §4.5 的六步，**破坏性、不可撤销**）。

    四种拒绝各有各的码，前端据此给不同的话：

    ==================== ===== ==============================================
    快照 ts 不认识        404   索引里没有这一份
    对象缺片              409   **整次拒绝**（不做半截回滚），响应列出缺的 sha 前 12 位
    已有同步在跑          409   与 ETL / 行情刷新串行，撞上就拒
    `confirm` 不相等      422   防手滑——**界面上那个确认框不是门**
    ==================== ===== ==============================================

    **同步返回完整结论**（不像 `POST /etl/run` 那样回 202）：回滚是分钟以内的事，而调用方
    要的正是「还回去了什么、之后的体检什么读数」，拆成两段反而要再轮询一次。
    """
    if payload.confirm != payload.snapshot:
        raise HTTPException(
            status_code=422,
            detail="confirm 与 snapshot 不一致——请把快照 ts 原样填两遍（防手滑，不是弹窗确认）",
        )
    try:
        return await asyncio.to_thread(
            rollback.execute,
            get_settings().data_dir,
            ts=payload.snapshot,
            channels=tuple(payload.channels),
        )
    except rollback.SnapshotUnknown as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except rollback.SnapshotIncomplete as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{exc.channel} 缺 {len(exc.missing)} 个对象，**整次拒绝、一个字节都没动**："
                f"{[sha[:12] for sha in exc.missing[:5]]}"
                + ("…" if len(exc.missing) > 5 else "")
                + "。对象不在本地库里 ⇒ 这一份回不去了（`raw/o` 是回滚的底座，不做自动清理）"
            ),
        ) from exc
    except Busy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
