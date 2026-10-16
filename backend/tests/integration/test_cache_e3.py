"""E3c 缓存的真 Redis 集成验收（SPEC §4.8）。

默认不收集；用 `uv run pytest -m integration tests/integration/test_cache_e3.py` 触发。
需要一个跑着的 Redis（`docker compose up -d redis`）与真实 `data/`。

三条判据，离线套件证不了的都在这儿：

* **命中与直读逐字段相等**（含类型：`trade_date` 得是 `date` 不是字符串）；
* ⚠ **代际变更后不返回旧值**——这一节的**正确性红线**：「读到旧代际的数据 = 另一种
  『看到不该看到的过去』」；
* **Redis 挂掉时请求仍 200**（指向一个死端口，不真停容器——停容器会打断同机其它测试）。
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core import reliability
from app.core.config import get_settings
from app.data import cache
from app.data import duckdb_client as dc

pytestmark = pytest.mark.integration

SYMBOL = "600519"


def redis_alive(url: str) -> bool:
    host, port = "127.0.0.1", 6379
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture
def live_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """打开缓存并清干净两层状态（L1 是进程级字典、熔断是进程级单例）。"""
    url = get_settings().redis_url
    if not redis_alive(url):
        pytest.skip(f"Redis 不可达（{url}）——先 docker compose up -d redis")
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("CACHE_TTL_SECONDS", "60")
    get_settings.cache_clear()
    cache.clear_local()
    cache.drop_client()
    reliability.reset("redis-cache")
    client = cache._client()
    assert client is not None, "缓存开关开着却建不出连接"
    client.flushdb()  # 只清本库（业务 redis 只我们一家用）
    try:
        yield
    finally:
        cache.clear_local()
        cache.drop_client()
        reliability.reset("redis-cache")
        get_settings.cache_clear()


def test_hit_equals_a_direct_read(live_cache: None, tmp_path: Path) -> None:
    """命中拿到的必须与直读**逐字段相等**（含 `date` 的类型）——缓存不该改变任何语义。"""
    direct = dc.bars(SYMBOL, start="2026-01-02", end="2026-09-30")
    assert direct, "真实 data/ 里没有该标的的行情"

    cache.clear_local()  # 逼一次「L1 未命中 ⇒ 走 L2」，写进去
    cache.through("bars", ("qfq", SYMBOL, "2026-01-02", "2026-09-30"), lambda: direct)
    cache.clear_local()  # 再清 L1 ⇒ 这次必然从 Redis 反序列化回来
    hit = cache.through("bars", ("qfq", SYMBOL, "2026-01-02", "2026-09-30"), lambda: pytest.fail("不该重算"))

    assert hit == direct
    assert isinstance(hit[0]["trade_date"], type(direct[0]["trade_date"]))
    assert hit[0]["trade_date"] == direct[0]["trade_date"]


def test_a_new_generation_never_returns_the_old_value(live_cache: None, tmp_path: Path) -> None:
    """**红线**：`bars.json` 一改（数据换代），同参数的查询必须走直读拿到新值。

    做法：把清单复制到临时数据目录、灌一次缓存、改清单（换 gen）、再查——第二次**必须**
    调 producer，且拿到的不是第一次的值。这条只有真 Redis 能证（要的是「键真的换了」）。
    """
    data_dir = tmp_path / "data"
    (data_dir / "_meta").mkdir(parents=True)
    (data_dir / "bars").mkdir()
    (data_dir / "_meta" / "events.json").write_text(json.dumps({"fingerprint": {"digest": "e"}}), encoding="utf-8")
    write_bars_meta(data_dir, "sha-A")

    calls = {"n": 0}

    def produce() -> list:
        calls["n"] += 1
        return [{"close": float(calls["n"])}]

    first = cache.through("bars", ("qfq", SYMBOL), produce, data_dir=data_dir)
    cache.clear_local()  # 清掉 L1，逼它走 L2
    again = cache.through("bars", ("qfq", SYMBOL), produce, data_dir=data_dir)
    assert first == again and calls["n"] == 1, "同代际应当命中缓存"

    write_bars_meta(data_dir, "sha-B")  # 数据换代
    fresh = cache.through("bars", ("qfq", SYMBOL), produce, data_dir=data_dir)

    assert calls["n"] == 2, "换代之后必须重算"
    assert fresh != first


def test_a_dead_redis_still_serves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redis 指向死端口 ⇒ 请求照常出结果（直读），且熔断打开（下次不再付超时）。"""
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    get_settings.cache_clear()
    cache.clear_local()
    cache.drop_client()
    reliability.reset("redis-cache")
    try:
        rows = dc.bars(SYMBOL, start="2026-01-02", end="2026-09-30")
        assert rows, "降级路径必须真的读到数据"
        assert reliability.get_dep("redis-cache").state == "open"
    finally:
        cache.clear_local()
        cache.drop_client()
        reliability.reset("redis-cache")
        get_settings.cache_clear()


def write_bars_meta(root: Path, sha: str) -> None:
    (root / "_meta" / "bars.json").write_text(
        json.dumps({"fingerprint": {"shards": [{"sha256": sha}]}}), encoding="utf-8"
    )
