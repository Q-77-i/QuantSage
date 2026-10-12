"""行情 API：给 T6 的 K 线图供数。

只做「裁剪 + 序列化」。`bars` 落盘有 27 列，整表外抛既浪费带宽，也把内部字段
（`quality_status`、`suspension_evidence_*` 一类）泄漏到前端。

DuckDB 查询是同步 IO，一律 `asyncio.to_thread` 卸载，不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query

from app.backtest import benchmark
from app.backtest.a_share_rules import count_limit_hits
from app.data import duckdb_client as dc
from app.data import naming

router = APIRouter(prefix="/api/v1/market", tags=["market"])

#: 六位数字。校验它是为了让「路径写错」返回 422 而不是静默给个空集
SYMBOL_PATTERN = r"^\d{6}$"

#: 截面支持的排序键（与查询层白名单同源，见 `dc.CROSS_SECTION_SORTS`）
SortKey = Literal["change_pct", "amount", "close", "turnover_pct", "symbol"]
SortOrder = Literal["desc", "asc"]


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


def _match_symbols(query: str, primary: dict[str, str]) -> list[str]:
    """按**代码或名称**匹配标的。代码按前缀（输「6005」能出 600519），名称按子串。

    名称取字典的**当前名**（众数），不碰别名——别名里混着源侧错配的别家公司名
    （实测 11 例一名多写里有 6 例是这种），拿来搜索会搜出不相干的标的。
    """
    needle = query.strip()
    if not needle:
        return []
    return sorted(
        symbol
        for symbol, name in primary.items()
        if symbol.startswith(needle) or needle in name
    )


@router.get("/cross-section")
async def get_cross_section(
    trade_date: Annotated[date | None, Query(alias="date")] = None,
    adjust: Literal["qfq", "raw"] = "qfq",
    sort: SortKey = "change_pct",
    order: SortOrder = "desc",
    q: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """某交易日的全市场截面：收盘 / 涨跌幅 / 成交额 / 换手率，带排序与分页（M5a）。

    `date` 缺省取行情里最后一个有价交易日（与 `/freshness` 同源）。`q` 走**代码前缀
    或名称子串**——名称来自事件语料抽出的字典（覆盖 96.8%），没有名字的标的仍能按
    代码搜到，此时 `name` 为 null。字典缺失时全部 `name` 为 null，**不是 500**。

    实测全市场排序 6–12ms（5,572 只有价），不落缓存。
    """
    primary = await asyncio.to_thread(lambda: naming.primary_names(naming.load_dictionary()))
    symbols = _match_symbols(q, primary) if q else None

    page = await asyncio.to_thread(
        dc.cross_section,
        trade_date.isoformat() if trade_date else None,
        adjust=adjust,
        sort=sort,
        order=order,
        symbols=symbols,
        limit=limit,
        offset=offset,
    )
    return {
        **page,
        "adjust": adjust,
        "sort": sort,
        "order": order,
        "items": [
            {
                "symbol": row["symbol"],
                "name": primary.get(row["symbol"]),
                "close": row["close"],
                "change_pct": row["change_pct"],
                "amount": row["amount"],
                "turnover_pct": row["turnover_pct"],
            }
            for row in page["items"]
        ],
    }


@router.get("/symbols")
async def search_symbols(
    q: Annotated[str, Query(min_length=1, max_length=40)],
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    """按代码或名称搜标的（自选股表单与 M10 标的浏览用）。

    与 `/cross-section` 共用同一套匹配口径，但**不依赖行情**——加的是一只还没看过
    K 线的股票时，不该因为查不到当日行情就搜不出来。字典缺失时如实返回空列表。
    """
    primary = await asyncio.to_thread(lambda: naming.primary_names(naming.load_dictionary()))
    matched = _match_symbols(q, primary)[:limit]
    return {
        "query": q,
        "count": len(matched),
        "items": [{"symbol": symbol, "name": primary[symbol]} for symbol in matched],
    }


#: 涨跌停家数的口径说明。与 `a_share_rules.count_limit_hits` 的 docstring **同源**——
#: 界面照登服务端的话，前端不自己拼一遍免责口径（M5b 定过的规矩）。
_LIMIT_NOTE = (
    "涨跌停按 raw（不复权）前收盘自算：前收 × 板别幅度（主板 10% / 创业板与科创板 20% / "
    "北交所 30%，主板 ST 5%）后四舍五入到分，与当日 raw 收盘比。"
    "必须用不复权序列——涨跌停价是价格概念，复权价上这个整数关系不成立；"
    "也不能读 change_pct，它在 raw 口径下自 2026-09-21 起整列为空。"
)


def _overview_sync(day: str | None, adjust: str) -> dict[str, Any]:
    """一次连接算完大盘总览（建视图有几十毫秒，四条查询各建一次不划算）。"""
    con = dc.connect()
    try:
        breadth = dc.market_breadth(day, adjust=adjust, con=con)
        trade_date = breadth["trade_date"]
        if trade_date is None:
            return {"trade_date": None, "adjust": adjust, "breadth": None,
                    "limits": None, "limits_meta": None, "index": None, "as_of": None}
        # 涨跌停与复权口径无关（走 raw），但**日期**跟着截面走
        raw_day = dc.raw_day_closes(trade_date, con=con)
    finally:
        con.close()

    # 字典缺失不是错误：ST 档退化成「无」，家数照出（`st_symbols` 覆盖 96.7%）
    names = naming.load_dictionary()
    st = naming.st_symbols(names)
    limits = count_limit_hits(raw_day["rows"], st_symbols=st)
    fresh = dc.latest_dates()
    return {
        "trade_date": trade_date,
        "adjust": adjust,
        "amount": breadth["amount"],
        "breadth": {
            "total": breadth["total"],
            "up": breadth["up"],
            "down": breadth["down"],
            "flat": breadth["flat"],
            "without_pct": breadth["without_pct"],
        },
        "limits": limits.to_json(),
        "limits_meta": {
            "adjust": "raw",
            "st_source": "dictionary" if names else "missing",
            "st_symbols": len(st),
            "note": _LIMIT_NOTE,
        },
        # 与回测基准**同一把尺子**（`benchmark._daily_rows` + `EXCLUDE_ABS_CHANGE_PCT`）
        "index": benchmark.market_index_level(date.fromisoformat(trade_date), adjust=adjust),
        "as_of": {
            "bars": fresh.get("latest_trade_date"),
            "events": (fresh.get("latest_event_available_at") or "")[:10] or None,
        },
    }


@router.get("/overview")
async def get_overview(
    trade_date: Annotated[date | None, Query(alias="date")] = None,
    adjust: Literal["qfq", "raw"] = "qfq",
) -> dict[str, Any]:
    """某交易日的**大盘总览**（M10 研究首页）：涨跌家数 / 涨跌停家数 / 成交额 / 全市场等权代理。

    四条读数一次给全，前端一次请求画完整个仪表盘。`date` 缺省取行情里最后一个有价交易日。

    **两个「截至」一并给出**（`as_of.bars` / `as_of.events`）：行情是年度整片重下载、不日更，
    事件语料却按日增量——两者天然不同步（实测行情 2026-09-30 / 事件 2026-10-08）。
    这是数据通道的真相，界面应当并列显示而不是挑一个假装同步。

    `index` **不是指数**：数据源不覆盖指数（M2a 已核实），那是全市场等权代理，
    口径与回测报告的基准曲线逐日相等（`kind` / `note` 照登界面）。
    """
    return await asyncio.to_thread(_overview_sync, trade_date.isoformat() if trade_date else None, adjust)


#: 板块归属口径说明（同 `dc.sector_performance` 的 docstring，一处写、界面照登）
_SECTOR_NOTE = (
    "行业 / 概念标签来自事件语料（非权威行业分类），归属取**截至当日收盘的 PIT 累积**："
    "某标的属于某行业，当且仅当截至该日 15:00 已有事件同时提到这个行业与这只标的。"
    "一只标的可同时属于多个标签，故**各行业成交额之和大于全市场**，不可读作份额。"
)


@router.get("/sectors")
async def get_sectors(
    trade_date: Annotated[date | None, Query(alias="date")] = None,
    adjust: Literal["qfq", "raw"] = "qfq",
    min_symbols: Annotated[int, Query(ge=3, le=200)] = 10,
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
) -> dict[str, Any]:
    """某交易日的**行业 / 概念板块表现**（M10 板块块）：成分数 / 成交额 / 平均与中位涨跌幅。

    ⚠ **`coverage` 必须照登界面**——归属来自事件语料，按事件类型覆盖不均
    （news 44.9% / policy 100% / announcement 0%，全局 31.4%；实测当日截面覆盖 87.7%）。
    画成一张「看起来完整」的板块图就是在编数。

    `min_symbols` 是**样本下限**：小样本的均值会霸榜（实测当日榜首是 21 只的行业），
    故响应里每条都带 `symbols`，供界面显示「这个均值建立在多少个标的上」。
    `avg_pct` 与 `median_pct` 一并给出：两者差得远说明这一档被少数极端值主导。

    不落缓存：实测单日 40ms（含建视图），比缓存的维护成本便宜。
    """
    result = await asyncio.to_thread(
        dc.sector_performance,
        trade_date.isoformat() if trade_date else None,
        adjust=adjust,
        min_symbols=min_symbols,
        limit=limit,
    )
    cov = result["coverage"]
    return {
        "trade_date": result["trade_date"],
        "adjust": adjust,
        "min_symbols": min_symbols,
        "count": len(result["rows"]),
        "coverage": {
            "total": cov["total"],
            "covered": cov["covered"],
            "ratio": (cov["covered"] / cov["total"]) if cov["total"] else None,
        },
        "items": [
            {
                "industry": row["industry"],
                "symbols": int(row["symbols"]),
                "amount": float(row["amount"]) if row["amount"] is not None else None,
                "avg_pct": float(row["avg_pct"]) if row["avg_pct"] is not None else None,
                "median_pct": float(row["median_pct"]) if row["median_pct"] is not None else None,
            }
            for row in result["rows"]
        ],
        "note": _SECTOR_NOTE,
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
