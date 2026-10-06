"""事件语料 ETL 的命令行入口：回填 / 日增量 / 看状态。

M2b 起事件语料改走**小石归档按日整片**（`xiaoshi-data` CLI），不再走 MCP 按标的拉取——
MCP 单次硬顶 500 行且不可翻页，装不下「全市场按日」（实测见 SPEC §3 M2b）。
本脚本只是 `app.etl.runner` 的薄壳：逻辑在库里，命令行只负责参数与打印。

用法：
    uv run python scripts/download_events.py                 # 日增量（最近 7 个自然日）
    uv run python scripts/download_events.py --trailing 30   # 自定义回落窗口
    uv run python scripts/download_events.py --backfill      # 一次性回填 92 天（≈ 归档保留期）
    uv run python scripts/download_events.py --status        # 看覆盖、缺口与最近一次运行
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.etl import runner  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backfill", action="store_true", help="一次性回填（默认 92 天）")
    parser.add_argument("--days", type=int, default=runner.BACKFILL_DAYS, help="回填天数（默认 92）")
    parser.add_argument("--trailing", type=int, default=runner.DEFAULT_TRAILING_DAYS, help="日增量回落窗口（自然日）")
    parser.add_argument("--refresh", action="store_true", help="即使来源分片未变也重新物化")
    parser.add_argument("--status", action="store_true", help="只打印状态，不拉数据")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(runner.status(), ensure_ascii=False, indent=2))
        return 0

    report = (
        runner.backfill(days=args.days, refresh=args.refresh)
        if args.backfill
        else runner.run("manual", trailing=args.trailing, refresh=args.refresh)
    )
    print(report.summary())
    for day in report.days:
        detail = f"rows={day.rows} symbols={day.symbols}" if day.status == "ok" else day.error
        print(f"  {day.date}  {day.status:<8} {detail}")
    return 1 if report.result == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
