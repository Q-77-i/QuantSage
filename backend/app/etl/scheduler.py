"""进程内定时：收盘后拉事件语料（M2b）、**按需刷新行情**（M2d）与**数据体检**（E3a），
启动时各补一次缺口。

三条职责分开看不混：

* **事件语料**（每天 21:10）：跑的是**自然日**——归档按自然日发布，周末与假期同样有新闻
  （实测中秋与周末都有分片）。回落窗口吸收迟到事件与平台修订。
* **行情**（每天 21:40，默认关）：跑的是**按需整片重下**——行情没有日增量可拉（`cn-daily`
  的源侧维度是 `{year, adjust}`，没有 `date`），先便宜探一次源侧发布标识，变了才下。
  时点取 21:40：实测源侧发布在 21:05–21:29、语料 job 在 21:10，落在两者之后。
* **数据体检**（每天 22:10，默认关，E3a）：跑的是**只读**的 23 项检查（+ 退市核实），
  结果落 `data/_meta/health_runs.jsonl`（摘要）与 `health_last.json`（全文）。
  **时点在两个写通道之后**——顺序是语义不是巧合：早于它们跑，测的就是上一版数据。

**三条 job 必须串行**：前两条共用 `data/raw`（同一份回执 + 同一个 CLI 文件锁），交错发请求
会互相清掉条目（实测语料那轮把行情 21 条冲掉）；体检虽然只读，但 B1 要逐片重算行情 sha
（888MB 的 I/O），跑在一半换入的分片上会得出没有意义的结论。互斥靠 `app.data.data_lock`
那把进程内锁，**撞上就跳过并留痕**，不排队等——体检那条会把跳过本身落成台账行，
否则「今天怎么没跑」在台账里查不到。

**补缺口**：进程重启后语料先对着归档的 `coverage.last` 补一次（机器关机、长假、崩溃都会
造成缺口，而**归档保留期是滚动的——news 只有 3 个月**，缺口超过保留期就永久拿不到）；
行情那条成本更低（没更新就零下载），延后 180s 起，避开语料的 catchup；体检再往后
（+300s），三条互斥的活儿不挤在一起。

**默认不开启**（`ETL_ENABLED=false`）：定时任务会对外发请求、写数据目录，本地开发与
测试不该被它打扰。要用就在 `.env` 里显式打开。**后两条各有自己的开关**
（`BARS_REFRESH_ENABLED` / `DATA_HEALTH_ENABLED`，默认也都关），但它们与本模块同生共死
——`ETL_ENABLED=false` 时三个 job 都不起（如实记，见 SPEC §3 M2d「落点」）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.data import bars
from app.data.data_lock import LOCK, Busy
from app.etl import runner

TZ = "Asia/Shanghai"

log = logging.getLogger(__name__)

JOB_ID = "events-daily"
BARS_JOB_ID = "bars-refresh"
HEALTH_JOB_ID = "data-health"
CATCHUP_DELAY_SECONDS = 20  # 让服务先把自身起完再拉数据，避免启动期争 IO
#: 行情 catchup 的延后：语料的 catchup 在 +20s、实测约 1 分钟，错开即可（三条 job 串行）
BARS_CATCHUP_DELAY_SECONDS = 180
#: 体检 catchup 再往后：它要读的是前两条写完之后的盘面，挤在一起只会互相等锁
HEALTH_CATCHUP_DELAY_SECONDS = 300


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


def _run_bars_sync() -> None:
    """在线程里跑行情刷新（CLI 子进程与 DuckDB 都是阻塞调用，不能占事件循环）。"""
    try:
        report = bars.refresh()
        log.info("行情刷新 %s", report.summary())
    except Busy:
        log.warning("行情刷新跳过：已有一次数据同步在执行（两条通道必须串行）")
    except Exception as exc:  # noqa: BLE001 —— 定时任务的异常不能把调度器带崩
        log.warning("行情刷新失败：%s: %s", type(exc).__name__, exc)


async def _run_bars_async() -> None:
    await asyncio.get_running_loop().run_in_executor(None, _run_bars_sync)


def _delisting_payload(report) -> dict:
    """退市核实进全文的机读形态——**完整清单只在落档里**，人读报告仍旧只出两行。"""
    return {
        "data_end": str(report.data_end),
        "stopped": len(report.stopped),
        "by_year": {str(year): count for year, count in report.by_year.items()},
        "window": [str(d) for d in report.window] if report.window else None,
        "window_candidates": len(report.window_candidates),
        "confirmed": len(report.confirmed),
        "unexplained": list(report.unexplained),
        "method_note": report.method_note,
    }


def _run_health_sync() -> None:
    """在线程里跑体检并落档（B1 要逐片重算行情 sha，是 888MB 的 I/O，不能占事件循环）。

    **与写通道互斥**：拿不到锁就跳过**并落一行 `skipped`**——跳过本身也要留痕，否则
    「今天怎么没跑」在台账里查不到（M2c 那条「台账的 failed 不等于缺数据」的同族）。
    持锁跑完，免得写通道在一半时插进来把分片换掉（那样 B1 的对账结论没有意义）。
    """
    from app.data import data_health, delisting, health_log

    data_dir = get_settings().data_dir  # 显式传，才吃得到 DATA_DIR（ETL/行情那两路走的是同一条）
    if not LOCK.acquire(blocking=False):
        health_log.append_skipped(data_dir, source="scheduler", reason="locked")
        log.warning("数据体检跳过：已有一次数据同步在执行（三条通道必须串行）")
        return
    started = time.monotonic()
    try:
        checks = data_health.run_all(data_dir=data_dir)
        delisting_payload = None
        try:
            delisting_payload = _delisting_payload(delisting.run(data_dir))
        except Exception as exc:  # noqa: BLE001 —— 退市核实挂了不该让整轮体检没有落档
            log.warning("退市核实失败（体检结果照常落档）：%s: %s", type(exc).__name__, exc)
        health_log.record(
            data_dir,
            source="scheduler",
            checks=checks,
            duration_ms=int((time.monotonic() - started) * 1000),
            delisting=delisting_payload,
            exit_code=data_health.exit_code(checks),
        )
        log.info("数据体检 %s", data_health.summarize(checks))
    except Exception as exc:  # noqa: BLE001 —— 定时任务的异常不能把调度器带崩
        log.warning("数据体检失败：%s: %s", type(exc).__name__, exc)
    finally:
        LOCK.release()


async def _run_health_async() -> None:
    await asyncio.get_running_loop().run_in_executor(None, _run_health_sync)


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
    if settings.bars_refresh_enabled:
        scheduler.add_job(
            _run_bars_async,
            CronTrigger(
                hour=settings.bars_refresh_hour, minute=settings.bars_refresh_minute, timezone=TZ
            ),
            id=BARS_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,  # 错过（如机器休眠）一小时内的仍补跑
            coalesce=True,
        )
        if settings.bars_refresh_catchup_on_start:
            scheduler.add_job(
                _run_bars_async,
                "date",
                run_date=datetime.now() + timedelta(seconds=BARS_CATCHUP_DELAY_SECONDS),
                id="bars-catchup",
                replace_existing=True,
            )
    if settings.data_health_enabled:
        scheduler.add_job(
            _run_health_async,
            CronTrigger(
                hour=settings.data_health_hour, minute=settings.data_health_minute, timezone=TZ
            ),
            id=HEALTH_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,  # 错过（如机器休眠）一小时内的仍补跑
            coalesce=True,
        )
        if settings.data_health_catchup_on_start:
            scheduler.add_job(
                _run_health_async,
                "date",
                run_date=datetime.now() + timedelta(seconds=HEALTH_CATCHUP_DELAY_SECONDS),
                id="data-health-catchup",
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
    if settings.bars_refresh_enabled:
        log.info(
            "行情按需刷新已启用：每天 %02d:%02d（%s），整轮预算 %ds",
            settings.bars_refresh_hour,
            settings.bars_refresh_minute,
            TZ,
            settings.bars_refresh_budget_seconds,
        )
    if settings.data_health_enabled:
        log.info(
            "数据体检已启用：每天 %02d:%02d（%s），结果落 _meta/health_runs.jsonl",
            settings.data_health_hour,
            settings.data_health_minute,
            TZ,
        )
    return scheduler


def next_run_time(scheduler: AsyncIOScheduler | None, job_id: str = JOB_ID) -> str | None:
    """下一次定时触发时刻（供 `/etl/status` 展示）——没有调度器就返回 None。"""
    if scheduler is None:
        return None
    job = scheduler.get_job(job_id)
    return job.next_run_time.isoformat() if job and job.next_run_time else None
