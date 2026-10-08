"""索引层用例：point id 稳定性、稀疏腿省略、按日删除与计数（用内存替身，不连真库）。"""

from __future__ import annotations

from app.rag import collection as col
from tests.fakes_rag import DIM, FakeQdrant


def payload(day: str, event_id: str, **overrides) -> dict:
    base = {
        "day": day,
        "event_id": event_id,
        "event_type": "news",
        "title": "标题",
        "text": "【标题】标题",
        "available_at": f"{day}T10:00:00+08:00",
        "importance_score": 50.0,
    }
    base.update(overrides)
    return base


def test_point_id_is_stable_and_never_collides_across_days() -> None:
    """`event_id` 会被平台跨月复用（实测 18 例），主键必须带日期维度。"""
    assert col.point_id("2026-08-17", "news:1818329") == col.point_id("2026-08-17", "news:1818329")
    assert col.point_id("2026-08-17", "news:1818329") != col.point_id("2026-09-17", "news:1818329")


def test_build_point_keeps_dense_and_drops_empty_sparse() -> None:
    point = col.build_point(payload("2026-08-17", "news:1"), [0.1] * DIM, {})
    assert col.DENSE_VECTOR in point.vector
    assert col.SPARSE_VECTOR not in point.vector  # 空稀疏不是「零分」而是「搜不到」


def test_build_point_sorts_sparse_indices() -> None:
    point = col.build_point(payload("2026-08-17", "news:1"), [0.1] * DIM, {7: 0.5, 2: 0.9})
    sparse = point.vector[col.SPARSE_VECTOR]
    assert list(sparse.indices) == [2, 7]
    assert list(sparse.values) == [0.9, 0.5]


def test_upsert_is_idempotent_by_point_id() -> None:
    """同日同 id 重嵌即覆盖：不产生重复点（增量重嵌的幂等基础）。"""
    client = FakeQdrant()
    for _ in range(2):
        col.upsert_points(client, [col.build_point(payload("2026-08-17", "news:1"), [0.1] * DIM, {1: 1.0})])
    assert len(client.points) == 1


def test_delete_day_removes_only_that_day() -> None:
    client = FakeQdrant()
    col.upsert_points(
        client,
        [
            col.build_point(payload("2026-08-17", "news:1"), [0.1] * DIM, {1: 1.0}),
            col.build_point(payload("2026-08-18", "news:2"), [0.1] * DIM, {1: 1.0}),
        ],
    )
    col.delete_day(client, "2026-08-17")
    assert col.count_by_day(client) == {"2026-08-18": 1}


def test_count_by_day_reads_payload_facet() -> None:
    client = FakeQdrant()
    col.upsert_points(
        client,
        [
            col.build_point(payload("2026-08-17", "news:1"), [0.1] * DIM, {1: 1.0}),
            col.build_point(payload("2026-08-17", "news:2"), [0.1] * DIM, {1: 1.0}),
            col.build_point(payload("2026-08-18", "news:3"), [0.1] * DIM, {1: 1.0}),
        ],
    )
    assert col.count_by_day(client) == {"2026-08-17": 2, "2026-08-18": 1}


def test_ensure_collection_creates_pit_index() -> None:
    """`available_at` 的 datetime 索引是 PIT 过滤的性能前提，不是可选项。"""
    client = FakeQdrant()
    col.ensure_collection(client)
    assert col.collection_name() in client.collections
    assert {"available_at", "day", "event_type", "symbols", "industries"} <= client.payload_indexes[
        col.collection_name()
    ]


def test_ensure_collection_recreate_drops_points() -> None:
    client = FakeQdrant()
    col.ensure_collection(client)
    col.upsert_points(client, [col.build_point(payload("2026-08-17", "news:1"), [0.1] * DIM, {1: 1.0})])
    col.ensure_collection(client, recreate=True)
    assert client.points == {}
