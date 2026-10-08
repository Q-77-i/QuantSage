"""批处理用例：增量判据、长度分桶、payload 形态、断点续跑与对账。

全部用内存替身 + 真实 Parquet（不 mock 文件层，沿用 P1 口径），**不下载任何模型**。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.backtest.types import CN_TZ
from app.rag import collection as col
from app.rag import embed
from tests.fakes_rag import DIM, FakeEmbedder, FakeQdrant

DAY = "2026-08-17"

EVENT_SCHEMA = pa.schema(
    [
        ("event_id", pa.string()),
        ("event_type", pa.string()),
        ("title", pa.string()),
        ("summary", pa.string()),
        ("event_time", pa.timestamp("us", tz="Asia/Shanghai")),
        ("available_at", pa.timestamp("us", tz="Asia/Shanghai")),
        ("direction_norm", pa.string()),
        ("importance_score", pa.float64()),
        ("symbols", pa.list_(pa.string())),
        ("industries", pa.list_(pa.string())),
        ("source", pa.string()),
        ("original_source", pa.string()),
        ("source_url", pa.string()),
        ("content_hash", pa.string()),
    ]
)


def write_day(base: Path, day: str, rows: list[dict]) -> str:
    """写一天的分区文件，并把清单（`_meta/events.json`）更新成与磁盘一致。"""
    import hashlib

    events = base / "events"
    meta = base / "_meta"
    events.mkdir(parents=True, exist_ok=True)
    meta.mkdir(parents=True, exist_ok=True)
    file = events / f"cn-events_{day}.parquet"
    table = pa.Table.from_pylist(
        [
            {
                "event_id": row.get("event_id", "news:1"),
                "event_type": row.get("event_type", "news"),
                "title": row.get("title", "标题"),
                "summary": row.get("summary"),
                "event_time": row.get("event_time", datetime(2026, 8, 17, 9, 0, tzinfo=CN_TZ)),
                "available_at": row.get("available_at", datetime(2026, 8, 17, 9, 5, tzinfo=CN_TZ)),
                "direction_norm": row.get("direction_norm"),
                "importance_score": row.get("importance_score", 50.0),
                "symbols": row.get("symbols", ["600519"]),
                "industries": row.get("industries", ["银行"]),
                "source": "xiaoshi-archive",
                "original_source": row.get("original_source", "证券时报"),
                "source_url": row.get("source_url", "https://example.com/x"),
                "content_hash": row.get("content_hash", "hash-1"),
            }
            for row in rows
        ],
        schema=EVENT_SCHEMA,
    )
    pq.write_table(table, file)
    sha = hashlib.sha256(file.read_bytes()).hexdigest()

    manifest_path = meta / "events.json"
    payload = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {"days": []}
    )
    payload["days"] = [d for d in payload.get("days", []) if d["date"] != day]
    payload["days"].append(
        {"date": day, "file": file.name, "rows": len(rows), "sha256": sha, "symbols": 1,
         "materialized_at": "", "shards": []}
    )
    payload["days"].sort(key=lambda d: d["date"])
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return sha


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    write_day(tmp_path, DAY, [{"event_id": "news:1", "title": "降准"}, {"event_id": "news:2"}])
    return tmp_path


# ── 增量判据 ──────────────────────────────────────────────────────────────


def test_plan_embeds_new_day(data_dir: Path) -> None:
    tasks = embed.plan(embed.load_manifest(data_dir), {"days": {}}, data_dir=data_dir)
    assert [(t.day, t.action) for t in tasks] == [(DAY, "embed")]


def test_plan_skips_when_sha_unchanged(data_dir: Path) -> None:
    manifest = embed.load_manifest(data_dir)
    state = {"days": {DAY: {"sha256": manifest[DAY]["sha256"]}}}
    tasks = embed.plan(manifest, state, data_dir=data_dir)
    assert [(t.day, t.action) for t in tasks] == [(DAY, "skip")]


def test_plan_reembeds_when_content_changed(data_dir: Path) -> None:
    """平台会改既有事件的内容——判据必须是内容指纹，不是时间戳。"""
    state = {"days": {DAY: {"sha256": "旧指纹"}}}
    tasks = embed.plan(embed.load_manifest(data_dir), state, data_dir=data_dir)
    assert [(t.day, t.action) for t in tasks] == [(DAY, "reembed")]
    assert "sha" in tasks[0].reason


def test_plan_force_reembeds_even_if_unchanged(data_dir: Path) -> None:
    manifest = embed.load_manifest(data_dir)
    state = {"days": {DAY: {"sha256": manifest[DAY]["sha256"]}}}
    tasks = embed.plan(manifest, state, data_dir=data_dir, force=True)
    assert tasks[0].action == "reembed"


def test_plan_skips_day_without_sidecar(data_dir: Path) -> None:
    """清单里 rows=-1 表示缺溯源旁注——不嵌，交给数据体检报错。"""
    manifest = embed.load_manifest(data_dir)
    manifest[DAY]["rows"] = -1
    tasks = embed.plan(manifest, {"days": {}}, data_dir=data_dir)
    assert tasks[0].action == "skip"


def test_plan_only_limits_days(data_dir: Path) -> None:
    write_day(data_dir, "2026-08-18", [{"event_id": "news:3"}])
    tasks = embed.plan(embed.load_manifest(data_dir), {"days": {}}, data_dir=data_dir, only=[DAY])
    assert [t.day for t in tasks] == [DAY]


# ── 分桶与 payload ────────────────────────────────────────────────────────


def test_length_order_covers_every_index_exactly_once() -> None:
    texts = ["x" * n for n in (5, 1, 9, 3, 7, 2, 8)]
    order = embed._length_order(texts)
    assert sorted(order) == list(range(len(texts)))
    assert [len(texts[i]) for i in order] == sorted(len(t) for t in texts)


def test_length_order_keeps_adjacent_items_similar_in_length() -> None:
    """排序后相邻项长度接近 ⇒ 编码器内部按 batch 切分时 padding 开销接近零。"""
    texts = ["x" * n for n in (1, 2, 3, 1000, 1001, 1002)]
    order = embed._length_order(texts)
    lengths = [len(texts[i]) for i in order]
    assert lengths == [1, 2, 3, 1000, 1001, 1002]


def test_payload_serialises_times_and_lists(data_dir: Path) -> None:
    rows = embed._read_rows(DAY, embed._PAYLOAD_COLUMNS, data_dir)
    payload = embed._payload_of(rows[0], DAY)
    assert payload["day"] == DAY
    assert payload["available_at"].startswith("2026-08-17T09:05:00")  # RFC3339 字符串
    assert payload["symbols"] == ["600519"]
    assert payload["text"].startswith("【标题】")


# ── 嵌入与对账 ────────────────────────────────────────────────────────────


def test_embed_day_writes_points_and_clears_day_first(data_dir: Path) -> None:
    client = FakeQdrant()
    embedder = FakeEmbedder(DIM)
    count = embed.embed_day(DAY, client=client, embedder=embedder, data_dir=data_dir)
    assert count == 2
    assert client.delete_calls == 1  # 先删后写：不留平台上已下线的事件
    assert {p.payload["event_id"] for p in client.points.values()} == {"news:1", "news:2"}


def test_sync_second_run_is_noop(data_dir: Path) -> None:
    client = FakeQdrant()
    embedder = FakeEmbedder(DIM)
    first = embed.sync(data_dir=data_dir, client=client, embedder=embedder)
    assert [t.day for t in first.embedded] == [DAY]
    assert first.points == 2

    second = embed.sync(data_dir=data_dir, client=client, embedder=embedder)
    assert second.embedded == ()
    assert client.upsert_calls == 1  # 没有重复写入
    assert embed.status(data_dir=data_dir, client=client).clean


def test_sync_reembeds_after_day_rewritten(data_dir: Path) -> None:
    client = FakeQdrant()
    embedder = FakeEmbedder(DIM)
    embed.sync(data_dir=data_dir, client=client, embedder=embedder)
    write_day(data_dir, DAY, [{"event_id": "news:1", "title": "降准（修订版）"}])
    report = embed.sync(data_dir=data_dir, client=client, embedder=embedder)
    assert [(t.day, t.action) for t in report.embedded] == [(DAY, "reembed")]
    assert len(client.points) == 1  # 旧的 news:2 已被删除，没有陈旧点


def test_sync_recreate_rebuilds_from_scratch(data_dir: Path) -> None:
    """`--full` 的路径：清库 + 清状态后全量重嵌（破坏性，靠 CLI 的 --yes 把关）。"""
    client = FakeQdrant()
    embedder = FakeEmbedder(DIM)
    embed.sync(data_dir=data_dir, client=client, embedder=embedder)
    write_day(data_dir, "2026-08-18", [{"event_id": "news:3"}])
    embed.sync(data_dir=data_dir, client=client, embedder=embedder)
    assert len(client.points) == 3

    report = embed.sync(data_dir=data_dir, client=client, embedder=embedder, recreate=True)
    assert [t.day for t in report.embedded] == [DAY, "2026-08-18"]
    assert len(client.points) == 3  # 重建后点数不变，但过程是「全删重写」
    assert embed.load_state(data_dir)["days"][DAY]["action"] == "embed"


def test_sync_writes_state_per_day(data_dir: Path) -> None:
    client = FakeQdrant()
    embed.sync(data_dir=data_dir, client=client, embedder=FakeEmbedder(DIM))
    state = embed.load_state(data_dir)
    assert state["days"][DAY]["points"] == 2
    assert state["days"][DAY]["sha256"] == embed.load_manifest(data_dir)[DAY]["sha256"]


def test_state_schema_mismatch_is_treated_as_empty(data_dir: Path) -> None:
    """schema 升版后旧状态文件必须失效，不能静默沿用。"""
    path = embed.state_path(data_dir)
    path.write_text(json.dumps({"schema": "old/v0", "days": {DAY: {"sha256": "x"}}}), encoding="utf-8")
    assert embed.load_state(data_dir)["days"] == {}


def test_status_reports_both_directions(data_dir: Path) -> None:
    write_day(data_dir, "2026-08-18", [{"event_id": "news:3"}])
    client = FakeQdrant()
    # 只嵌一天：另一天应报「本地有、索引无」；再造一个清单里没有的日子报「陈旧点」
    embed.embed_day(DAY, client=client, embedder=FakeEmbedder(DIM), data_dir=data_dir)
    col.upsert_points(
        client,
        [
            col.build_point(
                {"day": "2026-07-01", "event_id": "news:9", "text": "x"}, [0.1] * DIM, {1: 1.0}
            )
        ],
    )
    report = embed.status(data_dir=data_dir, client=client)
    assert report.missing == {"2026-08-18": 1}
    assert report.extra == {"2026-07-01": 1}
    assert report.ready_days == 1
    assert not report.clean
