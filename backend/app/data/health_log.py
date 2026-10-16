"""体检落档（E3a）——体检结果的家：`_meta/health_runs.jsonl`（台账）+ `_meta/health_last.json`（全文）。

**为什么落数据目录而不是 DB**：体检是**数据层自己的护栏**，它恰恰要在库挂了时也能跑
（`data_health.run_all()` 不碰 Postgres）。体例照 `_meta/etl_runs.jsonl`——同一个目录、
同样一行一轮、同样「坏行跳过不抛」。

**摘要与全文分家**（本模块存在的核心理由）：

* `health_runs.jsonl` 一行一轮，只放**摘要级 checks**（id / level / title）——台账要能长年
  只增不删（一行约 200 字节，一年 ≈ 73KB），而 detail 里写着行数、标的数、路径，会随数据长。
* `health_last.json` 覆盖写，放当次**全文**（含 detail 与退市核实）——「最近一次到底怎么
  了」的唯一去处，看板的首屏读它。

**被写锁挡下的一轮也要落行**（`append_skipped`）：否则「今天怎么没跑」在台账里查不到，
正是 M2c 那条「台账的 `failed` 不等于缺数据」教训的同族。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.backtest.types import CN_TZ  # 最底层、不 import 包内任何模块（见其 docstring）
from app.core.config import REPO_ROOT

if TYPE_CHECKING:  # 只为类型标注——本模块不依赖检查逻辑，反过来也一样
    from app.data.data_health import Check

RUNS_NAME = "health_runs.jsonl"
LAST_NAME = "health_last.json"


def runs_path(data_dir: Path | None = None) -> Path:
    return _meta_dir(data_dir) / RUNS_NAME


def last_path(data_dir: Path | None = None) -> Path:
    return _meta_dir(data_dir) / LAST_NAME


def _meta_dir(data_dir: Path | None) -> Path:
    base = Path(data_dir) if data_dir is not None else REPO_ROOT / "data"
    return base / "_meta"


def _stamp(at: datetime | None) -> datetime:
    """落盘时刻一律北京时间。**注入的裸 datetime 按本模块的既有口径当北京时间**——
    本模块输出的每一个时刻都是 CST，裸值在这里只可能指同一件事（写死比猜测安全）。"""
    if at is None:
        return datetime.now(CN_TZ)
    return at if at.tzinfo is not None else at.replace(tzinfo=CN_TZ)


def brief(check: Check) -> dict:
    """摘要级一条——**只有 id / level / title**，detail 刻意不进台账。"""
    return {"id": check.id, "level": check.level, "title": check.title}


def count_levels(checks: Iterable[Check]) -> dict[str, int]:
    counts = {"error": 0, "warn": 0, "info": 0}
    for check in checks:
        counts[check.level] = counts.get(check.level, 0) + 1
    return counts


def record(
    data_dir: Path | None,
    *,
    source: str,
    checks: list[Check],
    duration_ms: int,
    at: datetime | None = None,
    delisting: dict | None = None,
    exit_code: int = 0,
) -> dict[str, Any]:
    """落一轮体检：台账追加一行 + 全文覆盖写。返回那一行。"""
    stamp = _stamp(at)
    counts = count_levels(checks)
    row: dict[str, Any] = {
        "at": stamp.isoformat(),
        "duration_ms": duration_ms,
        "source": source,
        "exit_code": exit_code,
        **counts,
        "checks": [brief(c) for c in checks],
    }
    _append_row(data_dir, row)
    write_last(
        data_dir,
        {
            "at": row["at"],
            "duration_ms": duration_ms,
            "source": source,
            "exit_code": exit_code,
            **counts,
            "checks": [
                {"id": c.id, "level": c.level, "title": c.title, "detail": c.detail, "data": c.data}
                for c in checks
            ],
            **({"delisting": delisting} if delisting is not None else {}),
        },
    )
    return row


def append_skipped(data_dir: Path | None, *, source: str, reason: str, at: datetime | None = None) -> dict:
    """被写锁挡下的一轮——**只留痕，不记计数**（没跑就没有 error/warn/info 可记）。"""
    row = {"at": _stamp(at).isoformat(), "source": source, "skipped": reason}
    _append_row(data_dir, row)
    return row


def _append_row(data_dir: Path | None, row: dict) -> None:
    path = runs_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_last(data_dir: Path | None, payload: dict) -> Path:
    """覆盖写全文。**先写 .partial 再 `os.replace`**——与行情分片的换入同规，
    读者（看板）永远看不到半截 JSON。"""
    target = last_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".partial")
    staging.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(staging, target)
    return target


def read_runs(data_dir: Path | None = None, *, limit: int = 50, offset: int = 0) -> tuple[int, list[dict]]:
    """台账的 `(总行数, 本页行)`，**新的在前**。坏行与空行跳过不抛（同 X3 口径）。"""
    path = runs_path(data_dir)
    if not path.is_file():
        return 0, []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.reverse()
    return len(rows), rows[offset : offset + limit]


def read_last(data_dir: Path | None = None) -> dict | None:
    """最近一次的全文；没跑过或文件坏了都返回 None（「还没跑过」是答案不是错误）。"""
    path = last_path(data_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
