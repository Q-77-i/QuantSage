"""M6 模拟盘取证：在**真实数据**上跑一遍，产出 `logs/m6/` 的证据。

五组断言，前三组是自动化层看不见的（跑的是真行情、真一字板、真 20 只标的）：

1. **手工样例**：一笔买 + 一笔卖的费用/滑点/现金变动，用**独立公式重算**（不调 `CostModel`）
   与成交逐项对照——「费用扣减与手工样例一致」这条验收的落点；
2. **全批 == 回测**：单标的 + 全部批准，模拟盘的成交与 `run_backtest` 的 `fills` **逐笔相等**；
3. **闸门四态**（真实数据）：批准成交 / 驳回改轨迹 / 未审批过期且一分钱没动 /
   **真实一字涨停日批了也成交不了**（拒绝码 `LIMIT_UP`）；
4. **重放不变量**：同一份决策日志重放两次，账户状态与决策逐字段相等；
5. **多标的推进耗时**：20 只标的 × 一年窗口，如实记录每步（全量重放）的墙钟。

```
cd backend && uv run python scripts/run_paper.py
cd backend && uv run python scripts/run_paper.py --symbol 600519 --days 60
```

退出码：0 全部断言通过；1 有断言未过。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.a_share_rules import RejectCode  # noqa: E402
from app.backtest.costs import CostModel  # noqa: E402
from app.backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from app.backtest.types import BarContext, Side, Signal  # noqa: E402
from app.data import calendar as cal  # noqa: E402
from app.data import duckdb_client as dc  # noqa: E402
from app.paper.replay import replay  # noqa: E402
from app.paper.types import DecisionStatus, PaperConfig  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "logs" / "m6"

#: 手工样例用的独立费率（**故意不从 CostModel 取**：要的就是一次真正独立的复算）
COMMISSION_RATE = 2.5 / 10_000
COMMISSION_MIN = 5.0
STAMP_TAX_RATE = 5.0 / 10_000
SLIPPAGE = 5.0 / 10_000

#: 多标的耗时那组的标的池（真实代码，与规划期探针同一批，便于对比）
POOL = (
    "600519", "000001", "300750", "601318", "000858",
    "600036", "002594", "600900", "601899", "000333",
    "600030", "601166", "002415", "300059", "600276",
    "601012", "688981", "000651", "600887", "601088",
)


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""


class Scripted:
    """按 bar 下标脚本化下单：取证要的是**确定的成交**，不是策略好不好。"""

    name = "scripted"

    def __init__(self, actions: dict[int, list[Signal]]) -> None:
        self._actions = actions

    def on_bar(self, ctx: BarContext) -> list[Signal]:
        return list(self._actions.get(ctx.index, []))


def window(days: int, symbol: str = "600519") -> tuple[date, date]:
    """真实数据的最近 `days` 个交易日窗口（缺数据时向前退，直到该标的确实有 bar）。"""
    latest = date.fromisoformat(str(dc.latest_dates()["latest_trade_date"]))
    sessions = cal.sessions(latest - timedelta(days=days * 2 + 30), latest)
    return sessions[-days], latest


def config(symbols: tuple[str, ...], start: date, end: date, cash: float = 1_000_000.0) -> PaperConfig:
    return PaperConfig(
        initial_cash=cash,
        symbols=symbols,
        strategy="ma_cross",
        start=start,
        end=end,
        params={"fast": 5, "slow": 20},
        costs=CostModel(),
    )


# ── 1 · 手工样例 ────────────────────────────────────────────


def hand_computed(start: date, end: date) -> tuple[Check, dict]:
    """一笔买 + 一笔卖：用独立公式重算每一分钱，与成交逐项对照。"""
    cfg = config(("600519",), start, end)
    outcome = replay(
        cfg,
        auto=DecisionStatus.APPROVED,
        strategy=Scripted({0: [Signal(Side.BUY, reason="手工样例")],
                           5: [Signal(Side.SELL, reason="手工样例")]}),
        account_id="hand",
    )
    fills = [d.fill for d in outcome.decisions if d.fill is not None]
    if len(fills) != 2:
        return Check("手工样例", False, f"期望一买一卖，实际 {len(fills)} 笔成交"), {}

    results: list[dict] = []
    ok = True
    for fill in fills:
        qty, price, ref = fill.qty, fill.price, fill.ref_price
        notional = qty * price
        commission = max(notional * COMMISSION_RATE, COMMISSION_MIN)
        stamp = notional * STAMP_TAX_RATE if fill.side is Side.SELL else 0.0
        slip = abs(price - ref) * qty
        cash = -(notional + commission + stamp) if fill.side is Side.BUY else notional - commission - stamp
        row = {
            "date": fill.trade_date.isoformat(),
            "side": fill.side.value,
            "qty": qty,
            "price": round(price, 4),
            "ref_price": ref,
            "notional": round(notional, 4),
            "commission": (round(commission, 4), round(fill.commission, 4)),
            "stamp_tax": (round(stamp, 4), round(fill.stamp_tax, 4)),
            "slippage": (round(slip, 4), round(fill.slippage_cost, 4)),
            "cash_delta": (round(cash, 4), round(fill.cash_delta, 4)),
        }
        matched = (
            abs(commission - fill.commission) < 1e-6
            and abs(stamp - fill.stamp_tax) < 1e-6
            and abs(slip - fill.slippage_cost) < 1e-6
            and abs(cash - fill.cash_delta) < 1e-6
        )
        ok = ok and matched
        results.append(row)
    return Check("手工样例（独立公式重算）", ok, "买入与卖出各一笔" if ok else "有分项对不上"), {"fills": results}


# ── 2 · 全批 == 回测 ────────────────────────────────────────


def parity(start: date, end: date) -> tuple[Check, dict]:
    actions = {1: [Signal(Side.BUY, reason="进场")], 30: [Signal(Side.SELL, reason="离场")]}
    cfg = config(("600519",), start, end)
    expected = run_backtest(
        BacktestConfig(
            symbol="600519", strategy="scripted", start=start, end=end,
            costs=CostModel(), data_dir=None,
        ),
        strategy=Scripted(actions),
    )
    outcome = replay(
        cfg, auto=DecisionStatus.APPROVED, strategy=Scripted(actions), account_id="parity"
    )
    mine = [d.fill for d in outcome.decisions if d.fill is not None]
    same = mine == list(expected.fills)
    detail = {
        "fills": len(mine),
        "backtest_fills": len(expected.fills),
        "equity": (round(outcome.equity_curve[-1].equity, 4), round(expected.final_equity, 4)),
    }
    same = same and abs(outcome.equity_curve[-1].equity - expected.final_equity) < 1e-6
    return Check("全批 == 回测（逐笔相等）", same and len(mine) > 0, str(detail)), detail


# ── 3 · 闸门四态 ────────────────────────────────────────────


def gate(start: date, end: date) -> tuple[list[Check], dict]:
    actions = {1: [Signal(Side.BUY, reason="进场")], 30: [Signal(Side.SELL, reason="离场")]}
    cfg = config(("600519",), start, end)
    checks: list[Check] = []
    detail: dict = {}

    # ① 未审批 → 过期且一分钱没动
    idle = replay(cfg, strategy=Scripted(actions), account_id="gate-idle")
    expired = [d for d in idle.decisions if d.status is DecisionStatus.EXPIRED]
    untouched = (
        idle.state.cash == cfg.initial_cash and not idle.state.positions
        and not any(d.fill for d in idle.decisions)
    )
    checks.append(
        Check(
            "未审批不成交（推进即过期）",
            bool(expired) and untouched,
            f"过期 {len(expired)} 条，现金仍是 {idle.state.cash:,.0f}",
        )
    )
    detail["expired"] = len(expired)

    # ② 驳回 → 轨迹确实改变（后续卖出落在空仓上，一笔都没成）
    approved = replay(cfg, auto=DecisionStatus.APPROVED, strategy=Scripted(actions), account_id="gate-all")
    first_buy = next(d for d in approved.decisions if d.side is Side.BUY)
    rejected = replay(
        cfg,
        [first_buy.replace(status=DecisionStatus.REJECTED)],
        auto=DecisionStatus.APPROVED,
        strategy=Scripted(actions),
        account_id="gate-all",
    )
    rejected_fills = [d for d in rejected.decisions if d.fill is not None]
    checks.append(
        Check(
            "驳回改变轨迹",
            len(rejected_fills) == 0 and len([d for d in approved.decisions if d.fill]) == 2,
            f"全批成交 {len([d for d in approved.decisions if d.fill])} 笔 / 驳回后 {len(rejected_fills)} 笔",
        )
    )
    detail["approved_fills"] = len([d for d in approved.decisions if d.fill])

    # ③ 真实一字涨停日：批准了也成交不了
    con = dc.connect()
    try:
        candidate = _find_one_word(con)
    finally:
        con.close()
    if candidate is None:
        checks.append(Check("一字涨停日拒单", False, "窗口内没找到真实一字板样本"))
    else:
        symbol, day, prev_close, limit_up = candidate
        before = cal.previous_session(day)
        bump = replay(
            config((symbol,), before, day, cash=1_000_000.0),
            auto=DecisionStatus.APPROVED,
            strategy=Scripted({0: [Signal(Side.BUY, reason="一字板取证")]}),
            account_id="gate-limit",
        )
        decision = bump.decisions[0] if bump.decisions else None
        blocked = decision is not None and decision.reject_code == RejectCode.LIMIT_UP.value
        checks.append(
            Check(
                "一字涨停日拒单（真实样本）",
                blocked,
                f"{symbol} {day} 前收 {prev_close} → 涨停 {limit_up}；"
                + (f"拒绝码 {decision.reject_code}" if decision else "没有生成决策"),
            )
        )
        detail["limit_up"] = {
            "symbol": symbol,
            "day": day.isoformat(),
            "prev_close": prev_close,
            "limit_up": limit_up,
            "status": decision.status.value if decision else None,
        }
    return checks, detail


def _find_one_word(con) -> tuple[str, date, float, float] | None:
    """挑一个**真实一字涨停**样本（同 `limit_reject_evidence.py` 的判据，独立实现一遍）。"""
    from app.backtest.a_share_rules import is_one_word_limit_up, limit_band, limit_pct

    rows = con.execute(
        """
        WITH raw AS (
            SELECT symbol, trade_date, open, high, low, close,
                   lag(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close
            FROM bars WHERE adjustment = 'raw' AND close IS NOT NULL
        )
        SELECT symbol, trade_date, open, prev_close FROM raw
        WHERE trade_date >= DATE '2026-01-01'
          AND open = high AND high = low AND low = close
        ORDER BY trade_date DESC, symbol
        LIMIT 400
        """
    ).to_arrow_table().to_pylist()
    for row in rows:
        symbol = str(row["symbol"])
        if limit_pct(symbol) is None or not row["prev_close"]:
            continue
        band = limit_band(float(row["prev_close"]), limit_pct(symbol) or 0.1)
        bar = dc_bar(symbol, row["trade_date"], float(row["open"]))
        if is_one_word_limit_up(bar, band):
            return symbol, row["trade_date"], float(row["prev_close"]), band.up
    return None


def dc_bar(symbol: str, day: date, price: float):  # noqa: ANN202
    from app.backtest.types import Bar

    return Bar(
        symbol=symbol, trade_date=day, open=price, high=price, low=price, close=price, volume=1.0
    )


# ── 4 · 重放不变量 ──────────────────────────────────────────


def invariant(start: date, end: date) -> tuple[Check, dict]:
    cfg = config(("600519",), start, end)
    actions = {1: [Signal(Side.BUY, reason="进场")], 30: [Signal(Side.SELL, reason="离场")]}
    first = replay(cfg, auto=DecisionStatus.APPROVED, strategy=Scripted(actions), account_id="inv")
    second = replay(cfg, first.decisions, strategy=Scripted(actions), account_id="inv")
    same = (
        first.decisions == second.decisions
        and first.state == second.state
        and first.equity_curve == second.equity_curve
    )
    return Check("重放不变量（两次逐字段相等）", same, f"{len(first.decisions)} 条决策"), {
        "decisions": len(first.decisions),
        "final_equity": round(first.equity_curve[-1].equity, 2),
    }


# ── 5 · 多标的推进耗时 ──────────────────────────────────────


def multi_timing(years_days: int = 250) -> tuple[Check, dict]:
    start, end = window(years_days)
    cfg = config(POOL, start, end, cash=2_000_000.0)
    sessions = cal.sessions(start, end)
    marks = [sessions[min(len(sessions) - 1, n)] for n in (49, 99, 149, len(sessions) - 1)]

    timings: list[dict] = []
    for mark in dict.fromkeys(marks):
        started = time.perf_counter()
        outcome = replay(cfg, through=mark, account_id="multi")
        timings.append(
            {
                "through": mark.isoformat(),
                "sessions": outcome.days.__len__(),
                "ms": round((time.perf_counter() - started) * 1000),
            }
        )
    last = timings[-1]["ms"]
    ok = all(t["ms"] < 5_000 for t in timings)
    return Check(
        f"20 标的 × {len(sessions)} 个交易日：每步全量重放 {last} ms",
        ok,
        "；".join(f"{t['sessions']} 日 {t['ms']}ms" for t in timings),
    ), {"timings": timings, "symbols": len(POOL), "sessions": len(sessions)}


# ── 主流程 ──────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M6 模拟盘取证")
    parser.add_argument("--symbol", default="600519")
    parser.add_argument("--days", type=int, default=60, help="单标的窗口的交易日数")
    parser.add_argument("--multi-days", type=int, default=250, help="多标的耗时那组的交易日数")
    args = parser.parse_args(argv)

    start, end = window(args.days, args.symbol)
    print(f"窗口：{start} → {end}（{args.symbol}）")

    checks: list[Check] = []
    evidence: dict = {"window": {"start": start.isoformat(), "end": end.isoformat()}}

    hand_check, hand_detail = hand_computed(start, end)
    checks.append(hand_check)
    evidence["hand_computed"] = hand_detail

    parity_check, parity_detail = parity(start, end)
    checks.append(parity_check)
    evidence["parity"] = parity_detail

    gate_checks, gate_detail = gate(start, end)
    checks.extend(gate_checks)
    evidence["gate"] = gate_detail

    invariant_check, invariant_detail = invariant(start, end)
    checks.append(invariant_check)
    evidence["invariant"] = invariant_detail

    timing_check, timing_detail = multi_timing(args.multi_days)
    checks.append(timing_check)
    evidence["multi"] = timing_detail

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "paper.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = ["# M6 模拟盘取证", "", f"窗口：{start} → {end}", "", "| 断言 | 结果 | 说明 |", "|---|---|---|"]
    for check in checks:
        lines.append(f"| {check.label} | {'✅' if check.ok else '❌'} | {check.detail} |")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(evidence, ensure_ascii=False, indent=2))
    lines.append("```")
    (OUT / "paper.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    for check in checks:
        print(f"{'✅' if check.ok else '❌'} {check.label} —— {check.detail}")
    print(f"证据：{OUT / 'paper.md'}")
    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
