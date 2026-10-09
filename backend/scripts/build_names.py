"""名称字典重建的薄壳：全量重建 / 抽查。

逻辑全在 `app/data/naming.py`，这里只做参数与输出。ETL 每轮结束会自动重建一次
（`runner.refresh_names`），本脚本用于**首次生成**、排查与人工核对。

```
# 重建（默认，约 1.3s）
python scripts/build_names.py

# 只看不写：打印覆盖率与一名多写清单，用来核对规则有没有跑偏
python scripts/build_names.py --dry-run

# 查一个标的（可重复）
python scripts/build_names.py --show 688347 --show 301699
```

退出码：0 正常；1 出错。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import naming  # noqa: E402


def _coverage(rows: list[naming.NameRow], data_dir: Path | None) -> None:
    from app.data import duckdb_client as dc

    con = dc.connect(data_dir)
    try:
        universe = {
            row["symbol"]
            for row in con.execute(f"SELECT DISTINCT symbol FROM {dc.BARS_VIEW}")
            .to_arrow_table()
            .to_pylist()
        }
    finally:
        con.close()
    primary = naming.primary_names(rows)
    named = len(universe & set(primary))
    print(f"行情宇宙 {len(universe)} 只 ｜ 有名称 {named} = {named / len(universe):.1%}")
    if missing := sorted(universe - set(primary))[:10]:
        print(f"  无名称（前 10）：{', '.join(missing)}")


def _conflicts(rows: list[naming.NameRow], limit: int = 20) -> None:
    """别名不止一个的标的——本项目的负样例清单，**如实打印，不藏着**。"""
    by_symbol: dict[str, list[naming.NameRow]] = {}
    for row in rows:
        by_symbol.setdefault(row.symbol, []).append(row)
    conflicts = {symbol: items for symbol, items in by_symbol.items() if len(items) > 1}
    print(f"一名多写 {len(conflicts)} 只")
    for symbol, items in sorted(conflicts.items())[:limit]:
        rendered = "、".join(f"{row.name}({row.count})" for row in sorted(items, key=lambda r: -r.count))
        print(f"  {symbol}: {rendered}")
    if len(conflicts) > limit:
        print(f"  …另有 {len(conflicts) - limit} 只")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="名称字典重建（M5a）")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认取配置）")
    parser.add_argument("--dry-run", action="store_true", help="只统计与打印，不落盘")
    parser.add_argument("--show", action="append", default=[], help="查某标的的全部别名，可重复")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else None
    try:
        rows = naming.build_from_events(data_dir)
    except Exception as exc:  # noqa: BLE001 - CLI 边界，打印比抛栈友好
        print(f"✗ 构建失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    _coverage(rows, data_dir)
    _conflicts(rows)

    for symbol in args.show:
        items = sorted(
            (row for row in rows if row.symbol == symbol),
            key=lambda row: -row.count,
        )
        primary = naming.primary_names(items).get(symbol) if items else None
        print(f"  {symbol} → 当前名 {primary!r}")
        for row in items:
            print(
                f"      {row.name}({row.count})  "
                f"{row.first_seen.date()} → {row.last_seen.date()}"
            )

    if args.dry_run:
        print("（--dry-run：未落盘）")
        return 0

    target = naming.write_dictionary(rows, data_dir)
    print(f"✅ 已写入 {target}（{target.stat().st_size / 1024:.0f}KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
