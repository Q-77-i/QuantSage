#!/usr/bin/env python3
"""覆盖率闸门（P2 T4）：全局按行覆盖守 88%，核心模块再守各自底线。

为什么不用 `pytest --cov-fail-under` 一处了事：全局 88% 挡不住「整块模块归零」。
这里读 `coverage.json`，按 `covered_lines / num_statements` 判，分支覆盖只打印不设闸门
（当前综合 86.91%，把它当闸门等于把门槛悄悄降到 86）。

用法：
    uv run pytest -q --cov=app --cov-branch --cov-report=json:coverage.json ...
    uv run python scripts/check_coverage.py coverage.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

MIN_GLOBAL_LINE = 88.0

#: 模块行覆盖底线（P2 前实测往下留余量）。db_* 与 scheduler 是 ratchet：
#: 离线走 FakeDatabase，真 SQL 由定向集成覆盖；底线只防再降，不冒充质量达标。
MODULE_FLOORS: dict[str, float] = {
    "app/tasks/worker.py": 75.0,
    "app/tasks/stream.py": 78.0,
    "app/core/quota.py": 83.0,
    "app/core/coord.py": 83.0,
    "app/core/metrics.py": 72.0,
    "app/api/tasks.py": 90.0,
    "app/api/chat.py": 86.0,
    "app/api/research.py": 83.0,
    "app/core/db_tasks.py": 40.0,
    "app/core/db_research.py": 28.0,
    "app/core/db_paper.py": 31.0,
    "app/core/db_chat.py": 55.0,
    "app/memory/scheduler.py": 70.0,
}


def _line_pct(summary: dict[str, Any]) -> float:
    total = int(summary.get("num_statements") or 0)
    if total <= 0:
        return 100.0
    return 100.0 * int(summary.get("covered_lines") or 0) / total


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else "coverage.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL 读不到覆盖率 JSON：{path}（{exc}）")
        return 2

    failures: list[str] = []
    totals = payload.get("totals") or {}
    global_line = _line_pct(totals)
    print(
        "全局行覆盖 "
        f"{global_line:.2f}%（{totals.get('covered_lines', 0)}/{totals.get('num_statements', 0)} 语句；"
        f"分支 {totals.get('covered_branches', 0)}/{totals.get('num_branches', 0)}）"
    )
    if global_line < MIN_GLOBAL_LINE:
        failures.append(f"全局行覆盖 {global_line:.2f}% < {MIN_GLOBAL_LINE:.0f}%")

    files = payload.get("files") or {}
    for module, floor in MODULE_FLOORS.items():
        summary = (files.get(module) or {}).get("summary")
        if not summary:
            failures.append(f"{module}：coverage.json 里没有这个文件")
            continue
        pct = _line_pct(summary)
        print(f"  {module:36} {pct:6.2f}%  底线 {floor:.0f}%")
        if pct < floor:
            failures.append(f"{module} 行覆盖 {pct:.2f}% < {floor:.0f}%")

    if failures:
        print("\nFAIL")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
