"""M4a 薄壳：把一份用户策略文件送进沙箱，跑一次真实数据的回测。

    用法：
      cd backend && uv run python scripts/run_user_strategy.py --file my.py --symbol 600519
      uv run python scripts/run_user_strategy.py --file my.py --params fast=5 --params slow=20 \\
          --start 2026-08-01 --end 2026-09-30 --name 我的双均线 --json /tmp/report.json

    退出码：0 出报告 ｜ 1 策略被拒（用户要改的，带行号）｜ 2 沙箱执行层失败（配额/超时/崩溃）
             ｜ 3 参数或文件问题

**父侧只做静态的事**：解析 `PARAMS` / `USES_EVENTS`（AST，不执行代码）、校验参数、解析窗口；
用户代码一行都不在父进程里跑——与 API 走的是同一条路（M4c 直接复用 `run_user_strategy`）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.report import resolve_window  # noqa: E402
from app.backtest.engine import BacktestConfig  # noqa: E402
from app.backtest.types import BacktestError, Mode  # noqa: E402
from app.strategy import SandboxError, StrategyRejected  # noqa: E402
from app.strategy.api import parse_meta  # noqa: E402
from app.strategy.params import validate_params  # noqa: E402
from app.strategy.sandbox import run_user_strategy_sync  # noqa: E402

DEFAULT_SYMBOL = "600519"


def parse_cli_params(items: list[str]) -> dict[str, float | int | str]:
    params: dict[str, float | int | str] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise ValueError(f"--params 需要 k=v 形式：{item!r}")
        try:
            params[key] = int(raw)
        except ValueError:
            try:
                params[key] = float(raw)
            except ValueError:
                params[key] = raw  # 交给 schema 校验去报错，文案更准
    return params


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把用户策略文件送进沙箱跑一次回测")
    parser.add_argument("--file", required=True, type=Path, help="策略源码文件")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--start", default=None, help="缺省按 USES_EVENTS 定（同 API 口径）")
    parser.add_argument("--end", default=None)
    parser.add_argument("--params", action="append", default=[], metavar="K=V")
    parser.add_argument("--name", default=None, help="显示名（缺省取文件名）")
    parser.add_argument("--compare-pit", action="store_true", help="跑 PIT / 非 PIT 两遍做对比")
    parser.add_argument("--json", type=Path, default=None, help="把完整报告写到该路径")
    args = parser.parse_args(argv)

    try:
        source = args.file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读不到策略文件：{exc}")
        return 3

    name = args.name or args.file.stem
    try:
        meta = parse_meta(source)
        cli_params = parse_cli_params(args.params)
        params = validate_params(meta.params, cli_params)
    except (StrategyRejected, ValueError) as exc:
        print(f"✗ {exc}")
        return 3

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    try:
        start, end = resolve_window(args.symbol, "user", start, end, uses_events=meta.uses_events)
    except BacktestError as exc:
        print(f"✗ {exc}")
        return 3

    config = BacktestConfig(
        symbol=args.symbol, strategy="user", start=start, end=end, pit_mode=Mode.PIT, params=params
    )
    print(
        f"策略 {name}（参数 {params or '无'}，消费事件 {'是' if meta.uses_events else '否'}）"
        f"｜ {args.symbol} ｜ {start} → {end}"
    )
    try:
        outcome = run_user_strategy_sync(
            source,
            config=config,
            strategy_name=name,
            uses_events=meta.uses_events,
            compare_pit=args.compare_pit,
        )
    except StrategyRejected as exc:
        print(f"✗ 策略被拒{f'（第 {exc.line} 行）' if exc.line else ''}：{exc}")
        return 1
    except SandboxError as exc:
        print(f"✗ 沙箱[{exc.kind}]：{exc}")
        return 2

    report = outcome.report
    metrics = report["metrics"]
    print(
        f"✓ 跑通：{report['meta']['bars']} 根 bar ｜ 成交 {metrics['trade_count']} 笔 ｜ "
        f"总收益 {metrics['total_return']:.2%} ｜ 最大回撤 {metrics['max_drawdown']:.2%} ｜ "
        f"期末权益 {metrics['final_equity']:,.2f} ｜ 耗时 {outcome.duration_s:.2f}s"
    )
    if report["pit_comparison"]:
        delta = report["pit_comparison"]["delta"]
        print(f"  PIT 对比：非 PIT 期末权益差 {delta['final_equity_abs']:+,.2f} 元")
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  完整报告：{args.json}")
    if outcome.stderr_tail:
        print(f"  策略 stderr（截断）：{outcome.stderr_tail[:400]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
