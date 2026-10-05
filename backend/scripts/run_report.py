"""T5 验收脚本：把 SPEC §5 的两条验收判据跑成肉眼可核对的人读报告。

    ① 报告结构完整且自洽        → 结构键齐备、净值曲线与基准逐点对齐
    ② 对比报告能量化虚高幅度    → 同区间跑 PIT / 非 PIT，打印差值

第二条的**符号不预设**：非 PIT 允许策略使用「事发即知」的未来信息，但看到未来
不等于赚得更多——它会改变入场日，而入场日的优劣取决于样本。实测（2026-10）
600519 与 300750 上非 PIT 均**低于** PIT。报告如实打印方向，不套「虚高」二字。

指标与手工计算的一致性由 `uv run pytest tests/test_backtest_metrics.py` 覆盖（硬编码期望值）。

用法：cd backend && uv run python scripts/run_report.py [--symbol 300750] [--strict] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.costs import CostModel  # noqa: E402
from app.backtest.engine import BacktestConfig  # noqa: E402
from app.backtest.report import build_report  # noqa: E402
from app.backtest.types import BacktestError, Mode  # noqa: E402
from app.data import duckdb_client as dc  # noqa: E402

DEFAULT_SYMBOL = "600519"
DEFAULT_CASH = 1_000_000.0
TRADE_LIMIT = 15
#: 表格列宽（**显示宽度**，中文字符算 2 格）。手工对齐，不引第三方表格库。
COL = (24, 21, 21, 12)

METRIC_ROWS: tuple[tuple[str, str, str], ...] = (
    ("total_return", "总收益", "spct"),
    ("annual_return", "年化（252 日折算）", "spct"),
    ("max_drawdown", "最大回撤", "pct"),
    ("sharpe", "夏普（rf=0）", "num"),
    ("win_rate", "胜率", "pct"),
    ("trade_count", "交易次数", "int"),
    ("final_equity", "期末权益", "money"),
)
DELTA_ROWS: tuple[tuple[str, str], ...] = (
    ("total_return_pp", "总收益"),
    ("annual_return_pp", "年化"),
    ("max_drawdown_pp", "最大回撤"),
    ("sharpe_abs", "夏普"),
    ("win_rate_pp", "胜率"),
    ("trade_count", "交易次数"),
)


def disp_width(text: str) -> int:
    """终端显示宽度：东亚宽字符占 2 列，`str.format` 的对齐不认这点，故自行计算。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int, align: str = ">") -> str:
    """按显示宽度补空格对齐。"""
    spaces = " " * max(0, width - disp_width(text))
    return spaces + text if align == ">" else text + spaces


def fmt(value: Any, kind: str) -> str:
    """统一格式化：`—` 表示该指标在此样本下无定义（None）。

    收益/差值带正负号（方向有信息量）；回撤、胜率是**幅度**，不带号（+25% 的胜率是错的）。
    """
    if value is None:
        return "—"
    if kind == "pct":  # 幅度，无符号
        return f"{value:.2%}"
    if kind == "spct":  # 有符号百分比
        return f"{value:+.2%}"
    if kind == "pp":  # 百分点差
        return f"{value:+.2f}pp"
    if kind == "money":
        return f"{value:,.2f}"
    if kind == "int":
        return f"{value:d}"
    return f"{value:+.2f}"


@dataclass(frozen=True, slots=True)
class Plan:
    """一次报告的全部输入参数。"""

    symbol: str
    strategy: str
    start: date | None
    end: date | None
    cash: float
    adjust: str
    costs: CostModel
    pit_mode: Mode
    compare_pit: bool
    params: dict[str, float | int]

    def config(self) -> BacktestConfig:
        return BacktestConfig(
            symbol=self.symbol,
            strategy=self.strategy,
            start=self.start,
            end=self.end,
            initial_cash=self.cash,
            adjust=self.adjust,
            pit_mode=self.pit_mode,
            costs=self.costs,
            params=self.params,
        )


