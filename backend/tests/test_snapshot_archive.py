"""E3b 快照归档的离线用例（SPEC §4.4）。

**全用假数据目录**（同 `test_snapshot_window.py` 的既定口径，不碰真实 `data/`）：
真实目录是 2.6GB 且不进 Git，用例要能在没有它的机器上跑。

一条纪律钉在这里：**快照只是清单的副本，不是数据的副本**——字节早在 `raw/o` 的
内容寻址对象库里，快照存的是「指针」。所以本模块的用例几乎都在验「清单被正确抄下来」
与「保留份数的滚动」，不验任何跟数据内容有关的事。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.core.config import get_settings
from app.data import snapshots


def bars_manifest(shards: int) -> dict:
    """行情清单的最小形状——只放本模块会读到的那几个键。"""
    return {
        "schema": "quantsage.bars_meta/v2",
        "fingerprint": {
            "shards": [{"object_key": f"…/year={2000 + i}/data.parquet", "sha256": "a" * 64} for i in range(shards)],
            "receipt_manifest_version": "history-public-v1-20261012T200508Z",
        },
        "outputs": [{"rows": 100} for _ in range(shards)],
    }


def events_manifest(days: int) -> dict:
    return {
        "schema": "quantsage.events_meta/v2",
        "days": [{"date": f"2026-09-{i + 1:02d}", "sha256": "b" * 64} for i in range(days)],
        "fingerprint": {"digest": "c" * 64, "days": days, "rows": days * 1000},
    }


def write_meta(root: Path, *, bars: dict | None = None, events: dict | None = None) -> None:
    meta = root / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    if bars is not None:
        (meta / "bars.json").write_text(json.dumps(bars), encoding="utf-8")
    if events is not None:
        (meta / "events.json").write_text(json.dumps(events), encoding="utf-8")


def test_take_copies_manifests_and_appends_an_index_row(tmp_path: Path) -> None:
    write_meta(tmp_path, bars=bars_manifest(2), events=events_manifest(3))

    row = snapshots.take(tmp_path, scope="daily", reason="换入 2 片", at=datetime(2026, 10, 16, 22, 15, 30))

    assert row["ts"] == "20261016T221530Z"
    assert row["scope"] == "daily"
    assert row["bars"]["shards"] == 2
    assert row["events"]["days"] == 3
    # 清单是**抄下来的**，不是引用的：原文件改掉之后快照里那份必须还是旧的
    snap = snapshots.dir_for(tmp_path, "20261016T221530Z")
    assert json.loads((snap / "bars.json").read_text())["fingerprint"]["shards"][0]["sha256"] == "a" * 64
    assert snapshots.read_index(tmp_path)[0]["ts"] == "20261016T221530Z"


def test_take_twice_in_the_same_second_does_not_collide(tmp_path: Path) -> None:
    """同一秒内连着打两份（refresh 之后紧跟一次 rebuild 是真实路径）不能互相覆盖。"""
    write_meta(tmp_path, bars=bars_manifest(1), events=events_manifest(1))
    at = datetime(2026, 10, 16, 22, 15, 30)

    first = snapshots.take(tmp_path, scope="a", reason="一", at=at)
    second = snapshots.take(tmp_path, scope="b", reason="二", at=at)

    assert first["ts"] != second["ts"]
    assert len(snapshots.read_index(tmp_path)) == 2
    assert snapshots.dir_for(tmp_path, first["ts"]).is_dir()
    assert snapshots.dir_for(tmp_path, second["ts"]).is_dir()


def test_take_tolerates_a_missing_channel(tmp_path: Path) -> None:
    """只落了行情、语料还没跑过一次——快照照打，缺的那一路记 `null` 而不是编一个空壳。"""
    write_meta(tmp_path, bars=bars_manifest(1))

    row = snapshots.take(tmp_path, scope="bars-refresh", reason="只动行情")

    assert row["bars"]["shards"] == 1
    assert row["events"] is None
    assert not (snapshots.dir_for(tmp_path, row["ts"]) / "events.json").exists()


def test_take_with_no_manifest_at_all_is_a_noop(tmp_path: Path) -> None:
    """两份清单都不在（新数据目录）⇒ 不落空快照——空快照回滚起来是灾难。"""
    assert snapshots.take(tmp_path, scope="daily", reason="空目录") is None
    assert snapshots.read_index(tmp_path) == []


def test_prune_keeps_the_newest(tmp_path: Path) -> None:
    write_meta(tmp_path, bars=bars_manifest(1), events=events_manifest(1))
    for minute in range(5):
        snapshots.take(tmp_path, scope="daily", reason=f"第 {minute} 轮", at=datetime(2026, 10, 16, 22, minute))

    dropped = snapshots.prune(tmp_path, keep=2)

    assert len(dropped) == 3
    assert [row["reason"] for row in snapshots.read_index(tmp_path)] == ["第 4 轮", "第 3 轮"]
    assert not snapshots.dir_for(tmp_path, dropped[0]).exists()  # 目录也删了，不只是索引


def test_take_prunes_automatically(tmp_path: Path) -> None:
    """保留份数是**打快照时顺手做的**，不靠人记得跑 prune（那样迟早涨到几十 GB）。"""
    write_meta(tmp_path, bars=bars_manifest(1), events=events_manifest(1))
    for minute in range(get_settings().snapshot_keep + 3):
        snapshots.take(tmp_path, scope="daily", reason=f"第 {minute} 轮", at=datetime(2026, 10, 16, 22, minute))

    assert len(snapshots.read_index(tmp_path)) == get_settings().snapshot_keep


def test_index_survives_a_corrupt_line(tmp_path: Path) -> None:
    write_meta(tmp_path, bars=bars_manifest(1))
    snapshots.take(tmp_path, scope="daily", reason="一")
    with snapshots.index_path(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write("{半截\n")

    assert len(snapshots.read_index(tmp_path)) == 1


def test_missing_index_is_empty_not_an_error(tmp_path: Path) -> None:
    assert snapshots.read_index(tmp_path) == []


def test_write_is_atomic(tmp_path: Path) -> None:
    """先写 `.partial` 目录再换入——与分片物化同规，读的人永远看不到半截快照。"""
    write_meta(tmp_path, bars=bars_manifest(1), events=events_manifest(1))
    snapshots.take(tmp_path, scope="daily", reason="一")

    leftovers = [p.name for p in snapshots.root(tmp_path).iterdir() if ".partial" in p.name]
    assert leftovers == []
