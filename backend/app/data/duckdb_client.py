"""DuckDB 查询层：对 `data/bars/`、`data/events/` 与冻结交易日历建只读视图。

只读语义由两点保证：连接是进程内 `:memory:`，唯一数据源是 Parquet 文件（DuckDB 读 Parquet 本就只读）；
本模块只建视图、不建表，也不提供任何写回数据的出口。

数据由 `scripts/download_bars.py` 与 `scripts/download_events.py` 落盘；缺数据时抛 `DataNotReady`。
"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb

from app.core.config import get_settings
from app.data import cache
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


#: 每线程 × 数据目录一条连接。DuckDB 连接不是线程安全的，故按线程隔离；
#: 视图体是 glob，查询时才展开，新增分片不必重建，所以同一目录可以长期复用。
_LOCAL = threading.local()


class _CachedConnection:
    """`connect()` 返回的稳定句柄：底层连接由线程本地缓存持有。

    旧调用方普遍写 `finally: con.close()`；这里的 `close()` 只表示「本次用完」，
    真正关闭发生在数据目录切换或 `reset_connections()`。这样 21 个调用点不必各改一遍，
    也不会把共享连接关掉。
    """

    __slots__ = ("_con", "_base")

    def __init__(self, con: duckdb.DuckDBPyConnection, base: Path) -> None:
        self._con = con
        self._base = base

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)

    def close(self) -> None:
        """兼容旧调用方；连接由线程本地缓存持有，见 `reset_connections()`。"""
        return None

    def __enter__(self) -> _CachedConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def reset_connections() -> None:
    """关闭当前线程缓存的连接（测试与显式回收用）。"""
    cached = getattr(_LOCAL, "connection", None)
    if cached is None:
        return
    _LOCAL.connection = None
    cached._con.close()


def connect(data_dir: Path | None = None) -> Any:
    """取本线程在该数据目录上的连接（首次调用时建视图）。

    **数据缺失检查在缓存命中之前**：文件被删掉后，旧连接不能把 `DataNotReady`
    变成查询期错误。视图体是 glob，新增分片无需重建。
    """
    base = resolve_data_dir(data_dir)
    missing = [sub for sub in _SUBDIRS.values() if not any((base / sub).glob("*.parquet"))]
    if missing:
        raise DataNotReady(
            f"{base} 下缺少 {'、'.join(missing)} 的 Parquet；"
            "先跑 scripts/download_bars.py 与 scripts/download_events.py"
        )

    cached = getattr(_LOCAL, "connection", None)
    if cached is not None and cached._base == base:
        return cached
    if cached is not None:
        cached._con.close()
        _LOCAL.connection = None

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
    cached = _CachedConnection(con, base)
    _LOCAL.connection = cached
    return cached


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
    """单标的日线（**带缓存**，见 `app/data/cache.py`——加在本模块内部是 E3c 的纪律：
    一处实现，28 个调用点不会各做一半）。，按 trade_date 升序。`start` / `end` 为 YYYY-MM-DD（含端点）。

    **只返回四价齐全的 bar**。数据源里 `trading_status='no_turnover_observed'` 的日子
    可能整行价量为空（全市场约 271 行/复权，全落在 2026-08-19 之后）——那是「当天没观测到
    成交」的事实，不是 0 元。留它进来会被下游当成价格：`Bar.from_row` 把 None 读成 0.0，
    持仓估值当日期末权益直接塌到现金，回测静默给出 -100%。**无价即无价，不是零价**，
    故在这一层剔除（原始行仍在 Parquet 里，SQL 可查；体检脚本 M2c 会统计其数量）。
    """
    return cache.through(
        "bars",
        (adjust, symbol, start, end),
        lambda: _bars_query(symbol, start=start, end=end, adjust=adjust, data_dir=data_dir),
        # ⚠ 传**解析后**的目录，不是入参：缓存的键必须与 `connect()` 实际读的目录一致——
        # 两者不一致时（`data_dir=None` 走 settings、`resolve_data_dir` 被 monkeypatch 等）
        # 缓存会按 A 目录算键、从 B 目录读数据，实测表现为「测试里所有 None 调用共用一个键」
        data_dir=resolve_data_dir(data_dir),
    )


def _bars_query(
    symbol: str,
    *,
    start: str | None = None,
    end: str | None = None,
    adjust: str = "qfq",
    data_dir: Path | None = None,
) -> list[dict]:
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


def trade_dates(adjust: str | None = None, data_dir: Path | None = None) -> list[date]:
    """行情里出现过的全部交易日（去重、升序）。`adjust=None` 取全部复权口径的并集。

    日历对账专用：`bars()` 是逐标的的，对账要的是「全市场有哪些交易日」。**走视图而非
    逐片 glob**——分片命名或布局变了，这里跟着视图一起变，不会留下第二份会漂移的实现。
    """
    return cache.through(
        "trade_dates",
        (adjust,),
        lambda: _trade_dates_query(adjust=adjust, data_dir=data_dir),
        # ⚠ 传**解析后**的目录，不是入参：缓存的键必须与 `connect()` 实际读的目录一致——
        # 两者不一致时（`data_dir=None` 走 settings、`resolve_data_dir` 被 monkeypatch 等）
        # 缓存会按 A 目录算键、从 B 目录读数据，实测表现为「测试里所有 None 调用共用一个键」
        data_dir=resolve_data_dir(data_dir),
    )


def _trade_dates_query(adjust: str | None = None, data_dir: Path | None = None) -> list[date]:
    con = connect(data_dir)
    try:
        sql = f"SELECT DISTINCT trade_date FROM {BARS_VIEW}"
        params: list = []
        if adjust is not None:
            sql += " WHERE adjustment = ?"
            params.append(adjust)
        rows = _fetch(con, sql + " ORDER BY 1", params)
    finally:
        con.close()
    return [row["trade_date"] for row in rows]


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
    # 键里对 symbols **排序**（结果按 symbol 索引，与入参顺序无关）——提高命中率，不改语义
    return cache.through(
        "latest_closes",
        (adjust, tuple(sorted(symbols))),
        lambda: _latest_closes_query(symbols, adjust=adjust, data_dir=data_dir),
        # ⚠ 传**解析后**的目录，不是入参：缓存的键必须与 `connect()` 实际读的目录一致——
        # 两者不一致时（`data_dir=None` 走 settings、`resolve_data_dir` 被 monkeypatch 等）
        # 缓存会按 A 目录算键、从 B 目录读数据，实测表现为「测试里所有 None 调用共用一个键」
        data_dir=resolve_data_dir(data_dir),
    )


def _latest_closes_query(symbols: list[str], *, adjust: str = "qfq", data_dir: Path | None = None) -> dict[str, dict]:
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


#: 截面查询允许的排序键。**白名单而非转义**：排序列会拼进 SQL，参数化占位符管不到它。
CROSS_SECTION_SORTS = frozenset({"change_pct", "amount", "close", "turnover_pct", "symbol"})

#: 截面返回的列（查询层不掺名称——名称要过 PIT 与众数规则，那属 `naming` 层）
_CROSS_SECTION_COLUMNS = ("symbol", "close", "change_pct", "amount", "turnover_pct")


def cross_section(
    trade_date: str | None = None,
    *,
    adjust: str = "qfq",
    sort: str = "change_pct",
    order: str = "desc",
    symbols: list[str] | None = None,
    industry: str | None = None,
    limit: int = 50,
    offset: int = 0,
    data_dir: Path | None = None,
) -> dict[str, object]:
    """某交易日的全市场截面（收盘 / 涨跌幅 / 成交额 / 换手率）+ 排序 + 分页。

    `trade_date` 缺省取**该复权口径下最后一根有价 bar 的交易日**——与 `freshness`
    同源，不写死「今天」（数据是日增的，写死会指向一个空日子）。

    只返回四价齐全且有收盘价的行（与 `bars()` 同口径：无价即无价，不是零价）。
    `turnover_pct` 在源侧 2026-08 起逐步停更、2026-09 起全空——**如实返回 null**，
    不填 0（0 会被读成「换手率极低」，那是另一回事）。

    排序带 `NULLS LAST`：换手率大面积为空，让空值沉底比随数据库默认行为漂移可预期。

    `industry` 按 **PIT 累积归属**过滤（`tags_source` 的同一支）——板块块点一条进来的
    就是这批标的，故列表的 `total` 与条上标的只数**必须相等**。
    """
    if sort not in CROSS_SECTION_SORTS:
        raise ValueError(f"不支持的排序键 {sort!r}")
    return cache.through(
        "cross_section",
        (trade_date, adjust, sort, order, tuple(symbols or ()), industry, limit, offset),
        lambda: _cross_section_query(
            trade_date,
            adjust=adjust,
            sort=sort,
            order=order,
            symbols=symbols,
            industry=industry,
            limit=limit,
            offset=offset,
            data_dir=data_dir,
        ),
        # ⚠ 传**解析后**的目录，不是入参：缓存的键必须与 `connect()` 实际读的目录一致——
        # 两者不一致时（`data_dir=None` 走 settings、`resolve_data_dir` 被 monkeypatch 等）
        # 缓存会按 A 目录算键、从 B 目录读数据，实测表现为「测试里所有 None 调用共用一个键」
        data_dir=resolve_data_dir(data_dir),
    )


def _cross_section_query(
    trade_date: str | None = None,
    *,
    adjust: str = "qfq",
    sort: str = "change_pct",
    order: str = "desc",
    symbols: list[str] | None = None,
    industry: str | None = None,
    limit: int = 50,
    offset: int = 0,
    data_dir: Path | None = None,
) -> dict[str, object]:
    direction = "DESC" if order == "desc" else "ASC"

    con = connect(data_dir)
    try:
        if trade_date is None:
            row = _fetch(
                con,
                f"SELECT max(trade_date) AS day FROM {BARS_VIEW}"
                " WHERE adjustment = ? AND close IS NOT NULL",
                [adjust],
            )
            day = row[0]["day"] if row else None
            if day is None:
                return {"trade_date": None, "total": 0, "count": 0, "items": []}
            trade_date = day.isoformat()

        where = [
            "adjustment = ?",
            "trade_date = ?",
            "close IS NOT NULL",
            "open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL",
        ]
        params: list = [adjust, trade_date]
        if industry is not None:
            # 半连接（`IN (子查询)`）而不是 JOIN：截面的行本来就不该被标签数量放大
            where.append(
                "symbol IN (SELECT DISTINCT s.sym FROM "
                f"{tags_source()}"
                " WHERE i.ind = ? AND s.sym <> '' AND available_at <= CAST(? AS TIMESTAMPTZ))"
            )
            params.extend([industry, _pit_cutoff(str(trade_date))])
        if symbols is not None:
            if not symbols:
                return {"trade_date": trade_date, "total": 0, "count": 0, "items": []}
            where.append(f"symbol IN ({', '.join('?' for _ in symbols)})")
            params.extend(symbols)

        condition = " AND ".join(where)
        total = _fetch(con, f"SELECT count(*) AS n FROM {BARS_VIEW} WHERE {condition}", params)[0]
        rows = _fetch(
            con,
            f"SELECT {', '.join(_CROSS_SECTION_COLUMNS)} FROM {BARS_VIEW} WHERE {condition}"
            f" ORDER BY {sort} {direction} NULLS LAST, symbol ASC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
    finally:
        con.close()
    return {
        "trade_date": trade_date,
        "total": int(total["n"]),
        "count": len(rows),
        "items": rows,
    }


def symbol_rank(
    symbol: str,
    trade_date: str,
    *,
    adjust: str = "qfq",
    data_dir: Path | None = None,
) -> dict[str, object] | None:
    """某标的在**某交易日全市场截面**里的位置（涨跌幅与成交额的排名与分位）。

    给 M8 的因子角色用：它要回答的是「今天在全市场里排第几」，不是跑一条 IC 管道
    （那是 M5c 的活，全市场按日 ~0.6–0.9s，一次深度研究不该为它多花这一秒）。

    分位口径 = **排在它前面（含自己）的比例**：第 1 名 → 1.0、最后一名 → 1/total。
    `change_pct` 缺失的行不参与排名（`rank() ... NULLS LAST` 仍会给它们名次，故先滤掉）。
    该日无任何有价 bar 时返回 `None`——调用方按「取不到」处理，不编数。
    """
    con = connect(data_dir)
    try:
        rows = _fetch(
            con,
            f"""
            WITH day AS (
                SELECT symbol, change_pct, amount
                FROM {BARS_VIEW}
                WHERE adjustment = ? AND trade_date = ?
                  AND close IS NOT NULL AND open IS NOT NULL
                  AND high IS NOT NULL AND low IS NOT NULL
                  AND change_pct IS NOT NULL
            ),
            ranked AS (
                SELECT symbol, change_pct, amount,
                       rank() OVER (ORDER BY change_pct DESC) AS rank_change,
                       rank() OVER (ORDER BY amount DESC NULLS LAST) AS rank_amount,
                       count(*) OVER () AS total
                FROM day
            )
            SELECT * FROM ranked WHERE symbol = ?
            """,
            [adjust, trade_date, symbol],
        )
    finally:
        con.close()
    if not rows:
        return None
    row = rows[0]
    total = int(row["total"])
    rank_change = int(row["rank_change"])
    payload: dict[str, object] = {
        "trade_date": trade_date,
        "symbol": symbol,
        "change_pct": float(row["change_pct"]),
        "amount": float(row["amount"]) if row.get("amount") is not None else None,
        "rank_change": rank_change,
        "total": total,
        "percentile_change": (total - rank_change + 1) / total,
    }
    if row.get("rank_amount") is not None:
        payload["rank_amount"] = int(row["rank_amount"])
        payload["percentile_amount"] = (total - int(row["rank_amount"]) + 1) / total
    return payload


def tags_source() -> str:
    """PIT 累积归属的「事件 → (行业, 标的) 配对」展开源。**两个消费方共用同一支**：
    板块块按 `ind` 分组、行业钻取按 `ind` 过滤。口径必须是同一支，否则点进板块条看到的
    成分与条上那个数字对不上。

    ⚠ 写法是横向积（`FROM …, UNNEST(…) AS …`），**不是同一条 SELECT 里写两个 `unnest`**
    ——后者 DuckDB 会按**位置 zip**、短的补 NULL，一条「1 个行业 × 3 只标的」的事件
    只产出 1 个正确配对，不报错、只静默少算（M10a 实测踩过）。
    """
    return f"{EVENTS_VIEW}, UNNEST(industries) AS i(ind), UNNEST(symbols) AS s(sym)"


def _pit_cutoff(trade_date: str) -> str:
    """当日收盘时刻（北京时间）。归属取「截至这一刻已可得的事件」。"""
    return f"{trade_date} 15:00:00+08"


def _latest_trade_date(con: duckdb.DuckDBPyConnection, adjust: str) -> str | None:
    """该复权口径下最后一根**有价** bar 的交易日（`None` = 一片都没有）。

    「最新」必须是查出来的：数据是日增的，写死一个日期会在某天指向空日子
    （`cross_section` / `latest_dates` 同口径）。
    """
    rows = _fetch(
        con,
        f"SELECT max(trade_date) AS day FROM {BARS_VIEW}"
        " WHERE adjustment = ? AND close IS NOT NULL",
        [adjust],
    )
    day = rows[0]["day"] if rows else None
    return day.isoformat() if day else None


def market_breadth(
    trade_date: str | None = None,
    *,
    adjust: str = "qfq",
    data_dir: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, object]:
    """某交易日的**全市场广度**：涨跌平家数、有价标的数、总成交额（M10 大盘总览）。

    一条聚合搞定（实测 5,556 只有价、含建视图 277ms）。口径与 `cross_section` 逐条相同：
    四价齐全且收盘非空才算「有价」，`change_pct` 缺失（实测 4.8%）的行**不进涨跌平任何一档**——
    它们只是「没有涨跌幅这个事实」，归到「平」会把缺失读成零涨跌。

    `trade_date` 缺省取该复权口径下最后一根有价 bar 的交易日（与 `cross_section` / `freshness` 同源）。
    """
    own = con is None
    con = con if con is not None else connect(data_dir)
    try:
        if trade_date is None:
            trade_date = _latest_trade_date(con, adjust)
            if trade_date is None:
                return {"trade_date": None, "total": 0, "up": 0, "down": 0, "flat": 0,
                        "without_pct": 0, "amount": None}

        rows = _fetch(
            con,
            f"""
            SELECT count(*)                                                   AS total,
                   count(*) FILTER (WHERE change_pct > 0)                     AS up,
                   count(*) FILTER (WHERE change_pct < 0)                     AS down,
                   count(*) FILTER (WHERE change_pct = 0)                     AS flat,
                   count(*) FILTER (WHERE change_pct IS NULL)                 AS without_pct,
                   sum(amount)                                                AS amount
            FROM {BARS_VIEW}
            WHERE adjustment = ? AND trade_date = ?
              AND close IS NOT NULL AND open IS NOT NULL
              AND high IS NOT NULL AND low IS NOT NULL
            """,
            [adjust, trade_date],
        )
    finally:
        if own:
            con.close()
    row = rows[0] if rows else {}
    return {
        "trade_date": trade_date,
        "total": int(row.get("total") or 0),
        "up": int(row.get("up") or 0),
        "down": int(row.get("down") or 0),
        "flat": int(row.get("flat") or 0),
        "without_pct": int(row.get("without_pct") or 0),
        "amount": float(row["amount"]) if row.get("amount") is not None else None,
    }


#: 前收盘的回看窗口（交易日数）。取 2 是**保守**：停牌更久的标的拿不到前收 ⇒ 跳过并计数，
#: 不去翻更早的历史（那要把 raw 全史拉进来，为一个显示用的家数统计不值得）。
_PREV_SESSIONS = 2


def raw_day_closes(
    trade_date: str | None = None,
    *,
    data_dir: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, object]:
    """某交易日的 **raw（不复权）**收盘与前收盘，外加实际取到的交易日（涨跌停判定用）。

    为什么必须是 raw：涨跌停价是**价格**概念（前收 × 幅度后四舍五入到分），复权序列上
    这个整数关系被破坏。为什么不用 `change_pct`：它在 raw 口径下**自 2026-09-21 起整列为空**。

    `prev_close` 取的是**前一个有余价的交易日**的收盘；窗口只有 `_PREV_SESSIONS` 天，
    停牌更久的标的 `prev_close` 为 `None`（调用方跳过并计数，不猜）。
    """
    own = con is None
    con = con if con is not None else connect(data_dir)
    try:
        if trade_date is None:
            latest = _fetch(
                con,
                f"SELECT max(trade_date) AS day FROM {BARS_VIEW}"
                " WHERE adjustment = 'raw' AND close IS NOT NULL",
                [],
            )
            day = latest[0]["day"] if latest else None
            if day is None:
                return {"trade_date": None, "rows": []}
            trade_date = day.isoformat()

        rows = _fetch(
            con,
            f"""
            WITH sessions AS (
                SELECT DISTINCT trade_date FROM {BARS_VIEW}
                WHERE adjustment = 'raw' AND close IS NOT NULL AND trade_date <= ?
                ORDER BY trade_date DESC LIMIT ?
            ),
            seq AS (
                SELECT symbol, trade_date, close,
                       lag(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close
                FROM {BARS_VIEW}
                WHERE adjustment = 'raw' AND trade_date IN (SELECT trade_date FROM sessions)
                  AND close IS NOT NULL
            )
            SELECT symbol, close, prev_close FROM seq WHERE trade_date = ?
            """,
            [trade_date, _PREV_SESSIONS, trade_date],
        )
    finally:
        if own:
            con.close()
    return {"trade_date": trade_date, "rows": rows}


def sector_performance(
    trade_date: str | None = None,
    *,
    adjust: str = "qfq",
    min_symbols: int = 10,
    limit: int = 60,
    data_dir: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, object]:
    """某交易日的**行业 / 概念板块**表现（M10 板块块）。

    **一条事件怎么变成配对**：取它提到的**全部行业 × 全部标的**（笛卡尔积）。事件说的是
    「这些行业 + 这些标的」，语料里没有「哪只标的属于哪个行业」的更细映射，硬拆一个出来
    就是编精度。故这是一条**弱归属**——界面上的口径说明必须写明它来自事件、非权威分类。

    **归属口径 = PIT 累积**：行业 → 标的的映射取「`available_at <= 当日 15:00` 的**全部历史事件**」
    的累积，**不是当日事件**。理由是实测的一条反向污染：当天的新闻标签本身由当天的涨跌驱动
    （涨得好才被打标签），用当日归属算出来的「板块涨跌幅」是**选出涨得好的再算均值**
    （实测医药生物 +3.69% 即此），且样本薄（金融仅 127 只）。累积口径 16–260ms、覆盖率 **87.7%**。

    ⚠ **它不是一个划分**：一只标的可同时挂「电子」「半导体」「AI算力」，故各行业的成交额
    之和**远大于**全市场成交额（实测 30 个行业合计 39,516 亿 = 全市场的 **274%**）。
    任何**部分-整体**形态（树图 / 堆叠条 / 饼图）在这里都是错的——这是板块块不用 treemap 的原因。

    `median_pct` 与 `avg_pct` 一并给出：两者差得远说明这一档被少数极端值主导，读者该看得出来。
    """
    own = con is None
    con = con if con is not None else connect(data_dir)
    try:
        if trade_date is None:
            trade_date = _latest_trade_date(con, adjust)
            if trade_date is None:
                return {"trade_date": None, "rows": [], "coverage": {"total": 0, "covered": 0}}
        cutoff = _pit_cutoff(trade_date)
        rows = _fetch(
            con,
            f"""
            -- 展开源的写法（为什么是横向积而不是多个 unnest）见 `tags_source`
            WITH tag AS (
                SELECT i.ind AS ind, s.sym AS sym
                FROM {tags_source()}
                WHERE available_at <= CAST(? AS TIMESTAMPTZ)
                  AND len(industries) > 0 AND len(symbols) > 0
            ),
            clean AS (
                SELECT DISTINCT ind, sym FROM tag
                WHERE ind IS NOT NULL AND sym IS NOT NULL AND sym <> ''
            ),
            -- 池子 = **当日截面**（四价齐全且有收盘），与 `cross_section` 逐字同一个定义。
            -- 这里**不**再额外要求 `change_pct IS NOT NULL`：多一个条件就多一个「当日截面」的
            -- 定义，于是板块条上的成分数与点进去的列表 total 会差几只（实测医药生物 786 vs 787，
            -- 差的就是那天没有涨跌幅的那几只）。`avg`/`median` 本身就忽略 NULL，不需要预筛。
            -- **池子不因「有没有加筛选」而变**，这是一条要守住的纪律。
            px AS (
                SELECT symbol, change_pct, amount FROM {BARS_VIEW}
                WHERE adjustment = ? AND trade_date = ?
                  AND close IS NOT NULL AND open IS NOT NULL
                  AND high IS NOT NULL AND low IS NOT NULL
            )
            SELECT clean.ind AS industry,
                   count(*) AS symbols,
                   sum(px.amount) AS amount,
                   avg(px.change_pct) AS avg_pct,
                   median(px.change_pct) AS median_pct
            FROM clean JOIN px ON px.symbol = clean.sym
            GROUP BY 1 HAVING count(*) >= ?
            ORDER BY avg_pct DESC NULLS LAST, symbols DESC
            LIMIT ?
            """,
            [cutoff, adjust, trade_date, min_symbols, limit],
        )
        coverage = _fetch(
            con,
            f"""
            WITH tag AS (
                SELECT DISTINCT unnest(symbols) AS sym FROM {EVENTS_VIEW}
                WHERE available_at <= CAST(? AS TIMESTAMPTZ) AND len(industries) > 0
            ),
            px AS (
                SELECT symbol FROM {BARS_VIEW}
                WHERE adjustment = ? AND trade_date = ?
                  AND close IS NOT NULL AND open IS NOT NULL
                  AND high IS NOT NULL AND low IS NOT NULL
            )
            SELECT (SELECT count(*) FROM px) AS total,
                   count(*) AS covered
            FROM px WHERE symbol IN (SELECT sym FROM tag)
            """,
            [cutoff, adjust, trade_date],
        )
    finally:
        if own:
            con.close()
    cov = coverage[0] if coverage else {"total": 0, "covered": 0}
    return {
        "trade_date": trade_date,
        "rows": rows,
        "coverage": {"total": int(cov["total"] or 0), "covered": int(cov["covered"] or 0)},
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


#: 价格动量 / 反转因子的回看长度（**有价 bar 数**，M5c 固定 20）。
FACTOR_LOOKBACK = 20

#: 价格面板向前多扫的自然日 = 3 × lookback。窗口首日的 `close_lag` 需要更早的 bar 才存在；
#: 补窗不足时那是**保守缺失**（NULL 而非错值）。实测（2026-07-07 起 60 个交易日窗口）：
#: 补 60 天与全史扫描相比，331,372 个 (标的, 日) 键里只差 7 个（都是停牌稀疏标的），
#: 耗时 0.15s vs 0.60s。
FACTOR_PADDING_DAYS = FACTOR_LOOKBACK * 3


def factor_event_rows(data_dir: Path | None = None) -> list[dict]:
    """因子面板的事件原料：有向事件按标的摊平，只取三列（M5c）。

    - 只收 `direction_norm ∈ {bullish, bearish}`：方向是因子值的符号来源。中性/未映射
      **不是「无方向」而是「类别不等于方向」**（公告的 `direction_norm` 为 NULL 是设计，
      见 `etl/store.py`），一律不进面板。
    - **`unnest(symbols)`**：一条事件挂多只股票只存一行（M2b），不摊平就没法按标的排序。
      实测 70.5% 的有向事件一个标的都没挂，那些行摊平后自然消失——面板原料因此比事件数
      小得多（21,318 vs 57,403），池子口径如实写进报告 `notes`。
    - `factor_value` 原样带出（实测覆盖 100%）；为空的行由面板层剔除并计数。
    - 不做 PIT 过滤——按 `available_at` 设卡是面板层的职责（复用 `bar_cutoff`），同 `events()`。
    """
    con = connect(data_dir)
    try:
        return _fetch(
            con,
            f"""
            SELECT unnest(symbols) AS symbol, available_at, factor_value
            FROM {EVENTS_VIEW}
            WHERE direction_norm IN ('bullish', 'bearish')
            ORDER BY available_at, symbol
            """,
            [],
        )
    finally:
        con.close()


def factor_market_days(
    start: str, end: str, *, adjust: str = "qfq", data_dir: Path | None = None
) -> list[date]:
    """**补窗内**的市场交易日（升序），供因子面板做事件归属（M5c）。

    为什么单独一条查询：归属日必须能落在**窗口之前**的交易日上——否则「窗口前可得的事件」
    会被 `bucket_day` 的首日兜底吸进窗口第一天（`EventFilter` 的语义就是首根 bar 放行全部积压，
    见 `factor/panel.py`）。而价格行只取窗口内即可（`close_lag` 由 SQL 在补窗上算完），
    把补窗的 33 万行整段搬进内存纯属浪费——这里只取「有哪些日子」，几十行。

    只返回**有价 bar 出现过**的日子（与 `cross_section` / `bars()` 同口径：无价不算交易日）。
    """
    first = date.fromisoformat(start) - timedelta(days=int(FACTOR_PADDING_DAYS))
    con = connect(data_dir)
    try:
        rows = _fetch(
            con,
            f"""
            SELECT DISTINCT trade_date FROM {BARS_VIEW}
            WHERE adjustment = ? AND trade_date BETWEEN ? AND ?
              AND open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL
            ORDER BY trade_date
            """,
            [adjust, first, end],
        )
    finally:
        con.close()
    return [row["trade_date"] for row in rows]


def factor_price_rows(
    start: str, end: str, *, adjust: str = "qfq", data_dir: Path | None = None
) -> list[dict]:
    """价格面板原料：窗口内每根**有价** bar 的开盘 / 收盘 + `lookback` 根之前的收盘（M5c）。

    一条窗口函数查询出全市场面板——**禁止逐标的调 `bars()`**（那是 154ms/次的全史扫描，
    5,798 只就是分钟级）。`close_lag` 供动量 / 反转因子用（`close / close_lag - 1`）；
    它按**该标的自己的有价 bar** 回看，停牌空洞因此被跳过，即「20 个有价交易日」。

    向前多扫 `FACTOR_PADDING_DAYS` 个自然日**只为把 `close_lag` 算准**，返回的仍是
    窗口内（`start` / `end` 含端点，与 `bars()` 同口径）的行；补窗里的**日子**由
    `factor_market_days()` 单独给（事件归属要用，但不值得把 33 万行搬进内存）。
    无价 bar 与 `bars()` 同口径剔除。
    """
    first = date.fromisoformat(start) - timedelta(days=int(FACTOR_PADDING_DAYS))
    con = connect(data_dir)
    try:
        return _fetch(
            con,
            f"""
            WITH p AS (
                SELECT symbol, trade_date, open, close,
                       lag(close, {FACTOR_LOOKBACK}) OVER w AS close_lag
                FROM {BARS_VIEW}
                WHERE adjustment = ? AND trade_date BETWEEN ? AND ?
                  AND open IS NOT NULL AND high IS NOT NULL
                  AND low IS NOT NULL AND close IS NOT NULL
                WINDOW w AS (PARTITION BY symbol ORDER BY trade_date)
            )
            SELECT symbol, trade_date, open, close, close_lag FROM p
            WHERE trade_date >= ? ORDER BY trade_date, symbol
            """,
            [adjust, first, end, start],
        )
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


#: 证据面板要显示的列（30 列里取 11 个，别把整行塞进冻结报告）
EVIDENCE_COLUMNS = (
    "event_id",
    "event_time",
    "available_at",
    "title",
    "summary",
    "industries",
    "direction_norm",
    "source",
    "original_source",
    "source_url",
    "content_hash",
)


#: 快讯流要的列 = 证据面板那批（事件/可得双戳 + 来源三元组 + 行业 + 方向）+ 三列，
#: **但去掉 `summary`**（M10d 的 D1：正文只从详情抽屉出，别让每行白传几百字正文）。
#: 仍然从 `EVIDENCE_COLUMNS` 拼出来、不另抄一份——两处都靠「一条事件的来源可追溯」这条
#: 硬要求吃饭，抄一份迟早漂移；`EVIDENCE_COLUMNS` 本身不动（M7 的证据面板要用正文）。
FEED_COLUMNS = (
    *(column for column in EVIDENCE_COLUMNS if column != "summary"),
    "event_type",
    "importance_score",
    "symbols",
)

#: 事件详情要的列（M10d）：证据面板那批 + 六类真内容——
#: `direction`/`confidence`（方向与置信）· `factor_value`/`factor_scores`（因子分解）·
#: `person`（人物）· `stocks`（**每只标的的打标理由**）·
#: `record_version`/`revision_id`/`revision_time`/`is_corrected`/`correction_count`（修订史）·
#: `observed_at`/`reported_available_at`/`quality_status`/`source_time_quality`（时间与质量标注）。
#: **单列一份、不 `SELECT *`**：整行 30 列里有 `payload_json` 一类原始字段，不该进响应。
#: 其中 `person` / `stocks` / `factor_scores` 是**落盘的 JSON 文本**，由 API 层解析。
DETAIL_COLUMNS = (
    *EVIDENCE_COLUMNS,
    "event_type",
    "importance_score",
    "symbols",
    "direction",
    "confidence",
    "factor_value",
    "factor_scores",
    "person",
    "stocks",
    "observed_at",
    "reported_available_at",
    "quality_status",
    "source_time_quality",
    "dedup_key",
    "record_version",
    "revision_id",
    "revision_time",
    "is_corrected",
    "correction_count",
)


def event_days(
    start: str,
    end: str,
    *,
    high_score: float = 60.0,
    data_dir: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> list[dict]:
    """区间内**每日**的事件计数（总数 / 高分计数 / 按类型），供 M10 的日期带。

    返回按日期升序、且**只含真有事件的日子**——「那天没有事件」与「那天不在窗口里」是两回事，
    补零会把前者画成一个「零条的柱子」，读起来像数据缺失。

    `high_score` 的阈值**由调用方给**（端点暴露成查询参数），不在本层写死：什么叫「高分」
    是展示口径，不是数据事实。实测近 30 个自然日聚合 **74ms**。
    """
    own = con is None
    con = con if con is not None else connect(data_dir)
    try:
        rows = _fetch(
            con,
            f"""
            SELECT event_time::DATE AS day, event_type,
                   count(*) AS n,
                   count(*) FILTER (WHERE importance_score >= ?) AS high
            FROM {EVENTS_VIEW}
            WHERE event_time::DATE BETWEEN ? AND ?
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
            [high_score, start, end],
        )
    finally:
        if own:
            con.close()

    # 摊平成「一天一行」：SQL 里直接出嵌套结构要么靠 `histogram()` 这类方言，要么拼字符串，
    # 而这里最多 30 天 × 5 类 = 150 行，在 Python 里归并比在 SQL 里造结构清楚得多。
    days: dict[str, dict] = {}
    for row in rows:
        day = row["day"].isoformat()
        entry = days.setdefault(
            day, {"date": day, "total": 0, "high": 0, "by_type": {}}
        )
        entry["total"] += int(row["n"])
        entry["high"] += int(row["high"])
        entry["by_type"][row["event_type"]] = int(row["n"])
    return list(days.values())


