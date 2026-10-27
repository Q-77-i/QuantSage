"""检索管线：PIT 硬过滤 → 双路召回（dense ∥ sparse）→ 服务端 RRF → 精排 → top-k。

四条口径，每条都对得上一个具体的失败模式：

1. **PIT 过滤必须写进 Qdrant 的服务端 filter**，不能「先取 top-100 再本地过滤」。
   后者会让「该时点还不知道」的事件先占住候选位，把合法结果挤出去——结果看起来
   「没有未来事件」，但召回已经塌了，而且**不报错**。这是护城河的实现细节，不是优化项。
2. **RRF 的 k 显式传 61**：Qdrant 的默认值是 2，且它的公式是 `1/(k+rank)`（rank 从 0 起），
   论文的 k=60 等价于这里的 `k=61`。不显式传就等于悄悄换了融合算法。
3. **精排失败降级为 RRF 顺序**，并在返回里如实标出 `score_kind`——排序质量下降可以被接受，
   被隐瞒不行。
4. **`as_of` 缺省为「现在」**：对实时问答，语料里所有 `available_at` 都不晚于现在，
   过滤等于无约束；它的价值在回放与「那时候市场看到了什么」这类问题里。
   传进来的裸时间（无时区）按**北京时间**解释——语料是 A 股市场，轴就是它。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from qdrant_client import models

from app.backtest.types import CN_TZ
from app.core.config import get_settings
from app.rag import RagNotReady, RagOverloaded
from app.rag import collection as col
from app.rag import metrics as rag_metrics
from app.rag.clients import get_embedder, get_reranker
from app.rag.protocols import Embedder, Reranker

log = logging.getLogger(__name__)

#: 每路召回的候选数（融合前）
DENSE_LIMIT = 100
SPARSE_LIMIT = 100
#: RRF 的 k：Qdrant 的 rank 从 0 起，论文 k=60 ⇒ 这里 61（见模块 docstring）
RRF_K = 61
#: 消融用的三档管线。**评测的基线定义在这三档上**（SPEC §4 M3c）：
#: dense 只看稠密；hybrid 加稀疏并经 RRF 融合；rerank 再经 cross-encoder 精排。
MODE_DENSE = "dense"
MODE_HYBRID = "hybrid"
MODE_RERANK = "rerank"


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """一次检索请求。`as_of` 为 None 时取「现在」。"""

    query: str
    as_of: datetime | None = None
    symbol: str | None = None
    event_type: str | None = None
    industries: tuple[str, ...] = ()
    min_importance: float | None = None
    top_k: int | None = None


@dataclass(frozen=True, slots=True)
class RetrievedEvent:
    """检索结果。**双时间戳并列 + 来源三元组**是 PRD §5 的硬性要求，M7 证据面板直接消费。"""

    event_id: str
    day: str
    title: str
    summary: str | None
    event_time: str | None
    available_at: str | None
    event_type: str | None
    direction_norm: str | None
    importance_score: float | None
    symbols: tuple[str, ...]
    industries: tuple[str, ...]
    source: str | None
    original_source: str | None
    source_url: str | None
    content_hash: str | None
    text: str
    score: float
    score_kind: str  # "rerank" | "rrf" | "dense"
    #: 精排降级原因（`queue_full` / `timeout` / `unavailable`）；未降级为 None。
    #: `score_kind="rrf"` 在 hybrid 模式下是正常档位，只有这个字段非空才表示「降级」。
    degraded_reason: str | None = None


#: 降级原因 → 一句人话（机器码不进给模型/用户看的文本；两个调用面共用一份）
DEGRADED_LABELS = {
    "queue_full": "队列满",
    "timeout": "等待超时",
    "unavailable": "服务不可用",
}


def degraded_label(reason: str) -> str:
    return DEGRADED_LABELS.get(reason, reason)


def parse_as_of(value: str | None) -> datetime | None:
    """`YYYY-MM-DD` 或完整 ISO 时间 → datetime；未传（或空白）→ `None`；解析失败抛 `ValueError`。

    **两个调用方共用这一份**（chat 的 `search_events` 工具与对外 MCP Server）：接受哪些写法是
    **对外契约**，各写一份迟早漂移。裸时间（只有日期、或不带时区的 ISO）返回**不带时区**的
    datetime，由 `resolve_as_of` 按北京时间补齐——「解释成哪个时区」只该有一处口径，故两步分开。

    解析失败**抛**而不是返回哨兵：两个调用方各有各的报错通道（工具返回一句人话 / MCP 返回
    结构化错误），把选择权留给它们。
    """
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if len(text) == 10:
        return datetime.combine(date.fromisoformat(text), datetime.min.time())
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def resolve_as_of(value: datetime | None) -> datetime:
    """`as_of` 归一：缺省为现在；裸时间按北京时间补齐时区。"""
    if value is None:
        return datetime.now(CN_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=CN_TZ)
    return value


def build_filter(spec: SearchQuery) -> models.Filter:
    """构造服务端过滤。`available_at <= as_of` **必带**，其余可选。"""
    must: list[Any] = [
        models.FieldCondition(
            key="available_at",
            range=models.DatetimeRange(lte=resolve_as_of(spec.as_of)),
        )
    ]
    if spec.symbol:
        must.append(models.FieldCondition(key="symbols", match=models.MatchValue(value=spec.symbol)))
    if spec.event_type:
        must.append(
            models.FieldCondition(key="event_type", match=models.MatchValue(value=spec.event_type))
        )
    if spec.industries:
        must.append(
            models.FieldCondition(
                key="industries", match=models.MatchAny(any=list(spec.industries))
            )
        )
    if spec.min_importance is not None:
        must.append(
            models.FieldCondition(
                key="importance_score", range=models.Range(gte=float(spec.min_importance))
            )
        )
    return models.Filter(must=must)


def _to_event(
    point: Any, score: float, kind: str, degraded_reason: str | None = None
) -> RetrievedEvent:
    payload: Mapping[str, Any] = point.payload or {}
    return RetrievedEvent(
        event_id=str(payload.get("event_id") or ""),
        day=str(payload.get("day") or ""),
        title=str(payload.get("title") or ""),
        summary=payload.get("summary"),
        event_time=payload.get("event_time"),
        available_at=payload.get("available_at"),
        event_type=payload.get("event_type"),
        direction_norm=payload.get("direction_norm"),
        importance_score=payload.get("importance_score"),
        symbols=tuple(payload.get("symbols") or ()),
        industries=tuple(payload.get("industries") or ()),
        source=payload.get("source"),
        original_source=payload.get("original_source"),
        source_url=payload.get("source_url"),
        content_hash=payload.get("content_hash"),
        text=str(payload.get("text") or ""),
        score=float(score),
        score_kind=kind,
        degraded_reason=degraded_reason,
    )


def search(
    spec: SearchQuery,
    *,
    client=None,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    mode: str = MODE_RERANK,
    request_class: str = "interactive",
) -> list[RetrievedEvent]:
    """跑检索管线。依赖不可用抛 `RagNotReady`；精排不可用则降级为 RRF 顺序。

    `mode` 是消融开关（默认三档里最完整的一档），生产路径与评测路径共用同一份代码——
    评测另写一套实现的话，量出来的就不是线上跑的东西了。

    `request_class`（`interactive` / `research`）只影响远程精排的排队上限与超时（SPEC §4.3）；
    降级时原因写进每条结果的 `degraded_reason`，调用方不得只写日志。
    """
    settings = get_settings()
    top_k = spec.top_k or settings.rag_top_k
    candidates = settings.rag_rerank_candidates
    client = client or col.get_client()
    embedder = embedder or get_embedder()

    dense_vec, sparse = embedder.encode_query(spec.query)
    prefetch = [models.Prefetch(query=dense_vec, using=col.DENSE_VECTOR, limit=DENSE_LIMIT)]
    if sparse and mode != MODE_DENSE:
        indices = sorted(sparse)
        prefetch.append(
            models.Prefetch(
                query=models.SparseVector(
                    indices=indices, values=[float(sparse[i]) for i in indices]
                ),
                using=col.SPARSE_VECTOR,
                limit=SPARSE_LIMIT,
            )
        )
    # **单路也走 prefetch**：本 collection 只有命名向量（dense / sparse），
    # 把裸向量当 `query` 传会让 Qdrant 去找「默认向量」而报
    # `Not existing vector name error: ''`（实测）。RRF 对单列表排序不改变名次，
    # 所以 dense 档的语义不变，只是省掉了一条会踩坑的特殊分支。
    fused = _query(
        client,
        prefetch=prefetch,
        query=models.RrfQuery(rrf=models.Rrf(k=RRF_K)),
        spec=spec,
        limit=max(candidates, top_k),
    )
    if not fused:
        return []

    # score_kind 描述的是**分数从哪来**（余弦 / 融合名次 / 精排分），不是管线档位名
    fused_kind = "dense" if len(prefetch) == 1 else "rrf"
    if mode != MODE_RERANK:
        return [_to_event(p, p.score, fused_kind) for p in fused[:top_k]]

    reranker = reranker or get_reranker()
    head, tail = fused[:candidates], fused[candidates:]
    try:
        scores = reranker.rerank(
            spec.query,
            [str((p.payload or {}).get("text") or "") for p in head],
            request_class=request_class,
        )
    except RagOverloaded as exc:
        # 过载（队列满/排队超时）：原因来自服务端，如实带给调用方。
        # **指标不在这里记**：服务端已按 queue_full/timeout 计数，API 只记 unavailable（§4.4）。
        log.warning("精排过载（%s），降级为 RRF 顺序", exc.reason)
        return [_to_event(p, p.score, "rrf", exc.reason) for p in fused[:top_k]]
    except Exception as exc:  # noqa: BLE001 —— 精排失败（未就绪 / OOM / 计算异常）都要降级
        # 降级可以，隐瞒不行：score_kind 与 degraded_reason 都会如实写进返回值。
        # 只捕 `RagNotReady` 不够：模型载入后的 torch/MPS OOM 或 compute_score 异常
        # 同样属于「精排失败」。
        rag_metrics.RERANK_DEGRADED.labels(**{"class": request_class, "reason": "unavailable"}).inc()
        log.warning("精排失败，降级为 RRF 顺序：%s", exc)
        return [_to_event(p, p.score, "rrf", "unavailable") for p in fused[:top_k]]

    ranked = sorted(zip(head, scores, strict=True), key=lambda pair: pair[1], reverse=True)
    events = [_to_event(p, s, "rerank") for p, s in ranked[:top_k]]
    events.extend(_to_event(p, p.score, "rrf") for p in tail[: max(0, top_k - len(events))])
    return events[:top_k]


def _query(client, *, prefetch, query, spec: SearchQuery, limit: int):
    """发查询，并把「collection 不存在」翻译成可执行的人话。

    Qdrant 那边只会回一个 `UnexpectedResponse`——对排障没有信息量，
    而真实原因往往是「索引还没建」（先跑 `scripts/embed_events.py`）。
    """
    from qdrant_client.http.exceptions import UnexpectedResponse

    try:
        return client.query_points(
            collection_name=col.collection_name(),
            prefetch=prefetch,
            query=query,
            query_filter=build_filter(spec),
            limit=limit,
            with_payload=True,
        ).points
    except UnexpectedResponse as exc:
        if getattr(exc, "status_code", None) == 404:
            raise RagNotReady(
                f"索引 collection 不存在（{col.collection_name()}）："
                "先跑 scripts/embed_events.py 建索引"
            ) from exc
        raise


def coverage_window(*, data_dir=None) -> tuple[str | None, str | None]:
    """本地语料覆盖区间（起点固化、终点随日增前移）。

    从清单里查，**不写死「最近 3 个月」**——那会让人以为「等三个月就能回测 2020 年」。
    """
    from app.rag.embed import load_manifest

    manifest = load_manifest(data_dir)
    if not manifest:
        return None, None
    days: Sequence[str] = sorted(manifest)
    return days[0], days[-1]
