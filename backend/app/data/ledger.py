"""数据目录的运行台账：`data/_meta/etl_runs.jsonl`，一行一轮。

**两条通道写同一个文件、同一个格式**（每轮一行 JSON）：事件语料（`etl/runner.py` 的
`RunReport`）与行情整片（`data/bars.py` 的 `BarsReport`）。行与行之间**字段不同是正常的**
——体检 X3 的口径就是「宽松解析、缺键容忍」（schema 已经演进过）。

谁在读它：
  * 体检 **X3**（逐行可解析）、**X6**（从最后一条带 `archive_last` 的行看归档停更）
    ——都**只在本地读**，不联网（E7 的教训：别在体检里调 xiaoshi CLI）；
  * `/api/v1/etl/status` 的 `last_run` 与 `last_bars_run`。

**按 scope 过滤**：行情那路用 `BARS_SCOPE`，事件的 `last_run` 必须排除它——否则状态页
会把「行情刷新」显示成「最近一次语料 ETL」。
"""

from __future__ import annotations

import json
from pathlib import Path

LEDGER_NAME = "etl_runs.jsonl"
#: 行情刷新那一路的 scope（`BarsReport.scope` 固定用它）
BARS_SCOPE = "bars-refresh"


def path(meta_dir: Path) -> Path:
    return Path(meta_dir) / LEDGER_NAME


def append(meta_dir: Path, payload: dict) -> Path:
    """追加一行。**半截写入的风险由 X3 承担**（坏行跳过而不是让整份台账失效）。"""
    target = path(meta_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return target


def last(meta_dir: Path, *, scope: str | None = None, exclude: str | None = None) -> dict | None:
    """台账最后一行（可按 scope 取或排除）。

    `scope=None` 且 `exclude=None` 时返回任意通道的最后一行。坏行跳过——台账损坏是
    体检 X3 的职责，不该让「看最近一次跑成什么样」跟着一起失效。
    """
    target = path(meta_dir)
    if not target.exists():
        return None
    found: dict | None = None
    with target.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if scope is not None and entry.get("scope") != scope:
                continue
            if exclude is not None and entry.get("scope") == exclude:
                continue
            found = entry
    return found
