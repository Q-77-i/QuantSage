"""离线单测：任务端点与提交契约（P3-E1a）。

本文件守四件事：

  * **提交不等引擎**：`POST /api/v1/backtest` 回 202 + `{task_id}`，此刻库里只有一条
    `queued` 行、没有任何回测记录；
  * **闸门仍在提交期**：非法请求（401 / 404 / 422）**一条任务都不建**——这是 M4c
    「运行前带行号给话术」与整张既有异常映射矩阵不退化的前提；
  * **冻结材料**：提交后改策略源码，跑的仍是**提交那一刻**的代码（`code_sha256` 为证）；
  * **取消与归属**：`queued` 可取消、`running` 与终态 409；越权与不存在同为 404。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.quota import TaskAdmissionBusy
from app.data import duckdb_client
from app.main import app
from app.strategy.templates import template_source
from tests.conftest import TEST_USER, drive_tasks, make_backtest_dir, trading_days
from tests.fakes import FakeDatabase

client = TestClient(app)

SYMBOL = "600519"
START = date(2026, 7, 1)
BAR_COUNT = 60
#: 取件/终态写入的 worker 标识（P4-S2：终态必须带 `worker_id` 栅栏）
WID = "test-worker"


def sample_bars() -> list[dict[str, Any]]:
    return [
        {"trade_date": day, "open": 100.0 + i, "close": 100.5 + i, "high": 101.0 + i, "low": 99.0 + i}
        for i, day in enumerate(trading_days(START, BAR_COUNT))
    ]


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    make_backtest_dir(tmp_path, sample_bars(), [], symbol=SYMBOL)
    monkeypatch.setattr(duckdb_client, "resolve_data_dir", lambda _=None: tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))  # 用户策略分支的子进程读它
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_admission_busy_maps_to_503(
    signed_in: FakeDatabase, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """撞槽用尽不是 429 配额满：503 + Retry-After，前端可重试。"""

    async def busy(*args: Any, **kwargs: Any) -> None:
        raise TaskAdmissionBusy("任务准入繁忙，请稍后重试")

    monkeypatch.setattr(signed_in, "create_task", busy)

    response = submit()

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["kind"] == "task_admission_busy"


def submit(**overrides: Any):
    return client.post(
        "/api/v1/backtest", json={"strategy": "ma_cross", "symbol": SYMBOL, **overrides}
    )


def make_strategy(code: str, name: str = "我的策略") -> dict[str, Any]:
    response = client.post("/api/v1/strategies", json={"name": name, "code": code})
    assert response.status_code == 201, response.text
    return response.json()


def task_of(db: FakeDatabase, body: dict[str, Any]) -> dict[str, Any]:
    return db.tasks[body["task_id"]]


# ── 提交契约 ────────────────────────────────────────────────────────────────


def test_submit_returns_202_without_running(signed_in: FakeDatabase, data_dir: Path) -> None:
    response = submit()

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"
    # 此刻：任务在、报告不在（同步时代这里已经跑完了）
    assert task_of(signed_in, response.json())["status"] == "queued"
    assert signed_in.runs == {}


def test_poll_reaches_done_and_report_is_fetchable(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    submitted = submit(pit_mode="both").json()
    # 还没跑：详情是 queued，前端据此显示「排队中」
    assert client.get(f"/api/v1/tasks/{submitted['task_id']}").json()["status"] == "queued"

    assert drive_tasks(signed_in) == 1
    task = client.get(f"/api/v1/tasks/{submitted['task_id']}").json()
    assert task["status"] == "done"
    assert task["progress"] == {"done": 1, "total": 1}
    assert task["kind"] == "backtest"

    detail = client.get(f"/api/v1/backtest/runs/{task['result']['run_id']}").json()
    assert detail["report"]["meta"]["symbol"] == SYMBOL
    # 报告经**既有**端点取回，落库的 request 里不该出现执行字段
    for field in ("source", "strategy_name", "uses_events", "compare_pit"):
        assert field not in detail["request"]


def test_gates_run_before_enqueue(signed_in: FakeDatabase, data_dir: Path) -> None:
    """422 / 404 各来一发：**一条任务都不许建**（建了就说明闸门被挪到 worker 里了）。"""
    assert submit(strategy="no_such").status_code == 422
    assert submit(params={"fast": 20, "slow": 5}).status_code == 422
    assert submit(symbol="000001").status_code == 404  # 本地无该标的行情
    assert submit(params={"nope": 1}).status_code == 422

    assert signed_in.tasks == {}
    assert signed_in.runs == {}


def test_user_strategy_gates_run_before_enqueue(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    assert client.post(
        "/api/v1/backtest", json={"strategy": "user", "symbol": SYMBOL}
    ).status_code == 422  # 缺 strategy_id（模型校验器挡的）

    unknown = "00000000-0000-0000-0000-000000000000"
    assert client.post(
        "/api/v1/backtest", json={"strategy": "user", "strategy_id": unknown, "symbol": SYMBOL}
    ).status_code == 404

    broken = make_strategy("def on_bar(ctx):\n    return\nimport duckdb\n")
    response = client.post(
        "/api/v1/backtest",
        json={"strategy": "user", "strategy_id": broken["id"], "symbol": SYMBOL},
    )
    assert response.status_code == 422
    assert response.json()["findings"]  # 「运行前带行号给话术」：findings 照常交出去

    assert signed_in.tasks == {}


def test_frozen_source_is_what_runs(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    """提交后改源码：跑的仍是**提交那一刻**的代码（异步化引入的窗口，SPEC §2.1）。"""
    original = make_strategy(template_source("ma_cross"), name="冻结前")
    submitted = client.post(
        "/api/v1/backtest",
        json={"strategy": "user", "strategy_id": original["id"], "symbol": SYMBOL},
    ).json()
    assert submitted["status"] == "queued"

    # 提交之后、执行之前：源码被改（换成另一套模板）
    changed = client.put(
        f"/api/v1/strategies/{original['id']}", json={"code": template_source("rsi_reversal")}
    )
    assert changed.status_code == 200
    assert changed.json()["code_sha256"] != original["code_sha256"]

    assert drive_tasks(signed_in) == 1
    task = client.get(f"/api/v1/tasks/{submitted['task_id']}").json()
    detail = client.get(f"/api/v1/backtest/runs/{task['result']['run_id']}").json()
    assert detail["code_sha256"] == original["code_sha256"]  # 跑的是冻结的那份
    # 执行字段（源码 / 策略名 / 事件开关 / 对比开关）一律不进回测记录表
    for field in ("source", "strategy_name", "uses_events", "compare_pit"):
        assert field not in detail["request"]


# ── 归属与列表 ──────────────────────────────────────────────────────────────


def test_tasks_are_scoped_to_the_owner(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    task_id = submit().json()["task_id"]
    drive_tasks(signed_in)
    signed_in.tasks[task_id]["user_id"] = 2  # 当成别人的任务

    assert client.get("/api/v1/tasks").json() == []
    assert client.get(f"/api/v1/tasks/{task_id}").status_code == 404
    assert client.post(f"/api/v1/tasks/{task_id}/cancel").status_code == 404


def test_tasks_list_is_newest_first_and_limited(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    first = submit().json()["task_id"]
    second = submit(pit_mode="non_pit").json()["task_id"]

    assert [row["id"] for row in client.get("/api/v1/tasks").json()] == [second, first]
    summary = client.get("/api/v1/tasks").json()[0]
    assert summary["symbol"] == SYMBOL and summary["strategy"] == "ma_cross"
    assert "request" not in summary  # 列表不带整份 request（源码在里面）

    assert client.get("/api/v1/tasks", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/tasks", params={"limit": 101}).status_code == 422


def test_endpoints_require_login(jwt_secret: Any) -> None:
    uid = "00000000-0000-0000-0000-000000000000"
    assert client.get("/api/v1/tasks").status_code == 401
    assert client.get(f"/api/v1/tasks/{uid}").status_code == 401
    assert client.post(f"/api/v1/tasks/{uid}/cancel").status_code == 401


def test_malformed_task_id_is_422(signed_in: FakeDatabase) -> None:
    assert client.get("/api/v1/tasks/not-a-uuid").status_code == 422
    assert client.post("/api/v1/tasks/not-a-uuid/cancel").status_code == 422
    assert client.get("/api/v1/tasks/00000000-0000-0000-0000-000000000000").status_code == 404


# ── 取消 ────────────────────────────────────────────────────────────────────


def test_cancel_queued_task(signed_in: FakeDatabase, data_dir: Path) -> None:
    task_id = submit().json()["task_id"]

    response = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    # 取消后再驱动：没有活干，也不落记录
    assert drive_tasks(signed_in) == 0
    assert signed_in.runs == {}
    # 幂等：再取消一次仍是 200（状态本来就是 cancelled）
    assert client.post(f"/api/v1/tasks/{task_id}/cancel").status_code == 200


def test_cancel_running_cooperative_task_writes_db_flag(signed_in: FakeDatabase) -> None:
    """running 的网格/批量：取消写成 DB 标志，不再依赖请求落到 worker 进程。"""
    task_id = str(uuid.uuid4())
    asyncio.run(signed_in.create_task(task_id, TEST_USER["id"], "grid", {"symbol": SYMBOL}))
    asyncio.run(signed_in.claim_next_task(WID, lease_seconds=60))  # worker 已取走，正在跑

    response = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert response.status_code == 200
    body = response.json()
    assert body["cancelling"] is True
    assert body["status"] == "running"
    assert signed_in.tasks[task_id]["cancel_requested_at"] is not None
    # 幂等：再取消一次仍返回 cancelling
    assert client.post(f"/api/v1/tasks/{task_id}/cancel").json()["cancelling"] is True


def test_cancel_running_or_finished_is_409(
    signed_in: FakeDatabase, data_dir: Path
) -> None:
    task_id = submit().json()["task_id"]
    claimed = asyncio.run(
        signed_in.claim_next_task(WID, lease_seconds=60)
    )  # 模拟「worker 已取走，正在跑」
    assert claimed is not None
    running = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert running.status_code == 409
    assert "执行" in running.json()["detail"]

    asyncio.run(
        signed_in.finish_task(
            task_id,
            claim_token=str(claimed["claim_token"]),
            result={"run_id": "whatever"},
        )
    )
    done = client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert done.status_code == 409
    assert "已结束" in done.json()["detail"]
