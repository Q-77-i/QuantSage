"""T2 数据层离线单测：DuckDB 视图与查询函数（bars / events / 日历）。

合成数据落在 tmp_path，不碰真实 data/；真实样例数据的验收在 integration 用例里。
"""

from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from app.core.config import get_settings
from app.data import duckdb_client as dc
from tests.conftest import ts, write_bars_parquet, write_events_parquet


@pytest.fixture(autouse=True)
def _reset_duckdb_connections():
    """连接按线程缓存；每条用例前后清掉，避免上一个 tmp 目录的连接串进来。"""
    dc.reset_connections()
    yield
    dc.reset_connections()


@pytest.fixture
def sample_data_dir(tmp_path: Path) -> Path:
    """两标的 × 两复权日线 + 两标的事件，形状与真实落盘一致。"""
    bars_dir = tmp_path / "bars"
    events_dir = tmp_path / "events"
    bars_dir.mkdir()
    events_dir.mkdir()

    con = duckdb.connect()
    try:
        for symbol in ("600519", "300750"):
            for adjust in ("raw", "qfq"):
                target = bars_dir / f"{symbol}.{adjust}.parquet"
                con.execute(
                    "COPY (SELECT * FROM (VALUES"
                    f" ('{symbol}', '{adjust}', DATE '2025-06-30', 100.0, 100.0, 100.0, 100.0),"
                    f" ('{symbol}', '{adjust}', DATE '2025-12-31', 110.0, 110.0, 110.0, 110.0),"
                    f" ('{symbol}', '{adjust}', DATE '2026-06-30', 120.0, 120.0, 120.0, 120.0)"
                    " ) t(symbol, adjustment, trade_date, open, high, low, close))"
                    f" TO '{target}' (FORMAT PARQUET)"
                )
    finally:
        con.close()

    # 事件走与生产同源的写盘函数：查询层对 `data/events/*.parquet` 是严格 glob，
    # 列集不一致会直接报错（有意如此），夹具不能各写各的 schema
    for symbol in ("600519", "300750"):
        write_events_parquet(
            events_dir,
            symbol,
            [
                {
                    "event_id": "news:1",
                    "event_time": datetime.fromisoformat("2026-07-10T10:00:00+08:00"),
                    "available_at": datetime.fromisoformat("2026-07-10T12:00:00+08:00"),
                    "direction_norm": "bullish",
                },
                {
                    "event_id": "news:2",
                    "event_time": datetime.fromisoformat("2026-08-10T10:00:00+08:00"),
                    "available_at": datetime.fromisoformat("2026-08-11T09:00:00+08:00"),
                    "direction_norm": "neutral",
                },
            ],
        )
    return tmp_path


def test_connect_exposes_both_views(sample_data_dir: Path) -> None:
    con = dc.connect(sample_data_dir)
    try:
        assert con.execute(f"SELECT count(*) FROM {dc.BARS_VIEW}").fetchone()[0] == 12
        assert con.execute(f"SELECT count(*) FROM {dc.EVENTS_VIEW}").fetchone()[0] == 4
    finally:
        con.close()


def test_connect_reuses_the_connection_for_the_same_thread_and_dir(
    sample_data_dir: Path,
) -> None:
    """同一线程、同一目录重复 connect 不再重建视图；reset 后才真正关闭。"""
    first = dc.connect(sample_data_dir)
    second = dc.connect(sample_data_dir)

    assert first is second
    assert first.execute(f"SELECT count(*) FROM {dc.BARS_VIEW}").fetchone()[0] == 12

    dc.reset_connections()
    with pytest.raises(duckdb.Error):
        first.execute("SELECT 1")


def test_cached_connection_does_not_hide_missing_data(sample_data_dir: Path) -> None:
    """文件删掉后必须仍抛 `DataNotReady`，不能拿旧连接去跑一个空查询。"""
    dc.connect(sample_data_dir)
    for path in (sample_data_dir / "bars").glob("*.parquet"):
        path.unlink()

    with pytest.raises(dc.DataNotReady, match="download_bars"):
        dc.connect(sample_data_dir)


