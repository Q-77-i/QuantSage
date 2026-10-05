"""T2 验收：真实样例数据（data/bars、data/events）必须先在盘上。

先跑 `uv run python scripts/download_bars.py` 与 `scripts/download_events.py`。
默认不收集；用 `uv run pytest -m integration` 触发。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.core.config import REPO_ROOT, get_settings
from app.data import duckdb_client as dc
from scripts.download_bars import SYMBOLS as BAR_SYMBOLS
from scripts.download_events import SYMBOLS as EVENT_SYMBOLS

pytestmark = pytest.mark.integration

DATA_DIR = get_settings().data_dir
ADJUSTS = ("raw", "qfq")
EVENT_WINDOW_START = "2026-07-05"
EVENT_WINDOW_END = "2026-09-30"


def _meta(name: str) -> dict:
    return json.loads((DATA_DIR / "_meta" / name).read_text(encoding="utf-8"))


def test_bars_files_cover_three_symbols_and_two_adjusts() -> None:
    assert BAR_SYMBOLS == EVENT_SYMBOLS, "行情与事件的标的集合必须一致"

    for symbol in BAR_SYMBOLS:
        for adjust in ADJUSTS:
            rows = dc.bars(symbol, adjust=adjust)
            assert len(rows) > 400, f"{symbol}/{adjust} 行数异常：{len(rows)}"
            assert {row["symbol"] for row in rows} == {symbol}
            assert {row["adjustment"] for row in rows} == {adjust}
            dates = [row["trade_date"].isoformat() for row in rows]
            assert dates == sorted(dates), "日线必须按 trade_date 升序"
            assert dates[0] >= "2025-01-02" and dates[-1] == "2026-09-30"
            # 事件窗口必须落在行情覆盖区间内，否则事件驱动策略无价可成交
            assert dates[0] <= EVENT_WINDOW_START and dates[-1] >= EVENT_WINDOW_END
            assert all(row["available_at"] is not None for row in rows), "行情缺 available_at"


def test_sql_can_query_any_symbol_bars() -> None:
    """SPEC 验收：SQL 可查任意标的日线。"""
    con = dc.connect()
    try:
        for symbol in BAR_SYMBOLS:
            rows = con.execute(
                f"SELECT count(*) FROM {dc.BARS_VIEW} WHERE symbol = ? AND adjustment = 'qfq'",
                [symbol],
            ).fetchone()
            assert rows is not None and rows[0] > 400

        # 视图对消费方可见的列（T3 工具 / T4 引擎 / T6 API 都依赖这几个）
        columns = {row[0] for row in con.execute(f"DESCRIBE {dc.BARS_VIEW}").fetchall()}
        assert {"symbol", "trade_date", "open", "high", "low", "close", "volume", "adjustment"} <= columns
    finally:
        con.close()


def test_single_symbol_one_year_query_under_one_second() -> None:
    """SPEC 验收：单标的 1 年日线查询 < 1s（取三次最快值，避开首次导入抖动）。"""
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        rows = dc.bars("600519", start="2025-10-01", end="2026-09-30", adjust="qfq")
        timings.append(time.perf_counter() - started)
    assert rows, "1 年窗口查不到数据"
    best = min(timings)
    print(f"单标的 1 年查询最快 {best * 1000:.1f} ms（{len(rows)} 行）")
    assert best < 1.0, f"1 年查询耗时 {best:.3f}s，超过 1s"


def test_events_pit_invariants_hold() -> None:
    """SPEC 验收：available_at 无空值；且不得早于 event_time（早于即为 PIT 破缺）。"""
    for symbol in EVENT_SYMBOLS:
        rows = dc.events(symbol)
        assert rows, f"{symbol} 没有事件"
        assert all(row["available_at"] is not None for row in rows)
        violations = [
            row["event_id"] for row in rows if row["available_at"] < row["event_time"]
        ]
        assert not violations, f"{symbol} 存在 available_at < event_time：{violations[:5]}"
        assert {row["symbol"] for row in rows} == {symbol}


def test_events_keep_spec_fields_and_nested_shapes() -> None:
    rows = dc.events("300750")
    required = {
        "event_id", "event_type", "title", "summary", "event_time", "available_at", "observed_at",
        "direction", "direction_norm", "confidence", "importance_score", "factor_value",
        "factor_scores", "industries", "stocks", "source", "original_source",
        "content_hash", "quality_status", "source_time_quality",
    }
    assert required <= set(rows[0]), f"缺字段：{required - set(rows[0])}"
    assert all(row["content_hash"] and row["source"] for row in rows), "溯源字段不得为空"

    stocks = next(row["stocks"] for row in rows if row["stocks"])
    assert isinstance(stocks, list) and {"code", "name", "reason"} <= set(stocks[0])
    assert all(isinstance(row["industries"], list) for row in rows)
    assert json.loads(rows[0]["factor_scores"]) is not None


def test_meta_files_match_materialized_rows() -> None:
    bars_meta = _meta("bars.json")
    assert bars_meta["source"]["verify"]["valid"] is True
    assert bars_meta["source"]["verify"]["issues"] in ([], None)
    assert len(bars_meta["source"]["partitions"]) == 4, "应为 4 个年度分片（2 年 × 2 复权）"

    for item in bars_meta["outputs"]:
        path = REPO_ROOT / item["path"]
        assert path.is_file(), f"清单里的文件不存在：{item['path']}"
        assert len(dc.bars(item["symbol"], adjust=item["adjustment"])) == item["rows"]

    events_meta = _meta("events.json")
    assert events_meta["window"] == {"since": EVENT_WINDOW_START, "to": EVENT_WINDOW_END}
    total = sum(item["rows"] for item in events_meta["outputs"])
    assert total == len(dc.events()), "事件清单行数与落盘不一致"


def test_no_stale_temp_files_in_data_dirs() -> None:
    """失败重跑不得留下中间文件——它们会被视图 glob 捞进去。"""
    for sub in ("bars", "events"):
        leftovers = [p.name for p in (DATA_DIR / sub).glob("*") if not p.name.endswith(".parquet")]
        assert not leftovers, f"data/{sub} 有非 Parquet 残留：{leftovers}"
        assert not list((DATA_DIR / sub).glob("*.partial.parquet"))


def test_meta_dir_is_not_tracked_by_git() -> None:
    """数据与清单都在 .gitignore 覆盖范围内。"""
    tracked = Path(REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/" in tracked
