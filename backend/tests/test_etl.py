"""M2b ETL 离线单测：归档分片 → 日分区的口径、幂等、清单与遗留清理。

全部走合成分片（形状与归档一致：少量顶层列 + `payload_json`），不打网络、不碰真实 data/。
真实归档的验收在 integration 用例与 `scripts/audit_calendar.py`。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from app.etl import archive, runner, store

CN = "Asia/Shanghai"

#: 归档分片的列集（实测形状，见 SPEC §3 M2b）
ARCHIVE_COLUMNS = {
    "event_id": "VARCHAR",
    "event_type": "VARCHAR",
    "event_time": "TIMESTAMPTZ",
    "reported_available_at": "TIMESTAMPTZ",
    "available_at": "TIMESTAMPTZ",
    "source": "VARCHAR",
    "importance_score": "DOUBLE",
    "content_hash": "VARCHAR",
    "payload_json": "VARCHAR",
    "canonical_schema_version": "VARCHAR",
    "quality_status": "VARCHAR",
}


def payload(**overrides: object) -> str:
    """一条 quant-event-v2 记录的 JSON 文本。"""
    base: dict[str, object] = {
        "schema_version": "quant-event-v2",
        "title": "标题",
        "summary": "摘要",
        "direction": "利多",
        "confidence": 0.4,
        "factor_value": 0.5,
        "factor_scores": {"score": 55.0, "version": "factor-v2"},
        "industries": ["半导体"],
        "stocks": [{"code": "600519.SH", "name": "贵州茅台", "reason": None}],
        "source": "巨潮资讯",
        "source_url": "https://example.invalid/a.pdf",
        "quality_status": "stable",
        "source_time_quality": "exact",
        "dedup_key": "news:1",
        "record_version": 1,
        "revision_id": "abc123",
        "revision_time": None,
        "is_corrected": False,
        "correction_count": 0,
        "person": [],
        "observed_at": f"2026-09-29T10:00:00+08:00",
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def write_shard(
    path: Path,
    rows: list[dict],
    *,
    event_type: str = "news",
) -> Path:
    """写一片归档分片；`rows` 给 `event_id` / `payload` / 可选时间覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    values = []
    for row in rows:
        event_id = row.get("event_id", "news:1")
        event_time = row.get("event_time", f"2026-09-29T10:00:00+08:00")
        available_at = row.get("available_at", event_time)
        body = row.get("payload", payload(event_id=event_id))
        values.append(
            f"('{event_id}', '{event_type}', TIMESTAMPTZ '{event_time}',"
            f" TIMESTAMPTZ '{available_at}', TIMESTAMPTZ '{available_at}', '巨潮资讯', 50.0,"
            f" 'hash-{event_id}', {_sql_str(body)}, 'history-canonical-v2', 'passed')"
        )
    con = duckdb.connect()
    try:
        con.execute(
            "COPY (SELECT * FROM (VALUES " + ", ".join(values) + ") t("
            + ", ".join(ARCHIVE_COLUMNS) + "))"
            f" TO '{path}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    return path


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def shard_of(path: Path, event_type: str = "news") -> archive.Shard:
    return archive.Shard(
        event_type=event_type,
        object_key=f"history/v2/public/releases/x/data/event-timeline/date=d/event_type={event_type}/data.parquet",
        path=path,
        sha256=f"sha-{event_type}",
        size=path.stat().st_size,
    )


# ── 归一化 ────────────────────────────────────────────────


def test_symbols_normalized_from_code_name_and_suffixes(tmp_path: Path) -> None:
    """`code` 为 null 时退回 `name`；`.SH`/`.SZ` 接受，非 CN 后缀（韩股等）一律丢弃。"""
    shard = write_shard(
        tmp_path / "news.parquet",
        [
            {
                "event_id": "news:1",
                "payload": payload(
                    stocks=[
                        {"code": "600519.SH", "name": "贵州茅台", "reason": None},  # 带后缀
                        {"code": None, "name": "300750.SZ", "reason": None},  # 只能是名字里的码
                        {"code": None, "name": "000660.KS", "reason": None},  # 韩股，丢弃
                        {"code": None, "name": "东山精密", "reason": None},  # 无名无码，丢弃
                        {"code": "688825", "name": "长鑫科技", "reason": None},  # 裸码
                    ]
                ),
            }
        ],
    )
    entry = store.materialize_day(date(2026, 9, 29), {"news": shard_of(shard)}, tmp_path / "e", tmp_path / "r")

    row = _read_day(tmp_path / "e" / entry.file)
    assert row["symbols"] == ["300750", "600519", "688825"]
    assert row["stocks"].startswith("[")  # 原始 stocks 原样留档备审计


def test_event_without_any_symbol_keeps_empty_array(tmp_path: Path) -> None:
    shard = write_shard(
        tmp_path / "policy.parquet",
        [{"event_id": "policy:1", "payload": payload(stocks=[])}],
        event_type="policy",
    )
    entry = store.materialize_day(
        date(2026, 9, 29), {"policy": shard_of(shard, "policy")}, tmp_path / "e", tmp_path / "r"
    )
    row = _read_day(tmp_path / "e" / entry.file)
    assert row["symbols"] == []
    assert json.loads((tmp_path / "r" / "2026-09-29.json").read_text(encoding="utf-8"))[
        "symbols_without_code"
    ] == 1


def test_direction_normalized_and_non_sentiment_left_null(tmp_path: Path) -> None:
    shard = write_shard(
        tmp_path / "news.parquet",
        [
            {"event_id": "n1", "payload": payload(direction="利多")},
            {"event_id": "n2", "payload": payload(direction="融资定增")},
        ],
    )
    entry = store.materialize_day(date(2026, 9, 29), {"news": shard_of(shard)}, tmp_path / "e", tmp_path / "r")
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"SELECT direction_norm FROM read_parquet('{tmp_path / 'e' / entry.file}') ORDER BY event_id"
        ).to_arrow_table().to_pylist()
    finally:
        con.close()
    assert [row["direction_norm"] for row in rows] == ["bullish", None]


