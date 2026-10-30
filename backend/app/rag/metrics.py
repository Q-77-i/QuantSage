"""RAG 指标（S3）：服务端队列/等待/降级与 API 侧降级计数。

**同一份定义两处注册**（RAG 服务与 API 进程各一个 registry）：
服务端记 `queue_full` / `timeout`（它才知道队列发生了什么）；
API 侧只记 `unavailable`（连接失败/客户端超时）——同一事件不两处计数。

指标名与标签按 SPEC §4.4；`class` 是请求分级（interactive / research / batch），
不是 Python 的 class。
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

RERANK_QUEUE_DEPTH = Gauge(
    "rag_rerank_queue_depth",
    "Rerank requests waiting for the single inference slot.",
)

RERANK_INFLIGHT = Gauge(
    "rag_rerank_inflight",
    "Rerank requests currently running (0 or 1; MPS is serial).",
)

RERANK_DEGRADED = Counter(
    "rag_rerank_degraded_total",
    "Rerank degradations by request class and reason.",
    ["class", "reason"],
)

#: S8：降级率的**分母**。入口即计——成功与过载都算；`unavailable` 由 API 侧记，
#: 不进这个比率（SPEC §9.3.1）。
RERANK_REQUESTS = Counter(
    "rag_rerank_requests_total",
    "Rerank requests entering the service (success and overload).",
    ["class"],
)

RERANK_WAIT = Histogram(
    "rag_rerank_wait_seconds",
    "Time spent waiting for the rerank slot, by request class.",
    ["class"],
    # 交互 1.5s / 研究 10s / 批量 600s：分桶覆盖三档
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 0.8, 1.0, 1.5, 2.5, 5, 10, 30, 60, 300, 600),
)

EMBED_DURATION = Histogram(
    "rag_embed_duration_seconds",
    "Embedding inference duration, by request class.",
    ["class"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300),
)

__all__ = [
    "EMBED_DURATION",
    "RERANK_DEGRADED",
    "RERANK_INFLIGHT",
    "RERANK_QUEUE_DEPTH",
    "RERANK_REQUESTS",
    "RERANK_WAIT",
]
