"""T2 数据层离线单测：DuckDB 视图与查询函数（bars / events / 日历）。

合成数据落在 tmp_path，不碰真实 data/；真实样例数据的验收在 integration 用例里。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from app.data import duckdb_client as dc
from tests.conftest import write_events_parquet


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