def test_snapshot_event_types_are_excluded(tmp_path: Path) -> None:
    """状态流（成分快照、未来概率观察）不进语料——实测两天占 91.7% 体量且无 title。"""
    shard = write_shard(
        tmp_path / "sector.parquet",
        [{"event_id": "sector-constituent:1", "payload": payload(stocks=[])}],
        event_type="sector_constituent",
    )
    with pytest.raises(ValueError, match="没有可收的事件类型"):
        store.materialize_day(
            date(2026, 9, 29), {"sector_constituent": shard_of(shard, "sector_constituent")},
            tmp_path / "e", tmp_path / "r",
        )


# ── 落盘校验 ──────────────────────────────────────────────


def test_pit_violation_aborts_and_leaves_no_partial_file(tmp_path: Path) -> None:
    """`available_at < event_time` 是 PIT 语义被破坏，必须中止且不留半截文件。"""
    shard = write_shard(
        tmp_path / "news.parquet",
        [
            {
                "event_id": "n1",
                "event_time": "2026-09-29T10:00:00+08:00",
                "available_at": "2026-09-28T10:00:00+08:00",  # 早于事发：不合法
            }
        ],
    )
    with pytest.raises(ValueError, match="落盘校验失败"):
        store.materialize_day(date(2026, 9, 29), {"news": shard_of(shard)}, tmp_path / "e", tmp_path / "r")
    assert list((tmp_path / "e").glob("*.parquet")) == []
    assert list((tmp_path / "e").glob(".*partial*")) == []


