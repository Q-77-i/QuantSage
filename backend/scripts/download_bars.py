"""P2-M2d 全市场行情：按需刷新 / 本地重物化 / 看状态。**逻辑全在 `app.data.bars`**——
本脚本只是薄壳，只负责参数与打印（对齐 `scripts/download_events.py ↔ app.etl.runner`）。

行情是**年度整片、内容寻址**：`cn-daily` 的源侧维度是 `{year, adjust}`，没有 `date`
——所以没有「增量行情」可拉，能做到的是「按需整片重下」（详见 SPEC §3 M2d）。

    data/bars/cn-daily_CN_{adjust}_{year}.parquet     # 21 片 = 7 年 × 3 复权

用法：
    uv run python scripts/download_bars.py                    # 按需刷新（默认，带两级门禁）
    uv run python scripts/download_bars.py --force            # 不看门禁，跑满 21 次下载会话
    uv run python scripts/download_bars.py --skip-download    # 用本地 data/raw 重物化（零网络会话）
    uv run python scripts/download_bars.py --status           # 只看状态，不发请求

`--force` **不等于**「一定重写 `data/bars/`」：跑完 21 次会话若分片 sha 与本地指纹全同，
仍然不动物化（那是同一批字节，重写只是换 mtime）。要「用本地分片重建产物」用
`--skip-download`。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import bars  # noqa: E402

#: 集成用例（`tests/integration/test_data_t2.py`）按这个名字从这里取分片契约，
#: 搬到 `app.data.bars` 之后必须继续可从本脚本导入——**迁移期别删这五个名字**。
from app.data.bars import (  # noqa: F401  (re-export)
    ADJUSTS,
    BARS_NAME,
    LEGACY_PATTERN,
    PARTITIONS,
    YEARS,
)
from app.data.data_lock import Busy  # noqa: E402


def _print_status() -> int:
    state = bars.read_sync_state()
    last = bars.last_run()
    print(json.dumps({"sync": asdict(state), "last_run": last}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-download", action="store_true", help="复用已有 data/raw/ 分片重物化（零网络会话）")
    parser.add_argument("--force", action="store_true", help="不看门禁，跑满 21 次下载会话")
    parser.add_argument("--status", action="store_true", help="只打印门禁记忆与最近一次运行，不发请求")
    parser.add_argument("--budget-seconds", type=int, default=None, help="整轮预算（默认取配置，1200s）")
    parser.add_argument("--raw-dir", type=Path, default=None)
    parser.add_argument("--bars-dir", type=Path, default=None)
    parser.add_argument("--meta-dir", type=Path, default=None)
    args = parser.parse_args()

    if args.status:
        return _print_status()

    try:
        if args.skip_download:
            outputs = bars.rebuild(args.raw_dir, args.bars_dir, args.meta_dir)
            total = sum(item["rows"] for item in outputs)
            print(f"\n重物化 {len(outputs)} 片，共 {total} 行（零网络会话）")
            return 0
        report = bars.refresh(force=args.force, budget_seconds=args.budget_seconds)
    except Busy as exc:
        print(f"跳过：{exc}")
        return 1
    print(f"\n{report.summary()}")
    if report.error:
        print(f"  {report.error}")
    return 1 if report.status in {"failed", "rate_limited", "incomplete"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