def data_range(symbol: str) -> tuple[date, date]:
    rows = dc.bars(symbol)
    if not rows:
        raise BacktestError(f"{symbol} 无行情数据，先跑 scripts/download_bars.py")
    return rows[0]["trade_date"], rows[-1]["trade_date"]


def first_event_date(symbol: str) -> date | None:
    """事件语料仅约 3 个月，事件策略以事件窗口为起点才有意义（与 T4 验收同口径）。"""
    rows = dc.events(symbol)
    return min(row["event_time"].date() for row in rows) if rows else None


def build_plan(args: argparse.Namespace) -> Plan:
    full = data_range(args.symbol)
    start = args.start
    if start is None and args.strategy == "event_driven":
        start = first_event_date(args.symbol)
        if start is None:
            raise BacktestError(f"{args.symbol} 无事件数据，先跑 scripts/download_events.py")
    if args.no_costs:
        costs = CostModel.disabled()
    else:
        costs = CostModel(slippage_bps=args.slippage_bps)
        if args.no_fees:
            costs = costs.without_fees()
        if args.no_slippage:
            costs = costs.without_slippage()

    # ma_cross 不消费事件语料，PIT 与非 PIT 必然同结果，跑第二遍只会误导
    compare = not args.no_pit_compare and args.strategy == "event_driven"

    return Plan(
        symbol=args.symbol,
        strategy=args.strategy,
        start=start,
        end=args.end or full[1],
        cash=args.cash,
        adjust=args.adjust,
        costs=costs,
        pit_mode=Mode.PIT if args.pit_mode == "both" else Mode(args.pit_mode),
        compare_pit=compare,
        params={
            "fast": args.fast,
            "slow": args.slow,
            "min_score": args.min_score,
            "hold_days": args.hold_days,
        },
    )


# ── 判据 ──────────────────────────────────────────────────────────────────


def judge(report: dict[str, Any]) -> list[tuple[bool, str]]:
    """SPEC §5 的两条验收判据（拆成三项可执行检查）。"""
    verdicts: list[tuple[bool, str]] = []
    spec_keys = {"metrics", "equity_curve", "trades", "pit_comparison"}
    extra_keys = {"meta", "open_position"}
    missing = (spec_keys | extra_keys) - set(report)
    verdicts.append((not missing, f"① SPEC §5 结构完整：缺失 {sorted(missing) if missing else '无'}"))

    curve = report["equity_curve"]
    aligned = bool(curve) and all(
        point.get("benchmark") is not None and point.get("date") for point in curve
    )
    verdicts.append(
        (aligned, f"② 净值曲线与基准逐点对齐：{len(curve)} 点，每点含 date/equity/benchmark")
    )

    comparison = report["pit_comparison"]
    if comparison is None:
        verdicts.append((True, "③ 前视偏差对比：本次未请求（ma_cross 不消费事件，两模式等价）"))
    else:
        delta = comparison["delta"]
        gap = delta["final_equity_abs"]
        if gap == 0:
            # 合格事件恰好都「当天即得」时两模式同结果——量化值为 0 也是量化结果
            outcome = "两模式期末权益相同（本样本内合格事件的 available_at 均早于当日收盘）"
        else:
            direction = "高" if gap > 0 else "低"
            outcome = f"非 PIT 期末权益较 PIT {direction} {abs(gap):,.2f} 元（{delta['final_equity_pct']:+.2%}）"
        verdicts.append((delta["final_equity_pct"] is not None, f"③ 前视偏差已量化：{outcome}"))
    return verdicts


# ── 打印 ──────────────────────────────────────────────────────────────────


