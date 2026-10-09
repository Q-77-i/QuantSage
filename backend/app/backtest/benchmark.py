"""全市场等权组合代理（M5a）：没有指数数据时的基准。

**为什么是代理而不是沪深 300**：M2a 已核实数据源不覆盖指数——`cn-daily` 的标的前缀
全集只有 14 个，全部是个股；`000300` / `399xxx` 查无此码，Manifest 也未声明任何 index
数据集。CLAUDE.md 又定了不引第二数据源（akshare 没有 PIT 语义，引进来会污染护城河）。
故基准取**全市场等权组合**，并在报告与 UI 里如实标注口径——**不写成「沪深 300」**。

**剔除口径**：聚合前剔掉 `|change_pct| > 30%` 的样本。实测 2026 年有 102 行（新股首日
与复牌，raw 极值 **+1942%**），单只即可把当日等权均值拉高 **0.36pp**——而日波动量级
才 1%，不剔就没法看。这些样本只可能是「上市首日 / 复牌 / 数据异常」，且它们在回测里
本来就不可投资。剔了多少条写进报告，不是静默处理。

日报酬取源给的 `change_pct`（已含除权调整），不是 close 比值：`change_pct` 缺失 4.8%，
缺失的那些日子**不参与**当日均值（而不是当 0 算进去——把缺失当 0 会系统性压低基准）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.data import duckdb_client as dc

#: 基准类型标识。UI 与报告据此显示口径文案，**不显示成指数名**。
MARKET_KIND = "market_equal_weight"

#: 剔除阈值：超出任何板别的涨跌停幅度（北交所 30% 为最宽）
EXCLUDE_ABS_CHANGE_PCT = 30.0

NOTE = (
    "数据源不覆盖指数（M2a 已核）；本基准为全市场等权组合代理，日频再平衡、"
    "剔除 |涨跌幅|>30% 的新股首日与复牌样本"
)


@dataclass(frozen=True, slots=True)
class MarketBenchmark:
    """与传入 `dates` **一一对齐**的基准净值序列，外加口径自述。"""

    levels: tuple[float, ...]
    total_return: float
    excluded: int
    sample_days: int
    avg_samples: int

    def describe(self) -> dict[str, object]:
        return {
            "kind": MARKET_KIND,
            "note": NOTE,
            "exclude_rule": f"abs(change_pct) > {EXCLUDE_ABS_CHANGE_PCT:g}",
            "excluded": self.excluded,
            "sample_days": self.sample_days,
            "avg_samples": self.avg_samples,
            "total_return": self.total_return,
        }


def _daily_rows(
    start: date, end: date, adjust: str, data_dir: Path | None
) -> list[dict[str, object]]:
    con = dc.connect(data_dir)
    try:
        rows = (
            con.execute(
                f"""
                SELECT trade_date,
                       avg(change_pct) FILTER (WHERE abs(change_pct) <= ?) AS ret,
                       count(*)        FILTER (WHERE abs(change_pct) <= ?) AS kept,
                       count(*)        FILTER (WHERE abs(change_pct) >  ?) AS dropped
                FROM {dc.BARS_VIEW}
                WHERE adjustment = ? AND change_pct IS NOT NULL AND trade_date BETWEEN ? AND ?
                GROUP BY trade_date ORDER BY trade_date
                """,
                [EXCLUDE_ABS_CHANGE_PCT, EXCLUDE_ABS_CHANGE_PCT, EXCLUDE_ABS_CHANGE_PCT,
                 adjust, start, end],
            )
            .to_arrow_table()
            .to_pylist()
        )
    finally:
        con.close()
    return rows


def market_benchmark(
    dates: Sequence[date],
    initial_cash: float,
    *,
    adjust: str = "qfq",
    data_dir: Path | None = None,
) -> MarketBenchmark:
    """按 `dates` 走一遍全市场等权净值；某日没有样本则**当日收益为 0**（净值不动）。"""
    if not dates:
        return MarketBenchmark((), 0.0, 0, 0, 0)

    by_date = {row["trade_date"]: row for row in _daily_rows(dates[0], dates[-1], adjust, data_dir)}

    level = initial_cash
    levels: list[float] = []
    excluded = 0
    kept_total = 0
    kept_days = 0
    for day in dates:
        row = by_date.get(day)
        if row is not None:
            excluded += int(row["dropped"] or 0)
            kept = int(row["kept"] or 0)
            if row["ret"] is not None:
                level *= 1.0 + float(row["ret"]) / 100.0
                kept_total += kept
                kept_days += 1
        levels.append(level)

    return MarketBenchmark(
        levels=tuple(levels),
        total_return=level / initial_cash - 1.0 if initial_cash else 0.0,
        excluded=excluded,
        sample_days=kept_days,
        avg_samples=round(kept_total / kept_days) if kept_days else 0,
    )


__all__ = [
    "EXCLUDE_ABS_CHANGE_PCT",
    "MARKET_KIND",
    "NOTE",
    "MarketBenchmark",
    "market_benchmark",
]
