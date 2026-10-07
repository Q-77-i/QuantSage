"""回填与日增量的编排：定日期、串流程、写台账、加锁。

**漏拉判据是「本地最新日 vs 归档 `coverage.last`」**，不是「昨天必须有数据」——实测
归档对最新交易日有滞后（2026-10-07 时归档止于 09-29，而行情已到 09-30），节假日更是
整天没有分片。用固定窗口去猜「应该有什么」必然误报，用归档自述的发布边界才准。

**日任务拉的是回落窗口**（最近 N 个自然日，默认 7）而非只拉昨天：实测 `available_at` 相对
`event_time` 中位滞后 2.2h、p90 达 3.4 天，只拉昨天会漏掉迟到的事件与平台修订。
已拉过且来源分片 sha 未变的日子会被跳过（只花一次下载，不重新物化）。

幂等的口径：**同一份归档分片重跑 → 逐字节一致**。归档若发布了新版本（分片 sha 变了），
那是数据修订，不是「不幂等」——台账会如实记下新的 sha。
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.core.config import get_settings
from app.data import calendar as cal
from app.etl import archive, store

#: 日任务的回落窗口：拉最近 N 个自然日，吸收迟到事件与平台修订
DEFAULT_TRAILING_DAYS = 7
#: 回填窗口：在线/归档的事件保留期约 3 个月（滚动），取 92 天
BACKFILL_DAYS = 92
LEDGER_NAME = "etl_runs.jsonl"

_LOCK = threading.Lock()


class EtlBusy(RuntimeError):
    """已有一次 ETL 在跑——并发跑会同时写同一批文件。"""


@dataclass(frozen=True, slots=True)
class Paths:
    data_dir: Path
    events_dir: Path
    raw_dir: Path
    meta_dir: Path


@dataclass(slots=True)
class DayOutcome:
    date: str
    status: str  # ok | skipped | empty | failed
    rows: int = 0
    symbols: int = 0
    sha256: str = ""
    error: str = ""


@dataclass(slots=True)
class RunReport:
    scope: str
    started_at: str
    finished_at: str = ""
    archive_last: str = ""
    days: list[DayOutcome] = field(default_factory=list)
    result: str = "ok"
    #: 被平台限流中止时：等待秒数（来自 `Retry-After`）与尚未处理的天数
    retry_after_seconds: int | None = None
    unattempted: int = 0
    note: str = ""

    def summary(self) -> str:
        counted = {status: sum(1 for day in self.days if day.status == status) for status in
                   ("ok", "skipped", "empty", "failed")}
        text = (
            f"{self.scope}：{len(self.days)} 天 → 物化 {counted['ok']}、跳过 {counted['skipped']}、"
            f"无分片 {counted['empty']}、失败 {counted['failed']}（结果 {self.result}）"
        )
        if self.note:
            text += f"；{self.note}"
        return text


def paths(data_dir: Path | None = None) -> Paths:
    base = Path(data_dir) if data_dir is not None else get_settings().data_dir
    return Paths(
        data_dir=base,
        events_dir=base / "events",
        raw_dir=base / "raw" / "events",
        meta_dir=base / "_meta",
    )


def plan_days(target_end: date, *, trailing: int | None = None, since: date | None = None) -> list[date]:
    """要处理的**自然日**：`[since, target_end]`（两端含），升序。

    **归档按自然日发布，不是交易日**——实测 2026-09-25（中秋）、09-26/27（周末）都有分片，
    新闻不停市。用交易日枚举会静默丢掉周末与假期的新闻，所以这里一律按自然日走；
    日历的用途是**分类**（缺口落在交易日还是非交易日）与回测撮合，不是筛选采集窗口。

    `since` 缺省按回落窗口（最近 `trailing` 个自然日）推。目标日超出冻结日历覆盖即抛错：
    那是「该重生成日历了」，不是「这天没数据」。
    """
    if target_end > cal.coverage()[1]:
        raise cal.CalendarOutOfRange(
            f"目标日 {target_end} 超出冻结日历覆盖（止于 {cal.coverage()[1]}）；先重生成日历"
        )
    span = trailing if trailing is not None else DEFAULT_TRAILING_DAYS
    start = since if since is not None else target_end - timedelta(days=span - 1)
    if start > target_end:
        return []
    return [start + timedelta(days=offset) for offset in range((target_end - start).days + 1)]


def run_day(day: date, paths_: Paths, *, refresh: bool = False) -> DayOutcome:
    """拉一天、必要时物化。已物化且来源分片未变则跳过物化。"""
    outcome = DayOutcome(date=day.isoformat(), status="empty")
    try:
        if not archive.download_day(day, paths_.data_dir):
            return outcome  # 404：该日没有分片（节假日或尚未发布）
        shards = archive.day_shards(day, paths_.data_dir)
        if not any(t in shards for t in store.KEPT_EVENT_TYPES):
            outcome.error = f"分片里没有可收类型：{sorted(shards)}"
            return outcome
        if not refresh and _unchanged(day, shards, paths_):
            outcome.status = "skipped"
            return outcome
        entry = store.materialize_day(day, shards, paths_.events_dir, paths_.raw_dir)
    except archive.RateLimited:
        raise  # 限流不是「这一天坏了」，是「现在都别问了」——交回给整轮编排去中止
    except Exception as exc:  # noqa: BLE001 —— 单日失败不该打断整轮回填，记台账继续
        outcome.status = "failed"
        outcome.error = f"{type(exc).__name__}: {exc}"[:300]
        return outcome
    outcome.status = "ok"
    outcome.rows = entry.rows
    outcome.symbols = entry.symbols
    outcome.sha256 = entry.sha256
    return outcome


def run(
    scope: str = "daily",
    *,
    days: list[date] | None = None,
    trailing: int | None = None,
    since: date | None = None,
    data_dir: Path | None = None,
    refresh: bool = False,
) -> RunReport:
    """跑一轮 ETL。`scope` 只用于台账与日志（daily / backfill / manual）。"""
    if not _LOCK.acquire(blocking=False):
        raise EtlBusy("已有一次 ETL 在执行")
    paths_ = paths(data_dir)
    report = RunReport(scope=scope, started_at=datetime.now(timezone.utc).isoformat())
    try:
        cover = archive.coverage(paths_.data_dir)
        report.archive_last = cover.last_day.isoformat()
        todo = days if days is not None else plan_days(cover.last_day, trailing=trailing, since=since)
        for index, day in enumerate(todo):
            try:
                report.days.append(run_day(day, paths_, refresh=refresh))
            except archive.RateLimited as exc:
                # 限流是停止信号：继续试下一天只会把窗口越推越远（本项目的红线也是这么写的）
                report.result = "rate_limited"
                report.retry_after_seconds = exc.retry_after
                report.unattempted = len(todo) - index
                minutes = f"{exc.retry_after / 60:.0f} 分钟" if exc.retry_after else "未知时长"
                report.note = f"平台限流，需等待约 {minutes}；{report.unattempted} 天未处理，稍后重跑同命令即续"
                break
        else:
            report.result = "failed" if any(d.status == "failed" for d in report.days) else "ok"
    finally:
        report.finished_at = datetime.now(timezone.utc).isoformat()
        _LOCK.release()
    _append_ledger(paths_, report)
    store.write_manifest(
        paths_.meta_dir,
        store.read_day_entries(paths_.events_dir, paths_.raw_dir),
        coverage_last=report.archive_last or None,
    )
    return report


def is_running() -> bool:
    """是否已有一次 ETL 在执行（端点据此回 409，状态页据此显示在跑）。"""
    return _LOCK.locked()


def backfill(data_dir: Path | None = None, *, days: int = BACKFILL_DAYS, refresh: bool = False) -> RunReport:
    """一次性回填归档保留期内的最近 `days` 天（默认 92 天 ≈ 3 个月窗口）。

    **只补缺**：已有本地日分区的日子不再重下。实测连续申请约 50 次会被平台 429
    （`retry_after` 约 44 分钟），重复申请既浪费也会推迟窗口——而「最近 N 天会不会
    被平台修订」由日增量的回落窗口负责，回填不必重复检查历史日。
    """
    paths_ = paths(data_dir)
    cover = archive.coverage(paths_.data_dir)
    window = plan_days(cover.last_day, since=date.today() - timedelta(days=days))
    todo = [
        day
        for day in window
        if refresh or not (paths_.events_dir / store.day_file_name(day)).exists()
    ]
    return run("backfill", days=todo, data_dir=data_dir, refresh=refresh)


def rematerialize(data_dir: Path | None = None) -> RunReport:
    """用**本地已有分片**重物化全部日分区，**零网络会话**。

    用途：改了口径（方向映射表、落盘列集）后让历史数据与新口径一致。分片是内容寻址的
    本地对象（`data/raw/o/`），`archive.day_shards()` 只读回执状态文件——所以这条路与
    `refresh=True`（要重新向平台发请求）是两件完全不同成本的事，别混用。
    """
    if not _LOCK.acquire(blocking=False):
        raise EtlBusy("已有一次 ETL 在执行")
    paths_ = paths(data_dir)
    report = RunReport(scope="rematerialize", started_at=datetime.now(timezone.utc).isoformat())
    try:
        for entry in store.read_day_entries(paths_.events_dir, paths_.raw_dir):
            day = date.fromisoformat(entry.date)
            try:
                shards = archive.day_shards(day, paths_.data_dir)
                fresh = store.materialize_day(day, shards, paths_.events_dir, paths_.raw_dir)
                report.days.append(
                    DayOutcome(
                        date=entry.date,
                        status="ok",
                        rows=fresh.rows,
                        symbols=fresh.symbols,
                        sha256=fresh.sha256,
                    )
                )
            except archive.RateLimited:
                raise
            except Exception as exc:  # noqa: BLE001 —— 单日失败记台账继续
                report.days.append(
                    DayOutcome(date=entry.date, status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
                )
        report.result = "failed" if any(d.status == "failed" for d in report.days) else "ok"
    finally:
        report.finished_at = datetime.now(timezone.utc).isoformat()
        _LOCK.release()
    _append_ledger(paths_, report)
    store.write_manifest(
        paths_.meta_dir,
        store.read_day_entries(paths_.events_dir, paths_.raw_dir),
        coverage_last=_archive_last_or_none(paths_),
    )
    return report


def _archive_last_or_none(paths_: Paths) -> str | None:
    """清单里的归档边界：查不到就留空，不让「归档不可达」把重物化整轮带崩。"""
    try:
        return archive.coverage(paths_.data_dir).last_day.isoformat()
    except archive.ArchiveError:
        return None


def status(data_dir: Path | None = None) -> dict:
    """给 `/api/v1/etl/status` 的口径：本地覆盖、归档边界、缺口、最近一次运行。"""
    paths_ = paths(data_dir)
    entries = store.read_day_entries(paths_.events_dir, paths_.raw_dir)
    local_start = entries[0].date if entries else None
    local_end = entries[-1].date if entries else None

    try:
        cover = archive.coverage(paths_.data_dir)
        archive_last, manifest_version = cover.last_day, cover.manifest_version
    except archive.ArchiveError as exc:  # 归档不可达时状态仍要能看（本地部分照常显示）
        archive_last, manifest_version = None, f"不可用：{exc}"[:200]

    # 缺口 = 「归档已覆盖的窗口内、本地却没有」的自然日。**只看「本地末日 vs 归档边界」会漏掉
    # 中间的洞**（首次回填被限流打断时就踩到：末日追平了归档，中间却空了 33 天）。
    # 台账里记为 `empty` 的日子不算缺口——那是归档当天真的没有可收内容。
    empty_days = empty_day_set(paths_)
    missing: list[dict] = []
    if archive_last:
        window_start = (
            date.fromisoformat(local_start)
            if local_start
            else archive_last - timedelta(days=BACKFILL_DAYS)
        )
        for day in plan_days(archive_last, since=window_start):
            stamp = day.isoformat()
            if stamp in empty_days or (paths_.events_dir / store.day_file_name(day)).exists():
                continue
            missing.append({"date": stamp, "trading_day": cal.is_session(day)})

    return {
        "local": {
            "start": local_start,
            "end": local_end,
            "days": len(entries),
            "rows": sum(entry.rows for entry in entries if entry.rows > 0),
            "symbols": max((entry.symbols for entry in entries), default=0),
        },
        "archive": {"last_day": archive_last.isoformat() if archive_last else None,
                    "manifest_version": manifest_version},
        "gap": {
            "missing_days": missing,
            "count": len(missing),
            "trading_day_count": sum(1 for item in missing if item["trading_day"]),
        },
        "last_run": _last_ledger(paths_),
    }


def _unchanged(day: date, shards: dict[str, archive.Shard], paths_: Paths) -> bool:
    """本地这一天已物化，且来源分片 sha 与当前归档一致 → 不必重物化。"""
    target = paths_.events_dir / store.day_file_name(day)
    sidecar = paths_.raw_dir / f"{day.isoformat()}.json"
    if not target.exists() or not sidecar.exists():
        return False
    recorded = {
        item["event_type"]: item["sha256"]
        for item in (json.loads(sidecar.read_text(encoding="utf-8")).get("shards") or [])
    }
    current = {t: s.sha256 for t, s in shards.items() if t in store.KEPT_EVENT_TYPES}
    return recorded == current


def _append_ledger(paths_: Paths, report: RunReport) -> None:
    paths_.meta_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(report), ensure_ascii=False)
    with (paths_.meta_dir / LEDGER_NAME).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def empty_day_set(paths_: Paths) -> set[str]:
    """历史台账里所有记为「该日无分片」的日期（404 或只有被排除的类型）。

    `status()` 与 M2c 体检脚本共用这一份语义：**「归档当天真没内容」不算缺口**，
    各自实现一份迟早会漂移。
    """
    empty: set[str] = set()
    ledger = paths_.meta_dir / LEDGER_NAME
    if not ledger.exists():
        return empty
    with ledger.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                # 坏行跳过而不是抛出去：本函数的职责是「哪些天被记成了 empty」，
                # 台账损坏由体检的 X3 报出——一次半截写入不该让缺口判定整个失效
                continue
            for item in (payload.get("days") or []):
                if item.get("status") == "empty":
                    empty.add(str(item.get("date")))
    return empty


def _last_ledger(paths_: Paths) -> dict | None:
    ledger = paths_.meta_dir / LEDGER_NAME
    if not ledger.exists():
        return None
    last: str | None = None
    with ledger.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                last = line
    return json.loads(last) if last else None