def test_connect_raises_when_data_missing(tmp_path: Path) -> None:
    (tmp_path / "bars").mkdir()
    with pytest.raises(dc.DataNotReady, match="download_bars"):
        dc.connect(tmp_path)


def test_bars_filters_symbol_adjust_and_range(sample_data_dir: Path) -> None:
    rows = dc.bars("600519", start="2025-01-01", end="2025-12-31", adjust="qfq", data_dir=sample_data_dir)
    assert [row["trade_date"].isoformat() for row in rows] == ["2025-06-30", "2025-12-31"]
    assert {row["adjustment"] for row in rows} == {"qfq"}

    assert len(dc.bars("600519", adjust="raw", data_dir=sample_data_dir)) == 3
    assert len(dc.bars("600519", adjust="qfq", data_dir=sample_data_dir)) == 3
    assert dc.bars("000001", data_dir=sample_data_dir) == []


def _write_no_price_dir(tmp_path: Path) -> Path:
    """一根正常 bar + 一根整行价量为空的 bar（数据源表示「当天没观测到成交」）。"""
    (tmp_path / "bars").mkdir()
    (tmp_path / "events").mkdir()
    con = duckdb.connect()
    try:
        con.execute(
            "COPY (SELECT * FROM (VALUES"
            " ('600519', 'qfq', DATE '2026-06-30', 100.0, 100.0, 100.0, 100.0, 'traded'),"
            " ('600519', 'qfq', DATE '2026-07-01', NULL, NULL, NULL, NULL, 'no_turnover_observed'),"
            " ('600519', 'qfq', DATE '2026-07-02', 101.0, 101.0, 101.0, 101.0, 'traded')"
            ") t(symbol, adjustment, trade_date, open, high, low, close, trading_status))"
            f" TO '{tmp_path / 'bars' / 'x.parquet'}' (FORMAT PARQUET)"
        )
        con.execute(
            "COPY (SELECT * FROM (VALUES ('600519', 'news:1', '利多'))"
            " t(symbol, event_id, direction))"
            f" TO '{tmp_path / 'events' / 'x.parquet'}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return tmp_path


def test_bars_excludes_rows_without_prices(tmp_path: Path) -> None:
    """无价 bar 不是零价：必须从定价路径剔除，但原始行仍留在视图里可查。

    M2a 全市场后才会遇到：`trading_status='no_turnover_observed'` 的日子可能整行价量为空。
    留它进 bars() 会被 `Bar.from_row` 读成 0.0，持仓估值当日期末权益直接塌到现金，
    回测静默给出 -100%。
    """
    data_dir = _write_no_price_dir(tmp_path)

    rows = dc.bars("600519", data_dir=data_dir)
    assert [row["trade_date"].isoformat() for row in rows] == ["2026-06-30", "2026-07-02"]

    con = dc.connect(data_dir)
    try:
        # 事实数据不藏：SQL 层仍然看得见那三行
        assert con.execute(f"SELECT count(*) FROM {dc.BARS_VIEW}").fetchone()[0] == 3
    finally:
        con.close()


def test_latest_closes_skips_unpriced_tail(tmp_path: Path) -> None:
    """「最新可得收盘价」要跳过没成交的空行，否则会显示成「取不到价」。"""
    data_dir = _write_no_price_dir(tmp_path)
    latest = dc.latest_closes(["600519"], data_dir=data_dir)

    assert latest["600519"]["trade_date"].isoformat() == "2026-07-02"


def test_events_filters_symbol_and_window(sample_data_dir: Path) -> None:
    rows = dc.events("300750", start="2026-08-01", data_dir=sample_data_dir)
    assert [row["event_id"] for row in rows] == ["news:2"]
    assert len(dc.events(data_dir=sample_data_dir)) == 4


def test_events_match_any_symbol_in_the_array(sample_data_dir: Path) -> None:
    """一条事件挂多只股票只存一行（M2b）：数组里任一命中即该标的的事件。

    夹具必须**写全 schema**：查询层对 `data/events/*.parquet` 是严格 glob，列集不一致
    会直接报错而不是静默并集——那是有意的（见 `store.py` 的落盘约定）。
    """
    write_events_parquet(
        sample_data_dir / "events",
        "600519",
        [
            {
                "event_id": "news:9",
                "symbols": ["600519", "300750"],
                "event_time": datetime.fromisoformat("2026-09-01T10:00:00+08:00"),
                "available_at": datetime.fromisoformat("2026-09-01T10:00:00+08:00"),
            }
        ],
    )

    for symbol in ("600519", "300750"):
        ids = [row["event_id"] for row in dc.events(symbol, data_dir=sample_data_dir)]
        assert "news:9" in ids


def test_event_coverage_reads_from_data(sample_data_dir: Path) -> None:
    """覆盖区间必须查出来：起点固化、终点随日增前移，写死「最近 3 个月」会骗人。"""
    coverage = dc.event_coverage(data_dir=sample_data_dir)
    assert coverage == {"start": "2026-07-10", "end": "2026-08-10", "rows": 4}


# ── M5c：因子面板的两条取数 ──────────────────────────────────

#: 25 根连续 bar：够 lookback=20 回看，窗口首日就能有 `close_lag`
FACTOR_DAYS = [date(2026, 6, 1) + timedelta(days=i) for i in range(25)]


def _write_factor_dir(tmp_path: Path) -> Path:
    """一只够长的标的 + 一只稀疏标的（补窗内不足 lookback+1 根 bar）。"""
    write_bars_parquet(
        tmp_path / "bars",
        "600519",
        [
            {"trade_date": day, "open": float(100 + i), "close": float(100 + i)}
            for i, day in enumerate(FACTOR_DAYS)
        ],
    )
    write_bars_parquet(
        tmp_path / "bars",
        "000001",
        [{"trade_date": day, "open": 10.0, "close": 10.0} for day in FACTOR_DAYS[-3:]],
    )
    write_events_parquet(
        tmp_path / "events",
        "panel",
        [
            {
                "event_id": "news:1",
                "symbols": ["600519"],
                "direction_norm": "bullish",
                "factor_value": 0.5,
                "event_time": ts("2026-06-22 09:30:00"),
            },
            {
                "event_id": "news:2",
                "symbols": ["600519", "000001"],
                "direction_norm": "bearish",
                "factor_value": -0.4,
                "event_time": ts("2026-06-23 16:00:00"),
            },
            {
                "event_id": "news:3",
                "symbols": ["600519"],
                # 中性不进面板：方向是因子值的符号来源
                "direction_norm": "neutral",
                "factor_value": 0.1,
                "event_time": ts("2026-06-24 09:30:00"),
            },
            {
                "event_id": "news:4",
                # 一个标的都没挂（实测占全部有向事件的 70.5%）——摊平后自然消失
                "symbols": [],
                "direction_norm": "bullish",
                "factor_value": 0.9,
                "event_time": ts("2026-06-24 09:30:00"),
            },
        ],
    )
    return tmp_path


def test_factor_event_rows_unnest_and_direction_filter(tmp_path: Path) -> None:
    data_dir = _write_factor_dir(tmp_path)

    rows = dc.factor_event_rows(data_dir=data_dir)

    assert [(row["symbol"], row["factor_value"]) for row in rows] == [
        ("600519", 0.5),
        ("000001", -0.4),
        ("600519", -0.4),
    ]


def test_factor_price_rows_lag_is_per_symbol_and_window_only(tmp_path: Path) -> None:
    """`close_lag` 按**该标的有价 bar 数**回看；只返回窗口内的日子（补窗只为算 lag）。"""
    data_dir = _write_factor_dir(tmp_path)

    rows = dc.factor_price_rows(
        FACTOR_DAYS[20].isoformat(), FACTOR_DAYS[24].isoformat(), data_dir=data_dir
    )

    by_key = {(row["symbol"], row["trade_date"]): row for row in rows}
    assert [row["trade_date"] for row in rows if row["symbol"] == "600519"] == FACTOR_DAYS[20:]
    assert by_key[("600519", FACTOR_DAYS[20])]["close_lag"] == 100.0  # 20 根之前 = 第 0 天
    assert by_key[("600519", FACTOR_DAYS[24])]["close"] == 124.0
    # 稀疏标的补窗内不足 21 根 bar：**保守缺失**（None），不是错值
    assert by_key[("000001", FACTOR_DAYS[22])]["close_lag"] is None


def test_factor_price_rows_excludes_bars_without_prices(tmp_path: Path) -> None:
    """无价 bar 与 `bars()` 同口径剔除——留它进来会被算成 0 元的价格。"""
    (tmp_path / "bars").mkdir()
    (tmp_path / "events").mkdir()
    con = duckdb.connect()
    try:
        con.execute(
            "COPY (SELECT * FROM (VALUES"
            " ('600519', 'qfq', DATE '2026-06-01', 10.0, 10.0, 10.0, 10.0),"
            " ('600519', 'qfq', DATE '2026-06-02', NULL, NULL, NULL, NULL),"
            " ('600519', 'qfq', DATE '2026-06-03', 11.0, 11.0, 11.0, 11.0)"
            ") t(symbol, adjustment, trade_date, open, high, low, close))"
            f" TO '{tmp_path / 'bars' / 'x.parquet'}' (FORMAT PARQUET)"
        )
        con.execute(
            "COPY (SELECT * FROM (VALUES ('600519', 'news:1'))"
            f" t(symbol, event_id)) TO '{tmp_path / 'events' / 'x.parquet'}' (FORMAT PARQUET)"
        )
    finally:
        con.close()

    rows = dc.factor_price_rows("2026-06-01", "2026-06-03", data_dir=tmp_path)

    assert [row["trade_date"] for row in rows] == [date(2026, 6, 1), date(2026, 6, 3)]


# ── S1：有界事件查询与结果缓存 ───────────────────────────────


def test_events_page_returns_latest_first_and_projects_columns(sample_data_dir: Path) -> None:
    from app.data import cache

    cache.clear_local()
    rows = dc.events_page("300750", limit=1, data_dir=sample_data_dir)

    assert [row["event_id"] for row in rows] == ["news:2"]
    # 只投影 `_event_row` 要的列：不把 30 列整行塞进缓存
    assert set(rows[0]) == set(dc.EVENTS_PAGE_COLUMNS)
    assert "stocks" not in rows[0]
    cache.clear_local()


def test_events_page_applies_window_limit_and_offset(sample_data_dir: Path) -> None:
    from app.data import cache

    cache.clear_local()
    assert [row["event_id"] for row in dc.events_page(
        "300750", start="2026-08-01", limit=10, data_dir=sample_data_dir
    )] == ["news:2"]
    assert [row["event_id"] for row in dc.events_page(
        "300750", limit=1, offset=1, data_dir=sample_data_dir
    )] == ["news:1"]
    cache.clear_local()


def test_events_page_is_cached_by_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.data import cache

    cache.clear_local()
    calls: list[int] = []

    def fake(*_args: object, **_kwargs: object) -> list[dict]:
        calls.append(1)
        return [{"event_id": "x"}]

    monkeypatch.setattr(dc, "_events_page_query", fake, raising=False)

    dc.events_page("600519", data_dir=tmp_path)
    dc.events_page("600519", data_dir=tmp_path)
    assert len(calls) == 1

    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "events.json").write_text("{}")
    dc.events_page("600519", data_dir=tmp_path)
    assert len(calls) == 2
    cache.clear_local()


