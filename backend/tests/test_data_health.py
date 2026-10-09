"""M2c 数据体检的离线用例（SPEC §3 M2c「注入脏数据时体检脚本报警」）。

夹具走**真实列集**（bars 27 列 / events 30 列）：`connect()` 的视图是 `SELECT *` 直读
Parquet，列少一个检查就 binder 报错，而现成的 `write_bars_parquet` 只有 9 列、
`write_events_parquet` 12 列，盖不住体检的检查面。

脏数据一律打在**零容忍项**上（sha、重复行、日历、倒挂、缺分区）——往 raw 里塞一根错价
bar 触发不了 B5，那条的基线本身就是 2.9 万条，加一条等于没加。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.data import calendar as cal
from app.data import data_health as dh
from app.data import duckdb_client as dc
from tests.conftest import CN_TZ, ts

# ── 真实列集夹具 ────────────────────────────────────────────

BARS_COLUMNS = pa.schema(
    [
        ("market", pa.string()),
        ("symbol", pa.string()),
        ("trade_date", pa.date32()),
        ("trade_time", pa.timestamp("us")),
        ("trade_time_utc", pa.timestamp("us", tz="UTC")),
        ("timezone", pa.string()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
        ("amount", pa.float64()),
        ("trading_status", pa.string()),
        ("is_suspended", pa.bool_()),
        ("suspension_reason", pa.string()),
        ("suspension_evidence_url", pa.string()),
        ("suspension_evidence_sha256", pa.string()),
        ("turnover_scope", pa.string()),
        ("currency", pa.string()),
        ("adjustment", pa.string()),
        ("adj_factor", pa.float64()),
        ("available_at", pa.timestamp("us", tz=CN_TZ)),
        ("revision_time", pa.timestamp("us", tz=CN_TZ)),
        ("change_pct", pa.float64()),
        ("turnover_pct", pa.float64()),
        ("canonical_schema_version", pa.string()),
        ("quality_status", pa.string()),
    ]
)

EVENTS_COLUMNS = pa.schema(
    [
        ("event_id", pa.string()),
        ("event_type", pa.string()),
        ("event_time", pa.timestamp("us", tz=CN_TZ)),
        ("available_at", pa.timestamp("us", tz=CN_TZ)),
        ("reported_available_at", pa.timestamp("us", tz=CN_TZ)),
        ("observed_at", pa.timestamp("us", tz=CN_TZ)),
        ("title", pa.string()),
        ("summary", pa.string()),
        ("direction", pa.string()),
        ("direction_norm", pa.string()),
        ("confidence", pa.float64()),
        ("importance_score", pa.float64()),
        ("factor_value", pa.float64()),
        ("factor_scores", pa.string()),
        ("industries", pa.list_(pa.string())),
        ("stocks", pa.string()),
        ("symbols", pa.list_(pa.string())),
        ("source", pa.string()),
        ("original_source", pa.string()),
        ("source_url", pa.string()),
        ("content_hash", pa.string()),
        ("quality_status", pa.string()),
        ("source_time_quality", pa.string()),
        ("dedup_key", pa.string()),
        ("record_version", pa.int32()),
        ("revision_id", pa.string()),
        ("revision_time", pa.timestamp("us", tz=CN_TZ)),
        ("is_corrected", pa.bool_()),
        ("correction_count", pa.int32()),
        ("person", pa.string()),
    ]
)

SHARD = "cn-daily_CN_qfq_2026.parquet"


def bar(symbol: str, day: date, **over: object) -> dict:
    """一根正常 bar；用 `over` 覆盖出脏数据。"""
    values: dict = {
        "market": "CN",
        "symbol": symbol,
        "trade_date": day,
        "trade_time": ts(f"{day} 15:00:00").replace(tzinfo=None),
        "trade_time_utc": ts(f"{day} 07:00:00"),
        "timezone": CN_TZ,
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.0,
        "volume": 1e5,
        "amount": 1e6,
        "trading_status": "traded",
        "is_suspended": False,
        "suspension_reason": None,
        "suspension_evidence_url": None,
        "suspension_evidence_sha256": None,
        "turnover_scope": "source_reported_daily_total_scope_undocumented",
        "currency": "CNY",
        "adjustment": "qfq",
        "adj_factor": 1.0,
        "available_at": ts(f"{day} 20:25:00"),
        "revision_time": ts(f"{day} 20:25:00"),
        "change_pct": 0.0,
        "turnover_pct": 1.0,
        "canonical_schema_version": "quant-daily-v2",
        "quality_status": "passed",
    }
    values.update(over)
    return values


def event(event_id: str, day: date, **over: object) -> dict:
    values: dict = {
        "event_id": event_id,
        "event_type": "news",
        "event_time": ts(f"{day} 10:00:00"),
        "available_at": ts(f"{day} 10:30:00"),
        "reported_available_at": ts(f"{day} 10:30:00"),
        "observed_at": ts(f"{day} 10:35:00"),
        "title": "测试事件",
        "summary": "摘要",
        "direction": "利多",
        "direction_norm": "bullish",
        "confidence": 0.8,
        "importance_score": 60.0,
        "factor_value": 1.0,
        "factor_scores": None,
        "industries": ["银行"],
        "stocks": "600519.SH",
        "symbols": ["600519"],
        "source": "xiaoshi-archive",
        "original_source": "示例来源",
        "source_url": "https://example.invalid/a",
        "content_hash": hashlib.sha256(event_id.encode()).hexdigest(),
        "quality_status": "stable",
        "source_time_quality": "exact",
        "dedup_key": f"key-{event_id}",
        "record_version": 1,
        "revision_id": "r1",
        "revision_time": ts(f"{day} 10:30:00"),
        "is_corrected": False,
        "correction_count": 0,
        "person": None,
    }
    values.update(over)
    return values


def _write_table(directory: Path, name: str, records: list[dict], schema: pa.Schema) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    pq.write_table(pa.Table.from_pylist(records, schema=schema), target)
    return target


def write_bars(root: Path, records: list[dict], name: str = SHARD) -> Path:
    return _write_table(root / "bars", name, records, BARS_COLUMNS)


def write_event_day(root: Path, day: date, records: list[dict]) -> Path:
    """写一天的语料分区，**连溯源旁注一起**（体检把缺旁注当 error）。"""
    path = _write_table(root / "events", f"cn-events_{day}.parquet", records, EVENTS_COLUMNS)
    sidecar = root / "raw" / "events"
    sidecar.mkdir(parents=True, exist_ok=True)
    (sidecar / f"{day}.json").write_text(
        json.dumps(
            {
                "date": str(day),
                "file": path.name,
                "rows": len(records),
                "symbols": len({s for r in records for s in r["symbols"]}),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "materialized_at": ts(f"{day} 21:10:00").isoformat(),
                "shards": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_manifests(root: Path) -> None:
    """按磁盘现状重建两份清单——`audit_calendar` 与体检都拿它当「声明」。"""
    meta = root / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    outputs = []
    for path in sorted((root / "bars").glob("*.parquet")):
        rows = pq.read_table(path).num_rows
        outputs.append({"path": f"data/bars/{path.name}", "rows": rows,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (meta / "bars.json").write_text(json.dumps({"outputs": outputs}), encoding="utf-8")

    days = []
    for path in sorted((root / "events").glob("cn-events_*.parquet")):
        day = path.name[len("cn-events_") : -len(".parquet")]
        sidecar = json.loads((root / "raw" / "events" / f"{day}.json").read_text(encoding="utf-8"))
        days.append({"date": day, "file": path.name, "rows": sidecar["rows"],
                     "symbols": sidecar["symbols"], "sha256": sidecar["sha256"]})
    (meta / "events.json").write_text(json.dumps({"days": days}), encoding="utf-8")


def refresh_manifest(root: Path) -> None:
    """落盘变了之后重建清单——用来把「清单与磁盘不符」这条排除掉，聚焦被测检查。"""
    _write_manifests(root)


@pytest.fixture
def health_dir(tmp_path: Path) -> Path:
    """干净数据集：3 只标的 × 5 个**真实交易日** + 1 天语料 + 台账。"""
    days = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[:5]
    write_bars(tmp_path, [bar(symbol, day) for day in days for symbol in ("600519", "000001", "300750")])
    write_event_day(tmp_path, days[0], [event("news:1", days[0])])
    meta = tmp_path / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "etl_runs.jsonl").write_text(
        json.dumps({"scope": "manual", "days": [], "result": "ok"}) + "\n", encoding="utf-8"
    )
    _write_manifests(tmp_path)
    return tmp_path


def run_one(root: Path, check_id: str) -> dh.Check:
    """只跑一项检查——避免注入的脏数据在别的检查上顺带触发 error，混淆断言。"""
    con = dc.connect(root)
    try:
        (check,) = dh.run_all(data_dir=root, only={check_id})
        return check
    finally:
        con.close()


# ── 干净基线 ────────────────────────────────────────────────


def test_clean_dataset_raises_no_error(health_dir: Path) -> None:
    checks = dh.run_all(data_dir=health_dir)
    errors = [(c.id, c.title) for c in checks if c.level == "error"]
    assert errors == []
    assert dh.exit_code(checks) == 0
    assert len(checks) == len(dh.CHECKS)  # 一项都没漏跑


# ── 行情 ────────────────────────────────────────────────────


def test_b1_flags_undeclared_shard(health_dir: Path) -> None:
    write_bars(health_dir, [bar("600519", date(2026, 10, 9))], name="cn-daily_CN_qfq_2025.parquet")
    check = run_one(health_dir, "B1")
    assert check.level == "error"
    assert "cn-daily_CN_qfq_2025.parquet" in check.data["extra"]


def test_b1_flags_rows_off_manifest(health_dir: Path) -> None:
    manifest = json.loads((health_dir / "_meta" / "bars.json").read_text(encoding="utf-8"))
    manifest["outputs"][0]["rows"] += 1
    (health_dir / "_meta" / "bars.json").write_text(json.dumps(manifest), encoding="utf-8")
    check = run_one(health_dir, "B1")
    assert check.level == "error"
    assert check.data["mismatched"] == [SHARD]


def test_b2_flags_duplicate_bar(health_dir: Path) -> None:
    days = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[:5]
    write_bars(health_dir, [bar(symbol, day) for day in days
                            for symbol in ("600519", "000001", "300750", "600519")])
    check = run_one(health_dir, "B2")
    assert check.level == "error"
    assert check.data["duplicates"]


def test_b3_flags_priced_trading_day_without_price(health_dir: Path) -> None:
    """有成交却无价——「无价即无价不是零价」的反面，必须报警。"""
    days = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[:5]
    records = [bar(symbol, day) for day in days for symbol in ("600519", "000001", "300750")]
    records = [r for r in records if r["trade_date"] != days[0]]
    write_bars(health_dir, [*records, bar("600519", days[0], close=None)])
    check = run_one(health_dir, "B3")
    assert check.level == "error"
    assert check.data["stray"] == 1


def test_b4_counts_no_price_rows(health_dir: Path) -> None:
    write_bars(
        health_dir,
        [bar("600519", date(2026, 9, 1), close=None, open=None, high=None, low=None,
             trading_status="no_turnover_observed")],
    )
    check = run_one(health_dir, "B4")
    assert check.level == "info"
    assert check.data["by_adjust"] == {"qfq": 1}


def test_b6_flags_unknown_trading_status(health_dir: Path) -> None:
    write_bars(health_dir, [bar("600519", date(2026, 9, 1), trading_status="unknown")])
    check = run_one(health_dir, "B6")
    assert check.level == "warn"
    assert check.data["unknown"][0]["trading_status"] == "unknown"


def test_b7_flags_envelope_drop(health_dir: Path) -> None:
    """某日标的数腰斩 = 分片截断——这是 B7 真正要抓的形态。"""
    days = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[:5]
    records = [bar(symbol, day) for day in days for symbol in ("600519", "000001", "300750")]
    records = [r for r in records if not (r["trade_date"] == days[3] and r["symbol"] != "600519")]
    write_bars(health_dir, records)
    check = run_one(health_dir, "B7")
    assert check.level == "error"
    assert check.data["drops"] == 1


def test_b8_flags_day_outside_calendar(health_dir: Path) -> None:
    days = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[:5]
    weekend = next(d for d in (days[0] + timedelta(n) for n in range(1, 8)) if not cal.is_session(d))
    write_bars(health_dir, [bar("600519", weekend)])
    check = run_one(health_dir, "B8")
    assert check.level == "error"
    assert str(weekend) in check.data["only_in_data"]


# ── 事件 ────────────────────────────────────────────────────


def test_e1_flags_missing_sidecar(health_dir: Path) -> None:
    day = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[0]
    (health_dir / "raw" / "events" / f"{day}.json").unlink()
    check = run_one(health_dir, "E1")
    assert check.level == "error"
    assert check.data["missing_sidecar"] == [f"cn-events_{day}.parquet"]


def test_e1_flags_corrupted_bytes(health_dir: Path) -> None:
    """旁注里的 sha 是**记录**不是测量——只改 Parquet、不动旁注，就要报出来。"""
    day = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[0]
    target = _write_table(
        health_dir / "events",
        f"cn-events_{day}.parquet",
        [event("news:1", day), event("news:2", day)],
        EVENTS_COLUMNS,
    )
    check = run_one(health_dir, "E1")
    assert check.level == "error"
    assert check.data["corrupt"] == [target.name]


def test_e2_flags_duplicate_event(health_dir: Path) -> None:
    day = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[0]
    write_event_day(health_dir, day, [event("news:1", day), event("news:1", day)])
    check = run_one(health_dir, "E2")
    assert check.level == "error"
    assert check.data["pair_groups"] == 1


def test_e3_flags_inverted_available_at(health_dir: Path) -> None:
    day = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[0]
    write_event_day(
        health_dir, day, [event("news:1", day, available_at=ts(f"{day} 09:00:00"))]
    )
    check = run_one(health_dir, "E3")
    assert check.level == "error"
    assert check.data["inverted"] == 1


def test_e4_flags_bad_norm_and_dirty_direction(health_dir: Path) -> None:
    day = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[0]
    write_event_day(
        health_dir,
        day,
        [
            event("news:1", day, direction_norm="sideways"),
            event("announcement:2", day, direction="融资 from theellsellsellsellsell"),
        ],
    )
    check = run_one(health_dir, "E4")
    assert check.level == "error"  # 自产列的取值不变量被破坏，比脏串更严重
    assert check.data["bad_norm"][0]["direction_norm"] == "sideways"
    assert any("theells" in item for item in check.data["dirty"])


def test_e5_flags_malformed_symbol(health_dir: Path) -> None:
    day = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[0]
    write_event_day(health_dir, day, [event("news:1", day, symbols=["600519.SH"])])
    check = run_one(health_dir, "E5")
    assert check.level == "warn"
    assert check.data["malformed"] == ["600519.SH"]


def test_e6_reports_reuse_without_error(health_dir: Path) -> None:
    """`event_id` 跨日复用是平台行为（按月复发），**只能报不能判负**。"""
    days = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[:2]
    write_event_day(health_dir, days[0], [event("news:9", days[0])])
    write_event_day(health_dir, days[1], [event("news:9", days[1])])
    check = run_one(health_dir, "E6")
    assert check.level == "info"
    assert check.data["reused"] == ["news:9"]


def test_e7_flags_trading_day_gap(health_dir: Path) -> None:
    days = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[:3]
    write_event_day(health_dir, days[0], [event("news:1", days[0])])
    write_event_day(health_dir, days[2], [event("news:3", days[2])])
    check = run_one(health_dir, "E7")
    assert check.level == "error"  # 中间缺的那天是交易日
    assert check.data["trading_day_gaps"] == [str(days[1])]


def test_e7_ignores_ledger_empty_days(health_dir: Path) -> None:
    """台账记为 `empty` 的日子不算缺口——「归档当天真没内容」与「我们漏拉了」是两件事。"""
    days = cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[:3]
    write_event_day(health_dir, days[0], [event("news:1", days[0])])
    write_event_day(health_dir, days[2], [event("news:3", days[2])])
    (health_dir / "_meta" / "etl_runs.jsonl").write_text(
        json.dumps({"scope": "backfill", "days": [{"date": str(days[1]), "status": "empty"}]}) + "\n",
        encoding="utf-8",
    )
    check = run_one(health_dir, "E7")
    assert check.level == "info"
    assert check.data["empty_days"] == [str(days[1])]


# ── 日历、遗留物与短路 ───────────────────────────────────────


def test_x1_warns_on_calendar_runway() -> None:
    check = dh.check_x1_calendar_runway(None, None)  # 不依赖数据目录
    first, last = cal.coverage()
    runway = (last - date.today()).days
    assert check.data["runway_days"] == runway
    assert check.level == ("warn" if runway < dh.CALENDAR_RUNWAY_WARN_DAYS else "info")


def test_x2_flags_legacy_files(health_dir: Path) -> None:
    (health_dir / "raw" / "events" / "600519.jsonl").write_text("", encoding="utf-8")
    check = run_one(health_dir, "X2")
    assert check.level == "warn"
    assert check.data["legacy"] == ["600519.jsonl"]


def test_x3_flags_broken_ledger_without_breaking_e7(health_dir: Path) -> None:
    """台账半截写入只该由 X3 报出——缺口判定（E7）不能跟着一起废掉。"""
    with (health_dir / "_meta" / "etl_runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{不是 JSON\n")
    check = run_one(health_dir, "X3")
    assert check.level == "error"
    assert check.data["bad_lines"]

    gap = run_one(health_dir, "E7")
    assert gap.title == "语料窗口内无缺口"


def test_missing_data_short_circuits_without_traceback(tmp_path: Path) -> None:
    checks = dh.run_all(data_dir=tmp_path)
    assert [c.id for c in checks] == ["DATA"]
    assert checks[0].level == "error"
    assert dh.exit_code(checks) == 1


def test_run_all_wraps_check_exception(health_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """一条 SQL 炸掉不该吞掉整份报告——异常要折成该项的 error。

    替换 `CHECKS` 而不是模块属性：`run_all` 遍历的是那份元组，函数对象在导入时就被捕获了。
    """

    def check_b2_duplicates(_con, _data_dir):  # noqa: ANN001 - 签名对齐被替换项
        raise RuntimeError("boom")

    monkeypatch.setattr(dh, "CHECKS", (check_b2_duplicates,))
    checks = dh.run_all(data_dir=health_dir, only={"B2"})
    assert checks[0].id == "B2"
    assert checks[0].level == "error"
    assert "boom" in checks[0].detail


# ── 脚本级：退出码与机读副本 ─────────────────────────────────


def test_script_exits_nonzero_and_writes_json(health_dir: Path, tmp_path: Path) -> None:
    """「注入脏数据时体检脚本报警」验的是**这条链**：报告 + 退出码，不只是函数。"""
    from scripts.run_health_check import main

    write_bars(health_dir, [bar("600519", date(2026, 9, 1), close=None)])
    refresh_manifest(health_dir)  # 让唯一的问题是 B3，而不是顺带的清单不符
    out = tmp_path / "health.json"
    code = main(["--data-dir", str(health_dir), "--json", str(out)])

    assert code == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    hits = [c for c in payload["checks"] if c["id"] == "B3"]
    assert hits and hits[0]["level"] == "error"
    assert payload["exit_code"] == 1
    assert payload["delisting"] is not None


def test_script_strict_turns_warn_into_failure(health_dir: Path) -> None:
    from scripts.run_health_check import main

    write_bars(health_dir, [bar("600519", date(2026, 9, 1), trading_status="unknown")])
    refresh_manifest(health_dir)
    assert main(["--data-dir", str(health_dir)]) == 0  # 只有 warn
    assert main(["--data-dir", str(health_dir), "--strict"]) == 1


# ── X4 / X5 / X6：四通道的新鲜度（M2c 补口，2026-10-09）────────
#
# 这三项的共同点：它们**不是数据正确性问题，是「落后了没人知道」**。收口当天两次缺口
# 都是手工撞见的，所以判据的重点不在阈值，而在**每个「查不到」都要与「没问题」分开**：
# 依赖不可达一律降级 info，绝不静默成「一致」。


def test_x4_flags_days_missing_from_the_index(health_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """索引少了一天 → warn。**这是本项存在的唯一理由**：补嵌是后台动作，它没跟上必须有人喊。"""
    from app.rag import collection as rag

    monkeypatch.setattr(rag, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(rag, "count_by_day", lambda *a, **k: {})  # 一天都没嵌
    check = run_one(health_dir, "X4")

    assert check.level == "warn"
    assert check.data["missing"] == ["2026-09-01"]
    assert "2026-09-01" in check.detail


def test_x4_is_quiet_when_the_index_matches(health_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.rag import collection as rag

    monkeypatch.setattr(rag, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(rag, "count_by_day", lambda *a, **k: {"2026-09-01": 1})
    check = run_one(health_dir, "X4")

    assert check.level == "info"
    assert check.data["missing"] == []


def test_x4_dependency_down_is_info_not_error(health_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Qdrant 不可达 → **info**。索引是语料的派生物，它没跟上不该把整份体检判失败；
    但文案必须挡住「查不到 = 没落后」这个误读——那正是本项要防的事。"""
    from app.rag import collection as rag

    def boom(*_a, **_k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(rag, "get_client", boom)
    check = run_one(health_dir, "X4")

    assert check.level == "info"
    assert "Qdrant 不可达" in check.title
    assert "别把「查不到」读成「没落后」" in check.detail


def test_x5_flags_bars_behind_the_calendar(health_dir: Path) -> None:
    """夹具的行情停在 2026-09-05，今天远在其后 → 落后远超阈值。"""
    check = run_one(health_dir, "X5")

    assert check.level == "warn"
    assert check.data["latest_bar"] == str(cal.sessions(date(2026, 9, 1), date(2026, 10, 31))[:5][-1])
    assert check.data["behind_trading_days"] > dh.BARS_STALE_TRADING_DAYS
    # 「不是日更」这句必须在文案里，否则读者会以为坏了
    assert "不是日更" in check.detail


def test_x5_is_quiet_when_bars_reach_the_last_session(tmp_path: Path) -> None:
    """阈值按**交易日**给：行情写到「今天及以前最后一个交易日」就是干净的。"""
    last = cal.last_session_on_or_before(date.today())
    write_bars(tmp_path, [bar("600519", last)])
    write_event_day(tmp_path, last, [event("news:1", last)])
    (tmp_path / "_meta").mkdir(parents=True, exist_ok=True)
    _write_manifests(tmp_path)

    check = run_one(tmp_path, "X5")
    assert check.level == "info"
    assert check.data["behind_trading_days"] == 0


def test_x6_flags_a_stalled_archive(tmp_path: Path) -> None:
    """归档上界停在很久以前 → warn。抓的是「源侧不发了」，与 E7 的「源侧有、本地无」互补。"""
    stalled = date.today() - timedelta(days=dh.ARCHIVE_STALL_DAYS + 2)
    (tmp_path / "_meta").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_meta" / "etl_runs.jsonl").write_text(
        json.dumps({"scope": "daily", "archive_last": str(stalled), "started_at": "x"}) + "\n",
        encoding="utf-8",
    )
    last = cal.last_session_on_or_before(date.today())
    write_bars(tmp_path, [bar("600519", last)])
    write_event_day(tmp_path, last, [event("news:1", last)])
    _write_manifests(tmp_path)

    check = run_one(tmp_path, "X6")
    assert check.level == "warn"
    assert check.data["archive_last"] == str(stalled)
    # 停更期间 gap 一直是 0，这条提示是给人看的出路
    assert "--backfill" in check.detail


def test_x6_missing_key_is_unknown_not_stalled(tmp_path: Path) -> None:
    """台账里没有 `archive_last`（老 schema）→ **无从判断**，不能当成停更。"""
    (tmp_path / "_meta").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_meta" / "etl_runs.jsonl").write_text(
        json.dumps({"scope": "manual", "days": [], "result": "ok"}) + "\n", encoding="utf-8"
    )
    last = cal.last_session_on_or_before(date.today())
    write_bars(tmp_path, [bar("600519", last)])
    write_event_day(tmp_path, last, [event("news:1", last)])
    _write_manifests(tmp_path)

    check = run_one(tmp_path, "X6")
    assert check.level == "info"
    assert "无从判断" in check.detail
