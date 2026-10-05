"""离线单测：健康探针（不启动 lifespan、不连数据库）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "quantsage-backend"
    assert body["version"] == app.version


def test_ready_reports_degraded_without_dependencies() -> None:
    """未启动 lifespan 时依赖必然未就绪 —— 就绪探针应返回 503 而不是 500。"""
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "postgres": False}