# ── S4：DuckDB 上限 / temp 隔离 / 查询闸门 ────────────────────────────────


def test_connect_applies_s4_limits(sample_data_dir: Path) -> None:
    """每条连接显式设 threads / memory_limit / temp_directory。"""
    settings = get_settings()
    con = dc.connect(sample_data_dir)
    threads = int(con.execute("SELECT current_setting('threads')").fetchone()[0])
    memory = str(con.execute("SELECT current_setting('memory_limit')").fetchone()[0])
    temp = Path(str(con.execute("SELECT current_setting('temp_directory')").fetchone()[0]))

    assert threads == settings.duckdb_threads == 2
    # 1GB 在 DuckDB 里显示为 953.6 MiB（十进制 vs 二进制）；断言上限远低于默认的 19.1GiB
    assert memory != "19.1 GiB"
    assert float(memory.split()[0]) < 1200
    assert temp.parent == (sample_data_dir / "_tmp").resolve()
    assert temp.name.startswith(f"duckdb-{os.getpid()}-")


def test_sandbox_temp_root_uses_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """策略沙箱的数据目录只读，spill 必须落到可写的 TMPDIR（work_dir）。"""
    monkeypatch.setenv("QS_SANDBOX", "1")
    monkeypatch.setattr(dc.tempfile, "gettempdir", lambda: str(tmp_path / "work"))
    assert dc._temp_root(tmp_path / "data") == tmp_path / "work" / "_tmp"


