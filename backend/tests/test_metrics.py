"""E7e 可观测性：`/metrics`、request id、JSON 日志、缓存计数器。

进程内指标是全局累计的——断言一律用「前后差值」或「≥」，不依赖测试执行顺序。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import metrics
from app.core.config import get_settings
from app.core.logging import JsonFormatter, request_id_var
from app.core.metrics import CACHE_HITS
from app.data import cache
from app.main import app

client = TestClient(app)


def metric_value(text: str, prefix: str) -> float:
    for line in text.splitlines():
        if line.startswith(prefix):
            return float(line.rsplit(" ", 1)[-1])
    return 0.0


def sample_count(text: str, name: str) -> int:
    """只数样本行——`# HELP` / `# TYPE` 也含指标名，不能直接 `str.count`。"""
    return sum(1 for line in text.splitlines() if line.startswith(name + " "))


def test_metrics_endpoint_exposes_http_request_id_and_counters() -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.headers["x-request-id"]

    CACHE_HITS.labels(tier="l1").inc()
    metrics.set_task_counts({"queued": 2})
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert "quantsage_http_requests_total" in text
    assert 'route="/health"' in text
    assert "quantsage_cache_hits_total" in text
    assert 'quantsage_tasks{status="queued"} 2.0' in text
    # 自定义 collector 每个状态只产出一行（多进程聚合时也不能翻倍）
    assert text.count('quantsage_tasks{status="queued"}') == 1


def test_request_id_is_echoed_when_it_looks_safe() -> None:
    response = client.get("/health", headers={"X-Request-Id": "req-abc-123"})
    assert response.headers["x-request-id"] == "req-abc-123"


def test_unsafe_request_id_is_replaced() -> None:
    response = client.get("/health", headers={"X-Request-Id": "bad/value"})
    assert response.headers["x-request-id"] != "bad/value"
    assert len(response.headers["x-request-id"]) == 32  # uuid4().hex


def test_json_formatter_carries_request_id_and_access_fields() -> None:
    token = request_id_var.set("req-json-1")
    try:
        record = logging.LogRecord(
            "quantsage.http", logging.INFO, __file__, 1, "hello %s", ("world",), None
        )
        record.method = "GET"
        record.path = "/health"
        record.status = 200
        record.duration_ms = 1.23
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)

    assert payload["message"] == "hello world"
    assert payload["request_id"] == "req-json-1"
    assert payload["method"] == "GET"
    assert payload["path"] == "/health"
    assert payload["status"] == 200
    assert payload["duration_ms"] == pytest.approx(1.23)


def test_cache_through_counts_l1_hit_and_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缓存命中/未命中的计数器真的接在 `through` 上，而不是一个摆设。"""
    cache.clear_local()
    monkeypatch.setattr(cache, "_redis_get", lambda key: cache._MISS)
    monkeypatch.setattr(cache, "_redis_set", lambda key, value: None)
    calls: list[int] = []

    def producer() -> dict[str, int]:
        calls.append(1)
        return {"x": 1}

    before = metrics.render().decode()
    cache.through("probe", (1,), producer, data_dir=tmp_path)
    cache.through("probe", (1,), producer, data_dir=tmp_path)
    after = metrics.render().decode()

    assert len(calls) == 1  # 第二次是 L1 命中，producer 不再跑
    assert metric_value(after, "quantsage_cache_misses_total") > metric_value(
        before, "quantsage_cache_misses_total"
    )
    assert metric_value(after, 'quantsage_cache_hits_total{tier="l1"}') > metric_value(
        before, 'quantsage_cache_hits_total{tier="l1"}'
    )
    cache.clear_local()


def test_metrics_requires_token_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配了 METRICS_TOKEN 就要求请求头；否则公网 scrape 面白送队列深度。"""
    monkeypatch.setenv("METRICS_TOKEN", "metrics-secret-value")
    get_settings.cache_clear()
    try:
        assert client.get("/metrics").status_code == 403
        allowed = client.get(
            "/metrics", headers={"X-Metrics-Token": "metrics-secret-value"}
        )
        assert allowed.status_code == 200
        # S8：容器 scrape 走 Bearer；旧头保留
        bearer = client.get(
            "/metrics", headers={"Authorization": "Bearer metrics-secret-value"}
        )
        assert bearer.status_code == 200
        assert (
            client.get(
                "/metrics", headers={"Authorization": "Bearer wrong-value"}
            ).status_code
            == 403
        )
    finally:
        get_settings.cache_clear()


def test_snapshot_gauges_are_rendered_once() -> None:
    """S8：一次 scrape 每个 Gauge 只出一条（多进程聚合时也不能翻倍）。"""
    metrics.set_snapshot(
        {
            "pg_connections": 12,
            "host_available_bytes": 5e9,
            "host_swap_used_bytes": 9.6e9,
            "backup_age_seconds": 3600,
        }
    )
    try:
        text = metrics.render().decode()
    finally:
        metrics.set_snapshot({})
    for name in (
        "quantsage_pg_connections",
        "quantsage_host_available_bytes",
        "quantsage_host_swap_used_bytes",
        "quantsage_backup_age_seconds",
    ):
        assert sample_count(text, name) == 1


def test_backup_age_uses_the_manifest_mtime(tmp_path: Path) -> None:
    """S8：年龄取 mtime，不取文件名日期（否则每日 03:30 前必假报）。"""
    assert metrics.backup_age_seconds(tmp_path) == 1e9

    manifests = tmp_path / "manifests"
    manifests.mkdir()
    manifest = manifests / "20260101.json"
    manifest.write_text("{}", encoding="utf-8")
    old = 1_700_000_000.0
    os.utime(manifest, (old, old))
    assert metrics.backup_age_seconds(tmp_path, now=old + 3600) == pytest.approx(3600)


def test_metrics_endpoint_exposes_host_and_backup_gauges() -> None:
    text = client.get("/metrics").text
    for name in (
        "quantsage_host_available_bytes",
        "quantsage_host_swap_used_bytes",
        "quantsage_backup_age_seconds",
    ):
        assert sample_count(text, name) == 1


def test_queue_histogram_is_observed_on_claim() -> None:
    from app.tasks.worker import _observe_queue_wait

    prefix = 'quantsage_task_queue_seconds_count{kind="probe"}'
    before = metric_value(metrics.render().decode(), prefix)
    started = datetime.now(UTC)
    _observe_queue_wait(
        {
            "kind": "probe",
            "created_at": started - timedelta(seconds=2),
            "started_at": started,
        }
    )
    after = metric_value(metrics.render().decode(), prefix)
    assert after == before + 1


def test_backup_status_reports_partial_and_failures(tmp_path: Path) -> None:
    """P1：partial manifest 的失败项必须可观测；年龄仍按 mtime 算。"""
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    manifest = manifests / "20260101.json"
    manifest.write_text(
        '{"partial": true, "failures": ["pg: down", "data: rsync failed"]}',
        encoding="utf-8",
    )
    old = 1_700_000_000.0
    os.utime(manifest, (old, old))

    status = metrics.backup_status(tmp_path, now=old + 60)

    assert status["backup_age_seconds"] == pytest.approx(60)
    assert status["backup_partial"] == 1.0
    assert status["backup_failures"] == 2.0


def test_backup_status_without_manifest_is_stale_and_not_partial(tmp_path: Path) -> None:
    status = metrics.backup_status(tmp_path)
    assert status["backup_age_seconds"] == 1e9
    assert status["backup_partial"] == 0.0
    assert status["backup_failures"] == 0.0
