"""事件语料 API：给 T6 回测页的事件表供数。

两条口径写死在这里：

* **不做 PIT 过滤**——与 `duckdb_client.events()` 一致。按可得时间设卡是消费方
  （回测 feed / 对话工具）的职责，这个端点只负责把落盘事实如实呈现。
* **`event_time` 与 `available_at` 并列给出**——两者之差就是本项目的护城河，
  是 UI 上最直观的展示位。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from app.backtest.events import parse_factor_score
from app.data import duckdb_client as dc
from app.data import naming

router = APIRouter(prefix="/api/v1/events", tags=["events"])

SYMBOL_PATTERN = r"^\d{6}$"


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _event_row(row: dict[str, Any]) -> dict[str, Any]:
    """裁到 UI 真正要的列。

    `source` / `original_source` / `content_hash` 是 PRD §5 的硬性要求（来源标注必须
    可见），原样带出；`content_hash` 不在这里截断，展示该由 UI 决定。
    """
    return {
        "event_id": row["event_id"],
        "title": row["title"],
        "summary": row.get("summary"),
        "event_type": row.get("event_type"),
        "event_time": _iso(row.get("event_time")),
        "available_at": _iso(row.get("available_at")),
        "direction_norm": row.get("direction_norm"),
        "importance_score": row.get("importance_score"),
        # 落盘是双重编码的 JSON 文本，解析出 score 再出接口（不把转义字符串丢给前端）
        "score": parse_factor_score(row.get("factor_scores")),
        "source": row.get("source"),
        "original_source": row.get("original_source"),
        "content_hash": row.get("content_hash"),
        "quality_status": row.get("quality_status"),
    }


#: 类型白名单的合法取值（与 `KEPT_EVENT_TYPES` 同源，但只收语料里真出现过的五类）
EVENT_TYPES = frozenset({"announcement", "news", "person", "policy", "research"})

#: 日期带默认回看的自然日数。30 天 ≈ 一个月的滚动窗口，实测聚合 74ms。
CALENDAR_DAYS = 30


def _feed_row(row: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """一条快讯。字段集与 M7 证据面板对齐（前端 `evidenceRows` 直接吃它）。

    `symbols` 出成 `{code, name}`：名称来自字典（覆盖 96.7%），**缺名就给 null 而不是编一个**
    ——快讯标题里通常已经写了公司名，多一个假的更糟。
    """
    return {
        "event_id": row["event_id"],
        "event_type": row.get("event_type"),
        "title": row.get("title"),
        "summary": row.get("summary"),
        "event_time": _iso(row.get("event_time")),
        "available_at": _iso(row.get("available_at")),
        "direction_norm": row.get("direction_norm"),
        "industries": list(row.get("industries") or []),
        "importance_score": row.get("importance_score"),
        "source": row.get("source"),
        "original_source": row.get("original_source"),
        "source_url": row.get("source_url"),
        "content_hash": row.get("content_hash"),
        "symbols": [
            {"code": code, "name": names.get(code)}
            for code in (row.get("symbols") or [])
        ],
    }


@router.get("/calendar")
async def get_calendar(
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    high_score: Annotated[float, Query(ge=0, le=100)] = 60.0,
) -> dict[str, Any]:
    """日期带：区间内**每日**的事件计数（总数 / 高分 / 按类型）。

    `end` 缺省取语料里最后一个有事件的日子（**查出来的**，不写死今天——语料是滚动窗口，
    写死会指向一个还没拉到的日子）。`start` 缺省回看 `CALENDAR_DAYS` 个自然日。

    `day` 一律是 `YYYY-MM-DD` 字符串，前端不做时区换算——语料的 `event_time` 是北京时间，
    按日分组已在服务端做完（`event_time::DATE`），两边各自换算必然漂。
    """
    if end is None:
        coverage = await asyncio.to_thread(dc.event_coverage)
        if not coverage["end"]:
            return {"start": None, "end": None, "days": [], "coverage": coverage}
        end = date.fromisoformat(str(coverage["end"]))
    if start is None:
        start = end - timedelta(days=CALENDAR_DAYS - 1)
    days = await asyncio.to_thread(
        dc.event_days, start.isoformat(), end.isoformat(), high_score=high_score
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "high_score": high_score,
        "days": days,
    }


@router.get("/feed")
async def get_feed(
    day: Annotated[date | None, Query(alias="date")] = None,
    types: Annotated[list[str] | None, Query()] = None,
    min_importance: Annotated[float | None, Query(ge=0, le=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """某一天的全市场事件流水，按重要度降序分页（M10 快讯流）。

    ⚠ **重要度不跨类型可比**——实测 2026-09-30 按分数降序的**前 200 名全是 `announcement`**
    （该日公告均分 30.9 但满分 100，新闻均分 48.5、满分 92）。所以本端点**不替调用方设默认类型**：
    要「快讯」就自己把 `announcement` 排除在外，口径由调用方写在界面上。

    `types` 是白名单（可重复：`?types=news&types=policy`）；非法取值 **422 而不是静默忽略**
    ——静默忽略会让「怎么一条都没有」变成无从追查的谜。
    """
    if types is not None:
        unknown = sorted(set(types) - EVENT_TYPES)
        if unknown:
            raise HTTPException(status_code=422, detail=f"未知的事件类型：{'、'.join(unknown)}")

    result = await asyncio.to_thread(
        dc.event_feed,
        day.isoformat() if day else None,
        types=sorted(set(types)) if types else None,
        min_importance=min_importance,
        limit=limit,
        offset=offset,
    )
    names = await asyncio.to_thread(lambda: naming.primary_names(naming.load_dictionary()))
    return {
        **result,
        "offset": offset,
        "limit": limit,
        "types": sorted(set(types)) if types else None,
        "min_importance": min_importance,
        "items": [_feed_row(row, names) for row in result["items"]],
    }


@router.get("")
async def get_events(
    symbol: Annotated[str, Query(pattern=SYMBOL_PATTERN)],
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """单标的事件语料，按 `event_time` 升序；`start` / `end` 过滤的也是 `event_time`。

    「该标的在这个窗口内没有事件」是合法结果（返回空列表而非 404）——它是一个有意义
    的事实，UI 有对应的空态；不像 bars 为空那样让图表无从画起。
    """
    rows = await asyncio.to_thread(
        dc.events,
        symbol,
        start=start.isoformat() if start else None,
        end=end.isoformat() if end else None,
    )
    return {
        "symbol": symbol,
        "count": len(rows),
        "events": [_event_row(row) for row in rows],
    }
