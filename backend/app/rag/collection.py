"""Qdrant 索引层：collection schema、point id、写入与按日重建。

四条口径（每条都有实测依据）：

1. **point id = `uuid5(名称空间, "日|event_id")`**——`event_id` 与 `dedup_key` 都**实测可重复**
   （`dedup_key` 289,494 distinct / 289,519 行；平台对「按月复发的同题事件」复用 id）。
   日分区内 `event_id` 唯一是落盘时就校验过的（`etl/store.py`），故「日 + id」才是稳定主键。
2. **payload 索引在写入前建好**：`available_at` 的 datetime range 是 PIT 过滤的性能前提，
   没有索引就是全表扫；`day` 的 keyword 索引供按日重建与 `facet` 计数。
3. **时间戳一律存 RFC3339 字符串**（带时区）——Qdrant 的 `DatetimeRange` 按此解析；
   存 epoch 数值会让过滤条件与人对不上账。
4. **按日重建 = 先删后写**：平台会修订既有事件（M2b 实测 353 条里 255 条一天内被改过），
   日分区文件 sha 一变就整日重嵌；「先删」保证不会留下已下线的事件。

降级：Qdrant 不可达时抛 `RagNotReady`（不吞异常、不返回空结果）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from qdrant_client import QdrantClient, models

from app.core.config import get_settings
from app.rag import RagNotReady

#: collection 名的默认值（名字带版本：schema 变更时另起名字重建，不做原地迁移）。
#: 实际取 `settings.rag_collection`——集成用例指向测试库，不拿生产索引当试验场。
DEFAULT_COLLECTION = "cn_events_v1"


def collection_name() -> str:
    return get_settings().rag_collection

DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"
DENSE_DIM = 1024  # BGE-M3 稠密维度

#: point id 的名称空间：固定值，改了就等于换了一套主键
NAMESPACE = uuid.UUID("6f1c1f2e-6a1b-4f2b-9d5a-2f4a7c3e8b10")

#: payload 字段清单（落库形态的唯一真源，检索层与测试都引用它）
PAYLOAD_FIELDS: tuple[str, ...] = (
    "day", "event_id", "event_type", "title", "summary", "text",
    "event_time", "available_at",
    "direction_norm", "importance_score", "symbols", "industries",
    "source", "original_source", "source_url", "content_hash",
)


def point_id(day: str, event_id: str) -> str:
    """稳定的 point id（字符串 UUID）。同日同 id 重嵌即覆盖，不产生重复点。"""
    return str(uuid.uuid5(NAMESPACE, f"{day}|{event_id}"))


def get_client(url: str | None = None) -> QdrantClient:
    """Qdrant 客户端。连不上时抛 `RagNotReady`（由调用方转成人话）。"""
    target = url or get_settings().qdrant_url
    client = QdrantClient(url=target, timeout=10.0)
    try:
        client.get_collections()
    except Exception as exc:  # noqa: BLE001 —— 任何连不上都归为「不可用」，由上层降级
        raise RagNotReady(f"Qdrant 不可用（{target}）：{type(exc).__name__}") from exc
    return client


def ensure_collection(client: QdrantClient, *, recreate: bool = False) -> None:
    """建 collection 与 payload 索引（幂等）。

    `recreate=True` 用于 schema 变更后的全量重建——**只删本 collection**。
    """
    if recreate and client.collection_exists(collection_name()):
        client.delete_collection(collection_name())
    if not client.collection_exists(collection_name()):
        client.create_collection(
            collection_name=collection_name(),
            vectors_config={
                DENSE_VECTOR: models.VectorParams(
                    size=DENSE_DIM, distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config={SPARSE_VECTOR: models.SparseVectorParams()},
        )
    for field, schema in (
        ("available_at", models.PayloadSchemaType.DATETIME),
        ("event_time", models.PayloadSchemaType.DATETIME),
        ("day", models.PayloadSchemaType.KEYWORD),
        ("event_type", models.PayloadSchemaType.KEYWORD),
        ("symbols", models.PayloadSchemaType.KEYWORD),
        ("industries", models.PayloadSchemaType.KEYWORD),
        ("importance_score", models.PayloadSchemaType.FLOAT),
    ):
        client.create_payload_index(collection_name(), field_name=field, field_schema=schema)


def build_point(
    payload: Mapping[str, Any], dense: Sequence[float], sparse: Mapping[int, float]
) -> models.PointStruct:
    """把一行语料 + 向量装成 PointStruct。

    `sparse` 用 `{token_id: weight}` 表达（BGE-M3 的 lexical weights 就是这个形状），
    这里转成 Qdrant 的 `indices` / `values` 两列。**稀疏为空时整条腿省略**——
    空稀疏向量不是「零分」而是「搜不到」，留着它只会让这个点对稀疏分支永远隐身。
    """
    indices = sorted(sparse)
    vector: dict[str, Any] = {DENSE_VECTOR: list(dense)}
    if indices:
        vector[SPARSE_VECTOR] = models.SparseVector(
            indices=indices, values=[float(sparse[i]) for i in indices]
        )
    return models.PointStruct(
        id=point_id(str(payload["day"]), str(payload["event_id"])),
        vector=vector,
        payload=dict(payload),
    )


def upsert_points(
    client: QdrantClient, points: Iterable[models.PointStruct], *, wait: bool = True
) -> int:
    """批量写入。返回写入点数；`wait=True` 时返回即已可检索。"""
    batch = list(points)
    if not batch:
        return 0
    client.upsert(collection_name(), points=batch, wait=wait)
    return len(batch)


def delete_day(client: QdrantClient, day: str) -> None:
    """删掉某日全部点——按日重建的第一步。依赖 `day` 的 keyword 索引。"""
    client.delete(
        collection_name(),
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="day", match=models.MatchValue(value=day))]
            )
        ),
        wait=True,
    )


def count_by_day(client: QdrantClient, *, limit: int = 2048) -> dict[str, int]:
    """逐日点数（`facet` 走 payload 索引，不拉全量点）。

    对账用：与 `_meta/events.json` 的逐日行数比，**两个方向的差集都要报**
    （本地有而索引无 = 漏嵌；索引有而本地无 = 陈旧点）。
    """
    response = client.facet(
        collection_name(), key="day", limit=limit, exact=True
    )
    return {str(hit.value): int(hit.count) for hit in response.hits}