def print_head(report: dict[str, Any], plan: Plan) -> None:
    meta = report["meta"]
    print(f"QuantSage T5 分析报告 · 前视偏差对比（{meta['symbol']}）")
    print(
        f"策略 {meta['strategy']} · 模式 {meta['mode']}（按 {meta['cutoff_field']} 设卡） · "
        f"{meta['start']} → {meta['end']} · {meta['bars']} 根 bar · 复权 {meta['adjust']}"
    )
    print(f"初始资金 {meta['initial_cash']:,.2f} 元 · {meta['costs']}")
    for warning in meta["warnings"]:
        print(f"⚠ {warning}")
    print()


def print_metrics(report: dict[str, Any]) -> None:
    metrics = report["metrics"]
    benchmark_final = report["meta"]["initial_cash"] * (1.0 + metrics["benchmark_return"])
    print("指标（基准 = 同标的买入持有：首根收盘价份额化买入，扣一次买入成本，持有到期末）")
    print(pad("指标", COL[0], "<") + pad("策略", COL[1]) + pad("基准", COL[2]))
    print("-" * (COL[0] + COL[1] + COL[2]))
    for key, label, kind in METRIC_ROWS:
        if key == "total_return":
            base = fmt(metrics["benchmark_return"], kind)
        elif key == "final_equity":
            base = fmt(benchmark_final, kind)
        else:
            base = "—"  # 基准只算收益口径，不比策略的择时指标
        print(pad(label, COL[0], "<") + pad(fmt(metrics[key], kind), COL[1]) + pad(base, COL[2]))
    print(
        pad("超额收益（策略 − 基准）", COL[0], "<")
        + pad(fmt(metrics["excess_return"], "spct"), COL[1])
    )

    curve = report["equity_curve"]
    equities = [point["equity"] for point in curve]
    print()
    print(
        f"净值曲线 {len(curve)} 点 · 起点 {equities[0]:,.2f} → 终点 {equities[-1]:,.2f} · "
        f"最低 {min(equities):,.2f} · 最高 {max(equities):,.2f}"
    )


def print_trades(report: dict[str, Any]) -> None:
    trades = report["trades"]
    print()
    print(f"交易明细（{len(trades)} 笔已平仓{'' if len(trades) <= TRADE_LIMIT else f'，仅列前 {TRADE_LIMIT}'}）")
    if not trades:
        print("  （无）")
    else:
        widths = (4, 12, 12, 7, 14, 10, 5)
        header = (
            pad("#", widths[0], "<")
            + pad("入场", widths[1], "<")
            + pad("出场", widths[2], "<")
            + pad("股数", widths[3])
            + pad("盈亏", widths[4])
            + pad("收益率", widths[5])
            + pad("持有", widths[6])
        )
        print(header + "  入场 → 出场")
        print("-" * (sum(widths) + 2 + disp_width("入场 → 出场")))
        for i, trade in enumerate(trades[:TRADE_LIMIT], 1):
            print(
                pad(str(i), widths[0], "<")
                + pad(str(trade["entry_date"]), widths[1], "<")
                + pad(str(trade["exit_date"]), widths[2], "<")
                + pad(f"{trade['qty']:d}", widths[3])
                + pad(f"{trade['pnl']:,.2f}", widths[4])
                + pad(fmt(trade["return_pct"], "spct"), widths[5])
                + pad(f"{trade['hold_bars']:d}", widths[6])
                + f"  {trade['entry_reason']} → {trade['reason']}"
            )

    position = report["open_position"]
    print()
    if position is None:
        print("期末持仓：无（全部已平仓）")
    else:
        print(
            f"期末持仓：{position['shares']} 股 @ 成本 {position['entry_price']:,.2f}"
            f"（{position['entry_date']}），最新收盘 {position['last_close']:,.2f}，"
            f"浮动盈亏 {position['unrealized_pnl']:,.2f}（{position['unrealized_return']:+.2%}）"
        )
        print("  ⚠ 未平仓交易不进 trades，故不计入胜率与交易次数")


