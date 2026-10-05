"""pytest 全局配置与合成数据夹具。

放在任何 langgraph.checkpoint 导入之前锁死反序列化白名单（与 app/__init__.py 同源）。

`write_bars_parquet` / `write_events_parquet` 供回测用例使用：直接写真实 Parquet，
形状与 `data/` 落盘一致（**不用 mock**）。事件侧刻意保留 `factor_scores` 的
**双重编码**——落盘值是「内容为 JSON 文本的 str」，与生产数据同形。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

CN_TZ = "Asia/Shanghai"

BARS_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("adjustment", pa.string()),
        ("trade_date", pa.date32()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
        ("is_suspended", pa.bool_()),
    ]
)

EVENTS_SCHEMA = pa.schema(
    [
        ("event_id", pa.string()),
        ("symbol", pa.string()),
        ("title", pa.string()),
        ("event_time", pa.timestamp("us", tz=CN_TZ)),
        ("available_at", pa.timestamp("us", tz=CN_TZ)),
        ("direction_norm", pa.string()),
        ("factor_scores", pa.string()),
    ]
)


def encode_factor_scores(score: object) -> str | None:
    """模仿落盘的双重编码：内层 dict → JSON 文本 → 再 JSON 编码一次。"""
    if score is None:
        return None
    return json.dumps(json.dumps({"score": score, "version": "factor-v2"}))


def write_bars_parquet(
    directory: Path,
    symbol: str,
    rows: Iterable[Mapping[str, Any]],
    adjust: str = "qfq",
) -> Path:
    """写单标的单复权日线，列名与真实落盘一致。"""
    directory.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "symbol": symbol,
            "adjustment": adjust,
            "trade_date": row["trade_date"],
            "open": float(row["open"]),
            "high": float(row.get("high", row["open"])),
            "low": float(row.get("low", row["open"])),
            "close": float(row.get("close", row["open"])),
            "volume": float(row.get("volume", 1e5)),
            "is_suspended": bool(row.get("is_suspended", False)),
        }
        for row in rows
    ]
    target = directory / f"{symbol}.{adjust}.parquet"
    table = pa.Table.from_pylist(records, schema=BARS_SCHEMA)
    pq.write_table(table, target)
    return target


def write_events_parquet(
    directory: Path,
    symbol: str,
    rows: Iterable[Mapping[str, Any]],
) -> Path:
    """写单标的事件语料；`score` 传入即按双重编码落盘。"""
    directory.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "event_id": row["event_id"],
            "symbol": symbol,
            "title": str(row.get("title", "")),
            "event_time": row["event_time"],
            "available_at": row.get("available_at", row["event_time"]),
            "direction_norm": row.get("direction_norm"),
            "factor_scores": row.get("factor_scores", encode_factor_scores(row.get("score"))),
        }
        for row in rows
    ]
    target = directory / f"{symbol}.parquet"
    table = pa.Table.from_pylist(records, schema=EVENTS_SCHEMA)
    pq.write_table(table, target)
    return target


def make_backtest_dir(
    root: Path,
    bars: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]] = (),
    symbol: str = "600519",
) -> Path:
    """一次性建好 bars/ 与 events/ 两个子目录（查询层要求两者都有 Parquet）。"""
    write_bars_parquet(root / "bars", symbol, bars)
    write_events_parquet(root / "events", symbol, events)
    return root


def trading_days(start: date, count: int) -> list[date]:
    """连续日历日序列（测试不需要真实交易日历，只需单调递增）。"""
    from datetime import timedelta

    return [start + timedelta(days=i) for i in range(count)]


def ts(text: str) -> datetime:
    """'2026-08-03 10:00:00' → tz-aware Asia/Shanghai。"""
    from zoneinfo import ZoneInfo

    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(CN_TZ))
