"""M7a 数据快照与指纹（`app.report.snapshot`）。

两条口径在这里钉死：
① **digest 从实际分片重算**，不采信 `_meta/*.json` 清单（清单是记录不是测量——体检 B1 同规）；
② `report_hash` 是**冻结产物正文**的规范化 JSON sha256：同 snapshot 重放 ⇒ 同一个值。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.report.snapshot import (
    canonical_json,
    data_snapshot,
    decisions_digest,
    report_hash,
    snapshot_hash,
)
from tests.conftest import make_backtest_dir, ts, write_bars_parquet
from tests.test_memory_settle import buy, sell

BARS = [
    {"trade_date": date(2026, 9, 28), "open": 10.0, "close": 10.5},
    {"trade_date": date(2026, 9, 29), "open": 10.5, "close": 11.0},
]
EVENTS = [
    {
        "event_id": "news:1",
        "event_time": ts("2026-09-28 15:48:00"),
        "title": "标题",
        "content_hash": "hash-a",
    }
]


def _dir(tmp_path: Path) -> Path:
    return make_backtest_dir(tmp_path, bars=BARS, events=EVENTS)


def test_digest_is_recomputed_from_shards_not_from_the_manifest(tmp_path: Path) -> None:
    """清单里写一个**错的** sha，快照照样报出真实值——不采信清单。"""
    root = _dir(tmp_path)
    meta = root / "_meta"
    meta.mkdir()
    (meta / "bars.json").write_text(
        json.dumps({"outputs": [{"object_key": "whatever", "sha256": "0" * 64}]}),
        encoding="utf-8",
    )

    snapshot = data_snapshot(root)

    shards = snapshot["bars"]["shards"]
    assert [s["name"] for s in shards] == ["600519.qfq.parquet"]
    assert shards[0]["sha256"] != "0" * 64
    assert len(shards[0]["sha256"]) == 64
    assert snapshot["events"]["days"] == 1
    assert snapshot["events"]["digest"] != "0" * 64


def test_snapshot_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    """同一目录两次调用逐字段相等；改一个收盘价，digest 必须变。"""
    root = _dir(tmp_path)
    first = data_snapshot(root)
    assert snapshot_hash(first) == snapshot_hash(data_snapshot(root))

    write_bars_parquet(
        root / "bars",
        "600519",
        [{**BARS[0], "close": 99.0}, BARS[1]],
    )

    assert snapshot_hash(data_snapshot(root)) != snapshot_hash(first)


def test_snapshot_reports_corpus_and_market_end(tmp_path: Path) -> None:
    """语料覆盖与行情末端都从数据里查出来，不写死。"""
    snapshot = data_snapshot(_dir(tmp_path))

    assert snapshot["corpus"] == {"start": "2026-09-28", "end": "2026-09-28"}
    assert snapshot["events"]["rows"] == 1
    assert snapshot["events"]["last_day"] == "2026-09-28"
    assert snapshot["market_end"] == "2026-09-29"


def test_snapshot_hash_detects_any_field_change(tmp_path: Path) -> None:
    """任一字段变化都要反映到 hash 上（snapshot_hash 是「这批输入」的身份）。"""
    snapshot = data_snapshot(_dir(tmp_path))
    tweaked = {**snapshot, "market_end": "2026-10-08"}

    assert snapshot_hash(snapshot) != snapshot_hash(tweaked)


def test_canonical_json_ignores_key_order_and_serializes_dates() -> None:
    """规范化：键序无关（字典序）、日期/Decimal 可序列化——两次生成才能得同一个 hash。"""
    left = {"b": 1, "a": [date(2026, 9, 28), {"y": 2, "x": 3}]}
    right = {"a": [date(2026, 9, 28), {"x": 3, "y": 2}], "b": 1}

    assert canonical_json(left) == canonical_json(right)
    assert "2026-09-28" in canonical_json(left)


def test_report_hash_covers_the_frozen_body() -> None:
    """正文改一个字 hash 就变；与快照 hash 相互独立。"""
    body = {"blocks": [{"id": "overview", "text": "本策略 9 月收益 2.1%"}], "metrics": {"x": 1}}
    same = {"metrics": {"x": 1}, "blocks": [{"text": "本策略 9 月收益 2.1%", "id": "overview"}]}
    changed = {"blocks": [{"id": "overview", "text": "本策略 9 月收益 2.2%"}], "metrics": {"x": 1}}

    assert report_hash(body) == report_hash(same)
    assert report_hash(body) != report_hash(changed)


def test_decisions_digest_is_order_insensitive_but_content_sensitive() -> None:
    """决策日志指纹：顺序无关（库里读回来的顺序不该影响身份），内容变了就要变。"""
    one, two = buy(date(2026, 8, 3), did="d1"), sell(date(2026, 8, 10), did="d2")

    assert decisions_digest([one, two]) == decisions_digest([two, one])
    assert decisions_digest([one, two]) != decisions_digest([one])


def test_canonical_json_tolerates_psycopg_types() -> None:
    """真库读回来的是 `uuid.UUID` / `Decimal` / `date` 对象——指纹必须在这些类型下算得出来。

    这条是集成用例逮出来的：内存替身存字符串，真库给 UUID，规范化器先炸在 UUID 上。
    """
    import uuid as uuid_module
    from decimal import Decimal

    payload = {
        "account_id": uuid_module.UUID("691ad1b2-0abf-44d1-819c-25994fd9d150"),
        "cash": Decimal("100000.0000"),
        "day": date(2026, 9, 30),
    }

    text = canonical_json(payload)

    assert "691ad1b2-0abf-44d1-819c-25994fd9d150" in text
    assert "100000.0" in text and "2026-09-30" in text
