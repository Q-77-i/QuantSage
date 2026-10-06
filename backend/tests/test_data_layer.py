"""T2 数据层离线单测：DuckDB 视图/查询函数 + 事件归一化。

合成数据落在 tmp_path，不碰真实 data/；真实样例数据的验收在 integration 用例里。
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from app.data import duckdb_client as dc
from scripts import download_events


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
        for symbol in ("600519", "300750"):
            target = events_dir / f"{symbol}.parquet"
            con.execute(
                "COPY (SELECT * FROM (VALUES"
                f" ('{symbol}', 'news:1', TIMESTAMPTZ '2026-07-10 10:00:00+08',"
                "  TIMESTAMPTZ '2026-07-10 12:00:00+08', '利多', 'bullish'),"
                f" ('{symbol}', 'news:2', TIMESTAMPTZ '2026-08-10 10:00:00+08',"
                "  TIMESTAMPTZ '2026-08-11 09:00:00+08', '中性', 'neutral')"
                " ) t(symbol, event_id, event_time, available_at, direction, direction_norm))"
                f" TO '{target}' (FORMAT PARQUET)"
            )
    finally:
        con.close()
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


def test_normalize_maps_direction_and_keeps_nested_shapes() -> None:
    record = {
        "event_id": "news:1",
        "direction": "利多",
        "industries": ["食品饮料"],
        "stocks": [{"code": "600519", "name": "贵州茅台", "reason": None}],
        "factor_scores": {"score": 69.6},
        "title": "标题",
    }
    row = download_events.normalize(record, "600519")

    assert row["direction"] == "利多"
    assert row["direction_norm"] == "bullish"
    assert row["symbol"] == "600519"
    # 原生嵌套类型保持原样，只有键集不定的 factor_scores 序列化成 JSON 文本
    assert row["industries"] == ["食品饮料"]
    assert row["stocks"][0]["code"] == "600519"
    assert row["factor_scores"] == '{"score": 69.6}'
    assert row["summary"] is None


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [("利多", "bullish"), ("bullish", "bullish"), ("利空", "bearish"), ("bearish", "bearish"),
     ("中性", "neutral"), ("neutral", "neutral")],
)
def test_direction_map_known_values(raw_value: str, expected: str) -> None:
    assert download_events.DIRECTION_MAP[raw_value] == expected


def test_normalize_leaves_non_sentiment_direction_unmapped() -> None:
    """「高管人事」这类非情绪标签不臆造 sentiment，归为 NULL（SQL 过滤自然跳过）。"""
    row = download_events.normalize({"event_id": "x", "direction": "高管人事"}, "600519")
    assert row["direction"] == "高管人事"
    assert row["direction_norm"] is None
