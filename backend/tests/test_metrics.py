"""E7e 可观测性：`/metrics`、request id、JSON 日志、缓存计数器。

进程内指标是全局累计的——断言一律用「前后差值」或「≥」，不依赖测试执行顺序。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from app.core.config import get_settings
from app.core.logging import JsonFormatter, request_id_var
from app.core.metrics import CACHE_HITS, TASKS
from app.data import cache
from app.main import app

client = TestClient(app)


def metric_value(text: str, prefix: str) -> float:
    for line in text.splitlines():
        if line.startswith(prefix):
            return float(line.rsplit(" ", 1)[-1])
    return 0.0


def test_metrics_endpoint_exposes_http_request_id_and_counters() -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.headers["x-request-id"]

    CACHE_HITS.labels(tier="l1").inc()
    TASKS.labels(status="queued").set(2)
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert "quantsage_http_requests_total" in text
    assert 'route="/health"' in text
    assert "quantsage_cache_hits_total" in text
    assert 'quantsage_tasks{status="queued"} 2.0' in text


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

    before = generate_latest().decode()
    cache.through("probe", (1,), producer, data_dir=tmp_path)
    cache.through("probe", (1,), producer, data_dir=tmp_path)
    after = generate_latest().decode()

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
    finally:
        get_settings.cache_clear()
