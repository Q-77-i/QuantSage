"""数据快照（E3b）——**存的是指针，不是字节**。

```
data/_snapshots/index.jsonl        # 一行一份快照的索引（新的在后面）
data/_snapshots/<ts>/bars.json     # 当次 _meta/bars.json 的副本
data/_snapshots/<ts>/events.json   # 当次 _meta/events.json 的副本
```

**为什么这样就够**（E3 规格 §4.4，实测支撑）：`_meta/*.json` 里逐片 / 逐日的 `sha256`
就是「指针」，而**字节早在 `data/raw/o/` 的内容寻址对象库里**——实测当前 21 片行情 +
404 条事件来源分片按 sha 寻址**全部命中**，且**被替换掉的旧版本仍然在**（10-05 的 qfq 旧片
与 10-13 重算的新片并存）。所以快照要补的缺口不是空间，是**指针**：两份清单都是单槽覆盖写，
ETL 台账只记分片**文件名**不记 sha ⇒ 旧版本的清单无处可寻。本模块就是把清单按时间存下来。

**不做全量拷贝**：一份快照 ≈ 170KB（bars 16KB + events 153KB），留 20 份 ≈ 3.4MB；真拷数据
就是每份 888MB，那不叫快照叫镜像。

**触发点**（调用方负责，见 `bars.py` / `etl/runner.py`）：清单被改写的每一处——行情换入后、
语料每轮写完清单后。只读的动作不打（体检不改数据）。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.backtest.types import CN_TZ
from app.core.config import REPO_ROOT, get_settings

DIRNAME = "_snapshots"
INDEX_NAME = "index.jsonl"
MANIFEST_NAMES = {"bars": "bars.json", "events": "events.json"}
#: 触发者。落进索引的 `scope`，回滚与排查时靠它认「这份是哪种动作留下的」
SCOPES = ("bars-refresh", "bars-rebuild", "daily", "backfill", "rematerialize", "rollback")


def root(data_dir: Path | None = None) -> Path:
    return _base(data_dir) / DIRNAME


def index_path(data_dir: Path | None = None) -> Path:
    return root(data_dir) / INDEX_NAME


def dir_for(data_dir: Path | None, ts: str) -> Path:
    return root(data_dir) / ts


def _base(data_dir: Path | None) -> Path:
    return Path(data_dir) if data_dir is not None else REPO_ROOT / "data"


def take(
    data_dir: Path | None = None,
    *,
    scope: str,
    reason: str,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """把当前两份清单抄一份进 `_snapshots/`，追加索引行并顺手滚动保留份数。

    两种情形返回 `None` 且**不落任何东西**：

    * 两份清单都不在（新数据目录 / 还没落过盘）——空快照回滚起来是灾难（它会把一个空目录
      当成「目标状态」）；
    * 两份清单与**最新那份快照逐字节相同**——状态没变，再记一份只是占窗口（见 `_same_as_newest`）。
    """
    base = _base(data_dir)
    present = {
        channel: base / "_meta" / name
        for channel, name in MANIFEST_NAMES.items()
        if (base / "_meta" / name).is_file()
    }
    if not present:
        return None

    stamp = _stamp(at)
    ts = _unique_ts(base, stamp)
    staging = root(base) / f".{ts}.partial"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for channel, source in present.items():
            shutil.copy2(source, staging / MANIFEST_NAMES[channel])
        if _same_as_newest(base, staging):
            # 状态与上一份逐字节相同 ⇒ 再记一份只是占窗口。
            # 实测支撑：跑一轮集成套件会触发 11 次空转 catchup（7 天进出、清单一个字节没变），
            # 11/20 的窗口就这么没了——真快照会被挤出去。**幂等靠内容，不靠调用方自觉。**
            shutil.rmtree(staging, ignore_errors=True)
            return None
        os.replace(staging, dir_for(base, ts))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    moment = at or datetime.now(CN_TZ)
    row: dict[str, Any] = {
        "ts": ts,
        "at": (moment if moment.tzinfo is not None else moment.replace(tzinfo=CN_TZ)).isoformat(),
        "scope": scope,
        "reason": reason,
        **{channel: _summarize(channel, source) for channel, source in present.items()},
        **{channel: None for channel in MANIFEST_NAMES if channel not in present},
    }
    _append_row(base, row)
    prune(base)
    return row


def read_index(data_dir: Path | None = None) -> list[dict]:
    """索引，**新的在前**。坏行跳过不抛（同台账的既有口径）。"""
    path = index_path(data_dir)
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.reverse()
    return rows


def find(data_dir: Path | None, ts: str) -> dict | None:
    return next((row for row in read_index(data_dir) if row["ts"] == ts), None)


def manifest(data_dir: Path | None, ts: str, channel: str) -> dict | None:
    """读快照里那一份清单；没有返回 `None`（那一路上次没打）。"""
    path = dir_for(data_dir, ts) / MANIFEST_NAMES[channel]
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"快照 {ts} 的 {channel} 清单坏了：{exc}") from exc


def prune(data_dir: Path | None = None, *, keep: int | None = None) -> list[str]:
    """滚动保留最近 N 份，返回被删掉的 ts。**目录与索引一起删**——只删索引会留下孤儿目录。"""
    limit = keep if keep is not None else get_settings().snapshot_keep
    rows = read_index(data_dir)  # 新的在前
    survivors = {row["ts"] for row in rows[:limit]}
    dropped = [row["ts"] for row in rows[limit:]]
    for ts in dropped:
        shutil.rmtree(dir_for(data_dir, ts), ignore_errors=True)
    if dropped:
        _rewrite_index(data_dir, [row for row in rows if row["ts"] in survivors])
    return dropped


def _summarize(channel: str, path: Path) -> dict:
    """索引里的那一行摘要——只取**判身份**要用的三个数，不抄整份清单。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if channel == "bars":
        fingerprint = payload.get("fingerprint") or {}
        return {
            "shards": len(fingerprint.get("shards") or []),
            "manifest_version": fingerprint.get("receipt_manifest_version") or None,
            "rows": sum(int(out.get("rows") or 0) for out in payload.get("outputs") or []),
        }
    fingerprint = payload.get("fingerprint") or {}
    return {
        "days": len(payload.get("days") or []),
        "digest": fingerprint.get("digest") or None,
        "rows": int(fingerprint.get("rows") or 0),
    }


