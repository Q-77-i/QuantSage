"""任务进度总线（P3-E1b）：按 `task_id` 的**累积缓冲 + 订阅者队列**。

为什么需要它：网格/批量的执行搬进了 worker（不再活在请求的协程里），而前端仍要逐格看着
热力图长出来——**发起请求的那条 SSE 流与执行解耦**之后，进度得有地方广播。缓冲是**累积**
的而不是逐帧丢弃的：断线重连要能补齐订阅之前已经跑完的格。

进程内（与 E1a「队列实体是表」不同）：进度是**瞬时物**，结果落 `optimization_runs`。
服务重启 → 任务被扫尾标 `failed`、缓冲随之消失——如实，与 §2.4 的单实例假设同一条边界。

终态的权威是**数据库**而不是缓冲：任务终结后缓冲即释放，重连时从
`optimization_runs.summary.cells` 回放（见 `app/tasks/stream.py`）。所以这里只管
「还在跑的那些怎么广播」，不承担历史。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

#: 一帧 = `(事件名, 载荷)`；与 `api/sse.py` 的编码器同形
Frame = tuple[str, object]

#: task_id → 已发布的帧（累积）。终结时由 worker 调 `release` 释放
_buffers: dict[str, list[Frame]] = {}
#: task_id → 订阅者队列
_subscribers: dict[str, set[asyncio.Queue[Frame]]] = {}


def publish(task_id: str, event: str, payload: object) -> None:
    """发布一帧：进缓冲 + 推给所有订阅者。

    队列无界、`put_nowait` 不阻塞——订阅者走了（断线）也只是没人收，不会把执行卡住，
    这正是「断线不丢」的一半：**执行不认识连接**。
    """
    _buffers.setdefault(task_id, []).append((event, payload))
    for queue in _subscribers.get(task_id, ()):
        queue.put_nowait((event, payload))


def release(task_id: str) -> None:
    """释放缓冲（worker 在任务终结时调）。**不动订阅者**：它们的队列里已经有自己的副本，
    正在读的流照常把剩下的帧读完。"""
    _buffers.pop(task_id, None)


@asynccontextmanager
async def subscribe(task_id: str) -> AsyncIterator[asyncio.Queue[Frame]]:
    """订阅。**返回的队列里已经预填了订阅之前发布的帧**（回放），接着收新帧。

    调用方必须**先订阅、再查任务状态**：反过来的话，任务若在两次调用之间结束，
    那一格会落进空档——客户端什么都收不到还查不出原因。
    """
    queue: asyncio.Queue[Frame] = asyncio.Queue()
    for frame in _buffers.get(task_id, ()):
        queue.put_nowait(frame)
    _subscribers.setdefault(task_id, set()).add(queue)
    try:
        yield queue
    finally:
        queues = _subscribers.get(task_id)
        if queues is not None:
            queues.discard(queue)
            if not queues:
                _subscribers.pop(task_id, None)


def subscriber_count(task_id: str) -> int:
    """当前订阅者数（测试与诊断用）。"""
    return len(_subscribers.get(task_id, ()))


def reset() -> None:
    """清空全部状态（测试夹具用）。"""
    _buffers.clear()
    _subscribers.clear()
