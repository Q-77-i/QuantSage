"""M3 集成验收：对**真实 Qdrant** 跑一遍索引与检索，重点验 PIT 过滤发生在服务端。

默认不收集；用 `uv run pytest -m integration` 触发（需要 `docker compose up` 里的 Qdrant）。

两条纪律：

* **用独立 collection**（`settings.rag_collection` 指到测试名），绝不拿生产索引当试验场；
  用例结束把测试库删掉，不留垃圾。
* 编码器用**确定性替身**（不下载模型、不联网）：这里要证的是「我们对 Qdrant 的用法
  与过滤语义正确」，不是模型效果——模型那条链由 `scripts/spike_rag_encoder.py` 与
  全量嵌入的交付证据负责（SPEC §4 M3a）。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from qdrant_client import QdrantClient

from app.backtest.types import CN_TZ
from app.core.config import get_settings
from app.rag import collection as col
from app.rag.retrieve import SearchQuery, search
from tests.fakes_rag import FakeEmbedder, FakeReranker

pytestmark = pytest.mark.integration

TEST_COLLECTION = "cn_events_m3_test"
NOW = datetime(2026, 9, 20, 15, 0, tzinfo=CN_TZ)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[QdrantClient, FakeEmbedder]]:
    """把 collection 名指向测试库，建好索引与 payload 索引，用完删掉。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_collection", TEST_COLLECTION)
    client = col.get_client()
    if client.collection_exists(TEST_COLLECTION):
        client.delete_collection(TEST_COLLECTION)
    col.ensure_collection(client, recreate=True)
    try:
        # 真库的 dense 是 1024 维，替身必须同维，否则写入就被拒
        yield client, FakeEmbedder(col.DENSE_DIM)
    finally:
        client.delete_collection(TEST_COLLECTION)


def upsert(client: QdrantClient, embedder: FakeEmbedder, *, day: str, event_id: str, text: str,
           available_at: str, **payload) -> None:
    dense, sparse = embedder.encode_query(text)
    body = {
        "day": day,
        "event_id": event_id,
        "event_type": payload.pop("event_type", "news"),
        "title": payload.pop("title", text),
        "text": text,
        "event_time": available_at,
        "available_at": available_at,
        "symbols": payload.pop("symbols", ["600519"]),
        "industries": payload.pop("industries", []),
        "importance_score": payload.pop("importance_score", 50.0),
        "source": "xiaoshi-archive",
        "original_source": "证券时报",
        "source_url": "https://example.com/x",
        "content_hash": "hash",
        **payload,
    }
    col.upsert_points(client, [col.build_point(body, dense, sparse)])


def test_collection_carries_pit_index(store) -> None:
    """`available_at` 的 datetime 索引建在真库里（PIT 过滤的性能前提）。"""
    client, _ = store
    schema = client.get_collection(TEST_COLLECTION).payload_schema
    assert "available_at" in schema
    assert schema["available_at"].data_type == "datetime"


def test_pit_filter_runs_on_server(store) -> None:
    """真库行为：与 query 同向的未来事件**不参与候选**，合法结果不会被挤掉。"""
    client, embedder = store
    upsert(client, embedder, day="2026-09-18", event_id="news:legit", text="一条普通旧闻",
           available_at="2026-09-18T10:00:00+08:00")
    for i in range(5):  # 与 query 同向 ⇒ 若不过滤，必占满 top-5
        upsert(client, embedder, day="2026-09-25", event_id=f"news:future{i}",
               text="央行降准", available_at="2026-09-25T10:00:00+08:00")

    hits = search(
        SearchQuery(query="央行降准", as_of=NOW, top_k=5),
        client=client, embedder=embedder, reranker=FakeReranker(),
    )
    assert [h.event_id for h in hits] == ["news:legit"]


def test_dense_mode_single_leg(store) -> None:
    """单路档（消融用）在真库上必须能跑。

    本 collection 只有命名向量，没有「默认向量」——把裸向量当 query 传会被 Qdrant 拒
    （`Not existing vector name error`）。这条用例是补上来的：离线替身不会报这个错，
    第一版真库集成用例又只跑了完整档，于是漏洞一路活到评测池化时才炸。
    """
    client, embedder = store
    upsert(client, embedder, day="2026-09-10", event_id="news:a", text="一家钨业公司的业绩预告",
           available_at="2026-09-10T10:00:00+08:00")
    hits = search(
        SearchQuery(query="业绩预告", as_of=NOW, top_k=3),
        client=client, embedder=embedder, reranker=FakeReranker(), mode="dense",
    )
    assert [h.event_id for h in hits] == ["news:a"]
    assert hits[0].score_kind == "dense"


def test_symbol_and_industry_filters(store) -> None:
    client, embedder = store
    upsert(client, embedder, day="2026-09-10", event_id="news:mt", text="白酒动销",
           available_at="2026-09-10T10:00:00+08:00", symbols=["600519"], industries=["食品饮料"])
    upsert(client, embedder, day="2026-09-10", event_id="news:catl", text="电池排产",
           available_at="2026-09-10T10:00:00+08:00", symbols=["300750"], industries=["电力设备"])

    hits = search(
        SearchQuery(query="排产", as_of=NOW, symbol="300750", industries=("电力设备",)),
        client=client, embedder=embedder, reranker=FakeReranker(),
    )
    assert [h.event_id for h in hits] == ["news:catl"]


def test_rebuild_day_is_idempotent(store) -> None:
    """按日重建：先删后写，重跑不产生重复点，也不留已下线的事件。"""
    client, embedder = store
    upsert(client, embedder, day="2026-09-10", event_id="news:a", text="a",
           available_at="2026-09-10T10:00:00+08:00")
    upsert(client, embedder, day="2026-09-10", event_id="news:b", text="b",
           available_at="2026-09-10T11:00:00+08:00")

    col.delete_day(client, "2026-09-10")
    # 真库的 facet 会为该关键字保留 0 计数条目（实测），故判「这天没点」要看计数而不是看键在不在
    assert col.count_by_day(client).get("2026-09-10", 0) == 0
    upsert(client, embedder, day="2026-09-10", event_id="news:a", text="a（修订版）",
           available_at="2026-09-10T10:00:00+08:00")

    assert col.count_by_day(client) == {"2026-09-10": 1}
    hits = search(
        SearchQuery(query="a", as_of=NOW),
        client=client, embedder=embedder, reranker=FakeReranker(),
    )
    assert [h.event_id for h in hits] == ["news:a"]
    assert hits[0].title.endswith("（修订版）")


def test_same_event_id_on_two_days_are_two_points(store) -> None:
    """平台会复用 `event_id`（实测 18 例）——不同日的同 id 事件必须是两个点。"""
    client, embedder = store
    for day in ("2026-08-10", "2026-09-10"):
        upsert(client, embedder, day=day, event_id="news:1818329", text=f"{day} 的 CPI",
               available_at=f"{day}T10:00:00+08:00")
    assert col.count_by_day(client) == {"2026-08-10": 1, "2026-09-10": 1}
