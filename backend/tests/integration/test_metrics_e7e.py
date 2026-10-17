"""E7e 集成：`/metrics` 在真库上能读到任务表的状态计数。

离线用例用 `TestClient(app)` 且不跑 lifespan，`app.state.db` 是 None——那条路证明不了
`GROUP BY status` 的 SQL 与 gauge 的接线。这条只打真库。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_metrics_reads_task_counts_from_real_db(real_stack: TestClient) -> None:
    response = real_stack.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-request-id"]
    text = response.text
    assert "quantsage_http_requests_total" in text
    # 端点每次 scrape 会把五个状态补齐（缺的置 0），所以这个 gauge 一定在场
    assert 'quantsage_tasks{status="queued"}' in text
    assert 'quantsage_tasks{status="running"}' in text
