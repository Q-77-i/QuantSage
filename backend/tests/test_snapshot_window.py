"""E7d：回测/网格的数据指纹（窗口口径）。

核心断言只有一条：**同一份数据、同一个窗口，必得同一个指纹**——它是「实验可复现」
在数据维度的唯一可验证形态。其余用例都是它的边界：窗口外的东西不许影响指纹、
窗口内的改动必须影响指纹、没有数据的目录要如实降级而不是编一个值。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from app.report.snapshot import (
    _bars_in_window,
    _events_in_window,
    _shard_day,
    window_digest,
    window_snapshot,
)

BARS_SCHEMA = pa.schema([("symbol", pa.string()), ("close", pa.float64())])
EVENTS_SCHEMA = pa.schema([("event_id", pa.string()), ("title", pa.string())])


def make_data_dir(root: Path, *, years: tuple[int, ...], event_days: tuple[str, ...], adjust: str = "qfq") -> Path:
    """铺一份最小数据目录：bars 按年分片 + events 按日分片（形状贴合真实落盘）。"""
    (root / "bars").mkdir(parents=True, exist_ok=True)
    (root / "events").mkdir(parents=True, exist_ok=True)
    for year in years:
        for a in ("qfq", "raw"):
            table = pa.table({"symbol": ["600519"], "close": [float(year)]}, schema=BARS_SCHEMA)
            pq.write_table(table, root / "bars" / f"cn-daily_CN_{a}_{year}.parquet")
    for day in event_days:
        table = pa.table({"event_id": [day], "title": ["t"]}, schema=EVENTS_SCHEMA)
        pq.write_table(table, root / "events" / f"cn-events_{day}.parquet")
    return root


# ── 分片筛选 ────────────────────────────────────────────────────────────────


def test_bars_selection_covers_years_and_adjust(tmp_path: Path) -> None:
    base = make_data_dir(tmp_path, years=(2024, 2025, 2026), event_days=())
    picked = {p.name for p in _bars_in_window(base, date(2025, 6, 1), date(2026, 3, 1), "qfq")}

    assert picked == {"cn-daily_CN_qfq_2025.parquet", "cn-daily_CN_qfq_2026.parquet"}


def test_events_selection_is_closed_interval(tmp_path: Path) -> None:
    base = make_data_dir(
        tmp_path, years=(), event_days=("2026-01-01", "2026-02-01", "2026-03-01")
    )
    picked = {p.name for p in _events_in_window(base, date(2026, 1, 1), date(2026, 2, 1))}

    assert picked == {"cn-events_2026-01-01.parquet", "cn-events_2026-02-01.parquet"}


def test_shard_day_is_none_for_bad_names() -> None:
    assert _shard_day("cn-events_2026-01-01.parquet") == date(2026, 1, 1)
    assert _shard_day("cn-events_副本.parquet") is None


# ── 指纹的核心性质 ──────────────────────────────────────────────────────────


def test_same_data_same_digest(tmp_path: Path) -> None:
    """同数据、同窗口 ⇒ 同指纹（两次调用，逐字相等）。"""
    base = make_data_dir(tmp_path, years=(2025, 2026), event_days=("2026-01-05", "2026-02-05"))
    kwargs = {"start": date(2026, 1, 1), "end": date(2026, 2, 28), "adjust": "qfq"}

    first = window_digest(base, **kwargs)
    second = window_digest(base, **kwargs)

    assert first == second
    assert len(first) == 64  # sha256 hexdigest


def test_digest_changes_when_window_shard_changes(tmp_path: Path) -> None:
    """改**窗口内**的分片 ⇒ 指纹必须变（否则指纹没有鉴别力）。"""
    base = make_data_dir(tmp_path, years=(2026,), event_days=("2026-02-01",))
    kwargs = {"start": date(2026, 1, 1), "end": date(2026, 3, 1), "adjust": "qfq"}
    before = window_digest(base, **kwargs)

    # 重写窗口内的 events 分片（内容变、文件名不变——模拟平台修订重发）
    table = pa.table({"event_id": ["x"], "title": ["改过了"]}, schema=EVENTS_SCHEMA)
    pq.write_table(table, base / "events" / "cn-events_2026-02-01.parquet")

    assert window_digest(base, **kwargs) != before


def test_digest_ignores_shards_outside_window(tmp_path: Path) -> None:
    """改**窗口外**的分片 ⇒ 指纹不许变（窗口口径的意义就在这里）。"""
    base = make_data_dir(
        tmp_path,
        years=(2023, 2026),
        event_days=("2025-01-01", "2026-02-01"),
    )
    kwargs = {"start": date(2026, 1, 1), "end": date(2026, 3, 1), "adjust": "qfq"}
    before = window_digest(base, **kwargs)

    # 窗口外的 bars 年份分片 + 窗口外的 events 日子，内容全改
    pq.write_table(
        pa.table({"symbol": ["000001"], "close": [1.0]}, schema=BARS_SCHEMA),
        base / "bars" / "cn-daily_CN_qfq_2023.parquet",
    )
    pq.write_table(
        pa.table({"event_id": ["y"], "title": ["窗口外"]}, schema=EVENTS_SCHEMA),
        base / "events" / "cn-events_2025-01-01.parquet",
    )

    assert window_digest(base, **kwargs) == before


def test_digest_follows_adjust_axis(tmp_path: Path) -> None:
    """复权口径不同 ⇒ 取到的分片不同 ⇒ 指纹不同（engine 写死 qfq，但口径按参数走）。"""
    base = make_data_dir(tmp_path, years=(2026,), event_days=())
    start, end = date(2026, 1, 1), date(2026, 3, 1)

    assert window_digest(base, start=start, end=end, adjust="qfq") != window_digest(
        base, start=start, end=end, adjust="raw"
    )


def test_snapshot_shape_is_self_describing(tmp_path: Path) -> None:
    """快照自带窗口与分片清单——存疑时能回头看「这个指纹算的是哪一段」。"""
    base = make_data_dir(tmp_path, years=(2026,), event_days=("2026-02-01",))
    snap = window_snapshot(base, start=date(2026, 1, 1), end=date(2026, 3, 1), adjust="qfq")

    assert snap["window"] == {"start": "2026-01-01", "end": "2026-03-01", "adjust": "qfq"}
    assert [s["name"] for s in snap["bars"]["shards"]] == ["cn-daily_CN_qfq_2026.parquet"]
    assert snap["events"]["days"] == 1


def test_empty_window_is_stable_not_error(tmp_path: Path) -> None:
    """窗口里一个分片都没有（离线夹具/空目录）⇒ 仍给确定值，不抛。"""
    base = make_data_dir(tmp_path, years=(2020,), event_days=())
    kwargs = {"start": date(2026, 1, 1), "end": date(2026, 3, 1), "adjust": "qfq"}

    assert window_digest(base, **kwargs) == window_digest(base, **kwargs)


# ── P4-S9：窗口指纹的缓存 ───────────────────────────────────────────────────


def test_digest_is_cached_and_invalidated_by_window_shards(
    tmp_path: Path, monkeypatch
) -> None:
    """S9：同窗口同分片戳命中；窗口内分片重写（文件戳变）⇒ 换键重算。"""
    from app.data import cache
    from app.report import snapshot

    cache.clear_local()
    base = make_data_dir(tmp_path, years=(2026,), event_days=("2026-02-01",))
    kwargs = {"start": date(2026, 1, 1), "end": date(2026, 3, 1), "adjust": "qfq"}
    calls: list[int] = []
    real = snapshot.window_snapshot

    def counting(*args, **kw):
        calls.append(1)
        return real(*args, **kw)

    monkeypatch.setattr(snapshot, "window_snapshot", counting)

    first = window_digest(base, **kwargs)
    assert window_digest(base, **kwargs) == first
    assert len(calls) == 1

    table = pa.table({"event_id": ["x"], "title": ["改过了"]}, schema=EVENTS_SCHEMA)
    pq.write_table(table, base / "events" / "cn-events_2026-02-01.parquet")
    assert window_digest(base, **kwargs) != first
    assert len(calls) == 2
    cache.clear_local()


def test_digest_bypasses_the_cache_when_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    """`BT_DATA_CACHE_ENABLED=false` ⇒ 每次都重算指纹。"""
    from app.core.config import get_settings
    from app.data import cache
    from app.report import snapshot

    cache.clear_local()
    base = make_data_dir(tmp_path, years=(2026,), event_days=())
    kwargs = {"start": date(2026, 1, 1), "end": date(2026, 3, 1), "adjust": "qfq"}
    calls: list[int] = []
    real = snapshot.window_snapshot

    def counting(*args, **kw):
        calls.append(1)
        return real(*args, **kw)

    monkeypatch.setattr(snapshot, "window_snapshot", counting)
    monkeypatch.setattr(get_settings(), "bt_data_cache_enabled", False)

    assert window_digest(base, **kwargs) == window_digest(base, **kwargs)
    assert len(calls) == 2
    cache.clear_local()
