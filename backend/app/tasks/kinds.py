"""任务分类常量：`grid` / `batch` 支持协作式中止与逐格进度。

单独成模块，让 `tasks/stream.py` 能共用它，而不必为两个字符串 import 整条回测/沙箱
依赖链（`tasks/executors.py` 会拉入 `backtest.batch`）。
"""

from __future__ import annotations

COOPERATIVE_KINDS = frozenset({"grid", "batch"})
