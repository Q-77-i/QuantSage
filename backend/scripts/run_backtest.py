"""T4 验收脚本：跑固定矩阵，让 SPEC §4 的三条验收判据肉眼可验证。

    ① 两策略均可跑通          → 打印各行的成交/平仓笔数
    ② 手续费/滑点开关有可见影响 → 同策略「成本全开 / 只关滑点 / 全关」三行对比
    ③ PIT 与非 PIT 存在差异     → event_driven 两种模式对比入场日序列与期末权益

默认标的 600519（旗舰标的，与 T2 验收同源）；--symbol 300750 的 PIT 差异更显著。
招商银行 600036 事件样本过少（窗口内仅 2 条合格事件），不适合做事件策略验收。

用法：cd backend && uv run python scripts/run_backtest.py [--strict]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.costs import CostModel  # noqa: E402
from app.backtest.engine import BacktestConfig, BacktestResult, run_backtest  # noqa: E402
from app.backtest.types import BacktestError, Mode  # noqa: E402
from app.data import duckdb_client as dc  # noqa: E402

DEFAULT_SYMBOL = "600519"
DEFAULT_CASH = 1_000_000.0
DETAIL_LIMIT = 40


@dataclass
class Row:
    """矩阵里的一行 = 一次回测的摘要。"""

    label: str
    strategy: str
    mode: str
    fee: str
    slippage: str
    window: str
    bars: int
    fills: int
    closed: int
    fees: float
    slippage_cost: float
    final_equity: float
    total_return: float
    entry_dates: list[str]

    @classmethod
    def of(cls, label: str, result: BacktestResult) -> Row:
        costs = result.config.costs
        return cls(
            label=label,
            strategy=result.config.strategy,
            mode=result.config.pit_mode.value,
            fee="on" if costs.fee_enabled else "off",
            slippage=f"{costs.slippage_bps:g}bps" if costs.slippage_enabled else "off",
            window=f"{result.bars[0].trade_date} → {result.bars[-1].trade_date}",
            bars=len(result.bars),
            fills=len(result.fills),
            closed=len(result.trades),
            fees=result.total_fees,
            slippage_cost=result.total_slippage_cost,
            final_equity=result.final_equity,
            total_return=result.total_return,
            entry_dates=[d.isoformat() for d in result.entry_dates],
        )


def data_range(symbol: str) -> tuple[date, date]:
    rows = dc.bars(symbol)
    if not rows:
        raise BacktestError(f"{symbol} 无行情数据，先跑 scripts/download_bars.py")
    return rows[0]["trade_date"], rows[-1]["trade_date"]


def first_event_date(symbol: str) -> date | None:
    """事件窗口起点。事件语料仅约 3 个月，事件策略以此为回测起点才有意义。"""
    rows = dc.events(symbol)
    return min(row["event_time"].date() for row in rows) if rows else None


def run_one(
    symbol: str,
    strategy: str,
    label: str,
    costs: CostModel,
    pit_mode: Mode,
    start: date | None,
    end: date | None,
    cash: float,
    params: dict[str, float | int],
) -> Row:
    result = run_backtest(
        BacktestConfig(
            symbol=symbol,
            strategy=strategy,
            start=start,
            end=end,
            initial_cash=cash,
            pit_mode=pit_mode,
            costs=costs,
            params=params,
        )
    )
    return Row.of(label, result)


def build_matrix(args: argparse.Namespace, full: tuple[date, date], event_start: date) -> list[Row]:
    base = CostModel(slippage_bps=args.slippage_bps)
    params: dict[str, float | int] = {
        "fast": args.fast,
        "slow": args.slow,
        "min_score": args.min_score,
        "hold_days": args.hold_days,
    }
    rows: list[Row] = []

    # ── ma_cross：跑全窗（事件窗口内仅 2~3 个交叉，样本不足）──
    if args.strategy in ("all", "ma_cross"):
        ma_window = (args.start or full[0], args.end or full[1])
        plans = (
            ("成本全开", base, Mode.PIT),
            ("只关滑点", base.without_slippage(), Mode.PIT),
            ("成本全关", CostModel.disabled(), Mode.PIT),
        )
        for label, costs, mode in plans:
            rows.append(run_one(args.symbol, "ma_cross", f"ma_cross/{label}", costs, mode, *ma_window, args.cash, params))

    # ── event_driven：只跑事件窗口，两种模式必须同区间才可比 ──
    if args.strategy in ("all", "event_driven"):
        ev_window = (args.start or event_start, args.end or full[1])
        modes = (Mode.PIT, Mode.NON_PIT) if args.pit_mode == "both" else (Mode(args.pit_mode),)
        for mode in modes:
            rows.append(
                run_one(args.symbol, "event_driven", f"event_driven/{mode.value}", base, mode, *ev_window, args.cash, params)
            )
        # 成本开关对照（PIT 模式）
        rows.append(
            run_one(
                args.symbol, "event_driven", "event_driven/成本全关", CostModel.disabled(),
                Mode.PIT, *ev_window, args.cash, params,
            )
        )
    return rows


def judge(rows: list[Row]) -> list[tuple[bool, str]]:
    """三条验收判据。"""
    verdicts: list[tuple[bool, str]] = []

    traded = [r for r in rows if r.fills > 0]
    verdicts.append(
        (
            len(traded) == len(rows),
            f"① 两策略均可跑通：{len(traded)}/{len(rows)} 行有成交",
        )
    )

    ma = [r for r in rows if r.strategy == "ma_cross"]
    if len(ma) >= 3:
        on, no_slip, off = ma[0], ma[1], ma[2]
        ok = on.final_equity < no_slip.final_equity < off.final_equity
        verdicts.append(
            (
                ok,
                "② 成本开关有可见影响："
                f"全开 {on.final_equity:,.2f} < 只关滑点 {no_slip.final_equity:,.2f} "
                f"< 全关 {off.final_equity:,.2f}（各自独立生效）",
            )
        )

    pit = [r for r in rows if r.label == "event_driven/pit"]
    non_pit = [r for r in rows if r.label == "event_driven/non_pit"]
    if pit and non_pit:
        p, n = pit[0], non_pit[0]
        differs = p.entry_dates != n.entry_dates
        verdicts.append(
            (
                differs,
                f"③ PIT 与非 PIT 存在差异：入场日序列{'不同' if differs else '相同'} | "
                f"期末 {p.final_equity:,.2f} vs {n.final_equity:,.2f}",
            )
        )
    return verdicts


def print_report(symbol: str, rows: list[Row], verdicts: list[tuple[bool, str]], args: argparse.Namespace) -> None:
    header = f"{'#':<3}{'策略/情形':<26}{'模式':<9}{'费用':<5}{'滑点':<7}{'bar':>5}{'成交':>5}{'平仓':>5}{'费用合计':>13}{'滑点成本':>12}{'期末权益':>15}{'收益率':>10}"
    print(f"QuantSage T4 最小回测 · 验收矩阵（{symbol}）")
    print(f"初始资金 {args.cash:,.2f} 元 · 整手 100 股 · {CostModel(slippage_bps=args.slippage_bps).describe()}")
    print()
    print(header)
    print("-" * len(header))
    for i, row in enumerate(rows, 1):
        print(
            f"{i:<3}{row.label:<26}{row.mode:<9}{row.fee:<5}{row.slippage:<7}"
            f"{row.bars:>5}{row.fills:>5}{row.closed:>5}{row.fees:>13,.2f}{row.slippage_cost:>12,.2f}"
            f"{row.final_equity:>15,.2f}{row.total_return * 100:>9.2f}%"
        )
    print()
    print("验收判据")
    for ok, text in verdicts:
        print(f"{'✔' if ok else '✗'} {text}")

    pit = next((r for r in rows if r.label == "event_driven/pit"), None)
    non_pit = next((r for r in rows if r.label == "event_driven/non_pit"), None)
    if pit and non_pit:
        print()
        print("判据③ 证据（PIT 按 available_at 设卡 / 非 PIT 按 event_time 设卡）")
        print(f"  pit     入场日：{', '.join(pit.entry_dates) or '（无）'}")
        print(f"  non_pit 入场日：{', '.join(non_pit.entry_dates) or '（无）'}")


def print_detail(symbol: str, args: argparse.Namespace, full: tuple[date, date], event_start: date) -> None:
    """--detail：打印某一策略的逐笔成交与平仓。"""
    strategy = args.strategy if args.strategy != "all" else "ma_cross"
    window = (args.start or full[0], args.end or full[1]) if strategy == "ma_cross" else (args.start or event_start, args.end or full[1])
    costs = CostModel.disabled() if args.no_costs else CostModel(slippage_bps=args.slippage_bps)
    if args.no_fees:
        costs = costs.without_fees()
    if args.no_slippage:
        costs = costs.without_slippage()

    result = run_backtest(
        BacktestConfig(
            symbol=symbol, strategy=strategy, start=window[0], end=window[1], initial_cash=args.cash,
            pit_mode=Mode.PIT if args.pit_mode == "both" else Mode(args.pit_mode), costs=costs,
            params={"fast": args.fast, "slow": args.slow, "min_score": args.min_score, "hold_days": args.hold_days},
        )
    )
    print()
    print(f"逐笔成交（{strategy}，按 {result.cutoff_field} 设卡，成本={costs.describe()}）")
    for fill in result.fills[:DETAIL_LIMIT]:
        print(
            f"  {fill.trade_date} {fill.side.value:<4} {fill.qty:>7} 股 @ {fill.price:>10.2f} "
            f"费用 {fill.fees:>10.2f} 滑点 {fill.slippage_cost:>9.2f}  {fill.reason}"
        )
    if result.dropped_signals:
        print(f"  未成交信号 {len(result.dropped_signals)} 条（首条：{result.dropped_signals[0].reason}）")


def to_json(symbol: str, rows: list[Row], verdicts: list[tuple[bool, str]]) -> dict[str, Any]:
    return {
        "schema": "quantsage.backtest_acceptance/v1",
        "symbol": symbol,
        "rows": [asdict(row) for row in rows],
        "verdicts": [{"pass": ok, "text": text} for ok, text in verdicts],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="T4 回测验收矩阵")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--strategy", choices=("all", "ma_cross", "event_driven"), default="all")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--cash", type=float, default=DEFAULT_CASH)
    parser.add_argument("--fast", type=int, default=5)
    parser.add_argument("--slow", type=int, default=20)
    parser.add_argument("--min-score", type=float, default=50.0)
    parser.add_argument("--hold-days", type=int, default=5)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--no-costs", action="store_true", help="费用与滑点全关")
    parser.add_argument("--no-fees", action="store_true", help="只关佣金与印花税")
    parser.add_argument("--no-slippage", action="store_true", help="只关滑点")
    parser.add_argument("--pit-mode", choices=("both", "pit", "non_pit"), default="both")
    parser.add_argument("--detail", action="store_true", help="打印逐笔成交")
    parser.add_argument("--json", type=Path, default=None, help="另存机器可读副本")
    parser.add_argument("--strict", action="store_true", help="有判据未通过时退出码 1")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    full = data_range(args.symbol)
    event_start = first_event_date(args.symbol)

    if args.strategy in ("all", "event_driven") and event_start is None:
        print(f"✗ {args.symbol} 无事件数据，无法跑 event_driven；先跑 scripts/download_events.py")
        return 1

    rows = build_matrix(args, full, event_start or full[0])
    verdicts = judge(rows)
    print_report(args.symbol, rows, verdicts, args)
    if args.detail:
        print_detail(args.symbol, args, full, event_start or full[0])

    if args.json:
        args.json.write_text(json.dumps(to_json(args.symbol, rows, verdicts), ensure_ascii=False, indent=2))
        print(f"\n机器可读副本：{args.json}")

    failed = [text for ok, text in verdicts if not ok]
    if failed and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
