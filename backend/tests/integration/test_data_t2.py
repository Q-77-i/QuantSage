"""T2 + P2-M2a/M2b 验收：真实数据（data/bars、data/events）必须先在盘上。

先跑 `uv run python scripts/download_bars.py` 与 `scripts/download_events.py`。
默认不收集；用 `uv run pytest -m integration` 触发。

M2a 起行情是**全市场**（21 片 = 7 年 × 3 复权，约 5500 只标的）；M2b 起事件语料
同样是**全市场按日**（一条事件一行、标的是数组），覆盖面差异不再是「行情全市场 /
事件三只标的」那种断层。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import duckdb
import pytest

from app.core.config import REPO_ROOT, get_settings
from app.data import data_health as dh
from app.data import delisting
from app.data import duckdb_client as dc
from scripts.download_bars import ADJUSTS, BARS_NAME, LEGACY_PATTERN, PARTITIONS, YEARS

pytestmark = pytest.mark.integration

DATA_DIR = get_settings().data_dir

#: 示例标的（P1 的三只）。M2b 起事件语料是全市场，这里只是挑三只做断言样例。
SAMPLE_SYMBOLS = ("600519", "300750", "600036")

#: P1 事件语料的回归锚点（清理按标的文件前导出的 353 对 event_id+content_hash）
P1_EVENTS = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "p1_events.json").read_text(encoding="utf-8")
)

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


def _file_sha256(path: Path) -> str:
    """分块算 sha256：单片最大约 80MB，不值得整块读进内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_bars_manifest_carries_data_version_fingerprint() -> None:
    """M2a 决策：价量口径不动，靠这组指纹声明「这批数字跑在哪份数据上」。

    锚点是**逐片 `object_key` + sha256**，不是回执顶层的版本号——实测同一批分片来自
    多个 release，没有任何一片来自顶层那个版本号。

    **对账对象是盘上那 21 片本身，不是 CLI 的回执**（2026-10-08 订正）：回执
    `data/raw/.xiaoshi-history-state.json` 由行情下载与 M2b 的归档通道**共用同一个文件名**，
    CLI 每次调用都按最新 publication 重写它——日增量一跑，行情条目就没了（2026-10-07 踩坑
    记录已写「不能只信回执」）。物化是**字节级复制**（见 `download_bars.materialize`），
    所以「清单 sha256 == 盘上文件 sha256」与回执对账**等价且更强**：它验的是真正拿去算数字的
    那份字节。M2b 的 `events.json` 走的是同一条路子（自算逐日 digest，不看回执）；
    平台来源这件事由下载期写下的 `source.verify` 声明（回执在 store 里也已滚动清理，实测
    21 个行情对象全不在 `data/raw/o/`）。
    """
    meta = _meta("bars.json")
    fingerprint = meta["fingerprint"]

    assert len(fingerprint["shards"]) == len(PARTITIONS)
    assert all(item["object_key"] and len(item["sha256"]) == 64 for item in fingerprint["shards"])
    assert fingerprint["receipt_data_version"]
    assert fingerprint["receipt_manifest_version"]
    assert meta["source"]["verify"]["valid"] is True
    assert meta["source"]["verify"]["issues"] in ([], None)

    # 逐片对账：清单里两处 sha 必须自洽，且与盘上那一份逐字节一致
    shard_sha = {item["object_key"]: item["sha256"] for item in fingerprint["shards"]}
    outputs = {(item["year"], item["adjust"]): item for item in meta["outputs"]}
    assert set(outputs) == set(PARTITIONS)
    for (year, adjust), output in sorted(outputs.items()):
        path = _partition_file(year, adjust)
        assert path.is_file(), f"缺分片 {path.name}"
        expected = shard_sha[output["object_key"]]
        assert output["sha256"] == expected, f"{path.name}：outputs 与 fingerprint 两处 sha 不一致"
        assert _file_sha256(path) == expected, f"{path.name} 与清单 sha 不符（盘上这份不是清单说的那份）"


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


def test_events_are_market_wide_and_per_day() -> None:
    """M2b 落盘形态：一条事件一行、按日分区、标的是数组；全市场覆盖（不是只剩三只）。"""
    rows = dc.events()
    assert rows, "事件语料为空"
    symbols = {code for row in rows for code in row["symbols"]}
    assert len(symbols) > 1000, f"只有 {len(symbols)} 只标的——不像全市场语料"

    files = sorted((DATA_DIR / "events").glob("cn-events_*.parquet"))
    assert files, "没有按日落盘的分区文件"
    assert len(files) >= 60, f"只有 {len(files)} 个日分区（回填应覆盖 92 天窗口）"
    assert not list((DATA_DIR / "events").glob("[0-9]" * 6 + ".parquet")), "P1 的按标的文件必须清掉"


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
        # 按标的过滤走数组包含：命中的每一行都必须真的挂着这只标的
        assert all(symbol in row["symbols"] for row in rows)


