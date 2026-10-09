"""M5c 因子面板：把落盘行摊成两张「日 → 标的」面——因子值与前向收益。

纯函数：输入是行、不是库（I/O 在 `duckdb_client`，统计与组装在 `analysis`）。

**PIT 归属只有一把尺子**：`bucket_day` 复用 `backtest.events.bar_cutoff`——与
`EventFeed.advance` 的判据是同一个（`available_at ≤ bar_cutoff(bar)` 才放行），故
「前一交易日 15:00 < `available_at` ≤ 当日 15:00 归当日」不是另立的口径，而是引擎语义的
直接改写（`tests/test_factor_panel.py` 用同批事件双跑把它钉死）。周末 / 节假日 / 盘后可得
的事件因此自然落到**下一个交易日**——这不是放宽 PIT，正是「事件在下一根 bar 才可见」的事实。

前向收益取 `open(t+1) → open(t+2)`，与引擎「信号 bar 收盘生成、next bar 开盘成交」同口径；
它按**市场交易日**对齐（t+1 / t+2 是市场的下一两个交易日），停牌标的当日缺席而不是顺延——
截面统计要求所有标的的持有期可比。
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from app.backtest.events import bar_cutoff

#: 价格因子的方向。`reversal` = 取负动量（跌得多的排在前面），`momentum` = 原样。
DIRECTIONS = ("reversal", "momentum")


def _num(value: object) -> float | None:
    """落盘值 → float；bool / 缺失 / 非数值一律 None（`True` 是 int，必须单独挡）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True, slots=True)
class EventPanel:
    """事件因子面板：`values[日][标的]` = 当日该标的的因子值（多事件取 mean）。"""

    values: dict[date, dict[str, float]]
    rows_seen: int
    dropped_no_value: int
    dropped_no_day: int


@dataclass(frozen=True, slots=True)
class PricePanel:
    """价格面板：市场交易日序列、因子值（已按 `direction` 定符号）、前向收益。"""

    days: tuple[date, ...]
    factor: dict[date, dict[str, float]]
    forward: dict[date, dict[str, float]]


def bucket_day(available_at: datetime, days: Sequence[date]) -> date | None:
    """可用窗口归属：第一个满足 `bar_cutoff(d) >= available_at` 的交易日（`days` 升序）。

    15:00:00 整点算当日、15:00:01 顺延（引擎用 `<=`，这里必须一致）；
    `days` 里没有这样的日子（事件晚于最后一天的收盘）时返回 None——**如实无归属**，
    调用方剔除并计数，不硬塞进最后一天。
    """
    if not days:
        return None
    i = bisect_left(days, available_at.date())
    if i < len(days) and bar_cutoff(days[i]) >= available_at:
        return days[i]
    i += 1
    return days[i] if i < len(days) else None


def build_event_panel(
    rows: Sequence[Mapping[str, object]], days: Sequence[date]
) -> EventPanel:
    """事件行（`dc.factor_event_rows()` 的形状）→ 事件面板。

    同 (日, 标的) 的多条事件取 **mean**（不是 sum）：池子里「新闻条数多」的标的会因此
    被系统性放大，均值没有这个偏向。实测只有 17.3% 的 (日, 标的) 受聚合规则影响。
    """
    acc: dict[date, dict[str, list[float]]] = {}
    seen = dropped_no_value = dropped_no_day = 0
    for row in rows:
        seen += 1
        value = _num(row.get("factor_value"))
        if value is None:
            dropped_no_value += 1
            continue
        day = bucket_day(row["available_at"], days)  # type: ignore[arg-type]
        if day is None:
            dropped_no_day += 1
            continue
        acc.setdefault(day, {}).setdefault(str(row.get("symbol") or ""), []).append(value)

    values = {
        day: {symbol: sum(items) / len(items) for symbol, items in bucket.items()}
        for day, bucket in acc.items()
    }
    return EventPanel(
        values=values,
        rows_seen=seen,
        dropped_no_value=dropped_no_value,
        dropped_no_day=dropped_no_day,
    )


def build_price_panel(
    rows: Sequence[Mapping[str, object]], *, direction: str = "reversal"
) -> PricePanel:
    """价格行（`dc.factor_price_rows()` 的形状）→ 价格面板。

    `factor = ±(close / close_lag - 1)`：`close_lag` 是**该标的自已有价 bar** 回看
    `lookback` 根的那个收盘价（停牌空洞被跳过）。缺 `close_lag`（上市不足 lookback、
    或补窗不足）的标的当天不进池——保守缺失，不是错值。
    """
    if direction not in DIRECTIONS:
        raise ValueError(f"未知的因子方向 {direction!r}，只支持 {DIRECTIONS}")
    sign = -1.0 if direction == "reversal" else 1.0

    days = tuple(sorted({row["trade_date"] for row in rows}))  # type: ignore[arg-type]
    index = {day: i for i, day in enumerate(days)}

    # 因子值在**第一遍**就算完（close 与 close_lag 同行），只留一份开盘价给前向收益用——
    # 全市场 33 万行，少建两份嵌套字典就少 ~100ms（这是端点里最重的一段）
    factor: dict[date, dict[str, float]] = {}
    # `opens[标的][日]`：缺开盘价的日子**也记键**（值为 None），否则那个日子的前向收益
    # 会被漏掉——它用的是 t+1 / t+2 的开盘价，与自己的开盘价无关
    opens: dict[str, dict[date, float | None]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        day = row["trade_date"]
        opens.setdefault(symbol, {})[day] = _num(row.get("open"))  # type: ignore[index]
        close, lag = _num(row.get("close")), _num(row.get("close_lag"))
        if close is not None and lag is not None and lag > 0:
            factor.setdefault(day, {})[symbol] = sign * (close / lag - 1.0)  # type: ignore[index]

    forward: dict[date, dict[str, float]] = {}
    for symbol, open_series in opens.items():
        for day in open_series:
            i = index[day]
            if i + 2 < len(days):
                entry, exit_ = open_series.get(days[i + 1]), open_series.get(days[i + 2])
                if entry and exit_ and entry > 0:
                    forward.setdefault(day, {})[symbol] = exit_ / entry - 1.0
    return PricePanel(days=days, factor=factor, forward=forward)


__all__ = [
    "DIRECTIONS",
    "EventPanel",
    "PricePanel",
    "bucket_day",
    "build_event_panel",
    "build_price_panel",
]
