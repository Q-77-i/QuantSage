"""因子报告 API（M5c）。

**公开**端点（同 `market/*`）：报告只读公共数据、不触用户数据、**不落库**——它是确定性
纯函数（同样的窗口给同样的数），落库只会引入「旧记录形状」那一类坑。同步返回：实测端到端
< 0.2s，M5b 的 SSE 是为 7–13s 的网格准备的，这里用不上。

请求级判死全部发生在取数**之前**（同 chat / optimize 的姿态）：结构性错误 422、
显式越界 400、数据未落盘 503——**不静默**，也不返回一份空报告糊弄过去。
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException

from app.backtest.costs import CostModel
from app.data import duckdb_client as dc
from app.factor import analysis
from app.factor.panel import build_event_panel, build_price_panel

router = APIRouter(prefix="/api/v1/factor", tags=["factor"])

#: 窗口跨度上限（自然日）。价格面板要按窗口读全市场行情，无上限的请求是内存与长尾延迟的
#: 入口；750 天 ≈ 三年交易日，够用且可控。超限 422 而非默默截断。
MAX_WINDOW_DAYS = 750

#: 语料为空时的缺省回看长度（正常情况下缺省窗口 = 语料起点 ∩ 行情末端）。
_FALLBACK_DAYS = 365


def _resolve_window(
    *, source: str, start: date | None, end: date | None, corpus_start: date | None, bars_end: date
) -> tuple[date, date]:
    """缺省窗口 = 语料 ∩ 行情的自然窗口；显式越界一律判死（400/422），不静默截断。"""
    resolved_end = end or bars_end
    resolved_start = start or corpus_start or (resolved_end - timedelta(days=_FALLBACK_DAYS))

    if resolved_start > resolved_end:
        raise HTTPException(status_code=422, detail="起始日不能晚于结束日")
    if (resolved_end - resolved_start).days + 1 > MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=422, detail=f"窗口跨度超过 {MAX_WINDOW_DAYS} 天上限，请缩小 start/end"
        )
    if end is not None and end > bars_end:
        raise HTTPException(status_code=400, detail=f"结束日超出行情末端 {bars_end.isoformat()}")
    if source == "event" and start is not None and corpus_start is not None and start < corpus_start:
        raise HTTPException(
            status_code=400, detail=f"起始日早于事件语料起点 {corpus_start.isoformat()}"
        )
    return resolved_start, resolved_end


@router.get("/report")
async def get_factor_report(
    source: Literal["event", "price"] = "event",
    start: date | None = None,
    end: date | None = None,
    direction: Literal["reversal", "momentum"] = "reversal",
    costs: bool = True,
    adjust: Literal["qfq", "raw"] = "qfq",
) -> dict[str, Any]:
    """因子报告：RankIC / ICIR、分层收益、多空价差、换手与费用（M5c）。

    两个因子源走同一条管道：`event`（有向事件的新闻信号因子，池子薄）与
    `price`（20 日动量 / 反转，全市场池）。`direction` 只在 `price` 源有意义；
    `costs=false` 时净曲线为 null，毛曲线照常。
    """
    corpus = await asyncio.to_thread(dc.event_coverage)
    latest = await asyncio.to_thread(dc.latest_dates)
    if not latest["latest_trade_date"]:
        raise HTTPException(status_code=503, detail="行情数据尚未落盘")
    bars_end = date.fromisoformat(str(latest["latest_trade_date"]))
    corpus_start = date.fromisoformat(str(corpus["start"])) if corpus["start"] else None

    resolved_start, resolved_end = _resolve_window(
        source=source, start=start, end=end, corpus_start=corpus_start, bars_end=bars_end
    )

    # 两条取数各司其职：**价格行**=窗口内（`close_lag` 已在 SQL 里按补窗算完）；
    # **交易日**=含补窗（事件的归属日必须能落在窗口**之前**的日子上，否则窗口第一天会把
    # 历史事件整堆吸进来——`bucket_day` 对更早的事件按引擎语义兜底到首日）
    price_rows = await asyncio.to_thread(
        dc.factor_price_rows, resolved_start.isoformat(), resolved_end.isoformat(), adjust=adjust
    )
    market_days = await asyncio.to_thread(
        dc.factor_market_days, resolved_start.isoformat(), resolved_end.isoformat(), adjust=adjust
    )
    price_panel = build_price_panel(price_rows, direction=direction)
    if not price_panel.days or not market_days:
        raise HTTPException(
            status_code=400, detail=f"{resolved_start} 至 {resolved_end} 窗口内没有行情数据"
        )

    if source == "event":
        event_rows = await asyncio.to_thread(dc.factor_event_rows)
        event_panel = build_event_panel(event_rows, market_days)
        values = event_panel.values
        universe: dict[str, Any] = analysis.event_universe(event_panel)
    else:
        values = price_panel.factor
        universe = analysis.price_universe(lookback=dc.FACTOR_LOOKBACK)

    model = CostModel() if costs else CostModel.disabled()
    report = analysis.build_report(
        source=source,
        values=values,
        forward=price_panel.forward,
        all_days=market_days,
        start=resolved_start,
        end=resolved_end,
        costs=model,
        costs_enabled=costs,
        direction=direction if source == "price" else None,
        lookback=dc.FACTOR_LOOKBACK if source == "price" else None,
        params=analysis.report_params(
            source=source,
            direction=direction if source == "price" else None,
            lookback=dc.FACTOR_LOOKBACK,
            adjust=adjust,
            costs=model,
        ),
        universe=universe,
    )
    report["window"] = {
        **report["window"],
        "corpus": corpus,
        "bars_end": bars_end.isoformat(),
    }
    return report
