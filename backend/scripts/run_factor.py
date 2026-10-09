"""M5c 因子取证：在**真实数据**上跑一遍两个因子源，产出 `logs/m5c/` 的证据。

它同时验三件事，前两件是自动化层看不见的：

1. **同尺子（真数据版）**：把真实语料逐条喂 `EventFeed.advance`，与因子面板的归属日
   **逐条比对**——单测里是合成行，这里是真的 30 万行语料与真的 65 个交易日；
2. **两个因子源都跑得出报告**：事件信号因子（池子薄）与 20 日反转因子（全市场），
   含 IC / 分层 / 多空 / 换手 / 费用拖累的完整读数；
3. **口径生效**：费用开关真的改变净值（净 ≠ 毛）、`long_short.tradable` 恒为 false。

```
cd backend && uv run python scripts/run_factor.py
cd backend && uv run python scripts/run_factor.py --start 2026-07-10 --end 2026-09-30
```

退出码：0 全部断言通过；1 有断言未过。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from statistics import fmean
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.costs import CostModel  # noqa: E402
from app.backtest.events import EventFeed, event_from_row  # noqa: E402
from app.backtest.types import Bar, Mode  # noqa: E402
from app.data import duckdb_client as dc  # noqa: E402
from app.factor import analysis  # noqa: E402
from app.factor.panel import build_event_panel, build_price_panel, bucket_day  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "logs" / "m5c"


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""


def same_ruler_check(days: tuple[date, ...]) -> tuple[Check, dict]:
    """真语料逐条比对：`EventFeed.advance` 的放行日 == `bucket_day` 的归属日。

    用对象身份（`id`）对齐而不是 `event_id`：平台对「按月复发的同题事件」会复用 id
    （M2b 记的 84 天里 18 例），拿 id 当键会把它们混起来。
    """
    started = time.perf_counter()
    rows = dc.events()  # 全量语料，不做 PIT 过滤（设卡是消费方的职责）
    # 自己持有同一批 `EventView` 对象：`EventFeed` 内部 `sorted()` 保留的是同一批引用，
    # 故 `id()` 能把「引擎放行的那条」与「我算归属日的那条」对上
    views = [event_from_row(row) for row in rows]
    feed = EventFeed(views, Mode.PIT)
    released: dict[int, date] = {}
    for day in days:
        bar = Bar(
            symbol="__market__", trade_date=day, open=1.0, high=1.0, low=1.0, close=1.0, volume=0.0
        )
        for fresh in feed.advance(bar):
            released[id(fresh)] = day

    mismatches = [
        (view.event_id, str(view.available_at))
        for view in views
        if released.get(id(view)) != bucket_day(view.available_at, days)
    ][:5]
    elapsed = time.perf_counter() - started
    detail = (
        f"{len(rows):,} 行语料 / {len(feed.visible):,} 条被放行 / {len(days)} 个交易日 / "
        f"不一致 {len(mismatches)} 条 / {elapsed:.2f}s"
    )
    return Check("同尺子：归属日逐条等于引擎放行日", not mismatches, detail), {
        "rows": len(rows),
        "visible": len(feed.visible),
        "days": len(days),
        "mismatch_samples": [str(item) for item in mismatches],
        "seconds": round(elapsed, 3),
    }


def source_report(
    *, source: str, values, forward, days, start: date, end: date, direction: str | None,
    universe: dict, costs: CostModel,
) -> dict:
    return analysis.build_report(
        source=source,
        values=values,
        forward=forward,
        all_days=days,
        start=start,
        end=end,
        costs=costs,
        costs_enabled=True,
        direction=direction,
        lookback=dc.FACTOR_LOOKBACK if source == "price" else None,
        params=analysis.report_params(
            source=source,
            direction=direction,
            lookback=dc.FACTOR_LOOKBACK,
            adjust="qfq",
            costs=costs,
        ),
        universe=universe,
    )


def cost_drag_bps(report: dict) -> float:
    """日均费用拖累：**毛/净曲线的日收益之差**取均值——从报告自身算出来的，不是估的。

    曲线首点是 1.0（初始净值），故收益序列要**把 1.0 补在头上**——否则首日（全新建仓、
    买 100%）那一笔费用被漏掉，读数会比报告 `notes` 里的那个略高（实测 19.33 vs 19.1 bps）。
    """
    drags = []
    for group in report["groups"]:
        gross = [1.0, *[point["level"] for point in group["gross"]["curve"]]]
        net = [1.0, *[point["level"] for point in group["net"]["curve"]]]
        for i in range(1, len(gross)):
            drags.append((gross[i] / gross[i - 1] - 1.0) - (net[i] / net[i - 1] - 1.0))
    return fmean(drags) * 1e4 if drags else 0.0


def summarize(report: dict) -> dict:
    """把一份报告压成读数表要用的几个数。"""
    ic = report["ic"]
    ls = report["long_short"]
    groups = report["groups"]
    gross_ann = [g["gross"]["metrics"]["annual_return"] for g in groups]
    net_ann = [g["net"]["metrics"]["annual_return"] if g["net"] else None for g in groups]
    turnover = fmean(g["turnover_avg"] for g in groups)
    drag = cost_drag_bps(report)
    return {
        "signal_days": report["window"]["signal_days"],
        "pool_avg": report["universe"]["pool_avg"],
        "pool_min": report["universe"]["pool_min"],
        "pool_max": report["universe"]["pool_max"],
        "dropped_no_price": report["universe"]["dropped_no_price"],
        "dropped_untradeable": report["universe"]["dropped_untradeable"],
        "ic_mean": ic["mean"],
        "ic_std": ic["std"],
        "icir": ic["icir"],
        "ic_t": ic["t_stat"],
        "ic_positive_days": ic["positive_days"],
        "group_annual_gross": gross_ann,
        "group_annual_net": net_ann,
        "turnover_avg": turnover,
        "cost_per_day_bps": drag,
        "cost_per_year_pct": drag * 252 / 100,
        "long_short_annual_gross": ls["gross"]["metrics"]["annual_return"],
        "long_short_annual_net": ls["net"]["metrics"]["annual_return"],
        "long_short_t": ls["t_stat"],
        "tradable": ls["tradable"],
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.1f}%"


def _f4(value: float | None) -> str:
    return "—" if value is None else f"{value:+.4f}"


def write_markdown(
    *, window: dict, corpus: dict, bars_end: str, ruler: dict, event_read: dict,
    price_read: dict, event_report: dict, price_report: dict, checks: list[Check]
) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    lines = [
        "# M5c 因子分析取证（真实数据）",
        "",
        f"窗口 **{window['start']} → {window['end']}**｜语料 {corpus['start']} → {corpus['end']}"
        f"（{corpus['rows']:,} 行）｜行情末端 {bars_end}",
        "",
        "## 一、同尺子（真语料逐条比对）",
        "",
        f"- `EventFeed.advance` 的放行日 == `bucket_day` 的归属日：**{ruler['mismatch_samples'] or '零不一致'}**",
        f"- 读数：{ruler['visible']:,} 条可见事件 / {ruler['days']} 个交易日 / {ruler['seconds']}s",
        "",
        "## 二、事件信号因子（`source=event`）",
        "",
        "| 读数 | 值 |",
        "|---|---|",
        f"| 有效信号日 | **{event_read['signal_days']}** |",
        f"| 池子（日均 / 最小 / 最大） | {event_read['pool_avg']} / {event_read['pool_min']} / {event_read['pool_max']} |",
        f"| 缺价剔除 | {event_read['dropped_no_price']} |",
        f"| 不可交易剔除（\\|收益\\|>30%） | {event_read['dropped_untradeable']} |",
        f"| **RankIC 均值** | **{_f4(event_read['ic_mean'])}**（标准差 {_f4(event_read['ic_std'])}） |",
        f"| ICIR / t | {_f4(event_read['icir'])} / {_f4(event_read['ic_t'])} |",
        f"| IC > 0 的天数 | {event_read['ic_positive_days']} / {event_read['signal_days']} |",
        f"| 分层年化（毛，Q1→Q5） | {' · '.join(_pct(v) for v in event_read['group_annual_gross'])} |",
        f"| 分层年化（净，Q1→Q5） | {' · '.join(_pct(v) for v in event_read['group_annual_net'])} |",
        f"| 多空年化（毛 / 净） | {_pct(event_read['long_short_annual_gross'])} / {_pct(event_read['long_short_annual_net'])} |",
        f"| 多空 t | {_f4(event_read['long_short_t'])}（**不可交易**：{event_read['tradable']}） |",
        f"| 日均换手 / 费用拖累 | {event_read['turnover_avg']:.3f} / {event_read['cost_per_day_bps']:.2f} bps·日（年化 {event_read['cost_per_year_pct']:.1f}%） |",
        "",
        "## 三、价格反转因子（`source=price&direction=reversal`，全市场池）",
        "",
        "| 读数 | 值 |",
        "|---|---|",
        f"| 有效信号日 | **{price_read['signal_days']}** |",
        f"| 池子（日均 / 最小 / 最大） | {price_read['pool_avg']} / {price_read['pool_min']} / {price_read['pool_max']} |",
        f"| 缺价剔除 | {price_read['dropped_no_price']} |",
        f"| **RankIC 均值** | **{_f4(price_read['ic_mean'])}**（标准差 {_f4(price_read['ic_std'])}） |",
        f"| ICIR / t | {_f4(price_read['icir'])} / {_f4(price_read['ic_t'])} |",
        f"| IC > 0 的天数 | {price_read['ic_positive_days']} / {price_read['signal_days']} |",
        f"| 分层年化（毛，Q1→Q5） | {' · '.join(_pct(v) for v in price_read['group_annual_gross'])} |",
        f"| 分层年化（净，Q1→Q5） | {' · '.join(_pct(v) for v in price_read['group_annual_net'])} |",
        f"| 多空年化（毛 / 净） | {_pct(price_read['long_short_annual_gross'])} / {_pct(price_read['long_short_annual_net'])} |",
        f"| 多空 t | {_f4(price_read['long_short_t'])}（**不可交易**：{price_read['tradable']}） |",
        f"| 日均换手 / 费用拖累 | {price_read['turnover_avg']:.3f} / {price_read['cost_per_day_bps']:.2f} bps·日（年化 {price_read['cost_per_year_pct']:.1f}%） |",
        "",
        "## 四、报告自带的 notes（如实标注清单）",
        "",
    ]
    lines += [f"- {note}" for note in event_report["notes"]]
    lines += [
        "",
        "## 五、断言",
        "",
        "| 判据 | 结果 | 读数 |",
        "|---|---|---|",
    ]
    lines += [f"| {c.label} | {'✅' if c.ok else '❌'} | {c.detail} |" for c in checks]
    lines += [
        "",
        "**结论（如实呈现）**：事件信号因子在这个语料上**测不出截面预测力**——RankIC 均值在噪声",
        "区间内、分层不单调、多空 t 很小；价格反转因子有读数但样本期特定、日间不独立。",
        "两个因子的日频再平衡换手都接近「每天全换一遍」，费用拖累是同一量级的硬约束。",
        "**对外不得写成「找到了因子」**——这份报告的价值是管道与如实口径，不是 alpha。",
    ]
    (OUT / "factor.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="M5c 因子取证（真实数据）")
    parser.add_argument("--start", default=None, help="YYYY-MM-DD，缺省取语料起点")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD，缺省取行情末端")
    args = parser.parse_args()

    corpus = dc.event_coverage()
    latest = dc.latest_dates()
    bars_end = str(latest["latest_trade_date"])
    start = date.fromisoformat(args.start or str(corpus["start"]))
    end = date.fromisoformat(args.end or bars_end)
    checks: list[Check] = []

    started = time.perf_counter()
    price_rows = dc.factor_price_rows(start.isoformat(), end.isoformat())
    price_seconds = time.perf_counter() - started
    panel = build_price_panel(price_rows, direction="reversal")
    market_days = dc.factor_market_days(start.isoformat(), end.isoformat())
    checks.append(
        Check(
            "价格面板有交易日",
            bool(panel.days),
            f"{len(price_rows):,} 行 / 窗口 {len(panel.days)} 天 / 归属日历 {len(market_days)} 天 / "
            f"读取 {price_seconds:.3f}s",
        )
    )

    ruler_check, ruler = same_ruler_check(market_days)
    checks.append(ruler_check)

    event_rows = dc.factor_event_rows()
    event_panel = build_event_panel(event_rows, market_days)

    event_report = source_report(
        source="event",
        values=event_panel.values,
        forward=panel.forward,
        days=market_days,
        start=start,
        end=end,
        direction=None,
        universe=analysis.event_universe(event_panel),
        costs=CostModel(),
    )
    price_report = source_report(
        source="price",
        values=panel.factor,
        forward=panel.forward,
        days=market_days,
        start=start,
        end=end,
        direction="reversal",
        universe=analysis.price_universe(lookback=dc.FACTOR_LOOKBACK),
        costs=CostModel(),
    )
    event_read = summarize(event_report)
    price_read = summarize(price_report)

    checks += [
        Check(
            "事件因子跑出有效信号日",
            event_read["signal_days"] > 0,
            f"{event_read['signal_days']} 天 / 日均池 {event_read['pool_avg']} 只",
        ),
        Check(
            "价格因子跑出有效信号日",
            price_read["signal_days"] > 0,
            f"{price_read['signal_days']} 天 / 日均池 {price_read['pool_avg']} 只",
        ),
        Check(
            "报告仍如实声明多空不可交易",
            event_report["long_short"]["tradable"] is False
            and price_report["long_short"]["tradable"] is False,
            "long_short.tradable == false",
        ),
        Check(
            "费用开关真的改变净值（净 ≠ 毛）",
            price_read["long_short_annual_net"] != price_read["long_short_annual_gross"],
            f"毛 {_pct(price_read['long_short_annual_gross'])} → "
            f"净 {_pct(price_read['long_short_annual_net'])}",
        ),
        Check(
            "notes 必填清单非空",
            bool(event_report["notes"]) and bool(price_report["notes"]),
            f"{len(event_report['notes'])} 条",
        ),
    ]

    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "corpus": corpus,
        "bars_end": bars_end,
        "ruler": ruler,
        "event_read": event_read,
        "price_read": price_read,
        "event_report": event_report,
        "price_report": price_report,
        "checks": [{"label": c.label, "ok": c.ok, "detail": c.detail} for c in checks],
    }
    (OUT / "factor.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    write_markdown(
        window=payload["window"],
        corpus=corpus,
        bars_end=bars_end,
        ruler=ruler,
        event_read=event_read,
        price_read=price_read,
        event_report=event_report,
        price_report=price_report,
        checks=checks,
    )

    for check in checks:
        print(f"{'✅' if check.ok else '❌'} {check.label} —— {check.detail}")
    print(f"\n证据：{OUT}/factor.md 与 factor.json")
    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
