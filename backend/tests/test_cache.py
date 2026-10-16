"""E3c 数据层缓存的离线用例（SPEC §4.6）。

三类判据分开验：

* **代际**：`generation` 是纯函数（清单变 ⇒ gen 变）、mtime 门不改变结果（只省开销）；
* **分层**：**L1 命中就不碰 L2**（用探针数调用次数，不看耗时——耗时受机器负载影响）；
* **可降级**：Redis 不可达 ⇒ 直读、**producer 照跑**、熔断后冷却期内不再试。

⚠ 用例之间必须清状态：L1 是进程级字典、熔断状态是进程级单例——不清就会串味
（这与 E4b 那条「令牌桶是进程级状态」的教训同族）。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from app.core import reliability
from app.core.config import get_settings
from app.data import cache


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch: pytest.MonkeyPatch) -> Any:
    """每个用例前后都清三样：L1、L2 连接、熔断状态。"""
    cache.clear_local()
    cache.drop_client()
    reliability.reset("redis-cache")
    yield
    cache.clear_local()
    cache.drop_client()
    reliability.reset("redis-cache")


@pytest.fixture
def meta_dir(tmp_path: Path) -> Path:
    (tmp_path / "_meta").mkdir(parents=True, exist_ok=True)
    return tmp_path


def write_bars_meta(root: Path, marker: str) -> None:
    (root / "_meta" / "bars.json").write_text(json.dumps({"fingerprint": {"shards": [marker]}}), encoding="utf-8")


def write_events_meta(root: Path, marker: str) -> None:
    (root / "_meta" / "events.json").write_text(json.dumps({"fingerprint": {"digest": marker}}), encoding="utf-8")


# ── 代际 ───────────────────────────────────────────────────


def test_generation_changes_with_the_manifest(meta_dir: Path) -> None:
    write_bars_meta(meta_dir, "a")
    first = cache.generation(meta_dir)
    write_bars_meta(meta_dir, "b")  # 清单换了 ⇒ 代际必须换（失效 = 换键，不靠 DEL）
    assert cache.generation(meta_dir) != first


def test_generation_is_stable_when_nothing_moved(meta_dir: Path) -> None:
    """没变就是没变——mtime 门只省开销，不改结果（这条钉住「门是安全的」）。"""
    write_bars_meta(meta_dir, "a")
    write_events_meta(meta_dir, "x")
    assert [cache.generation(meta_dir) for _ in range(3)] == [cache.generation(meta_dir)] * 3


def test_generation_separates_directories_without_manifests(tmp_path: Path) -> None:
    """两份清单都不在时**按目录区分**——都拿同一个占位符的话不同目录会互相串缓存。

    这不是假想：接线的第一版就是这么写的，`tests/test_data_layer.py` 当场撞了。
    """
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert cache.generation(a) != cache.generation(b)


def test_generation_survives_a_missing_channel(meta_dir: Path) -> None:
    """只有行情、语料还没落盘 —— 照样能算出代际（缓存命中率差些而已，不该报错）。"""
    write_bars_meta(meta_dir, "a")
    assert cache.generation(meta_dir)


# ── 分层 ───────────────────────────────────────────────────


def spy_l2(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """数 L2 的读写次数——「L1 命中不碰 Redis」只能这么证（耗时会被机器负载淹没）。"""
    calls = {"get": 0, "set": 0}
    sentinel = object()

    def fake_get(key: tuple) -> Any:
        calls["get"] += 1
        return sentinel

    def fake_set(key: tuple, value: Any) -> None:
        calls["set"] += 1

    monkeypatch.setattr(cache, "_redis_get", fake_get)
    monkeypatch.setattr(cache, "_redis_set", fake_set)
    monkeypatch.setattr(cache, "_MISS", sentinel)
    return calls


def test_l1_hit_does_not_touch_redis(meta_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_bars_meta(meta_dir, "a")
    calls = spy_l2(monkeypatch)
    produced = {"n": 0}

    def produce() -> list:
        produced["n"] += 1
        return [{"close": 1.0}]

    first = cache.through("bars", ("qfq", "600519"), produce, data_dir=meta_dir)
    second = cache.through("bars", ("qfq", "600519"), produce, data_dir=meta_dir)

    assert first == second
    assert produced["n"] == 1  # 真读只发生一次
    assert calls == {"get": 1, "set": 1}  # 第二次一次 L2 都没碰 —— 这就是「先本地后远端」


def test_a_new_generation_does_not_reuse_the_old_entry(meta_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**正确性红线**的离线版：清单变了就不能再拿旧结果（哪怕 L1 里还躺着）。"""
    write_bars_meta(meta_dir, "a")
    spy_l2(monkeypatch)
    produced = {"n": 0}

    def produce() -> list:
        produced["n"] += 1
        return [{"close": float(produced["n"])}]

    old = cache.through("bars", ("qfq", "600519"), produce, data_dir=meta_dir)
    write_bars_meta(meta_dir, "b")  # 数据换代
    new = cache.through("bars", ("qfq", "600519"), produce, data_dir=meta_dir)

    assert old != new
    assert produced["n"] == 2


