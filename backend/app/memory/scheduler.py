"""进程内定时：每天把「已到期的决策」结算进记忆（M7b）。

**独立于 ETL**：`ETL_ENABLED=false` 时 `etl/scheduler.py::start_scheduler()` 直接返回 None，
把结算挂在它下面等于「ETL 一关，结算也不跑」——两条任务的触发条件、失败代价、
花钱与否都不一样，各起一个调度器（同进程、不同 job）。

默认关（`MEMORY_SETTLE_ENABLED=false`）：它**会调模型**（每笔新到期的回合一次 flash），
本地开发与测试不该被它打扰。手动触发与界面按钮走的是**同一个函数**
（`memory/service.py::settle_account`）——调度器与手动不该有两套语义。

app.state 在 lifespan 里装配后才可用，故 job 闭包**每次运行时现读** `app.state`：
库或记忆没起来（降级启动）就跳过并记一行日志，绝不把调度器带崩。
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from app.core.config import get_settings

TZ = "Asia/Shanghai"

log = logging.getLogger(__name__)

JOB_ID = "memory-settle"


async def settle_all_accounts(state: Any) -> dict[str, int]:
    """把所有账户结算一遍（幂等：已平仓的回合不会重复调模型）。返回计数供日志/测试断言。"""
    from app.memory.decision_store import MemoryUnavailable, memory_for
    from app.memory.service import settle_account

    db = getattr(state, "db", None)
    if db is None:
        log.warning("结算跳过：业务库未就绪（降级启动）")
        return {"accounts": 0, "saved": 0, "reused": 0, "failed": 0}
    try:
        memory = await memory_for(state)
    except MemoryUnavailable as exc:
        log.warning("结算跳过：决策记忆不可用（%s）", exc)
        return {"accounts": 0, "saved": 0, "reused": 0, "failed": 0}

    accounts = await db.list_all_paper_accounts()
    totals = {"accounts": 0, "saved": 0, "reused": 0, "failed": 0}
    for account in accounts:
        try:
            _review, run = await settle_account(
                db, memory, user_id=int(account["user_id"]), account_id=str(account["id"])
            )
        except Exception as exc:  # noqa: BLE001 —— 一个账户炸了不该拖垮整轮
            totals["failed"] += 1
            log.warning("结算失败 %s：%s: %s", account["id"], type(exc).__name__, exc)
            continue
        totals["accounts"] += 1
        totals["saved"] += run.saved
        totals["reused"] += run.reused
    log.info(
        "结算完成：账户 %(accounts)d（失败 %(failed)d），新写 %(saved)d 条、复用 %(reused)d 条",
        totals,
    )
    return totals


async def _run(app_state: Any) -> None:
    await settle_all_accounts(app_state)


def start_settlement_scheduler(app: Any) -> AsyncIOScheduler | None:
    """按配置起结算调度器；未启用返回 None（调用方据此决定是否注册关闭钩子）。"""
    settings = get_settings()
    if not settings.memory_settle_enabled:
        return None

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        _run,
        CronTrigger(
            hour=settings.memory_settle_hour, minute=settings.memory_settle_minute, timezone=TZ
        ),
        args=[app.state],
        id=JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,  # 错过（如机器休眠）一小时内的仍补跑
        coalesce=True,
    )
    scheduler.start()
    log.info(
        "决策结算定时已启用：每天 %02d:%02d（%s）",
        settings.memory_settle_hour,
        settings.memory_settle_minute,
        TZ,
    )
    return scheduler


def next_run_time(scheduler: AsyncIOScheduler | None) -> str | None:
    """下一次定时触发时刻（供端点/日志展示）——没有调度器就返回 None。"""
    if scheduler is None:
        return None
    job = scheduler.get_job(JOB_ID)
    return job.next_run_time.isoformat() if job and job.next_run_time else None
