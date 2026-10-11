"""SSE 帧编码与响应构造（M8 抽出，三处共用）。

抽出来的理由不是「好看」：这份编码函数在本仓已经有**两份逐字相同的手写副本**
（`api/chat.py` / `api/optimize.py`），M8 的深路径是第三个写者。第三份手写意味着
「改一处忘两处」从此变成常态——而 SSE 帧格式错了是**静默**的：浏览器收不到事件、
前端只是「什么都不动」，排查要一路摸到网络面板。

本模块**只做编码与响应头**，不含任何业务语义：不发帧、不记账、不管断连。
`chat` 与 `optimize` 改为 import 这里的实现（行为一字未改）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi.responses import StreamingResponse

#: 反代下禁用缓冲 + 不做缓存：少这两条，事件会在中间层被攒成一坨再吐
SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


def sse_frame(event: str, payload: Mapping[str, Any]) -> str:
    """编码一个 SSE 帧。payload 走 `json.dumps`，换行会被转义，不会截断帧。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sse_comment(text: str = "keep-alive") -> str:
    """注释帧（保活）：客户端会忽略，但能让中间层知道连接还活着。"""
    return f": {text}\n\n"


def sse_response(
    stream: AsyncIterator[str], *, headers: Mapping[str, str] | None = None
) -> StreamingResponse:
    """把「已经编好帧的异步生成器」包成 SSE 响应。

    `headers` 用于调用方自己的兜底通道（例如 chat 的 `X-Thread-Id`）——
    那些头与 SSE 无关，故由调用方给，这里不猜。
    """
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={**SSE_HEADERS, **(headers or {})},
    )
