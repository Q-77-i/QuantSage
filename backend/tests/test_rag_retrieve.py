"""检索管线用例：**PIT 硬过滤是主角**，另有过滤构造、排序与降级路径。

全部离线（内存向量库替身 + 假编码器），但替身照抄了服务端过滤语义，
故「未来事件挤掉合法结果」这个失败模式在这里能真的被复现出来。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.backtest.types import CN_TZ
from app.rag import collection as col
from app.rag.retrieve import SearchQuery, build_filter, resolve_as_of, search
from tests.fakes_rag import DIM, FakeEmbedder, FakeQdrant, FakeReranker

NOW = datetime(2026, 9, 20, 15, 0, tzinfo=CN_TZ)


def point(client: FakeQdrant, *, day: str, event_id: str, text: str, available_at: str,
          **payload) -> None:
    embedder = FakeEmbedder(DIM)
    dense, sparse = embedder.encode_query(text)
    body = {
        "day": day,
        "event_id": event_id,
        "event_type": payload.pop("event_type", "news"),
        "title": payload.pop("title", text),
        "text": text,
        "available_at": available_at,
        "event_time": available_at,
        "symbols": payload.pop("symbols", ["600519"]),
        "industries": payload.pop("industries", []),
        "source": "xiaoshi-archive",
        "original_source": "证券时报",
        "source_url": "https://example.com/x",
        "content_hash": "hash",
        "importance_score": payload.pop("importance_score", 50.0),
        **payload,
    }
    col.upsert_points(client, [col.build_point(body, dense, sparse)])


# ── 过滤构造 ──────────────────────────────────────────────────────────────


def test_filter_always_carries_available_at() -> None:
    """`available_at <= as_of` 是必带项——它不是可选优化，是护城河本身。"""
    filt = build_filter(SearchQuery(query="随便问问"))
    keys = [c.key for c in filt.must]
    assert "available_at" in keys
    assert len(filt.must) == 1  # 其余条件都没传时，只有这一条


def test_filter_adds_optional_conditions_only_when_given() -> None:
    filt = build_filter(
        SearchQuery(query="q", symbol="600519", event_type="policy",
                    industries=("银行",), min_importance=40.0)
    )
    keys = {c.key for c in filt.must}
    assert keys == {"available_at", "symbols", "event_type", "industries", "importance_score"}


def test_resolve_as_of_defaults_to_now_in_market_timezone() -> None:
    resolved = resolve_as_of(None)
    assert resolved.tzinfo is not None
    assert abs(resolved - datetime.now(CN_TZ)) < timedelta(seconds=5)


def test_resolve_as_of_treats_naive_as_beijing() -> None:
    """语料是 A 股市场，轴就是北京时间；裸时间按北京时间解释而不是 UTC。"""
    assert resolve_as_of(datetime(2026, 9, 20, 15, 0)).utcoffset() == timedelta(hours=8)


# ── PIT：结果边界 ─────────────────────────────────────────────────────────


def test_search_excludes_events_not_yet_available() -> None:
    client = FakeQdrant()
    point(client, day="2026-09-01", event_id="news:early", text="早已知晓",
          available_at="2026-09-01T10:00:00+08:00")
    point(client, day="2026-09-19", event_id="news:late", text="事后才知道",
          available_at="2026-09-19T10:00:00+08:00")

    hits = search(
        SearchQuery(query="知晓", as_of=datetime(2026, 9, 10, tzinfo=CN_TZ)),
        client=client, embedder=FakeEmbedder(DIM), reranker=FakeReranker(),
    )
    assert [h.event_id for h in hits] == ["news:early"]


def test_boundary_event_available_exactly_at_as_of_is_included() -> None:
    """边界：`available_at == as_of` **算可见**（<= 而非 <）。"""
    client = FakeQdrant()
    moment = datetime(2026, 9, 10, 10, 0, tzinfo=CN_TZ)
    point(client, day="2026-09-10", event_id="news:edge", text="刚好那一刻",
          available_at=moment.isoformat())
    hits = search(
        SearchQuery(query="那一刻", as_of=moment),
        client=client, embedder=FakeEmbedder(DIM), reranker=FakeReranker(),
    )
    assert [h.event_id for h in hits] == ["news:edge"]


def test_as_of_before_corpus_start_returns_nothing() -> None:
    client = FakeQdrant()
    point(client, day="2026-09-01", event_id="news:1", text="x",
          available_at="2026-09-01T10:00:00+08:00")
    hits = search(
        SearchQuery(query="x", as_of=datetime(2020, 1, 1, tzinfo=CN_TZ)),
        client=client, embedder=FakeEmbedder(DIM), reranker=FakeReranker(),
    )
    assert hits == []


def test_highly_relevant_future_event_does_not_displace_legit_results() -> None:
    """**服务端过滤的行为证据**：只断言「结果里没有未来事件」不够。

    这里构造一个与 query 完全同向、且排名必然第一的未来事件：
    如果过滤发生在「取回之后」，它会先占住候选位，把合法结果挤出去（甚至挤空）。
    """
    client = FakeQdrant()
    query = "央行降准"
    point(client, day="2026-09-18", event_id="news:legit", text="一条普通旧闻",
          available_at="2026-09-18T10:00:00+08:00")
    for i in range(5):  # 与 query 同向 ⇒ 未过滤时必排第一，且能占满 top-5
        point(client, day="2026-09-25", event_id=f"news:future{i}", text=query,
              available_at="2026-09-25T10:00:00+08:00")

    hits = search(
        SearchQuery(query=query, as_of=NOW, top_k=5),
        client=client, embedder=FakeEmbedder(DIM), reranker=FakeReranker(),
    )
    assert [h.event_id for h in hits] == ["news:legit"]
    assert client.last_filter is not None  # 过滤确实发给了服务端，而不是事后筛
    assert any(c.key == "available_at" for c in client.last_filter.must)


# ── 排序与降级 ────────────────────────────────────────────────────────────


def test_rerank_order_is_applied() -> None:
    client = FakeQdrant()
    point(client, day="2026-09-01", event_id="news:a", text="doc-a",
          available_at="2026-09-01T10:00:00+08:00")
    point(client, day="2026-09-01", event_id="news:b", text="doc-b",
          available_at="2026-09-01T11:00:00+08:00")
    reranker = FakeReranker(scores={"doc-a": 0.1, "doc-b": 0.9})
    hits = search(
        SearchQuery(query="q", as_of=NOW, top_k=2),
        client=client, embedder=FakeEmbedder(DIM), reranker=reranker,
    )
    assert [h.event_id for h in hits] == ["news:b", "news:a"]
    assert {h.score_kind for h in hits} == {"rerank"}
    assert [h.score for h in hits] == [0.9, 0.1]


def test_top_k_is_respected() -> None:
    client = FakeQdrant()
    for i in range(5):
        point(client, day="2026-09-01", event_id=f"news:{i}", text=f"doc-{i}",
              available_at="2026-09-01T10:00:00+08:00")
    hits = search(
        SearchQuery(query="q", as_of=NOW, top_k=2),
        client=client, embedder=FakeEmbedder(DIM), reranker=FakeReranker(),
    )
    assert len(hits) == 2


def test_reranker_failure_degrades_to_rrf_and_says_so() -> None:
    """降级可以，隐瞒不行：`score_kind` 必须如实标出「这次没精排」。"""
    client = FakeQdrant()
    point(client, day="2026-09-01", event_id="news:a", text="doc-a",
          available_at="2026-09-01T10:00:00+08:00")
    hits = search(
        SearchQuery(query="q", as_of=NOW),
        client=client, embedder=FakeEmbedder(DIM), reranker=FakeReranker(fail=True),
    )
    assert [h.event_id for h in hits] == ["news:a"]
    assert hits[0].score_kind == "rrf"


def test_mode_hybrid_skips_rerank_and_labels_score_kind() -> None:
    """消融档位必须如实标注自己在哪一档——否则三档数字对不上账。"""
    client = FakeQdrant()
    point(client, day="2026-09-01", event_id="news:a", text="doc-a",
          available_at="2026-09-01T10:00:00+08:00")
    reranker = FakeReranker(scores={"doc-a": 0.9})
    hits = search(
        SearchQuery(query="doc", as_of=NOW),
        client=client, embedder=FakeEmbedder(DIM), reranker=reranker, mode="hybrid",
    )
    assert hits[0].score_kind == "rrf"
    assert reranker.calls == 0  # 这一档就不该调精排


def test_mode_dense_runs_single_leg() -> None:
    client = FakeQdrant()
    point(client, day="2026-09-01", event_id="news:a", text="doc-a",
          available_at="2026-09-01T10:00:00+08:00")
    hits = search(
        SearchQuery(query="doc", as_of=NOW),
        client=client, embedder=FakeEmbedder(DIM), reranker=FakeReranker(fail=True), mode="dense",
    )
    assert [h.event_id for h in hits] == ["news:a"]
    assert hits[0].score_kind == "dense"  # 单路时如实标「稠密」，不冒充融合结果
    assert client.last_prefetch_len == 1


def test_symbol_filter_narrows_results() -> None:
    client = FakeQdrant()
    point(client, day="2026-09-01", event_id="news:mt", text="doc", symbols=["600519"],
          available_at="2026-09-01T10:00:00+08:00")
    point(client, day="2026-09-01", event_id="news:catl", text="doc", symbols=["300750"],
          available_at="2026-09-01T10:00:00+08:00")
    hits = search(
        SearchQuery(query="q", as_of=NOW, symbol="300750"),
        client=client, embedder=FakeEmbedder(DIM), reranker=FakeReranker(),
    )
    assert [h.event_id for h in hits] == ["news:catl"]


def test_result_carries_both_timestamps_and_source_triple() -> None:
    """PRD §5 硬性要求：双时间戳并列 + 来源三元组，M7 证据面板直接消费。"""
    client = FakeQdrant()
    point(client, day="2026-09-01", event_id="news:a", text="doc",
          available_at="2026-09-01T10:30:00+08:00", event_time="2026-09-01T10:00:00+08:00")
    hits = search(
        SearchQuery(query="q", as_of=NOW),
        client=client, embedder=FakeEmbedder(DIM), reranker=FakeReranker(),
    )
    hit = hits[0]
    assert hit.available_at and hit.event_time and hit.available_at != hit.event_time
    assert (hit.source, hit.original_source, hit.content_hash) != (None, None, None)
    assert hit.source_url


@pytest.mark.parametrize("top_k", [1, 3])
def test_empty_store_returns_empty_list(top_k: int) -> None:
    hits = search(
        SearchQuery(query="q", as_of=NOW, top_k=top_k),
        client=FakeQdrant(), embedder=FakeEmbedder(DIM), reranker=FakeReranker(),
    )
    assert hits == []
