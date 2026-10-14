"""集成用例：网格/批量的断线不丢（P3-E1b）。

M5b 时代这条路径是**断线即丢**（「落库在 done，流没走完就不留记录」）——本文件的两条用例
就是那句话的反面证据，跑在真库 + 真 lifespan（worker 真在跑）上：

* **断开订阅**（等价于关掉标签页）：任务照跑完、`optimization_runs` 有记录、
  `GET /tasks/{id}/events` 能把同一份 cells 回放出来；
* **协作式中止**：`POST /tasks/{id}/cancel` 投得进去，任务收尾成 `cancelled`，
  且**没有**落记录（中止不是结果）。

运行方式（README）：`uv run pytest -m integration`（需容器与真实密钥）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.integration.conftest import purge_rows, sign_up, wait_for_task

pytestmark = pytest.mark.integration

SYMBOL = "600519"


def fresh_emails(prefix: str, count: int) -> list[str]:
    suffix = uuid.uuid4().hex[:8]
    return [f"e1b-{prefix}-{suffix}-{index}@example.com" for index in range(count)]


def grid_body(cells: int = 2) -> dict[str, Any]:
    """小网格：`cells × cells` 格，全期窗口（每格数百毫秒）。"""
    return {
        "strategy": "ma_cross",
        "symbol": SYMBOL,
        "params": {"slow": 20},
        "axes": [{"param": "fast", "values": list(range(3, 3 + cells))}],
        "pit_mode": "pit",
    }


def frames_of(body: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        if not block.startswith("event: "):
            continue
        head, _, data = block.partition("\ndata: ")
        out.append((head.removeprefix("event: "), json.loads(data)))
    return out


def test_grid_finishes_after_the_stream_is_dropped(real_stack: TestClient) -> None:
    """**关掉标签页**：不订阅、直接走开，任务照跑完并落库（M5b 时代这里是零记录）。"""
    email = fresh_emails("drop", 1)[0]
    try:
        member = sign_up(email)
        response = member.post("/api/v1/optimize/grid", json=grid_body())
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]

        # 此刻既不订阅也不轮询——就是「用户关掉了页面」
        task = wait_for_task(member, task_id, timeout=180)
        assert task["status"] == "done", task
        assert task["progress"] == {"done": 2, "total": 2}

        run_id = task["result"]["run_id"]
        detail = member.get(f"/api/v1/optimize/runs/{run_id}").json()
        assert len(detail["summary"]["cells"]) == 2
        # 落库的 `request` 是用户可见的那一份（执行材料 `exec` 已剔掉）
        assert "exec" not in detail["request"]
        assert detail["request"]["kind"] == "grid"

        # 回头订阅：从库里回放出同一份 cells（内容与在线时逐字同形）
        with member.stream("GET", f"/api/v1/tasks/{task_id}/events") as streamed:
            assert streamed.status_code == 200
            events = frames_of("".join(streamed.iter_text()))
        names = [name for name, _ in events]
        assert names[0] == "start" and names[-1] == "done"
        assert names.count("cell") == 2
        assert events[-1][1]["run_id"] == run_id
        assert sorted(payload["index"] for name, payload in events if name == "cell") == [0, 1]
    finally:
        purge_rows([email], [])


def test_cancel_stops_a_running_grid_without_saving(real_stack: TestClient) -> None:
    """协作式中止：信号投得进去、任务收尾成 `cancelled`、**没有记录**（中止不是结果）。

    中止信号在同进程里投递，所以这里断言 `cancelling` 为真或任务已结束（竞态窗口极小，
    但真跑起来不该假失败——与 `test_tasks_e1.py::test_cancel_then_worker_skips_it` 同姿态）。
    """
    email = fresh_emails("stop", 1)[0]
    try:
        member = sign_up(email)
        # 大一点的网格：给「点中止」留出窗口
        response = member.post(
            "/api/v1/optimize/grid",
            json={
                "strategy": "ma_cross",
                "symbol": SYMBOL,
                "params": {},
                "axes": [
                    {"param": "fast", "values": [3, 5, 8, 10]},
                    {"param": "slow", "values": [20, 30, 40, 50]},
                ],
                "pit_mode": "pit",
            },
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]

        cancelled = member.post(f"/api/v1/tasks/{task_id}/cancel")
        # 200 = 真取消：排队中直接置 cancelled，或跑着时装上了中止信号（`cancelling: true`）
        # 409 = 极窄的竞态（点太晚，已跑完）——真跑起来不该假失败，但那条路不算覆盖
        assert cancelled.status_code in {200, 409}, cancelled.text

        if cancelled.status_code == 200:
            task = wait_for_task(member, task_id, timeout=120)
            assert task["status"] == "cancelled", task
            assert task["result"] is None
            # **没有记录**：中止的结果不作为可引用的记录保留
            assert member.get("/api/v1/optimize/runs").json() == []
    finally:
        purge_rows([email], [])