def test_clean_legacy_files_only_removes_p1_shape(tmp_path: Path) -> None:
    """P1 的 `{六位码}.parquet` 必须清掉（否则同一事件被算两遍），日分区不能误伤。"""
    events = tmp_path / "events"
    events.mkdir()
    for name in ("600519.parquet", "300750.parquet", "cn-events_2026-09-29.parquet"):
        (events / name).write_bytes(b"x")

    removed = store.clean_legacy_files(events)

    assert sorted(removed) == ["300750.parquet", "600519.parquet"]
    assert [path.name for path in events.glob("*.parquet")] == ["cn-events_2026-09-29.parquet"]


# ── 日期规划：按自然日（归档不停市）────────────────────────


def test_plan_days_covers_every_calendar_day() -> None:
    """归档按自然日发布：中秋 09-25 与周末 09-26/27 都得在计划里，否则静默丢新闻。"""
    days = runner.plan_days(date(2026, 9, 29), since=date(2026, 9, 25))
    assert [day.isoformat() for day in days] == [
        "2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29",
    ]


def test_plan_days_trailing_window_is_calendar_days() -> None:
    assert len(runner.plan_days(date(2026, 9, 29), trailing=7)) == 7


def test_plan_days_rejects_beyond_calendar_coverage() -> None:
    with pytest.raises(Exception, match="超出冻结日历覆盖"):
        runner.plan_days(date(2027, 6, 1), trailing=1)


# ── 幂等与编排 ────────────────────────────────────────────


def test_run_is_idempotent_and_skips_unchanged_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一份归档分片重跑：物化一次，第二次跳过；文件逐字节一致。"""
    day = date(2026, 9, 29)
    shard_path = write_shard(tmp_path / "src" / "news.parquet", [{"event_id": "n1"}])
    monkeypatch.setattr(archive, "download_day", lambda d, data_dir=None: True)
    monkeypatch.setattr(archive, "day_shards", lambda d, data_dir=None: {"news": shard_of(shard_path)})
    monkeypatch.setattr(
        archive, "coverage",
        lambda data_dir=None: archive.ArchiveCoverage(last_day=day, files=1, rows=1, manifest_version="x"),
    )

    first = runner.run("manual", days=[day], data_dir=tmp_path)
    second = runner.run("manual", days=[day], data_dir=tmp_path)

    assert [item.status for item in first.days] == ["ok"]
    assert [item.status for item in second.days] == ["skipped"]
    ledger = (tmp_path / "_meta" / "etl_runs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(ledger) == 2  # 每次跑都留痕，即便全是跳过
    manifest = json.loads((tmp_path / "_meta" / "events.json").read_text(encoding="utf-8"))
    assert manifest["fingerprint"]["days"] == 1
    assert manifest["kept_event_types"] == list(store.KEPT_EVENT_TYPES)
    assert manifest["excluded_event_types"] == list(store.EXCLUDED_EVENT_TYPES)


def test_run_records_empty_day_without_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """404（节假日/未发布）= 该日没有分片，记台账但不算失败。"""
    day = date(2026, 10, 1)
    monkeypatch.setattr(archive, "download_day", lambda d, data_dir=None: False)
    monkeypatch.setattr(archive, "day_shards", lambda d, data_dir=None: {})
    monkeypatch.setattr(
        archive, "coverage",
        lambda data_dir=None: archive.ArchiveCoverage(last_day=day, files=0, rows=0, manifest_version="x"),
    )

    report = runner.run("manual", days=[day], data_dir=tmp_path)

    assert report.result == "ok"
    assert [item.status for item in report.days] == ["empty"]


def test_day_failure_does_not_stop_the_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单日失败记台账继续跑：回填是长任务，一天出错不该让 84 天白拉。"""
    good = date(2026, 9, 29)
    bad = date(2026, 9, 28)
    shard_path = write_shard(tmp_path / "src" / "news.parquet", [{"event_id": "n1"}])

    def fake_download(day: date, data_dir: Path | None = None) -> bool:
        if day == bad:
            raise RuntimeError("模拟网络抖动")
        return True

    monkeypatch.setattr(archive, "download_day", fake_download)
    monkeypatch.setattr(archive, "day_shards", lambda d, data_dir=None: {"news": shard_of(shard_path)})
    monkeypatch.setattr(
        archive, "coverage",
        lambda data_dir=None: archive.ArchiveCoverage(last_day=good, files=1, rows=1, manifest_version="x"),
    )

    report = runner.run("manual", days=[bad, good], data_dir=tmp_path)

    assert [(item.date, item.status) for item in report.days] == [
        ("2026-09-28", "failed"), ("2026-09-29", "ok"),
    ]
    assert report.result == "failed"
    assert "模拟网络抖动" in report.days[0].error


