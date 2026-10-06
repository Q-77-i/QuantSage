"""对话 API：SSE 流式问答 + 会话列表。

SSE 帧手写而非引库：帧格式极简（`event:` + 单行 `data:` + 空行），自己拼能完全
控制事件类型，也少一个显式依赖。

两条容易踩的线：
  * 首字节一旦发出就无法再改状态码——图内的异常必须转成 `error` 帧，裸抛会让客户端
    直接断连、什么也收不到；
  * `asyncio.CancelledError`（客户端断连）必须原样抛，吞掉它会留下仍在跑的图。

归属（M1）：会话的主人是自建的 `chat_threads` 表，不是 checkpointer 的 `checkpoints`。
三个端点一律先按 `user_id` 查，命中 0 行翻 404——**与「会话不存在」同响应**，不泄露存在性。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.graph import astream_chat
from app.agent.history import messages_to_history
from app.core.auth import require_db, require_user

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
async def chat(
    request: Request, body: ChatRequest, user: dict[str, Any] = Depends(require_user)
) -> StreamingResponse:
    # 参数校验先于资源检查：两者都在流开始前，客户端错误应当优先暴露
    thread_id = normalize_thread_id(body.thread_id)
    db = require_db(request)

    # 续聊既有会话：先验归属（不属于本人一律 404，不区分「不存在」）
    if body.thread_id and await db.thread_owner(thread_id) != int(user["id"]):
        raise HTTPException(status_code=404, detail="会话不存在")

    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent 未就绪（模型或依赖不可用）")

    # 归属行必须赶在首字节之前落：中途失败宁可 500，也不留一条无主会话
    if body.thread_id:
        await db.touch_thread(thread_id, int(user["id"]))
    else:
        await db.claim_thread(thread_id, int(user["id"]))

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


async def _load_messages(checkpointer: Any, thread_id: str) -> list[Any] | None:
    """读最新 checkpoint 的消息；None 表示这个会话不存在。

    消息正文落在 `checkpoint_blobs`（msgpack），**不能手写 SQL 反序列化**，
    必须经 checkpointer 官方读接口取。
    """
    state = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    if state is None:
        return None
    return state.checkpoint.get("channel_values", {}).get("messages", [])


async def _thread_summary(checkpointer: Any, thread_id: str) -> dict[str, Any]:
    """取首条人类消息当标题。"""
    messages = await _load_messages(checkpointer, thread_id) or []
    title = "（空会话）"
    for msg in messages:
        if getattr(msg, "type", None) == "human":
            text = str(getattr(msg, "content", "")).strip().replace("\n", " ")
            title = text[:TITLE_MAX] + ("…" if len(text) > TITLE_MAX else "")
            break
    return {"thread_id": thread_id, "title": title, "messages": len(messages)}


async def _owned_or_404(request: Request, user_id: int, thread_id: str) -> None:
    """归属闸门：不属于本人（含不存在、含 P1 遗留的无主会话）一律 404。"""
    if await require_db(request).thread_owner(thread_id) != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")


@router.get("/threads")
async def list_threads(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    """会话列表，供前端侧栏使用。只列本人的会话，按最近活动倒序。"""
    checkpointer = getattr(request.app.state, "checkpointer", None)
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="checkpointer 不可用")

    thread_ids = await require_db(request).list_thread_ids(int(user["id"]), limit)
    return [await _thread_summary(checkpointer, tid) for tid in thread_ids]


@router.get("/threads/{thread_id}/messages")
async def thread_messages(
    request: Request, thread_id: str, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    """单个会话的历史消息，供对话页回看（含工具步骤）。"""
    # 路径参数不可能为空，`normalize_thread_id` 里「缺省生成 UUID」那条分支在这里不可达
    normalized = normalize_thread_id(thread_id)

    checkpointer = getattr(request.app.state, "checkpointer", None)
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="checkpointer 不可用")

    await _owned_or_404(request, int(user["id"]), normalized)

    messages = await _load_messages(checkpointer, normalized)
    if messages is None:
        # 归属行在、checkpoint 没了（上一次删除删到一半）：同上，404
        raise HTTPException(status_code=404, detail="会话不存在")

    return {"thread_id": normalized, "messages": messages_to_history(messages)}


@router.delete("/threads/{thread_id}")
async def delete_thread(
    request: Request, thread_id: str, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    """删除一个会话及其全部 checkpoint。

    先验归属再删（`adelete_thread` 对不存在的 thread 是静默成功的，不自己把关会让
    「点错了一个已经被删的会话」看起来像成功）；归属行最后删，两次删除之间失败
    只可能留下「空会话」行，再删一次即可。
    """
    normalized = normalize_thread_id(thread_id)

    checkpointer = getattr(request.app.state, "checkpointer", None)
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="checkpointer 不可用")

    user_id = int(user["id"])
    await _owned_or_404(request, user_id, normalized)

    await checkpointer.adelete_thread(normalized)
    await require_db(request).drop_thread(normalized, user_id)
    return {"thread_id": normalized, "deleted": True}
