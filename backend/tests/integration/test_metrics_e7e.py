"""E7e 集成：`/metrics` 在真库上能读到任务表的状态计数。

离线用例用 `TestClient(app)` 且不跑 lifespan，`app.state.db` 是 None——那条路证明不了
`GROUP BY status` 的 SQL 与 gauge 的接线。这条只打真库。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings

pytestmark = pytest.mark.integration


def test_metrics_reads_task_counts_from_real_db(real_stack: TestClient) -> None:
    # 本机 .env 常配 METRICS_TOKEN（Prometheus 用），CI 则是空串；两种配置都要能读。
    token = get_settings().metrics_token.get_secret_value()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = real_stack.get("/metrics", headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-request-id"]
    text = response.text
    assert "quantsage_http_requests_total" in text
    # 端点每次 scrape 会把五个状态补齐（缺的置 0），所以这个 gauge 一定在场
    assert 'quantsage_tasks{status="queued"}' in text
    assert 'quantsage_tasks{status="running"}' in text