def test_p1_events_survive_the_layout_change() -> None:
    """M2b 的回归锚点：P1 出现过的事件 **event_id 一条不少**。

    换落盘形态最怕的是「看起来有数据、其实换丢了一批」。P1 的三只标的是**最老的读者**，
    拿它们当锚点比统计总行数更能证明「没丢」。

    锚点比对的是 **event_id 而不是 `(event_id, content_hash)`**：实测平台会对既有事件重发
    修订版（内容一改 hash 就变，264 条缺失里 255 条属于这类），拿 hash 当锚点会把「平台改了
    摘要」误判成「我们丢了数据」。真正要守的是**事件在不在**；内容以平台当前修订为准。

    唯一合法缺口是**归档尚未发布的日期**（实测归档落后最新交易日 1 天，如 09-30）——
    把「还没发布」与「真丢了」分开报，不用一句「等一等」掩盖真丢数据。
    """
    coverage_last = dc.event_coverage()["end"]
    rows = dc.events()
    ids = {row["event_id"] for row in rows}
    pairs = {(row["event_id"], row["content_hash"]) for row in rows}

    lost = [p for p in P1_EVENTS["pairs"] if p[2] <= coverage_last and p[0] not in ids]
    assert not lost, f"归档已发布区间内丢了 {len(lost)} 条事件：{lost[:5]}"

    pending = [p for p in P1_EVENTS["pairs"] if p[2] > coverage_last and p[0] not in ids]
    revised = [p for p in P1_EVENTS["pairs"] if p[0] in ids and (p[0], p[1]) not in pairs]
    print(
        f"P1 锚点 {len(P1_EVENTS['pairs'])} 条：{len(P1_EVENTS['pairs']) - len(pending) - len(revised)} 条逐字节相同、"
        f"{len(revised)} 条被平台修订（hash 变、事件在）、{len(pending)} 条待归档发布"
    )


def test_event_id_is_unique_within_a_day_but_not_globally() -> None:
    """`event_id` 在**日分区内唯一**（物化时校验），但**不是全局唯一键**。

    实测平台对「按月复发的同题事件」复用 id（如 `news:1818329` 既是 8 月 CPI 也是 9 月 CPI，
    标题数值不同）——84 天里 18 例。所以：① 查询层不能拿它当主键去重；
    ② 前端列表的 React key 必须带时间（否则同窗口内两次出现会丢行）。
    """
    con = duckdb.connect()
    try:
        pattern = str(DATA_DIR / "events" / "*.parquet")
        dup_in_day = con.execute(
            f"SELECT count(*) FROM (SELECT filename, event_id FROM read_parquet('{pattern}', filename=true)"
            " GROUP BY 1, 2 HAVING count(*) > 1)"
        ).fetchone()[0]
        assert dup_in_day == 0, "同一个日分区里出现了重复 event_id"
        cross_day = con.execute(
            f"SELECT count(*) FROM (SELECT event_id FROM read_parquet('{pattern}')"
            " GROUP BY 1 HAVING count(*) > 1)"
        ).fetchone()[0]
    finally:
        con.close()
    # 跨日重复是平台行为，不设硬上限，但数量级突变要有人看见
    assert cross_day < 100, f"跨日重复 event_id 达 {cross_day} 条，平台行为可能变了，需复核"


def test_events_cover_the_same_window_as_p1_and_more() -> None:
    """语料覆盖区间只扩不缩，且必须追平归档已发布的最新日。"""
    coverage = dc.event_coverage()
    assert coverage["start"] and coverage["end"], "覆盖区间必须查得到"
    p1_start, _ = P1_EVENTS["window"]
    assert coverage["start"] <= p1_start, f"语料起点 {coverage['start']} 晚于 P1 的 {p1_start}"

    from app.etl import archive

    archive_last = archive.coverage().last_day.isoformat()
    assert coverage["end"] == archive_last, (
        f"本地语料止于 {coverage['end']}，归档已发布到 {archive_last}——漏拉了，跑 download_events.py"
    )

    for symbol in SAMPLE_SYMBOLS:
        dates = [row["trade_date"].isoformat() for row in dc.bars(symbol, adjust="qfq")]
        assert dates[0] <= coverage["start"] and dates[-1] >= coverage["end"]