def test_temp_directory_is_unique_per_connection(sample_data_dir: Path) -> None:
    """同进程两线程各一条连接：temp 目录不能共用（DuckDB 的 spill 文件会撞）。"""
    seen: list[str] = []
    errors: list[BaseException] = []

    def read_temp() -> None:
        try:
            con = dc.connect(sample_data_dir)
            seen.append(str(con.execute("SELECT current_setting('temp_directory')").fetchone()[0]))
        except BaseException as exc:  # noqa: BLE001 —— 线程里的异常要带回主线程
            errors.append(exc)
        finally:
            dc.reset_connections()

    threads = [threading.Thread(target=read_temp) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len(seen) == 2
    assert len(set(seen)) == 2


def test_reset_connections_removes_temp_directory(sample_data_dir: Path) -> None:
    con = dc.connect(sample_data_dir)
    temp = Path(str(con.execute("SELECT current_setting('temp_directory')").fetchone()[0]))
    assert temp.exists()

    dc.reset_connections()
    assert not temp.exists()


def test_stale_temp_dirs_are_swept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "_tmp"
    dead = root / "duckdb-999999-1-dead"
    live = root / "duckdb-424242-1-live"
    for path in (dead, live):
        path.mkdir(parents=True)

    monkeypatch.setattr(dc, "_pid_alive", lambda pid: pid == 424242)
    dc._SWEPT_ROOTS.clear()
    dc._sweep_stale_temp_dirs(tmp_path)

    assert not dead.exists()
    assert live.exists()


def test_query_gate_bounds_concurrent_queries() -> None:
    size = get_settings().duckdb_max_concurrent
    if size <= 0:  # pragma: no cover - 默认 4；关闸门的配置不测闸门本身
        pytest.skip("DUCKDB_MAX_CONCURRENT=0")
    dc.reset_query_gate()
    try:
        gate = dc._query_gate()
        assert gate is not None
        for _ in range(size):
            assert gate.acquire(blocking=False)
        assert not gate.acquire(blocking=False)
        for _ in range(size):
            gate.release()

        # 异常路径也必须释放，否则一次查询报错会永久占住一个槽
        with pytest.raises(RuntimeError), dc._query_slot():
            raise RuntimeError("boom")
        with dc._query_slot():
            pass
    finally:
        dc.reset_query_gate()