def print_comparison(report: dict[str, Any]) -> None:
    comparison = report["pit_comparison"]
    if comparison is None:
        return
    pit, non_pit = comparison["pit_metrics"], comparison["non_pit_metrics"]
    print()
    print("前视偏差对比（同区间、同成本、同参数，唯一差别是设卡字段）")
    print(
        pad("指标", COL[0], "<")
        + pad("PIT（available_at）", COL[1])
        + pad("非 PIT（event_time）", COL[2])
        + pad("差值", COL[3])
    )
    print("-" * sum(COL))
    for key, label, kind in METRIC_ROWS:
        delta_key = {
            "total_return": "total_return_pp",
            "annual_return": "annual_return_pp",
            "max_drawdown": "max_drawdown_pp",
            "sharpe": "sharpe_abs",
            "win_rate": "win_rate_pp",
            "trade_count": "trade_count",
        }.get(key)
        delta = comparison["delta"].get(delta_key) if delta_key else None
        delta_kind = "pp" if delta_key and delta_key.endswith("_pp") else kind
        print(
            pad(label, COL[0], "<")
            + pad(fmt(pit[key], kind), COL[1])
            + pad(fmt(non_pit[key], kind), COL[2])
            + pad(fmt(delta, delta_kind), COL[3])
        )
    delta = comparison["delta"]
    print()
    print(
        f"▸ 核心量化值 · 期末权益：PIT {pit['final_equity']:,.2f} vs 非 PIT {non_pit['final_equity']:,.2f}"
        f" → 差值 {delta['final_equity_abs']:,.2f} 元（{delta['final_equity_pct']:+.2%}）"
    )
    print(f"    PIT    入场日：{', '.join(comparison['entry_dates']['pit']) or '（无）'}")
    print(f"    非 PIT 入场日：{', '.join(comparison['entry_dates']['non_pit']) or '（无）'}")
    print(
        "    说明：非 PIT 确实用了当时尚不可得的信息（T4 反向证明已验），但前视偏差的收益\n"
        "          方向取决于信号 alpha，**不恒为正**——本报告如实打印方向，不预设「虚高」。"
    )


def print_verdicts(verdicts: list[tuple[bool, str]]) -> None:
    print()
    print("验收判据")
    for ok, text in verdicts:
        print(f"{'✔' if ok else '✗'} {text}")


# ── 入口 ──────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="T5 分析报告 + 前视偏差对比")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--strategy", choices=("event_driven", "ma_cross"), default="event_driven")
    parser.add_argument("--start", type=date.fromisoformat, default=None, help="默认取事件窗口起点")
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="默认取行情终点")
    parser.add_argument("--cash", type=float, default=DEFAULT_CASH)
    parser.add_argument("--adjust", choices=("qfq", "raw"), default="qfq")
    parser.add_argument("--fast", type=int, default=5)
    parser.add_argument("--slow", type=int, default=20)
    parser.add_argument("--min-score", type=float, default=50.0)
    parser.add_argument("--hold-days", type=int, default=5)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--no-costs", action="store_true", help="费用与滑点全关")
    parser.add_argument("--no-fees", action="store_true", help="只关佣金与印花税")
    parser.add_argument("--no-slippage", action="store_true", help="只关滑点")
    parser.add_argument("--pit-mode", choices=("both", "pit", "non_pit"), default="both")
    parser.add_argument("--no-pit-compare", action="store_true", help="不做双模式对比")
    parser.add_argument("--json", type=Path, default=None, help="另存机器可读报告")
    parser.add_argument("--strict", action="store_true", help="有判据未通过时退出码 1")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = build_plan(args)
        report = build_report(plan.config(), compare_pit=plan.compare_pit)
    except BacktestError as error:
        print(f"✗ {error}")
        return 1

    print_head(report, plan)
    print_metrics(report)
    print_trades(report)
    print_comparison(report)
    verdicts = judge(report)
    print_verdicts(verdicts)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\n机器可读报告：{args.json}")

    failed = [text for ok, text in verdicts if not ok]
    if failed and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
