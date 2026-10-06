"""进程内定时：交易日收盘后拉事件语料，启动时补缺口。

两条职责分开看不混：

* **定时**：每天固定时刻跑一次日增量（回落窗口，默认最近 7 个自然日）。跑的是自然日，
  不是交易日——归档按自然日发布，周末与假期同样有新闻（实测中秋与周末都有分片）。
* **补缺口**：进程重启后先对着归档的 `coverage.last` 补一次。机器关机、长假、崩溃都会
  造成缺口，而**归档的保留期是滚动的（news 只有 3 个月）——缺口超过保留期就永久拿不到**，
  所以补缺口不是「优化」，是必需品。

**默认不开启**（`ETL_ENABLED=false`）：定时任务会对外发请求、写数据目录，本地开发与
测试不该被它打扰。要用就在 `.env` 里显式打开。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.etl import runner

TZ = "Asia/Shanghai"

log = logging.getLogger(__name__)

JOB_ID = "events-daily"
CATCHUP_DELAY_SECONDS = 20  # 让服务先把自身起完再拉数据，避免启动期争 IO


def _run_sync(scope: str, trailing: int) -> None:
    """在线程里跑同步 ETL：CLI 子进程与 DuckDB 都是阻塞调用，不能占事件循环。"""
    try:
        report = runner.run(scope, trailing=trailing)
        log.info("ETL %s", report.summary())
    except runner.EtlBusy:
        log.warning("ETL 已在执行，本次 %s 跳过", scope)
    except Exception as exc:  # noqa: BLE001 —— 定时任务的异常不能把调度器带崩
        log.warning("ETL %s 失败：%s: %s", scope, type(exc).__name__, exc)


async def _run_async(scope: str, trailing: int) -> None:
    await asyncio.get_running_loop().run_in_executor(None, _run_sync, scope, trailing)


def start_scheduler() -> AsyncIOScheduler | None:
    """按配置起调度器；未启用返回 None（调用方据此决定是否注册关闭钩子）。"""
    settings = get_settings()
    if not settings.etl_enabled:
        return None

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        _run_async,
        CronTrigger(hour=settings.etl_hour, minute=settings.etl_minute, timezone=TZ),
        args=["daily", settings.etl_trailing_days],
        id=JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,  # 错过（如机器休眠）一小时内的仍补跑
        coalesce=True,
    )
    if settings.etl_catchup_on_start:
        scheduler.add_job(
            _run_async,
            "date",
            run_date=datetime.now() + timedelta(seconds=CATCHUP_DELAY_SECONDS),
            args=["catchup", settings.etl_trailing_days],
            id="events-catchup",
            replace_existing=True,
        )
    scheduler.start()
    log.info(
        "ETL 定时已启用：每天 %02d:%02d（%s）拉最近 %d 个自然日",
        settings.etl_hour,
        settings.etl_minute,
        TZ,
        settings.etl_trailing_days,
    )
    return scheduler


def next_run_time(scheduler: AsyncIOScheduler | None) -> str | None:
    """下一次定时触发时刻（供 `/etl/status` 展示）——没有调度器就返回 None。"""
    if scheduler is None:
        return None
    job = scheduler.get_job(JOB_ID)
    return job.next_run_time.isoformat() if job and job.next_run_time else None
