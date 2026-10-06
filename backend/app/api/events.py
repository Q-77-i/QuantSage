"""事件语料 API：给 T6 回测页的事件表供数。

两条口径写死在这里：

* **不做 PIT 过滤**——与 `duckdb_client.events()` 一致。按可得时间设卡是消费方
  （回测 feed / 对话工具）的职责，这个端点只负责把落盘事实如实呈现。
* **`event_time` 与 `available_at` 并列给出**——两者之差就是本项目的护城河，
  是 UI 上最直观的展示位。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.backtest.events import parse_factor_score
from app.data import duckdb_client as dc

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
