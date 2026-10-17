"""离线单测：进度总线与任务事件流（P3-E1b）。

这两件是网格/批量「断线不丢」的实现底座，单独测比隔着 HTTP 测清楚得多：

* **总线**：发布 → 缓冲累积 → 订阅者收到；**先订阅后发布**与**先发布后订阅**两条时序都要对
  （后者靠回放补齐——断线重连靠的就是它）；订阅者走了不影响发布；`release` 之后缓冲不再增长。
* **事件流**：还在跑的任务 → `start` + 回放已有格 + 续流；已终结的任务 → 从
  `optimization_runs` 回放 + 补 `done`；失败给 `error`、取消给 `cancelled`。
  **终态的权威是数据库**，不是缓冲（缓冲在终结时就被释放了）。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.tasks import bus
from app.tasks.stream import task_frames
from tests.fakes import FakeDatabase

USER_ID = 1
TASK_ID = "00000000-0000-0000-0000-0000000000b1"


def grid_request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "kind": "grid",
        "symbol": "600519",
        "exec": {
            "cells": [],
            "options": {},
            "start": {"kind": "grid", "total": 2, "symbol": "600519"},
        },
    }
    request.update(overrides)
    return request


async def make_task(db: FakeDatabase, *, status: str = "queued", request: dict | None = None) -> str:
    await db.create_task(TASK_ID, USER_ID, "grid", request or grid_request(), total=2)
    if status != "queued":
        db.tasks[TASK_ID]["status"] = status
    return TASK_ID


# ── 总线 ────────────────────────────────────────────────────


def test_publish_then_subscribe_replays_the_backlog() -> None:
    """**先发布后订阅**：订阅时预填已有帧——断线重连靠这条补齐「断线期间跑完的格」。"""

    async def scenario() -> list[Any]:
        bus.publish("t1", "start", {"total": 2})
        bus.publish("t1", "cell", {"index": 0})
        async with bus.subscribe("t1") as queue:
            return [queue.get_nowait(), queue.get_nowait()]

    assert asyncio.run(scenario()) == [
        ("start", {"total": 2}),
        ("cell", {"index": 0}),
    ]


def test_subscribe_then_publish_delivers_live() -> None:
    """**先订阅后发布**：新帧实时到达（这是执行期间在线的路径）。"""

    async def scenario() -> Any:
        async with bus.subscribe("t2") as queue:
            bus.publish("t2", "cell", {"index": 1})
            return await asyncio.wait_for(queue.get(), 1.0)

    assert asyncio.run(scenario()) == ("cell", {"index": 1})


def test_subscriber_leaving_does_not_block_publishing() -> None:
    """订阅者走了照样能发布（**执行不认识连接**——这是「断线不丢」的另一半）。"""

    async def scenario() -> int:
        async with bus.subscribe("t3"):
            assert bus.subscriber_count("t3") == 1
        assert bus.subscriber_count("t3") == 0
        bus.publish("t3", "cell", {"index": 0})  # 没人听也不该炸
        return len(bus._buffers.get("t3", []))

    assert asyncio.run(scenario()) == 1


def test_release_drops_the_buffer() -> None:
    bus.publish("t4", "cell", {"index": 0})
    bus.release("t4")
    bus.publish("t4", "cell", {"index": 1})
    assert len(bus._buffers["t4"]) == 1
    bus.release("t4")


# ── 事件流 ──────────────────────────────────────────────────


async def collect(db: FakeDatabase, task_id: str, *, limit: int = 50) -> list[Any]:
    frames: list[Any] = []
    async for frame in task_frames(db, USER_ID, task_id, keepalive_seconds=0.05):
        if frame is None:
            continue  # 保活帧不算事件
        frames.append(frame)
        if frame[0] in {"done", "error", "cancelled"} or len(frames) >= limit:
            break
    return frames


def test_running_task_streams_backlog_then_live() -> None:
    """还在跑：`start`（冻结在任务行里）→ 回放已有的格 → 续流到 `done`。"""

    async def scenario() -> list[Any]:
        db = FakeDatabase()
        await make_task(db, status="running")
        bus.publish(TASK_ID, "cell", {"index": 0})

        async def consumer() -> list[Any]:
            return await collect(db, TASK_ID)

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0)  # 让消费者先订阅上
        bus.publish(TASK_ID, "cell", {"index": 1})
        bus.publish(TASK_ID, "done", {"run_id": "r1", "cells_ok": 2})
        return await asyncio.wait_for(task, 2.0)

    frames = asyncio.run(scenario())
    assert [name for name, _ in frames] == ["start", "cell", "cell", "done"]
    assert frames[0][1]["total"] == 2
    assert [payload["index"] for name, payload in frames if name == "cell"] == [0, 1]


def test_keepalive_timeout_picks_up_a_terminal_state_from_the_database() -> None:
    """订阅落在非主实例：拿不到实时帧，但 keepalive 超时查库能收口到终态。"""

    async def scenario() -> list[Any]:
        bus.reset()
        db = FakeDatabase()
        await make_task(db, status="running")
        await db.save_optimization_run(
            "run-1",
            USER_ID,
            {"kind": "grid"},
            {
                "kind": "grid",
                "cells": [{"index": 0, "ok": True}],
                "cells_total": 1,
                "cells_ok": 1,
                "best_index": 0,
                "overfit": None,
                "duration_s": 0.5,
            },
        )

        async def finish_elsewhere() -> None:
            # 模拟另一个进程里的 worker：只落库，不 publish（本进程收不到实时帧）
            await asyncio.sleep(0.01)
            await db.finish_task(TASK_ID, result={"run_id": "run-1"})

        finisher = asyncio.create_task(finish_elsewhere())
        frames: list[Any] = []
        async for frame in task_frames(db, USER_ID, TASK_ID, keepalive_seconds=0.05):
            if frame is None:
                continue
            frames.append(frame)
            if frame[0] in {"done", "error", "cancelled"}:
                break
        await finisher
        return frames

    frames = asyncio.run(scenario())
    assert [name for name, _ in frames] == ["start", "cell", "done"]
    assert frames[-1][1]["run_id"] == "run-1"


def test_finished_task_replays_from_the_database() -> None:
    """已终结：**从库回放**（缓冲早释放了）——cells + 摘要拼出与在线时同形的帧。"""

    async def scenario() -> list[Any]:
        db = FakeDatabase()
        await make_task(db, status="running")
        await db.save_optimization_run(
            "run-1",
            USER_ID,
            {"kind": "grid"},
            {
                "kind": "grid",
                "cells": [{"index": 0, "ok": True}, {"index": 1, "ok": True}],
                "cells_total": 2,
                "cells_ok": 2,
                "best_index": 1,
                "overfit": {"n_trials": 2},
                "duration_s": 1.5,
            },
        )
        await db.finish_task(TASK_ID, result={"run_id": "run-1"})
        bus.release(TASK_ID)  # worker 收尾时会做这件事
        return await collect(db, TASK_ID)

    frames = asyncio.run(scenario())
    assert [name for name, _ in frames] == ["start", "cell", "cell", "done"]
    assert frames[-1][1]["run_id"] == "run-1"
    assert frames[-1][1]["cells_ok"] == 2


def test_failed_task_yields_an_error_frame() -> None:
    async def scenario() -> list[Any]:
        db = FakeDatabase()
        await make_task(db, status="running")
        await db.fail_task(TASK_ID, error={"kind": "no_data", "message": "区间内没有行情数据"})
        return await collect(db, TASK_ID)

    frames = asyncio.run(scenario())
    assert frames[-1][0] == "error"
    assert "没有行情数据" in frames[-1][1]["message"]


def test_cancelled_task_says_so() -> None:
    """取消**没有记录可回放**（中止的结果不落库）——如实给一帧说明，不假装有结果。"""

    async def scenario() -> list[Any]:
        db = FakeDatabase()
        await make_task(db, status="running")
        await db.finish_task_cancelled(TASK_ID)
        return await collect(db, TASK_ID)

    frames = asyncio.run(scenario())
    assert frames[-1][0] == "cancelled"
    assert "不落库" in frames[-1][1]["message"]


def test_unknown_task_yields_error_frame() -> None:
    async def scenario() -> list[Any]:
        db = FakeDatabase()
        return await collect(db, str(uuid.uuid4()))

    assert asyncio.run(scenario()) == [("error", {"code": "not_found", "message": "任务不存在"})]
