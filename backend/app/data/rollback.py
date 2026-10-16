"""按快照回滚（E3b）——SPEC §4.5 的六步。

**这是全仓唯一会主动把 `data/` 换回历史状态的模块**，所以三条护栏一步不少：

1. **先验证、缺片即整次拒绝**（`SnapshotIncomplete`）——不做半截回滚：一半旧一半新比不动更糟；
2. **回滚前自动留当前快照**——它本身不可撤销，先给自己一个退路；
3. **只做整快照**——「把 qfq 退回去但 raw 不动」会让同一份数据的复权轴自相矛盾，那种状态
   没有任何口径能解释。

admin 门与 `confirm` 字面比对在 `api/data.py`；**界面上那个确认框不是门**（SPEC §4.5）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.core.config import REPO_ROOT
from app.data import snapshots
from app.data.data_lock import LOCK, Busy
from app.etl import store as etl_store
from app.etl.archive import Shard

CHANNELS = ("bars", "events")


class SnapshotUnknown(ValueError):
    """快照 ts 不在索引里。"""


class SnapshotIncomplete(RuntimeError):
    """目标快照有对象不在本地库里——整次拒绝（`missing` 是 sha，调用方负责截断展示）。"""

    def __init__(self, missing: list[str], channel: str) -> None:
        self.missing = missing
        self.channel = channel
        super().__init__(f"{channel} 缺 {len(missing)} 个对象：{[sha[:12] for sha in missing[:5]]}")


@dataclass(frozen=True, slots=True)
class Plan:
    """验证过的一份回滚计划——`entries` 已是「按 sha 定位到的本地对象」。"""

    ts: str
    #: (本地对象, 目标文件名)
    bars: tuple[tuple[Path, str], ...]
    #: (日期, 该日的来源分片)
    events: tuple[tuple[date, dict[str, Shard]], ...]


def object_path(raw_dir: Path, sha: str) -> Path:
    """按 sha 定位本地对象：`raw/o/<sha[:2]>/<sha>.parquet`。

    ⚠ **全仓唯一一处自己拼对象路径的地方**——`etl/archive.py:139` 明写「内容寻址的目录
    布局是 CLI 的实现细节，不是我们的契约」。回滚必须破这个例，理由是**没有别的办法**：
    清单里记的是 `object_key` + `sha256`，而回执（唯一记着 `path` 的地方）**每次 CLI 调用
    都被累积重写、跨发布版本会丢旧条目** ⇒ 要还原一份旧快照，只能按 sha 去库里找。

    布局是**实测过的**：2026-10-16 抽全部 57 条回执条目，`path` 逐条等于本式（0 条不符）。
    **找不到就是找不到**（`SnapshotIncomplete`），不做任何猜测式兜底——源侧若改了布局，
    回滚会整次拒绝，而不是悄悄还错东西。
    """
    return raw_dir / "o" / sha[:2] / f"{sha}.parquet"


def plan(data_dir: Path | None = None, *, ts: str, channels: tuple[str, ...] = CHANNELS) -> Plan:
    """第 1 步：先验证。**缺任一片即抛 `SnapshotIncomplete`**（不返回半份计划）。"""
    base = _base(data_dir)
    if snapshots.find(base, ts) is None:
        raise SnapshotUnknown(f"没有 ts={ts} 这份快照")
    raw = base / "raw"

    bars_entries: list[tuple[Path, str]] = []
    if "bars" in channels:
        manifest = snapshots.manifest(base, ts, "bars")
        if manifest is not None:
            outputs = manifest.get("outputs") or []
            missing = [out["sha256"] for out in outputs if not object_path(raw, out["sha256"]).is_file()]
            if missing:
                raise SnapshotIncomplete(missing, "bars")
            bars_entries = [(object_path(raw, out["sha256"]), Path(out["path"]).name) for out in outputs]

    events_entries: list[tuple[date, dict[str, Shard]]] = []
    if "events" in channels:
        manifest = snapshots.manifest(base, ts, "events")
        if manifest is not None:
            days = manifest.get("days") or []
            missing = [
                shard["sha256"]
                for day in days
                for shard in day.get("shards") or []
                if not object_path(raw, shard["sha256"]).is_file()
            ]
            if missing:
                raise SnapshotIncomplete(missing, "events")
            events_entries = [(date.fromisoformat(day["date"]), _shards_of(raw, day)) for day in days]

    return Plan(ts=ts, bars=tuple(bars_entries), events=tuple(events_entries))


def execute(
    data_dir: Path | None = None,
    *,
    ts: str,
    channels: tuple[str, ...] = CHANNELS,
) -> dict[str, Any]:
    """六步走完并返回结论。**与 ETL / 行情刷新串行**（同一把进程内锁，撞上就拒）。"""
    base = _base(data_dir)
    if not LOCK.acquire(blocking=False):
        raise Busy("已有一次数据同步在执行（回滚与 ETL / 行情刷新必须串行）")
    try:
        return _execute_locked(base, ts=ts, channels=channels)
    finally:
        LOCK.release()


def _execute_locked(base: Path, *, ts: str, channels: tuple[str, ...]) -> dict[str, Any]:
    job = plan(base, ts=ts, channels=channels)  # ① 先验证
    # ② 回滚前自动留退路——它自己不可撤销，先给一个「刚才是什么样」
    snapshots.take(base, scope="rollback", reason=f"回滚到 {ts} 之前的自动留档")
    restored: dict[str, int] = {}
    if job.bars:
        restored["bars"] = _restore_bars(base, ts, job)
    if job.events:
        restored["events"] = _restore_events(base, ts, job)
    # ⑤ 回滚后的状态当场可见，不靠人记得再跑一次体检
    health = _rerun_health(base)
    return {"ok": True, "snapshot": ts, "restored": restored, "health": health}


def _restore_bars(base: Path, ts: str, job: Plan) -> int:
    """逐片按 sha 取对象 → 暂存 → 校验 → 整体换入 → 清单写回快照那一份。

    暂存用**子目录**而非 `.partial.parquet`（同 `bars.materialize` 的理由：后者会被
    `*.parquet` glob 捞到，半截文件会被当成正常分片读进去）。**边拷边算 sha**——
    校验不额外读一遍盘，且文件名声明不作数（测量不是记录）。
    """
    bars_dir = base / "bars"
    bars_dir.mkdir(parents=True, exist_ok=True)
    staging = bars_dir / "_staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        for source, name in job.bars:
            _copy_verified(source, staging / name)
        # 快照里**没有**的那些片要删掉：留着就是「目标状态之外的数据也在查询里」
        keep = {name for _, name in job.bars}
        for extra in bars_dir.glob("*.parquet"):
            if extra.name not in keep:
                extra.unlink()
        for staged in sorted(staging.glob("*.parquet")):
            os.replace(staged, bars_dir / staged.name)
        staging.rmdir()
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _write_manifest(base, ts, "bars")
    return len(job.bars)


def _restore_events(base: Path, ts: str, job: Plan) -> int:
    """逐日按清单里的来源分片 sha 重新组装——**走既有的 `materialize_day`**，不另写一份合并逻辑
    （口径只有一处：收哪些类型、怎么归一 `symbols`、落盘校验什么，全在 `etl/store.py`）。"""
    events_dir = base / "events"
    raw_dir = base / "raw"
    keep: set[str] = set()
    for day, shards in job.events:
        keep.add(etl_store.materialize_day(day, shards, events_dir, raw_dir).file)
    for extra in events_dir.glob(f"{etl_store.DAY_PREFIX}*.parquet"):
        if extra.name not in keep:
            extra.unlink()
    _write_manifest(base, ts, "events")
    return len(job.events)


def _rerun_health(base: Path) -> dict[str, Any]:
    """回滚后的当场体检（`source="rollback"`）。

    **不复用调度器那条路**：它要抢同一把锁，而回滚正握着；语义也不同——这是回滚的一部分，
    不是定时的一轮（台账里的 `source` 要分得开）。
    """
    from app.data import data_health, delisting, health_log
    from app.etl.scheduler import _delisting_payload

    started = time.monotonic()
    checks = data_health.run_all(data_dir=base)
    try:
        payload = _delisting_payload(delisting.run(base))
    except Exception:  # noqa: BLE001 —— 退市核实挂了不该让回滚的结论没有体检读数
        payload = None
    row = health_log.record(
        base,
        source="rollback",
        checks=checks,
        duration_ms=int((time.monotonic() - started) * 1000),
        delisting=payload,
        exit_code=data_health.exit_code(checks),
    )
    return {
        "error": row["error"],
        "warn": row["warn"],
        "info": row["info"],
        "status": data_health.overall_status(checks),
    }


def _shards_of(raw: Path, day: dict) -> dict[str, Shard]:
    """把清单里记的来源分片还原成 `Shard` 对象（`materialize_day` 要的就是它）。"""
    return {
        shard["event_type"]: Shard(
            event_type=shard["event_type"],
            object_key=shard.get("object_key") or "",
            path=object_path(raw, shard["sha256"]),
            sha256=shard["sha256"],
            size=int(shard.get("size") or 0),
        )
        for shard in day.get("shards") or []
    }


def _copy_verified(source: Path, target: Path) -> None:
    """字节级复制并**当场核对 sha**（不该被 DuckDB 重写——重写会换压缩参数、sha 必然变，
    而 sha 正是这份快照的身份）。"""
    digest = hashlib.sha256()
    with source.open("rb") as src, target.open("wb") as dst:
        while chunk := src.read(1 << 20):
            digest.update(chunk)
            dst.write(chunk)
    if digest.hexdigest() != source.stem:
        target.unlink(missing_ok=True)
        raise SnapshotIncomplete([source.stem], "bars(sha 不符)")


def _write_manifest(base: Path, ts: str, channel: str) -> None:
    """清单写回**快照里那一份**（不是重新生成）——快照的定义就是「当时清单长什么样」。"""
    shutil.copy2(
        snapshots.dir_for(base, ts) / snapshots.MANIFEST_NAMES[channel],
        base / "_meta" / snapshots.MANIFEST_NAMES[channel],
    )


def _base(data_dir: Path | None) -> Path:
    return Path(data_dir) if data_dir is not None else REPO_ROOT / "data"
