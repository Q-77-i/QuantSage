"""DuckDB 查询层：对 `data/bars/` 与 `data/events/` 建只读视图。

只读语义由两点保证：连接是进程内 `:memory:`，唯一数据源是 Parquet 文件（DuckDB 读 Parquet 本就只读）；
本模块只建视图、不建表，也不提供任何写回数据的出口。

数据由 `scripts/download_bars.py` 与 `scripts/download_events.py` 落盘；缺数据时抛 `DataNotReady`。
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from app.core.config import get_settings

BARS_VIEW = "bars"
EVENTS_VIEW = "events"
_SUBDIRS = {BARS_VIEW: "bars", EVENTS_VIEW: "events"}


class DataNotReady(RuntimeError):
    """样例数据尚未落盘，或落盘目录里没有 Parquet。"""


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def resolve_data_dir(data_dir: Path | None = None) -> Path:
    return Path(data_dir) if data_dir is not None else get_settings().data_dir


def connect(data_dir: Path | None = None) -> duckdb.DuckDBPyConnection:
    """建连接与两个视图。视图体是 glob，查询时才展开，新增分片无需重建。"""
    base = resolve_data_dir(data_dir)
    missing = [sub for sub in _SUBDIRS.values() if not any((base / sub).glob("*.parquet"))]
    if missing:
        raise DataNotReady(
            f"{base} 下缺少 {'、'.join(missing)} 的 Parquet；"
            "先跑 scripts/download_bars.py 与 scripts/download_events.py"
        )

    con = duckdb.connect()
    try:
        for view, sub in _SUBDIRS.items():
            pattern = str(base / sub / "*.parquet")
            con.execute(
                f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet({_sql_str(pattern)})"
            )
    except Exception:
        con.close()
        raise
    return con


def _fetch(con: duckdb.DuckDBPyConnection, sql: str, params: list) -> list[dict]:
    # 走 arrow 而非 df/fetchall：TIMESTAMPTZ 的 Python 转换依赖已废弃的 pytz，arrow 不需要
    return con.execute(sql, params).to_arrow_table().to_pylist()


def bars(
    symbol: str,
    *,
    start: str | None = None,
    end: str | None = None,
    adjust: str = "qfq",
    data_dir: Path | None = None,
) -> list[dict]:
    """单标的日线，按 trade_date 升序。`start` / `end` 为 YYYY-MM-DD（含端点）。"""
    con = connect(data_dir)
    try:
        sql = f"SELECT * FROM {BARS_VIEW} WHERE symbol = ? AND adjustment = ?"
        params: list = [symbol, adjust]
        if start:
            sql += " AND trade_date >= ?"
            params.append(start)
        if end:
            sql += " AND trade_date <= ?"
            params.append(end)
        return _fetch(con, sql + " ORDER BY trade_date", params)
    finally:
        con.close()


def events(
    symbol: str | None = None,
    *,
    start: str | None = None,
    end: str | None = None,
    data_dir: Path | None = None,
) -> list[dict]:
    """事件语料，按 event_time 升序；`start` / `end` 过滤 `event_time`。

    这里不做 PIT 过滤——按可得时间设卡是消费方（回测/对话工具）的职责，本层只提供事实数据。
    """
    con = connect(data_dir)
    try:
        sql = f"SELECT * FROM {EVENTS_VIEW} WHERE 1 = 1"
        params: list = []
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol)
        if start:
            sql += " AND event_time >= ?"
            params.append(start)
        if end:
            sql += " AND event_time <= ?"
            params.append(end)
        return _fetch(con, sql + " ORDER BY event_time, event_id", params)
    finally:
        con.close()
