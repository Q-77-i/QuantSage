"""Prometheus 指标（E7e）：进程内计数器/直方图，`/metrics` 文本暴露。

**边界如实写在这里**：
- **单进程单 worker**：指标是进程内累计，重启清零。多实例要聚合（P4 的规模，不在单机形态）；
  本项目本来就有「单实例」假设（任务 worker、限流、熔断都在进程内），指标不另开例外。
- **不引 Prometheus server / 告警**：只暴露 `/metrics`，由外部 scrape；告警属于部署层。
- **LLM 的观测仍在 Langfuse**（trace / generation / token / 成本）——这里不重复计 token，
  避免两套口径各说各话。
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

#: 任务表的全部终态/中间态（`/metrics` 每次 scrape 都会把它们补齐，缺的显式置 0）
TASK_STATUSES = ("queued", "running", "done", "failed", "cancelled")

HTTP_REQUESTS = Counter(
    "quantsage_http_requests_total",
    "HTTP requests by method, route template and status.",
    ["method", "route", "status"],
)

HTTP_DURATION = Histogram(
    "quantsage_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "route"],
    # 覆盖普通 API（毫秒级）到 SSE（分钟级）；分桶按本项目实测的量级取
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)

TASKS = Gauge(
    "quantsage_tasks",
    "Task rows by status (scraped from the tasks table).",
    ["status"],
)

CACHE_HITS = Counter(
    "quantsage_cache_hits_total",
    "Data-layer cache hits by tier.",
    ["tier"],
)

CACHE_MISSES = Counter(
    "quantsage_cache_misses_total",
    "Data-layer cache misses (producer had to run).",
)
