"""Prometheus 指标（E7e / S5）：进程内计数器/直方图，`/metrics` 文本暴露。

**两种形态**：

- **单进程**（默认）：指标注册在默认 registry，`render()` 直接 `generate_latest()`。
- **多进程**（`PROMETHEUS_MULTIPROC_DIR` 存在）：prometheus_client 把每个进程的值写进
  mmap 文件；`render()` 用 `MultiProcessCollector` 聚合。目录由 `dev.sh`/`prod.sh`
  每次启动清空重建，且只给后端子进程设置（RAG 有自己的 registry）。

**任务状态不按进程各算一份**：`TASKS` 没有做成模块级 Gauge——多进程下每个 worker
`set()` 一次会在聚合时翻倍。改为 `TaskCountsCollector`：scrape 时读一次
`set_task_counts()` 的快照，只产出一份序列。

**死进程清理**：worker 被 kill -9 时没有机会 `mark_process_dead`，它留下的 mmap 文件
会让 Counter 继续累加。**只在 worker 启动（uvicorn 补进程时）扫一遍目录**，按 pid 存活
判定清掉；不在 scrape 里删——否则会和另一个进程的并发 scrape 抢同一个 mmap 文件。

**边界如实写在这里**：
- 单机多进程（S5），不是多机聚合；指标仍是进程组的累计，重启清零。
- 不引 Prometheus server / 告警：只暴露 `/metrics`，由外部 scrape；告警属于部署层。
- **LLM 的观测仍在 Langfuse**（trace / generation / token / 成本）——这里不重复计 token。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import GaugeMetricFamily

log = logging.getLogger(__name__)

#: 任务表的全部终态/中间态（`/metrics` 每次 scrape 都会把它们补齐，缺的显式置 0）
TASK_STATUSES = ("queued", "running", "done", "failed", "cancelled")

#: `PROMETHEUS_MULTIPROC_DIR` 在 import 时读取——prometheus_client 的指标对象也在这个
#: 时刻决定用不用 mmap。所以它必须由启动脚本在进程环境里设置，不是运行时开关。
MULTIPROC = bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))

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

CACHE_HITS = Counter(
    "quantsage_cache_hits_total",
    "Data-layer cache hits by tier.",
    ["tier"],
)

CACHE_MISSES = Counter(
    "quantsage_cache_misses_total",
    "Data-layer cache misses (producer had to run).",
)

#: 共享限流降级次数。coord 不可用/熔断时，进程内额度按 floor(capacity/N) 收紧。
RATE_LIMIT_DEGRADED = Counter(
    "quantsage_rate_limit_degraded_total",
    "Rate-limit checks served by the in-process fallback.",
    ["reason"],
)

#: 跨进程进度：由 DB 轮询补出的格数（非 master worker 的 SSE 走这条）。
TASK_PROGRESS_DB_CELLS = Counter(
    "quantsage_task_progress_db_cells_total",
    "Task progress cells emitted from DB polling.",
)

#: 最近一次 scrape 读到的任务状态计数。`TaskCountsCollector` 只读它，不查库。
_task_counts: dict[str, int] = {}


def set_task_counts(counts: dict[str, int]) -> None:
    """更新任务状态快照（`/metrics` handler 在 render 之前调用）。"""
    _task_counts.clear()
    _task_counts.update(counts)


class TaskCountsCollector:
    """把 DB 快照翻成 `quantsage_tasks{status=...}`，每个 scrape 只产出一份。"""

    def collect(self) -> Any:
        family = GaugeMetricFamily(
            "quantsage_tasks",
            "Task rows by status (scraped from the tasks table).",
            labels=["status"],
        )
        for status in TASK_STATUSES:
            family.add_metric([status], float(_task_counts.get(status, 0)))
        yield family


if not MULTIPROC:
    # 单进程：默认 registry 里已经有各指标；任务计数用自定义 collector 替代 Gauge。
    REGISTRY.register(TaskCountsCollector())


def render() -> bytes:
    """渲染 `/metrics` 文本。多进程时临时 registry 只含聚合 collector。"""
    if not MULTIPROC:
        return generate_latest()
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    registry.register(TaskCountsCollector())
    return generate_latest(registry)


def _pid_of(filename: str) -> int | None:
    stem = filename.rsplit(".", 1)[0]
    _, _, tail = stem.rpartition("_")
    try:
        return int(tail)
    except ValueError:
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 别人的进程：不碰
    return True


def cleanup_dead_processes() -> None:
    """清掉已死 pid 的 mmap 文件，防止 kill -9 后 Counter 继续累加。

    **只在 worker 启动时调用**：scrape 期间调用会和另一个进程的 `MultiProcessCollector`
    抢同一个文件（list 之后被删 → `FileNotFoundError`），所以不放进 `render()`。

    `prometheus_client.multiprocess.mark_process_dead()` 只处理 live-gauge 的
    `gauge_*_<pid>.db`，**不删 Counter/Histogram 文件**；这里按文件名里的 pid 直接删。
    """
    if not MULTIPROC:
        return
    directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not directory:
        return
    try:
        names = os.listdir(directory)
    except OSError:
        return
    me = os.getpid()
    for name in names:
        if not name.endswith(".db"):
            continue
        pid = _pid_of(name)
        if pid is None or pid == me or _alive(pid):
            continue
        try:
            os.remove(os.path.join(directory, name))
        except OSError as exc:  # noqa: BLE001 —— 清理失败只记一条，不挡 scrape
            log.debug("清理死进程指标文件失败（%s: %s）", pid, type(exc).__name__)
