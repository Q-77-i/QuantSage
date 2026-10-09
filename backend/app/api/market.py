"""行情 API：给 T6 的 K 线图供数。

只做「裁剪 + 序列化」。`bars` 落盘有 27 列，整表外抛既浪费带宽，也把内部字段
（`quality_status`、`suspension_evidence_*` 一类）泄漏到前端。

DuckDB 查询是同步 IO，一律 `asyncio.to_thread` 卸载，不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path

from app.data import duckdb_client as dc

router = APIRouter(prefix="/api/v1/market", tags=["market"])

#: 六位数字。校验它是为了让「路径写错」返回 422 而不是静默给个空集
SYMBOL_PATTERN = r"^\d{6}$"


def _bar_row(row: dict[str, Any]) -> dict[str, Any]:
    """图表口径的四种价 + 量。

    `time` 用 `YYYY-MM-DD`：Lightweight Charts 的 business day 就吃这个形状，
    且与回测取数同复权口径，图表上的买卖点才落得准。
    """
    return {
        "time": row["trade_date"].isoformat(),
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row["volume"],
        "is_suspended": bool(row.get("is_suspended")),
    }


@router.get("/freshness")
async def get_freshness() -> dict[str, object]:
    """本地数据的最新时点，供页头显示「数据截至 X」。

    路径不与 `/{symbol}/bars` 冲突（那个必须带 `/bars` 段）。数据没落盘时由
    `DataNotReady` → 503，前端据此不显示这枚标签，而不是瞎写一个日期。
    """
    return await asyncio.to_thread(dc.latest_dates)


@router.get("/{symbol}/probe")
async def probe_symbol(
    symbol: Annotated[str, Path(pattern=SYMBOL_PATTERN)],
) -> dict[str, Any]:
    """6 位代码体检：本地有没有这个标的的行情（自选股表单边输边查）。

    **「没有」是答案不是错误**，故无数据回 200 `has_data: false`——`/{symbol}/bars`
    那边同一种情况是 404，因为那里问的是「序列给不给得出」。行情层整体不可用
    （`DataNotReady`）照旧 → 503：那是「依赖没就绪」，前端据此**不拦**用户。两者必须
    分得开——数据没落盘时每个代码都查不到，当成「没有」会让自选股表单对所有输入禁用，
    正是自选股那条降级口径要防的事。
    """
    latest = (await asyncio.to_thread(dc.latest_closes, [symbol])).get(symbol)
    return {
        "symbol": symbol,
        "has_data": latest is not None,
        "latest_trade_date": latest["trade_date"].isoformat() if latest else None,
        "latest_close": latest["close"] if latest else None,
    }


@router.get("/{symbol}/bars")
async def get_bars(
    symbol: Annotated[str, Path(pattern=SYMBOL_PATTERN)],
    start: date | None = None,
    end: date | None = None,
    adjust: Literal["qfq", "raw"] = "qfq",
) -> dict[str, Any]:
    """单标的日线，区间缺省 = 全部可得数据。"""
    rows = await asyncio.to_thread(
        dc.bars,
        symbol,
        start=start.isoformat() if start else None,
        end=end.isoformat() if end else None,
        adjust=adjust,
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"{symbol} 在指定区间无行情数据")
    return {
        "symbol": symbol,
        "adjust": adjust,
        "count": len(rows),
        "bars": [_bar_row(row) for row in rows],
    }