def _same_as_newest(base: Path, staging: Path) -> bool:
    """staging 里的两份清单与**最新那份快照**逐字节相同吗。

    只比同名文件；一路有、另一路没有即视为不同（那正是「语料刚开始落盘」这类真变化）。
    最新那份快照的目录若已被删（人手工清过），按「不同」处理——宁可多记一份，也不要
    因为找不到参照就把一次真变化吞掉。
    """
    rows = read_index(base)
    if not rows:
        return False
    newest = dir_for(base, rows[0]["ts"])
    if not newest.is_dir():
        return False
    names = {path.name for path in staging.iterdir()}
    if names != {path.name for path in newest.iterdir()}:
        return False
    return all((staging / name).read_bytes() == (newest / name).read_bytes() for name in names)


def _stamp(at: datetime | None) -> str:
    moment = at or datetime.now(CN_TZ)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _unique_ts(base: Path, stamp: str) -> str:
    """同一秒内连打两份（`refresh` 之后紧跟一次 `rebuild` 是真实路径）不能互相覆盖。"""
    candidate, n = stamp, 1
    while dir_for(base, candidate).exists():
        n += 1
        candidate = f"{stamp}-{n}"
    return candidate


def _append_row(base: Path, row: dict) -> None:
    path = index_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _rewrite_index(base: Path, rows: list[dict]) -> None:
    """保留下来的那些按**索引原有的顺序**（旧→新）写回，与追加语义一致。"""
    path = index_path(base)
    ordered = sorted(rows, key=lambda row: row["ts"])
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ordered), encoding="utf-8"
    )
