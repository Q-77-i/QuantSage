"""交易日历生成：`exchange_calendars` 的 XSHG 日历 → 冻结为仓库内数据文件。

**只在生成时依赖该库**（dev 依赖），运行期零依赖：ETL 定时任务与回测撮合都读冻结文件
`app/data/trading_calendar.json`。这样换库、换版本都不会在运行期引发口径漂移，
而重生成只需重跑本脚本（来源与版本记进文件头，可追溯）。

用法：cd backend && uv run python scripts/generate_calendar.py [--start 2015-01-01] [--end 2030-12-31]

区间取 2015→2030：下界盖住本仓库全部行情（2020 起）并留出对账余量，
上界要覆盖 ETL 未来的运行期（日历是「未来某天是不是交易日」的判据，必须比今天远）。
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

import exchange_calendars  # type: ignore[import-untyped]

TARGET = Path(__file__).resolve().parents[1] / "app" / "data" / "trading_calendar.json"
CALENDAR = "XSHG"
SOURCE = "https://github.com/gerrymanoim/exchange_calendars"
#: 文件头声明版本，便于日后判断是否需要重生成（交易所临时休市/调休会改日历）
SCHEMA = "quantsage.trading_calendar/v1"

DEFAULT_START = "2015-01-01"
#: 上界默认取库的实际边界：中国假期逐年公布，库不预测未来年份
#: （实测 4.13.2 的 XSHG 假期只记到 2026 年底，写 2030 会直接抛 DateOutOfBounds）
DEFAULT_END = "2026-12-31"


def build(start: str, end: str) -> dict:
    cal = exchange_calendars.get_calendar(CALENDAR)
    # 库的边界就是硬边界：请求超出时**截断并如实记录**，不假装覆盖到请求的那一天
    bound_start, bound_end = cal.first_session.date().isoformat(), cal.last_session.date().isoformat()
    effective_start = max(start, bound_start)
    effective_end = min(end, bound_end)
    if (effective_start, effective_end) != (start, end):
        print(f"⚠ 请求区间 {start} → {end} 超出库边界，实际生成 {effective_start} → {effective_end}")
    sessions = cal.sessions_in_range(effective_start, effective_end)
    return {
        "schema": SCHEMA,
        "calendar": CALENDAR,
        "generator": f"exchange_calendars=={exchange_calendars.__version__}",
        "source": SOURCE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requested_range": {"start": start, "end": end},
        "range": {"start": effective_start, "end": effective_end},
        "count": len(sessions),
        "sessions": [day.date().isoformat() for day in sessions],
        "note": (
            "运行期零依赖：只读本文件。重生成见 scripts/generate_calendar.py。"
            f"上界受库的假期记录限制（{CALENDAR} 记到 {bound_end}），到期必须重生成——"
            "`app/data/calendar.py` 对越界日期显式报错，不静默当作非交易日"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--output", type=Path, default=TARGET)
    args = parser.parse_args()

    payload = build(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    first, last = payload["sessions"][0], payload["sessions"][-1]
    print(f"{CALENDAR} {args.start} → {args.end}：{payload['count']} 个交易日（{first} … {last}）")
    print(f"生成器 {payload['generator']}；落盘 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
