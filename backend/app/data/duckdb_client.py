"""DuckDB 查询层：对 `data/bars/`、`data/events/` 与冻结交易日历建只读视图。

只读语义由两点保证：连接是进程内 `:memory:`，唯一数据源是 Parquet 文件（DuckDB 读 Parquet 本就只读）；
本模块只建视图、不建表，也不提供任何写回数据的出口。

数据由 `scripts/download_bars.py` 与 `scripts/download_events.py` 落盘；缺数据时抛 `DataNotReady`。
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from app.core.config import get_settings
from app.data.calendar import CALENDAR_FILE

BARS_VIEW = "bars"
EVENTS_VIEW = "events"
#: 交易日历视图：与 `data/` 下的行情/事件无关，直接读仓库内的冻结文件（M2b）
CALENDAR_VIEW = "calendar"
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
        # 日历视图读的是仓库内冻结文件，不是数据目录——它随代码版本走，与落盘数据无关
        con.execute(
            f"CREATE OR REPLACE VIEW {CALENDAR_VIEW} AS SELECT unnest(sessions)::DATE AS trade_date"
            f" FROM read_json({_sql_str(str(CALENDAR_FILE))}, columns={{'sessions': 'VARCHAR[]'}})"
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
    """单标的日线，按 trade_date 升序。`start` / `end` 为 YYYY-MM-DD（含端点）。

    **只返回四价齐全的 bar**。数据源里 `trading_status='no_turnover_observed'` 的日子
    可能整行价量为空（全市场约 271 行/复权，全落在 2026-08-19 之后）——那是「当天没观测到
    成交」的事实，不是 0 元。留它进来会被下游当成价格：`Bar.from_row` 把 None 读成 0.0，
    持仓估值当日期末权益直接塌到现金，回测静默给出 -100%。**无价即无价，不是零价**，
    故在这一层剔除（原始行仍在 Parquet 里，SQL 可查；体检脚本 M2c 会统计其数量）。
    """
    con = connect(data_dir)
    try:
        sql = (
            f"SELECT * FROM {BARS_VIEW} WHERE symbol = ? AND adjustment = ?"
            " AND open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL"
        )
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


def latest_closes(
    symbols: list[str], *, adjust: str = "qfq", data_dir: Path | None = None
) -> dict[str, dict]:
    """一批标的各自最近一根**有价** bar 的收盘价（自选股「加自选以来涨幅」用）。

    一次连接批量取，不做 N 次 connect/close。**未命中的 symbol 不进结果**——调用方按
    缺省处理（自选股要如实留空，不得编价）。空列表提前返回：`IN ()` 是语法错误。

    与 `bars()` 同口径地排除无价 bar：否则「最新可得收盘价」会取到某天没成交的空行，
    把「有价但那是几天前」显示成「取不到价」。
    """
    if not symbols:
        return {}
    con = connect(data_dir)
    try:
        placeholders = ", ".join("?" for _ in symbols)
        rows = _fetch(
            con,
            f"SELECT symbol, trade_date, close FROM {BARS_VIEW} "
            f"WHERE adjustment = ? AND symbol IN ({placeholders}) AND close IS NOT NULL "
            "QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) = 1",
            [adjust, *symbols],
        )
    finally:
        con.close()
    return {
        row["symbol"]: {"trade_date": row["trade_date"], "close": row["close"]} for row in rows
    }


def event_coverage(data_dir: Path | None = None) -> dict[str, object]:
    """本地事件语料的覆盖区间与总条数——**从数据里查出来的**，不写死「最近 3 个月」。

    起点由「首次回填日」固化，终点随日增前移；事件驱动策略的时间收口、
    报告 `meta.event_coverage`、前端提示都读这一份。
    """
    con = connect(data_dir)
    try:
        rows = _fetch(
            con,
            f"SELECT min(event_time)::DATE AS first_day, max(event_time)::DATE AS last_day,"
            f" count(*) AS rows FROM {EVENTS_VIEW}",
            [],
        )
    finally:
        con.close()
    row = rows[0] if rows else {"first_day": None, "last_day": None, "rows": 0}
    return {
        "start": row["first_day"].isoformat() if row["first_day"] else None,
        "end": row["last_day"].isoformat() if row["last_day"] else None,
        "rows": int(row["rows"] or 0),
    }


def latest_dates(data_dir: Path | None = None) -> dict[str, object]:
    """样例数据的最新时点：行情最后一根 bar 的交易日、事件最晚 `available_at`。

    前端页头拿它显示「数据截至 X」——**必须是查出来的**，不能写死：M2 的日增量 ETL
    接上后这个值会随每次拉取自动前移，界面不用改一个字。
    """
    con = connect(data_dir)
    try:
        bars = _fetch(con, f"SELECT max(trade_date) AS day FROM {BARS_VIEW}", [])
        events = _fetch(
            con,
            f"SELECT max(available_at) AS ts, min(event_time)::DATE AS first_day,"
            f" max(event_time)::DATE AS last_day, count(*) AS rows FROM {EVENTS_VIEW}",
            [],
        )
    finally:
        con.close()

    day = bars[0]["day"] if bars else None
    row = events[0] if events else {}
    stamp = row.get("ts")
    first, last = row.get("first_day"), row.get("last_day")
    return {
        "latest_trade_date": day.isoformat() if day else None,
        "latest_event_available_at": stamp.isoformat() if stamp else None,
        # 语料覆盖区间（M2b）：事件驱动能回测到哪，看的就是这一段
        "event_coverage": {
            "start": first.isoformat() if first else None,
            "end": last.isoformat() if last else None,
            "rows": int(row.get("rows") or 0),
        },
    }


def events(
    symbol: str | None = None,
    *,
    start: str | None = None,
    end: str | None = None,
    data_dir: Path | None = None,
) -> list[dict]:
    """事件语料，按 event_time 升序；`start` / `end` 过滤 `event_time`。

    **按标的过滤走 `list_contains(symbols, ?)`**：M2b 起语料是「一条事件一行、标的是数组」
    （同一事件挂多只股票只存一行），等值比较会永远匹配不到。传 `symbol=None` 取全市场。

    这里不做 PIT 过滤——按可得时间设卡是消费方（回测/对话工具）的职责，本层只提供事实数据。
    """
    con = connect(data_dir)
    try:
        sql = f"SELECT * FROM {EVENTS_VIEW} WHERE 1 = 1"
        params: list = []
        if symbol:
            sql += " AND list_contains(symbols, ?)"
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
