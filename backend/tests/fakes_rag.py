"""RAG 的离线替身：假编码器、假重排器、内存向量库。

**内存库必须照抄服务端语义**，否则用例是假的（同 M1 的内存业务库口径）：

* `available_at <= as_of` 在**检索时**过滤（不是检索后过滤）——这正是 PIT 用例要验的行为，
  替身若先排序后过滤，「未来事件挤掉合法结果」这个失败模式就测不出来；
* 稀疏腿为空时该点对稀疏分支隐身（与 Qdrant 的命名向量语义一致）；
* RRF 按 `1/(k+rank)` 融合，**rank 从 0 起**（与 Qdrant 实现对齐，见 `retrieve.RRF_K`）。

编码器返回确定性向量：同一段文本永远同一向量（用哈希播种），
故「相关性」在用例里可被构造——把某条事件的向量设成与 query 同向即可。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.rag.collection import DENSE_VECTOR, SPARSE_VECTOR
from app.rag.encoder import EncodedDocs

DIM = 8  # 用例里不需要 1024 维，8 维足够表达「同向/正交」


def seeded_vector(text: str, dim: int = DIM) -> list[float]:
    """确定性伪向量：同文本同向量，跨进程稳定（用 sha256 播种）。"""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = [(digest[i % len(digest)] + i) % 17 - 8 for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


class FakeEmbedder:
    """假编码器：`dim` 与真实一致（1024）以免绕过维度校验。"""

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self.calls = 0

    def _sparse(self, text: str) -> dict[int, float]:
        """字符二元组当 token——中文没有空格，按空格切会一律得到空稀疏。

        返回空稀疏的话，替身里的中文 query 就永远走单路召回，
        「融合」那条路径根本不会被执行到（真实 BGE-M3 对中文是切 subword 的）。
        """
        stripped = "".join(text.split())
        grams = [stripped[i : i + 2] for i in range(max(0, len(stripped) - 1))] or [stripped]
        return {
            int(hashlib.sha256(g.encode()).hexdigest()[:6], 16) % 5000: 1.0
            for g in grams
            if g
        }

    def encode_documents(self, texts: Sequence[str]) -> EncodedDocs:
        self.calls += 1
        return EncodedDocs(
            dense=[seeded_vector(t, self.dim) for t in texts],
            sparse=[self._sparse(t) for t in texts],
        )

    def encode_query(self, text: str) -> tuple[list[float], dict[int, float]]:
        return seeded_vector(text, self.dim), self._sparse(text)


class FakeReranker:
    """假重排器：按「与 query 的点积」打分（可构造相关性）；`fail=True` 时模拟不可用。"""

    def __init__(self, scores: Mapping[str, float] | None = None, *, fail: bool = False) -> None:
        self._scores = dict(scores or {})
        self._fail = fail
        self.calls = 0

    def rerank(self, query: str, docs: Sequence[str]) -> list[float]:
        from app.rag import RagNotReady

        if self._fail:
            raise RagNotReady("重排模型未安装（用例构造）")
        self.calls += 1
        out: list[float] = []
        for doc in docs:
            if doc in self._scores:
                out.append(float(self._scores[doc]))
            else:
                vec, _ = FakeEmbedder().encode_query(query)
                doc_vec = seeded_vector(doc, len(vec))
                out.append(sum(a * b for a, b in zip(vec, doc_vec, strict=True)))
        return out


@dataclass
class FakePoint:
    id: str
    vector: dict[str, Any]
    payload: dict


@dataclass
class _Leg:
    """单路召回的最小描述（真库里是 qdrant 的 Prefetch，单路时直接用向量当 query）。"""

    query: Any
    using: str
    limit: int


@dataclass
class _Result:
    points: list[Any]


@dataclass
class FakeQueryPoint:
    """模仿 qdrant_client 的 ScoredPoint：`payload` 与 `score` 是检索层唯一用到的字段。"""

    id: str
    payload: dict
    score: float


@dataclass
class FakeQdrant:
    """内存向量库。只实现本项目用到的面：upsert / delete / facet / query_points + 建库。"""

    points: dict[str, FakePoint] = field(default_factory=dict)
    collections: set[str] = field(default_factory=set)
    payload_indexes: dict[str, set[str]] = field(default_factory=dict)
    upsert_calls: int = 0
    delete_calls: int = 0
    #: 最近一次检索收到的过滤条件（用例据此断言「过滤确实发给了服务端」）
    last_filter: Any = None
    #: 最近一次检索用了几路召回（消融档位的断言点）
    last_prefetch_len: int = 0

    # ── 建库面 ──
    def get_collections(self) -> Any:
        return type("R", (), {"collections": list(self.collections)})()

    def collection_exists(self, name: str) -> bool:
        return name in self.collections

    def create_collection(self, collection_name: str, **_: Any) -> None:
        self.collections.add(collection_name)

    def delete_collection(self, collection_name: str) -> None:
        self.collections.discard(collection_name)
        self.points = {}  # 真 Qdrant 删库即删点；替身不能只删名字

    def create_payload_index(self, collection_name: str, field_name: str, field_schema: Any) -> None:
        self.payload_indexes.setdefault(collection_name, set()).add(field_name)

    # ── 写入面 ──
    def upsert(self, collection_name: str, points: Iterable[Any], wait: bool = True) -> None:
        self.upsert_calls += 1
        for point in points:
            pid = str(point.id)
            self.points[pid] = FakePoint(
                id=pid, vector=dict(point.vector), payload=dict(point.payload or {})
            )

    def delete(self, collection_name: str, points_selector: Any, wait: bool = True) -> None:
        self.delete_calls += 1
        day = _selector_day(points_selector)
        if day is None:
            return
        self.points = {
            pid: p for pid, p in self.points.items() if p.payload.get("day") != day
        }

    def facet(self, collection_name: str, key: str, limit: int = 10, exact: bool = False) -> Any:
        counts: dict[str, int] = {}
        for point in self.points.values():
            value = point.payload.get(key)
            if value is not None:
                counts[str(value)] = counts.get(str(value), 0) + 1
        hits = [type("H", (), {"value": k, "count": v})() for k, v in sorted(counts.items())]
        return type("F", (), {"hits": hits[:limit]})()

    def count(self, collection_name: str, exact: bool = True) -> Any:
        return type("C", (), {"count": len(self.points)})()

    # ── 检索面 ──
    def query_points(
        self,
        collection_name: str,
        *,
        prefetch: Sequence[Any] | None = None,
        query: Any = None,
        query_filter: Any = None,
        limit: int = 10,
        with_payload: bool = True,
        **_: Any,
    ) -> _Result:
        self.last_filter = query_filter
        as_of = _filter_as_of(query_filter)
        must = _filter_must(query_filter)

        candidates = [
            p for p in self.points.values() if _passes(p, must, as_of)
        ]

        legs_spec = list(prefetch or [])
        if not legs_spec and query is not None and not hasattr(query, "rrf"):
            # 单路检索：真库里是「不给 prefetch、直接把向量当 query」，等价于一路召回
            using = SPARSE_VECTOR if hasattr(query, "indices") else DENSE_VECTOR
            legs_spec = [_Leg(query=query, using=using, limit=limit)]
        self.last_prefetch_len = len(legs_spec)

        legs: list[list[FakePoint]] = []
        for leg in legs_spec:
            using = getattr(leg, "using", DENSE_VECTOR)
            scored = [(self._dot(p, leg.query, using), p) for p in candidates]
            # None = 该点在这条腿上没有向量（真 Qdrant 里就是搜不到）；
            # 相似度为 0 或负数的点**照常返回**——真实检索不会把负相似度丢掉。
            scored = [(s, p) for s, p in scored if s is not None]
            scored.sort(key=lambda pair: -pair[0])
            legs.append([p for _, p in scored[: getattr(leg, "limit", 100)]])

        if not legs:
            return _Result(points=[])

        k = _rrf_k(query)
        fused: dict[str, float] = {}
        for leg in legs:
            for rank, point in enumerate(leg):
                fused[point.id] = fused.get(point.id, 0.0) + 1.0 / (k + rank)

        ranked = sorted(
            (p for p in candidates if p.id in fused), key=lambda p: -fused[p.id]
        )
        return _Result(
            points=[
                FakeQueryPoint(id=p.id, payload=p.payload, score=fused[p.id])
                for p in ranked[:limit]
            ]
        )

    def _dot(self, point: FakePoint, query: Any, using: str) -> float | None:
        if isinstance(query, list):
            vec = point.vector.get(using)
            if vec is None:
                return None
            return sum(a * b for a, b in zip(vec, query, strict=False))
        # 稀疏腿：命中 token 的权重和；没有稀疏向量的点对这条腿隐身
        indices = set(getattr(query, "indices", []) or [])
        vec = point.vector.get(using)
        if vec is None or not indices:
            return None
        got = set(getattr(vec, "indices", []) or [])
        return float(len(indices & got))


def _selector_day(selector: Any) -> str | None:
    """从 FilterSelector 里取出 `day==X` 的值（按日删除的语义）。"""
    filt = getattr(selector, "filter", None)
    for condition in getattr(filt, "must", None) or []:
        match = getattr(condition, "match", None)
        if getattr(condition, "key", None) == "day" and match is not None:
            return str(getattr(match, "value", ""))
    return None


def _filter_must(query_filter: Any) -> list[Any]:
    return list(getattr(query_filter, "must", None) or [])


def _filter_as_of(query_filter: Any) -> datetime | None:
    for condition in _filter_must(query_filter):
        if getattr(condition, "key", None) == "available_at":
            rng = getattr(condition, "range", None)
            return getattr(rng, "lte", None)
    return None


def _rrf_k(query: Any) -> int:
    rrf = getattr(query, "rrf", None)
    return int(getattr(rrf, "k", None) or 2)


def _passes(point: FakePoint, must: Sequence[Any], as_of: datetime | None) -> bool:
    """照抄服务端过滤语义：满足全部 must 条件才进候选。"""
    for condition in must:
        key = getattr(condition, "key", None)
        value = point.payload.get(key)
        if key == "available_at":
            if as_of is None or value is None:
                return False
            if datetime.fromisoformat(str(value)) > as_of:
                return False
        elif getattr(condition, "match", None) is not None:
            match = condition.match
            wanted = getattr(match, "value", None)
            any_of = getattr(match, "any", None)
            if any_of:
                if not set(any_of) & set(value or []):
                    return False
            elif isinstance(value, list):
                # 数组字段（symbols / industries）用 MatchValue 时是「包含」语义
                if wanted not in value:
                    return False
            elif value != wanted:
                return False
    return True