def test_rate_limit_aborts_the_run_instead_of_hammering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """429 是停止信号：立刻中止整轮，而不是换下一天接着撞（实测撞了 40 次才被人工发现）。"""
    days = [date(2026, 8, 21), date(2026, 8, 22), date(2026, 8, 23)]
    shard_path = write_shard(tmp_path / "src" / "news.parquet", [{"event_id": "n1"}])

    def fake_download(day: date, data_dir: Path | None = None) -> bool:
        if day == date(2026, 8, 22):
            raise archive.RateLimited("限流", retry_after=2621)
        return True

    monkeypatch.setattr(archive, "download_day", fake_download)
    monkeypatch.setattr(archive, "day_shards", lambda d, data_dir=None: {"news": shard_of(shard_path)})
    monkeypatch.setattr(
        archive, "coverage",
        lambda data_dir=None: archive.ArchiveCoverage(last_day=days[-1], files=1, rows=1, manifest_version="x"),
    )

    report = runner.run("backfill", days=days, data_dir=tmp_path)

    assert report.result == "rate_limited"
    assert report.retry_after_seconds == 2621
    assert [item.date for item in report.days] == ["2026-08-21"]  # 只跑了被限流前的那一天
    assert report.unattempted == 2  # 余下两天没被尝试，也没被记成 failed
    assert "2621" not in report.summary() and "限流" in report.summary()
    # 台账要留下这条，运维才知道是「等一会儿再来」而不是「数据有问题」
    ledger = json.loads((tmp_path / "_meta" / "etl_runs.jsonl").read_text(encoding="utf-8").strip())
    assert ledger["result"] == "rate_limited" and ledger["retry_after_seconds"] == 2621


def test_rate_limited_marker_detected_from_cli_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """识别平台原文（`rate_limited_no_retry` + `Retry-After=`）——按原文判定，不猜退出码。"""
    output = "xiaoshi: rate_limited_no_retry HTTP 429 Retry-After=2621 fingerprint=abc"

    class Result:
        returncode = 1
        stdout = ""
        stderr = output

    monkeypatch.setattr(archive, "_run", lambda args: Result())  # type: ignore[arg-type]

    with pytest.raises(archive.RateLimited) as excinfo:
        archive.download_day(date(2026, 8, 21), tmp_path)

    assert excinfo.value.retry_after == 2621


