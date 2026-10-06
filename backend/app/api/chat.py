"""对话 API：SSE 流式问答 + 会话列表。

SSE 帧手写而非引库：帧格式极简（`event:` + 单行 `data:` + 空行），自己拼能完全
控制事件类型，也少一个显式依赖。

两条容易踩的线：
  * 首字节一旦发出就无法再改状态码——图内的异常必须转成 `error` 帧，裸抛会让客户端
    直接断连、什么也收不到；
  * `asyncio.CancelledError`（客户端断连）必须原样抛，吞掉它会留下仍在跑的图。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.graph import astream_chat

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

# 静默超过这个秒数就发一个注释帧：MCP 子进程冷启动 + 模型首 token + 工具查询叠起来
# 可能十几秒没有事件，前端与中间层都会以为连接死了
KEEPALIVE_SECONDS = 15.0

TITLE_MAX = 30


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = None


def sse_frame(event: str, payload: dict[str, Any]) -> str:
    """编码一个 SSE 帧。payload 走 json.dumps，换行会被转义，不会截断帧。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def normalize_thread_id(raw: str | None) -> str:
    """缺省时服务端生成；传了就必须是合法 UUID（否则 thread 键空间不受限）。"""
    if not raw:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="thread_id 必须是 UUID") from exc


@router.post("")
async def chat(request: Request, body: ChatRequest) -> StreamingResponse:
    # 参数校验先于资源检查：两者都在流开始前，客户端错误应当优先暴露
    thread_id = normalize_thread_id(body.thread_id)

    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent 未就绪（模型或依赖不可用）")

    handler = getattr(request.app.state, "langfuse_handler", None)
    callbacks = [handler] if handler is not None else None

    async def stream() -> AsyncIterator[str]:
        yield ": ok\n\n"  # 先送一帧，让响应头尽快落地

        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for item in astream_chat(
                    agent, message=body.message, thread_id=thread_id, callbacks=callbacks
                ):
                    queue.put_nowait(("event", item))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 在流内转成 error 帧
                queue.put_nowait(("error", exc))
            finally:
                queue.put_nowait(("end", None))

        task = asyncio.create_task(pump())
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(
                        queue.get(), timeout=KEEPALIVE_SECONDS
                    )
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if kind == "end":
                    break
                if kind == "error":
                    log.exception("对话失败 thread_id=%s", thread_id, exc_info=payload)
                    yield sse_frame(
                        "error", {"code": "internal", "message": "内部错误，请重试"}
                    )
                    break
                event, data = payload
                yield sse_frame(event, data)
        finally:
            # 客户端断连时在这里取消图；不能 await 太慢的东西（会立刻再被取消）
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # 反代下禁用缓冲
            "X-Thread-Id": thread_id,  # 中途断连时客户端拿不到 done，靠这个头兜底
        },
    )


async def _thread_ids(checkpointer: Any, limit: int) -> list[str]:
    """按最近活动排序取 thread_id。消息正文在 checkpoint_blobs 里，SQL 取不到。"""
    async with checkpointer.conn.connection() as conn:
        cur = await conn.execute(
            "SELECT thread_id FROM checkpoints WHERE checkpoint_ns = '' "
            "GROUP BY thread_id ORDER BY MAX(checkpoint_id) DESC LIMIT %s",
            (limit,),
        )
        rows = await cur.fetchall()
    return [row["thread_id"] for row in rows]


async def _thread_summary(checkpointer: Any, thread_id: str) -> dict[str, Any]:
    """反序列化最新 checkpoint，取首条人类消息当标题（不自己解 msgpack blob）。"""
    config = {"configurable": {"thread_id": thread_id}}
    state = await checkpointer.aget_tuple(config)
    messages = (state.checkpoint.get("channel_values", {}) if state else {}).get(
        "messages", []
    )
    title = "（空会话）"
    for msg in messages:
        if getattr(msg, "type", None) == "human":
            text = str(getattr(msg, "content", "")).strip().replace("\n", " ")
            title = text[:TITLE_MAX] + ("…" if len(text) > TITLE_MAX else "")
            break
    return {"thread_id": thread_id, "title": title, "messages": len(messages)}


@router.get("/threads")
async def list_threads(
    request: Request, limit: int = Query(20, ge=1, le=100)
) -> list[dict[str, Any]]:
    """会话列表，供前端侧栏使用。

    ⚠ 单用户 demo：**没有归属过滤**，任何调用方都能列出全部会话。
    P2 引入登录后必须按用户过滤（SPEC §6 已记为 M1 阻塞项）。
    """
    checkpointer = getattr(request.app.state, "checkpointer", None)
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="checkpointer 不可用")

    return [
        await _thread_summary(checkpointer, tid)
        for tid in await _thread_ids(checkpointer, limit)
    ]