def test_different_directories_do_not_share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        (root / "_meta").mkdir(parents=True)
        write_bars_meta(root, "same")  # 清单内容故意一样
    spy_l2(monkeypatch)

    assert cache.through("bars", ("qfq", "600519"), lambda: ["A"], data_dir=a) == ["A"]
    assert cache.through("bars", ("qfq", "600519"), lambda: ["B"], data_dir=b) == ["B"]


# ── 序列化 ─────────────────────────────────────────────────


def test_dates_and_datetimes_keep_their_types(meta_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """往返要还原成**对象**——不然缓存命中与直读的类型就不一致了（验收判据是「逐字段相等」）。"""
    spy_l2(monkeypatch)
    row = {"trade_date": date(2026, 10, 9), "available_at": datetime(2026, 10, 9, 20, 25), "close": 1.0}

    encoded = cache._decode(json.loads(json.dumps(cache._encode(row))))
    assert encoded == row
    assert isinstance(encoded["trade_date"], date) and not isinstance(encoded["trade_date"], datetime)
    assert isinstance(encoded["available_at"], datetime)


def test_a_corrupt_value_counts_as_a_miss(meta_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """一条脏值不该打穿整个请求——当没有，重算。"""
    write_bars_meta(meta_dir, "a")
    monkeypatch.setattr(cache, "_redis_get", lambda key: cache._MISS)
    monkeypatch.setattr(cache, "_redis_set", lambda key, value: None)

    class Bad:
        def get(self, _key: str) -> bytes:
            return b"{not json"

    monkeypatch.setattr(cache, "_client", lambda: Bad())
    assert cache.through("bars", ("qfq", "600519"), lambda: ["ok"], data_dir=meta_dir) == ["ok"]


# ── 可降级 ─────────────────────────────────────────────────


def test_disabled_switch_means_no_redis_at_all(meta_dir: Path) -> None:
    """`CACHE_ENABLED` 默认关 —— 关着就不该建连接（L1 照常工作，它不看这个开关）。"""
    assert get_settings().cache_enabled is False
    assert cache._client() is None
    assert cache.through("bars", ("qfq", "600519"), lambda: ["直读"], data_dir=meta_dir) == ["直读"]


def test_unreachable_redis_falls_back_and_trips_the_breaker(
    meta_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis 连不上 ⇒ **直读照走**，且**失败即开路**（下次不再付一遍超时）。"""
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # 一个必然连不上的端口
    get_settings.cache_clear()
    write_bars_meta(meta_dir, "a")
    try:
        assert cache.through("bars", ("qfq", "600519"), lambda: ["直读"], data_dir=meta_dir) == ["直读"]
        dep = reliability.get_dep("redis-cache")
        assert dep.state == "open", "阈值是 1：一次失败就该开路"
        # 冷却期内不再尝试（`_client` 直接返回 None，连 socket 都不建）
        assert cache._client() is None
    finally:
        get_settings.cache_clear()
