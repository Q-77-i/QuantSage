"""数据接入 API（M2b）：看语料覆盖/缺口 + 手动触发一次拉取。

两条口径：

* **状态永远看得见**：本地覆盖区间、归档发布边界、缺口（并区分缺口落在交易日还是
  非交易日）。归档不可达时本地部分照常返回——看不到远端不等于自己也坏了。
* **触发是异步的**：一轮日增量要跑几十秒到几分钟（CLI 下载 + 物化），同步 HTTP 请求
  会超时。端点只负责「起跑并立即回话」，进度由 `/status` 与台账反映；已在跑时 409。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.auth import require_user
from app.core.config import get_settings
from app.etl import runner, scheduler

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/etl", tags=["etl"])


@router.get("/status")
async def get_status(request: Request, _: Annotated[dict[str, Any], Depends(require_user)]) -> dict[str, Any]:
    """语料覆盖、缺口、最近一次运行、调度器状态。"""
    settings = get_settings()
    result = await asyncio.to_thread(runner.status)
    result["running"] = runner.is_running()
    result["scheduler"] = {
        "enabled": settings.etl_enabled,
        "hour": settings.etl_hour,
        "minute": settings.etl_minute,
        "trailing_days": settings.etl_trailing_days,
        "next_run_at": scheduler.next_run_time(getattr(request.app.state, "scheduler", None)),
    }
    return result


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def trigger_run(_: Annotated[dict[str, Any], Depends(require_user)]) -> dict[str, Any]:
    """手动触发一次日增量（回落窗口），后台执行、立即回话。"""
    if runner.is_running():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="已有一次 ETL 在执行")
    settings = get_settings()
    asyncio.get_running_loop().run_in_executor(
        None, _safe_run, settings.etl_trailing_days
    )
    return {"status": "started", "scope": "manual", "trailing_days": settings.etl_trailing_days}


def _safe_run(trailing: int) -> None:
    """后台线程入口：把异常留在日志里，不让它在无人接手的地方炸掉。"""
    try:
        report = runner.run("manual", trailing=trailing)
        log.info("手动 ETL %s", report.summary())
    except Exception as exc:  # noqa: BLE001
        log.warning("手动 ETL 失败：%s: %s", type(exc).__name__, exc)
