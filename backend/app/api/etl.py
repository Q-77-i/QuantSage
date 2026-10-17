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

from app.core.auth import require_admin
from app.core.config import get_settings
from app.data import bars
from app.etl import runner, scheduler

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/etl", tags=["etl"])


@router.get("/status")
async def get_status(request: Request, _: Annotated[dict[str, Any], Depends(require_admin)]) -> dict[str, Any]:
    """语料覆盖、缺口、最近一次运行、调度器状态（含行情按需刷新那一路，M2d）。

    **admin 门（E2b）**：这是系统级运维视图（全市场覆盖率与调度时刻），不是用户资产。
    此前任何登录用户都能看、能触发——本轮把它归到 admin，是本段唯一的越权面修复。
    """
    settings = get_settings()
    result = await asyncio.to_thread(runner.status)
    result["running"] = runner.is_running()
    result["scheduler"] = {
        "enabled": settings.etl_enabled,
        # owner/active 分开：前者是“本进程有没有后台主实例锁”，后者是“本进程是否真起了
        # 调度器”。enabled 仍是配置值——三者混在一起会让读者误判“配置开了就等于在跑”。
        "owner": getattr(request.app.state, "background_owner", None),
        "active": getattr(request.app.state, "scheduler", None) is not None,
        "hour": settings.etl_hour,
        "minute": settings.etl_minute,
        "trailing_days": settings.etl_trailing_days,
        "next_run_at": scheduler.next_run_time(getattr(request.app.state, "scheduler", None)),
        # 行情那一路：与本调度器同生共死（ETL_ENABLED=false 时两条都不起）
        "bars": {
            "enabled": settings.bars_refresh_enabled,
            "hour": settings.bars_refresh_hour,
            "minute": settings.bars_refresh_minute,
            "budget_seconds": settings.bars_refresh_budget_seconds,
            "next_run_at": scheduler.next_run_time(
                getattr(request.app.state, "scheduler", None), scheduler.BARS_JOB_ID
            ),
        },
    }
    # 两条通道写同一份台账，`last_run` 只报语料那一路 ⇒ 行情单独给一条
    result["last_bars_run"] = await asyncio.to_thread(bars.last_run)
    return result


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def trigger_run(
    request: Request, _: Annotated[dict[str, Any], Depends(require_admin)]
) -> dict[str, Any]:
    """手动触发一次日增量（回落窗口），后台执行、立即回话。

    **admin 门（E2b）**：一次触发是**全局**动作（全市场语料/行情），影响所有用户。
    **后台主实例门（E 批）**：非持锁进程只服务 HTTP，不能在这里起一轮全局 ETL。
    """
    if getattr(request.app.state, "background_owner", None) is False:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "本进程不是后台任务主实例（后台任务仅由持锁实例执行）；"
                "请在主实例触发，或重启本进程重新竞选"
            ),
        )
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
