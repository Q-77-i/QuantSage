"""M2b 数据接入端点离线单测：鉴权、并发冲突、异步触发的回话形状。

`runner.status` / `runner.run` 在这里被替换成桩——真跑要打网络（CLI 下载），
那是 integration 的事；这里验的是**端点的契约**：未登录 401、已在跑 409、
触发即回 202 而不是把 HTTP 请求挂几分钟。
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import etl as etl_api
from app.main import app

client = TestClient(app)

STATUS_STUB: dict[str, Any] = {
    "local": {"start": "2026-07-07", "end": "2026-09-29", "days": 85, "rows": 420000, "symbols": 5400},
    "archive": {"last_day": "2026-09-29", "manifest_version": "history-public-v1-x"},
    "gap": {"missing_days": [], "count": 0, "trading_day_count": 0},
    "last_run": {"scope": "daily", "result": "ok"},
}


@pytest.mark.usefixtures("jwt_secret")
def test_endpoints_require_login() -> None:
    assert client.get("/api/v1/etl/status").status_code == 401
    assert client.post("/api/v1/etl/run").status_code == 401


def test_status_merges_runner_and_scheduler(signed_in: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(etl_api.runner, "status", lambda data_dir=None: dict(STATUS_STUB))
    monkeypatch.setattr(etl_api.runner, "is_running", lambda: False)

    body = client.get("/api/v1/etl/status").json()

    assert body["local"]["end"] == "2026-09-29"
    assert body["gap"]["count"] == 0
    assert body["running"] is False
    assert body["scheduler"]["enabled"] is False  # 默认不启用定时
    assert body["scheduler"]["next_run_at"] is None


def test_run_conflicts_when_already_running(signed_in: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """并发跑会同时写同一批文件——必须挡在入口，不是靠「大概率不会撞上」。"""
    monkeypatch.setattr(etl_api.runner, "is_running", lambda: True)

    response = client.post("/api/v1/etl/run")

    assert response.status_code == 409
    assert "ETL 在执行" in response.json()["detail"]


def test_run_starts_in_background_and_answers_immediately(
    signed_in: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一轮要跑几十秒到几分钟，端点只负责起跑——不能把 HTTP 请求挂在那儿。"""
    called: list[int] = []
    monkeypatch.setattr(etl_api.runner, "is_running", lambda: False)
    monkeypatch.setattr(etl_api, "_safe_run", lambda trailing: called.append(trailing))

    response = client.post("/api/v1/etl/run")

    assert response.status_code == 202
    assert response.json()["status"] == "started"
    deadline = time.monotonic() + 2.0
    while not called and time.monotonic() < deadline:
        time.sleep(0.02)
    assert called, "后台线程没有跑起来"


def test_safe_run_swallows_errors(signed_in: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """后台线程里的异常没人接手——必须就地记日志，不能让它冒到 asyncio 的默认钩子上。"""

    def boom(trailing: int) -> None:
        raise RuntimeError("模拟下载失败")

    monkeypatch.setattr(etl_api.runner, "run", boom)

    etl_api._safe_run(7)  # 不抛即通过
