"""T2 + P2-M2a 验收：真实数据（data/bars、data/events）必须先在盘上。

先跑 `uv run python scripts/download_bars.py` 与 `scripts/download_events.py`。
默认不收集；用 `uv run pytest -m integration` 触发。

M2a 起行情是**全市场**（21 片 = 7 年 × 3 复权，约 5500 只标的），事件语料仍是
P1 的三只示例标的 + 约 3 个月窗口——两者的覆盖面差异是**有意的**，事件层要 M2b 才动。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb
import pytest

from app.core.config import REPO_ROOT, get_settings
from app.data import duckdb_client as dc
from scripts.download_bars import ADJUSTS, BARS_NAME, LEGACY_PATTERN, PARTITIONS, YEARS
from scripts.download_events import SYMBOLS as EVENT_SYMBOLS

pytestmark = pytest.mark.integration

DATA_DIR = get_settings().data_dir
EVENT_WINDOW_START = "2026-07-05"
EVENT_WINDOW_END = "2026-09-30"

#: 示例标的（事件语料只覆盖这三只）。全市场另有约 5500 只，见下面的覆盖用例。
SAMPLE_SYMBOLS = tuple(EVENT_SYMBOLS)

#: 分片里标的数的下限。实测 2020 年 5200+、2026 年 5500+，卡在 3000 是为了
#: 「过滤成样例子集」这类退化能被立刻抓住，又不至于对数据源的正常波动过敏
MIN_SYMBOLS_PER_PARTITION = 3000

#: 一个**不在示例里**的真标的（平安银行）。P1 时代它被当「不存在的代码」用，
#: 全市场落地后它必须查得到——这条断言正是 M2a 的意义所在
NON_SAMPLE_SYMBOL = "000001"


def _meta(name: str) -> dict:
    return json.loads((DATA_DIR / "_meta" / name).read_text(encoding="utf-8"))


def _partition_file(year: int, adjust: str) -> Path:
    return DATA_DIR / "bars" / BARS_NAME.format(adjust=adjust, year=year)


def test_bars_partitions_cover_full_market() -> None:
    """21 片齐备，且每片都是整市场——不是被样例白名单过滤过的子集。"""
    meta = _meta("bars.json")
    outputs = {(item["year"], item["adjust"]): item for item in meta["outputs"]}
    assert set(outputs) == set(PARTITIONS), "清单的分片集合与脚本声明不一致"

    for (year, adjust), item in sorted(outputs.items()):
        path = _partition_file(year, adjust)
        assert path.is_file(), f"缺分片 {path.name}"
        assert (REPO_ROOT / item["path"]).is_file()
        assert item["rows"] > 0
        assert item["symbols"] > MIN_SYMBOLS_PER_PARTITION, (
            f"{path.name} 只覆盖 {item['symbols']} 个标的，像是被过滤过"
        )


def test_bars_manifest_carries_data_version_fingerprint() -> None:
    """M2a 决策：价量口径不动，靠这组指纹声明「这批数字跑在哪份数据上」。

    锚点是**逐片 `object_key` + sha256**，不是回执顶层的版本号——实测同一批分片来自
    多个 release，没有任何一片来自顶层那个版本号。所以这里也断言「清单里的 sha 与回执一致」。
    """
    meta = _meta("bars.json")
    fingerprint = meta["fingerprint"]

    assert len(fingerprint["shards"]) == len(PARTITIONS)
    assert all(item["object_key"] and len(item["sha256"]) == 64 for item in fingerprint["shards"])
    assert fingerprint["receipt_data_version"]
    assert meta["source"]["verify"]["valid"] is True
    assert meta["source"]["verify"]["issues"] in ([], None)

    # 指纹与回执必须对得上，否则「同一快照」这个说法是空头支票
    receipt = json.loads((DATA_DIR / "raw" / ".xiaoshi-history-state.json").read_text(encoding="utf-8"))
    for item in fingerprint["shards"]:
        assert receipt["files"][item["object_key"]]["sha256"] == item["sha256"]


def test_no_legacy_symbol_files_in_bars_dir() -> None:
    """P1 的按标的文件必须清干净。

    留着不会报错，但会被查询层的 `*.parquet` glob 捞进去，**同一标的算两遍**——
    这是本次变更唯一会造成静默错误的点，所以要单独守一条。
    """
    legacy = sorted(
        path.name for path in (DATA_DIR / "bars").glob("*.parquet") if LEGACY_PATTERN.match(path.name)
    )
    assert not legacy, f"data/bars 仍有 P1 按标的文件：{legacy}"


def test_single_symbol_rows_match_partition_sum() -> None:
    """双计的正面证据：视图查出来的行数 = 逐片显式读取之和。"""
    symbol, adjust = "600519", "qfq"
    con = duckdb.connect()
    try:
        expected = sum(
            con.execute(
                f"SELECT count(*) FROM read_parquet('{_partition_file(year, adjust).as_posix()}')"
                " WHERE symbol = ?",
                [symbol],
            ).fetchone()[0]
            for year in YEARS
        )
    finally:
        con.close()

    assert expected > 1000, "七年数据下 600519 的行数不该这么少"
    assert len(dc.bars(symbol, adjust=adjust)) == expected


def test_sql_can_query_full_history_of_any_symbol() -> None:
    """SPEC 验收：SQL 可查任意标的日线。示例之外的标的也必须查得到。"""
    con = dc.connect()
    try:
        rows = con.execute(
            f"SELECT count(*) FROM {dc.BARS_VIEW} WHERE symbol = ? AND adjustment = 'qfq'",
            [NON_SAMPLE_SYMBOL],
        ).fetchone()
        assert rows is not None and rows[0] > 1000, f"{NON_SAMPLE_SYMBOL} 应当有七年日线"

        for symbol in SAMPLE_SYMBOLS:
            count = con.execute(
                f"SELECT count(*) FROM {dc.BARS_VIEW} WHERE symbol = ? AND adjustment = 'qfq'",
                [symbol],
            ).fetchone()
            assert count is not None and count[0] > 400

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


def test_whole_market_single_day_aggregate_under_one_second() -> None:
    """PRD 验收：全市场日线 DuckDB 秒级查询。M10 的大盘总览就长在这个形状上。"""
    con = dc.connect()
    try:
        row = None
        timings = []
        for _ in range(3):
            started = time.perf_counter()
            row = con.execute(
                f"SELECT count(*), count(*) FILTER (WHERE change_pct > 0), sum(amount)"
                f" FROM {dc.BARS_VIEW}"
                " WHERE trade_date = DATE '2026-09-30' AND adjustment = 'qfq'"
            ).fetchone()
            timings.append(time.perf_counter() - started)
        assert row is not None and row[0] > MIN_SYMBOLS_PER_PARTITION
        best = min(timings)
        print(f"单日全市场聚合最快 {best * 1000:.1f} ms（{row[0]} 只标的）")
        assert best < 1.0, f"单日全市场聚合耗时 {best:.3f}s，超过 1s"
    finally:
        con.close()


def test_events_pit_invariants_hold() -> None:
    """SPEC 验收：available_at 无空值；且不得早于 event_time（早于即为 PIT 破缺）。"""
    for symbol in SAMPLE_SYMBOLS:
        rows = dc.events(symbol)
        assert rows, f"{symbol} 没有事件"
        assert all(row["available_at"] is not None for row in rows)
        violations = [
            row["event_id"] for row in rows if row["available_at"] < row["event_time"]
        ]
        assert not violations, f"{symbol} 存在 available_at < event_time：{violations[:5]}"
        assert {row["symbol"] for row in rows} == {symbol}


def test_events_stay_inside_bar_coverage() -> None:
    """事件窗口必须落在行情覆盖区间内，否则事件驱动策略无价可成交。"""
    for symbol in SAMPLE_SYMBOLS:
        dates = [row["trade_date"].isoformat() for row in dc.bars(symbol, adjust="qfq")]
        assert dates == sorted(dates), "日线必须按 trade_date 升序"
        assert dates[0] <= EVENT_WINDOW_START and dates[-1] >= EVENT_WINDOW_END


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
    assert bars_meta["schema"] == "quantsage.bars_meta/v2"
    for item in bars_meta["outputs"]:
        path = REPO_ROOT / item["path"]
        assert path.is_file(), f"清单里的文件不存在：{item['path']}"

    events_meta = _meta("events.json")
    assert events_meta["window"] == {"since": EVENT_WINDOW_START, "to": EVENT_WINDOW_END}
    total = sum(item["rows"] for item in events_meta["outputs"])
    assert total == len(dc.events()), "事件清单行数与落盘不一致"


def test_every_adjust_is_present() -> None:
    """三复权都落盘：查询层按 adjustment 过滤，缺一种就是查询期才炸。"""
    for adjust in ADJUSTS:
        assert any(adjust == a for _, a in PARTITIONS)
        for year in YEARS:
            assert _partition_file(year, adjust).is_file()


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
