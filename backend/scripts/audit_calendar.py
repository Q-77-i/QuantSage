"""交易日历 × 全市场行情 `trade_date` 的全期双向对账。

M2b 的日历验收：不再「抽查 1 年」，而是拿 21 片行情的**全部**交易日（2020-01-02 起）
与冻结日历双向比对——「日历有而数据无」与「数据有而日历无」都要报出来，例外逐条解释。
有差异时退出码非零，可直接当检查用。

用法：cd backend && uv run python scripts/audit_calendar.py [--adjust qfq]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from app.core.config import REPO_ROOT  # noqa: E402
from app.data import calendar as cal  # noqa: E402


def trade_dates(adjust: str = "qfq", data_dir: Path | None = None) -> list[date]:
    """行情分片里出现过的全部交易日（去重、升序）。

    只取一根分片年份的表不够——跨年边界正是要验的地方，故对全部年份分片取并集。
    """
    base = Path(data_dir) if data_dir is not None else REPO_ROOT / "data"
    pattern = base / "bars" / f"cn-daily_CN_{adjust}_*.parquet"
    files = sorted(pattern.parent.glob(pattern.name))
    if not files:
        raise SystemExit(f"没有行情分片：{pattern}")
    con = duckdb.connect()
    try:
        sql = " UNION ".join(
            f"SELECT DISTINCT trade_date FROM read_parquet('{path}')" for path in files
        )
        rows = con.execute(f"SELECT trade_date FROM ({sql}) ORDER BY 1").to_arrow_table().to_pylist()
    finally:
        con.close()
    return [row["trade_date"] for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adjust", default="qfq")
    args = parser.parse_args()

    days = trade_dates(args.adjust)
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