def test_backfill_only_requests_missing_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回填只补缺：已落盘的日子不再重复申请（平台给了 429，重复申请还会推迟窗口）。"""
    events = tmp_path / "events"
    events.mkdir()
    (events / store.day_file_name(date(2026, 9, 28))).write_bytes(b"x")
    (tmp_path / "raw" / "events").mkdir(parents=True)
    seen: list[list[date]] = []

    monkeypatch.setattr(
        archive, "coverage",
        lambda data_dir=None: archive.ArchiveCoverage(last_day=date(2026, 9, 29), files=1, rows=1, manifest_version="x"),
    )
    monkeypatch.setattr(runner, "run", lambda scope, **kwargs: seen.append(kwargs["days"]) or runner.RunReport(scope=scope, started_at=""))

    runner.backfill(tmp_path, days=10)

    assert seen and date(2026, 9, 28) not in seen[0]
    assert date(2026, 9, 29) in seen[0]


def test_status_reports_gap_against_archive_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """漏拉判据 = 本地最新日 vs 归档 `coverage.last`，并按是否交易日分类。"""
    monkeypatch.setattr(
        archive, "coverage",
        lambda data_dir=None: archive.ArchiveCoverage(
            last_day=date(2026, 9, 30), files=1, rows=1, manifest_version="x"
        ),
    )
    events = tmp_path / "events"
    events.mkdir()
    (events / store.day_file_name(date(2026, 9, 28))).write_bytes(b"x")
    (tmp_path / "raw" / "events").mkdir(parents=True)

    result = runner.status(tmp_path)

    assert result["local"]["end"] == "2026-09-28"
    assert result["gap"]["missing_days"] == [
        {"date": "2026-09-29", "trading_day": True},
        {"date": "2026-09-30", "trading_day": True},
    ]
    assert result["gap"]["trading_day_count"] == 2


def test_status_reports_internal_hole_not_just_the_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """中间的空洞也要报：末日追平归档边界，不等于语料是连续的。

    首次回填被限流打断后就是这样——本地已有 07-07…08-20 与 09-23…09-29，末日追平了归档，
    旧口径报「缺口 0 天」，中间的 33 天没人知道。
    """
    monkeypatch.setattr(
        archive, "coverage",
        lambda data_dir=None: archive.ArchiveCoverage(
            last_day=date(2026, 9, 30), files=1, rows=1, manifest_version="x"
        ),
    )
    events = tmp_path / "events"
    events.mkdir()
    for day in (date(2026, 9, 28), date(2026, 9, 30)):
        (events / store.day_file_name(day)).write_bytes(b"x")
    (tmp_path / "raw" / "events").mkdir(parents=True)

    result = runner.status(tmp_path)

    assert [item["date"] for item in result["gap"]["missing_days"]] == ["2026-09-29"]


def test_status_excludes_days_the_archive_has_nothing_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """台账里记过 `empty` 的日子不是缺口——归档那天真的没有可收内容（如只有快照类分片）。"""
    monkeypatch.setattr(
        archive, "coverage",
        lambda data_dir=None: archive.ArchiveCoverage(
            last_day=date(2026, 9, 30), files=1, rows=1, manifest_version="x"
        ),
    )
    events = tmp_path / "events"
    events.mkdir()
    (events / store.day_file_name(date(2026, 9, 30))).write_bytes(b"x")
    (tmp_path / "raw" / "events").mkdir(parents=True)
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "etl_runs.jsonl").write_text(
        json.dumps({"scope": "backfill", "result": "ok", "days": [
            {"date": "2026-09-28", "status": "empty", "error": "分片里没有可收类型"},
            {"date": "2026-09-29", "status": "empty", "error": "归档没有这一天"},
        ]}),
        encoding="utf-8",
    )

    result = runner.status(tmp_path)

    assert result["gap"]["missing_days"] == []


def test_manifest_reflects_disk_not_memory(tmp_path: Path) -> None:
    """清单从磁盘重建：文件被手工删掉后，清单不会再宣称它有。"""
    events = tmp_path / "events"
    raw = tmp_path / "raw" / "events"
    events.mkdir()
    raw.mkdir(parents=True)
    day = date(2026, 9, 29)
    target = events / store.day_file_name(day)
    target.write_bytes(b"fake")
    (raw / "2026-09-29.json").write_text(
        json.dumps({"date": "2026-09-29", "file": target.name, "rows": 7, "symbols": 3,
                    "sha256": "x", "materialized_at": "", "shards": []}),
        encoding="utf-8",
    )

    target.unlink()
    entries = store.read_day_entries(events, raw)

    assert entries == []


def _read_day(path: Path) -> dict:
    con = duckdb.connect()
    try:
        return con.execute(f"SELECT * FROM read_parquet('{path}')").to_arrow_table().to_pylist()[0]
    finally:
        con.close()