def event_feed(
    day: str | None = None,
    *,
    types: list[str] | None = None,
    min_importance: float | None = None,
    limit: int = 20,
    offset: int = 0,
    data_dir: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, object]:
    """某一天的**全市场**事件分页流水，按 `importance_score` 降序（M10 快讯流）。

    与 `events()` 的差别：那个按 `list_contains(symbols, ?)` 过滤**单标的**且不分页；
    这个按**日**取全市场。`day` 缺省取语料里最后一个有事件的日子。

    **排序键是 `(importance_score DESC, event_id)`**：只按分数排时同分条目的顺序由数据库定，
    翻页会出现「第 2 页重复了第 1 页的两条」——加 `event_id` 是为了让**分页是确定的**
    （`symbol_rank` / `cross_section` 的 `, symbol ASC` 是同一条纪律）。

    `types` 是**白名单**（None = 全部类型）。这层不设默认值：默认看哪几类事件是**展示口径**，
    该由调用方决定并写进响应（`note`），藏在数据层会让「为什么公告不见了」无从追查。
    """
    own = con is None
    con = con if con is not None else connect(data_dir)
    try:
        if day is None:
            rows = _fetch(con, f"SELECT max(event_time)::DATE AS day FROM {EVENTS_VIEW}", [])
            resolved = rows[0]["day"] if rows else None
            if resolved is None:
                return {"date": None, "total": 0, "count": 0, "items": []}
            day = resolved.isoformat()

        where = ["event_time::DATE = ?"]
        params: list = [day]
        if types:
            where.append(f"event_type IN ({', '.join('?' for _ in types)})")
            params.extend(types)
        if min_importance is not None:
            where.append("importance_score >= ?")
            params.append(min_importance)
        condition = " AND ".join(where)

        total = _fetch(con, f"SELECT count(*) AS n FROM {EVENTS_VIEW} WHERE {condition}", params)[0]
        items = _fetch(
            con,
            f"SELECT {', '.join(FEED_COLUMNS)} FROM {EVENTS_VIEW} WHERE {condition}"
            " ORDER BY importance_score DESC NULLS LAST, event_id ASC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
    finally:
        if own:
            con.close()
    return {"date": day, "total": int(total["n"]), "count": len(items), "items": items}


def events_by_ids(event_ids: list[str], *, data_dir: Path | None = None) -> list[dict]:
    """按 `event_id` 批量取语料行（M7 证据面板）。

    **`event_id` 只在日分区内唯一**（M2b 实测：平台对同题事件复用 id）——调用方必须拿
    `(event_id, event_time::DATE)` 再筛一次，别单独拿 id 当键。一次连接批量取，不做 N 次查询。
    """
    if not event_ids:
        return []
    con = connect(data_dir)
    try:
        placeholders = ", ".join("?" for _ in event_ids)
        return _fetch(
            con,
            f"SELECT {', '.join(EVIDENCE_COLUMNS)} FROM {EVENTS_VIEW} "
            f"WHERE event_id IN ({placeholders})",
            list(event_ids),
        )
    finally:
        con.close()


def event_detail(
    event_id: str, *, day: str | None = None, data_dir: Path | None = None
) -> dict | None:
    """取**一条**事件的详情行（M10d）。

    **`event_id` 只在日分区内唯一**（平台对同题事件复用 id，实测 19 例、最多跨 8 天）——
    所以 `day` 给了就精确匹配，没给就取 `event_time` **最新**的那条；响应里回显实际日期
    （`day` 键），调用方不必自己推。查不到返回 `None`，由端点翻 404——**不返回空壳**。
    """
    con = connect(data_dir)
    try:
        sql = f"SELECT {', '.join(DETAIL_COLUMNS)}, event_time::DATE AS day FROM {EVENTS_VIEW} WHERE event_id = ?"
        params: list = [event_id]
        if day is not None:
            sql += " AND event_time::DATE = ?"
            params.append(day)
        rows = _fetch(con, sql + " ORDER BY event_time DESC LIMIT 1", params)
    finally:
        con.close()
    return rows[0] if rows else None


def closes_through(
    symbols: list[str], through: date, *, adjust: str = "qfq", data_dir: Path | None = None
) -> dict[str, float]:
    """每只标的**截至 `through`（含）**最近一根**有价** bar 的收盘价（M7 未平仓估值用）。

    与 `latest_closes` 的差别只有一个：那个取全局最新，这个取「账户 `as_of` 那天往前」——
    模拟盘结算到哪一天，估值就必须停在哪一天，否则期末浮盈会看到未来的价。
    未命中的标的**不进结果**（调用方如实留空，不编价）；无价 bar 同样排除。
    """
    if not symbols:
        return {}
    con = connect(data_dir)
    try:
        placeholders = ", ".join("?" for _ in symbols)
        rows = _fetch(
            con,
            f"SELECT symbol, close FROM {BARS_VIEW} "
            f"WHERE adjustment = ? AND symbol IN ({placeholders}) "
            "AND close IS NOT NULL AND trade_date <= ? "
            "QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) = 1",
            [adjust, *symbols, through],
        )
    finally:
        con.close()
    return {row["symbol"]: float(row["close"]) for row in rows}


def bars_multi(
    symbols: list[str],
    *,
    start: str | None = None,
    end: str | None = None,
    adjust: str = "qfq",
    data_dir: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> list[dict]:
    """一批标的的日线，按 `(symbol, trade_date)` 升序。**与 `bars()` 的差别只有形状**：
    它逐标的查，这里一次查全池——每次查询各自的建连接 + 建视图就有几十毫秒，
    20 只标的因此差出 10 倍（M6 规划期实测：逐标的 4,958ms / 批量 480ms）。

    过滤口径与 `bars()` 逐条相同（四个价格齐全才返回，无价 bar 不进定价路径）。
    `con` 供调用方复用同一条连接（一次读三条通道时省掉两次建视图），**借来的连接不由本函数关**。
    """
    if not symbols:
        return []
    own = con is None
    con = con if con is not None else connect(data_dir)
    try:
        marks = ", ".join("?" for _ in symbols)
        sql = (
            f"SELECT * FROM {BARS_VIEW} WHERE symbol IN ({marks}) AND adjustment = ?"
            " AND open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL"
        )
        params: list = [*symbols, adjust]
        if start:
            sql += " AND trade_date >= ?"
            params.append(start)
        if end:
            sql += " AND trade_date <= ?"
            params.append(end)
        return _fetch(con, sql + " ORDER BY symbol, trade_date", params)
    finally:
        if own:
            con.close()


def events_multi(
    symbols: list[str],
    *,
    start: str | None = None,
    end: str | None = None,
    data_dir: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> list[dict]:
    """一批标的的事件语料，按 `(event_time, event_id)` 升序。

    **按标的过滤走 `list_contains(symbols, ?)` 的 OR 并联**（口径同 `events()`：一条事件一行、
    标的是数组）。返回的行**没有按标的拆分**——一条事件可挂多只股票，拆分是消费方按
    `row["symbols"]` 自己做的事（模拟盘每只标的一份 feed，见 `app/paper/market.py`）。
    """
    if not symbols:
        return []
    own = con is None
    con = con if con is not None else connect(data_dir)
    try:
        sql = f"SELECT * FROM {EVENTS_VIEW} WHERE (" + " OR ".join(
            "list_contains(symbols, ?)" for _ in symbols
        ) + ")"
        params: list = list(symbols)
        if start:
            sql += " AND event_time >= ?"
            params.append(start)
        if end:
            sql += " AND event_time <= ?"
            params.append(end)
        return _fetch(con, sql + " ORDER BY event_time, event_id", params)
    finally:
        if own:
            con.close()