def test_events_keep_spec_fields_and_nested_shapes() -> None:
    rows = dc.events("300750")
    required = {
        "event_id", "event_type", "title", "summary", "event_time", "available_at", "observed_at",
        "reported_available_at", "direction", "direction_norm", "confidence", "importance_score",
        "factor_value", "factor_scores", "industries", "stocks", "symbols", "source",
        "original_source", "source_url", "content_hash", "quality_status", "source_time_quality",
        # quant-event-v2 的修订/去重字段（M2b 起按新 schema 落盘）
        "dedup_key", "record_version", "revision_id", "is_corrected", "correction_count", "person",
    }
    assert required <= set(rows[0]), f"缺字段：{required - set(rows[0])}"
    assert all(row["content_hash"] and row["source"] for row in rows), "溯源字段不得为空"
    assert all(row["original_source"] for row in rows), "原始发布方不得为空"

    # `stocks` 原样存 JSON 文本备审计；查询走归一化的 `symbols` 数组
    payload = json.loads(rows[0]["stocks"])
    assert isinstance(payload, list) and {"code", "name", "reason"} <= set(payload[0])
    assert all(isinstance(row["symbols"], list) for row in rows)
    assert all(isinstance(row["industries"], list) for row in rows)
    assert json.loads(rows[0]["factor_scores"]) is not None


def test_meta_files_match_materialized_rows() -> None:
    bars_meta = _meta("bars.json")
    assert bars_meta["schema"] == "quantsage.bars_meta/v2"
    for item in bars_meta["outputs"]:
        path = REPO_ROOT / item["path"]
        assert path.is_file(), f"清单里的文件不存在：{item['path']}"

    events_meta = _meta("events.json")
    assert events_meta["schema"] == "quantsage.events_meta/v2"
    assert events_meta["partition"]["key"].startswith("event_time")
    for item in events_meta["days"]:
        path = DATA_DIR / "events" / item["file"]
        assert path.is_file(), f"清单里的日分区不存在：{item['file']}"
        assert path.stat().st_size > 0
    assert events_meta["fingerprint"]["days"] == len(events_meta["days"])
    assert events_meta["fingerprint"]["rows"] == sum(item["rows"] for item in events_meta["days"])
    print(
        f"事件语料 {events_meta['fingerprint']['days']} 天 / {events_meta['fingerprint']['rows']} 行，"
        f"指纹 {events_meta['fingerprint']['digest'][:16]}"
    )


def test_etl_status_agrees_with_disk() -> None:
    """`/etl/status` 的口径来自同一个 runner：本地覆盖与磁盘一致，缺口可解释。"""
    from app.etl import runner

    status = runner.status()
    files = sorted((DATA_DIR / "events").glob("cn-events_*.parquet"))
    assert status["local"]["days"] == len(files)
    assert status["local"]["start"] == files[0].name[len("cn-events_") : -len(".parquet")]
    assert status["local"]["end"] == files[-1].name[len("cn-events_") : -len(".parquet")]


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


# ── P2-M2c：数据体检 + 退市股覆盖核实 ──────────────────────────


def test_data_health_reports_no_error_on_real_data() -> None:
    """M2c 验收：真实全量数据上 error 级检查全绿。

    **只断 error 级**——warn / info 的基线（B5 的不一致率、U1 的停止交易只数）会随日增量
    与平台修订漂移，把它们写进 assert 是给自己埋雷。
    """
    checks = dh.run_all()

    errors = [(check.id, check.title, check.detail) for check in checks if check.level == "error"]
    assert errors == [], errors
    assert len(checks) == len(dh.CHECKS), "有检查没跑成（`run_all` 会把异常折成 error，这里再兜一次）"


def test_delisting_coverage_is_not_survivor_biased() -> None:
    """生存者偏差核实的不变量：分片里**确实**有行情早于数据末端结束的标的。

    不断言具体只数——它会随退市与新增数据变化；要守的性质是「不为空」：只有活下来的
    公司才会让这个集合为空，而那正是生存者偏差的定义。
    """
    report = delisting.run()

    assert report.stopped, "分片里没有任何停止交易的标的——疑似生存者偏差"
    assert sum(report.by_year.values()) == len(report.stopped)
    # 口径声明必须在报告里——「本地可得清单」不是「全市场清单」
    assert "本地可得清单" in report.method_note
