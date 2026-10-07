"""交易日历 × 全市场行情 `trade_date` 的全期双向对账。

M2b 的日历验收：不再「抽查 1 年」，而是拿 21 片行情的**全部**交易日（2020-01-02 起）
与冻结日历双向比对——「日历有而数据无」与「数据有而日历无」都要报出来，例外逐条解释。
有差异时退出码非零，可直接当检查用。

用法：cd backend && uv run python scripts/audit_calendar.py [--adjust qfq]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import calendar as cal  # noqa: E402
from app.data import duckdb_client as dc  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adjust", default="qfq")
    args = parser.parse_args()

    # 取数走查询层（视图是 glob，全部分片自动纳入，跨年边界也在内）——不在这里另写一份
    try:
        days = dc.trade_dates(args.adjust)
    except dc.DataNotReady as exc:
        print(f"❌ {exc}")
        return 1
    result = cal.audit(days)
    first, last = result.window
    print(f"日历：{cal.describe()}")
    print(f"对账窗口：{first} → {last}（行情 {args.adjust} 分片，{result.data_days} 个交易日）")
    print(f"日历同期：{result.calendar_days} 个交易日")

    if result.ok:
        print("✅ 双向一致：日历与行情的交易日集合完全相同")
        return 0

    print(f"\n❌ 不一致：日历多 {len(result.only_in_calendar)} 天、数据多 {len(result.only_in_data)} 天")
    for day in result.only_in_calendar[:20]:
        print(f"  仅日历有：{day}")
    for day in result.only_in_data[:20]:
        print(f"  仅数据有：{day}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
