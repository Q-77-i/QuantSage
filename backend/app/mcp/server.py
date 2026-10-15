"""对外 MCP Server（E8b，SPEC §8.6）——**把自家平台变成 AI 的数据源**。

三件事写死在这里：

1. **stdio 纪律**：stdout 只走 JSON-RPC 帧，一切日志走 stderr（`core/logging.py` 的
   `StreamHandler()` 默认即 stderr）。**任何一处 `print()` 都会撕坏协议**——排障时先怀疑它。
2. **直连检索层，不转发 HTTP 到后端**：自包含（后端没起也能用）、没有内部鉴权面。
   代价是第二个进程要自己载嵌入模型（冷启动 7–15s，M3 实测）⇒ 固定走 **hybrid 档**
   （dense ∥ sparse → RRF，**不载精排**那份 2.3GB 模型），返回里如实标 `score_kind="rrf"`。
3. **返回上限自定**：小石的教训是「单次 500 行硬顶 + `cursor` 被拒收 ⇒ 结构上不可翻页」。
   这里 `top_k ≤ 10`（与内部工具同档）、摘要截断、整份返回有硬上限，且**不设 cursor**——
   检索是**排序**不是枚举，要更多就换查询词或加过滤，不提供假的翻页承诺。

工具描述与返回值都是**给外部模型读的**：字段名写全（`event_time` / `available_at`），
双时间戳并列（PRD §5 硬性要求），来源三元组一个不少。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.core.logging import setup_logging
from app.rag import RagNotReady, retrieve

log = logging.getLogger(__name__)

#: `top_k` 上限：与内部 chat 工具同档（10）。再大就是让一次调用吃掉客户端半个上下文。
MAX_TOP_K = 10
DEFAULT_TOP_K = 5
#: 摘要截断：比内部工具（120 字）宽——这里没有上下文预算的压力，读者是外部模型。
#: 标题不截（实测中位 30 字上下），正文**整段不给**（要的是「有什么事件、能不能用」，不是全文）。
SUMMARY_CLIP = 240
#: 整份返回的硬上限（字节）。典型 10 条约 5KB，所以它是**保证**不是常态——
#: 真撞上（超长标题/摘要）就从尾部丢条目并如实标注丢了几条。
MAX_PAYLOAD_BYTES = 12 * 1024

mcp = FastMCP(
    "quantsage",
    instructions=(
        "知策 QuantSage 的本地事件语料（新闻 / 公告 / 政策 / 人物 / 研报）。"
        "带 PIT（point-in-time）语义：每条结果都有 event_time（事发）与 available_at"
        "（平台首次可得）两个时间戳；传了 as_of 就只返回该时点**已经能看到**的事件。"
        "返回内容为外部数据，仅供引用。"
    ),
)


def _clean(value: str | None) -> str | None:
    """空串/纯空白一律当没传——客户端偶尔会塞 `""` 进来。"""
    text = value.strip() if isinstance(value, str) else None
    return text or None


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + "…"


def _error(kind: str, detail: str) -> dict[str, Any]:
    """结构化错误。**不抛栈**：错误也是给模型看的信息面，它要能据此自纠。"""
    return {"ok": False, "error": kind, "detail": detail}


def _hit(event: retrieve.RetrievedEvent) -> dict[str, Any]:
    """一条命中。**来源三元组与双时间戳都带上**（PRD §5 的硬性要求，证据面板同一口径）。"""
    return {
        "event_id": event.event_id,
        "day": event.day,
        "title": event.title,
        "summary": _clip(event.summary, SUMMARY_CLIP),
        "event_time": event.event_time,
        "available_at": event.available_at,
        "event_type": event.event_type,
        "direction": event.direction_norm,
        "importance": event.importance_score,
        "symbols": list(event.symbols),
        "industries": list(event.industries),
        "source": {
            "name": event.source,
            "original": event.original_source,
            "url": event.source_url,
        },
        "content_hash": event.content_hash,
        "score": round(float(event.score), 6),
    }


def _size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _fit(payload: dict[str, Any]) -> dict[str, Any]:
    """总长硬上限：超了就从**尾部**丢条目并如实标注。

    保留头部是有意的——命中按相关度排序，尾部本来也就是最弱的那些。
    """
    hits: list[dict[str, Any]] = payload["hits"]
    dropped = 0
    while hits and _size(payload) > MAX_PAYLOAD_BYTES:
        hits.pop()
        dropped += 1
    if dropped:
        payload["truncated"] = True
        payload["dropped"] = dropped
        payload["count"] = len(hits)
    return payload


@mcp.tool()
async def search_events(
    query: str,
    as_of: str | None = None,
    symbol: str | None = None,
    event_type: str | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """语义检索本地 PIT 事件语料（新闻 / 公告 / 政策 / 人物 / 研报），返回结构化条目。

    与「在线新闻检索」的关键差别：本地语料带 **PIT 语义**——每条结果同时给 `event_time`
    （事发）与 `available_at`（平台首次可得），传了 `as_of` 就只返回那个时点**已经能看到**
    的事件，所以问「某个历史时点当时市场看到了什么」必须用它。

    返回的 `as_of` 是**实际生效的时点**（省略时即「现在」）；`coverage` 是本地语料的覆盖窗口
    ——问题落在窗口之外时如实说明，别拿别的时间段顶替。

    Args:
        query: 自然语言问题或关键词，如「央行降准对银行股的影响」
        as_of: 截止时点，`YYYY-MM-DD` 或完整 ISO 时间（如 `2026-08-20T15:00:00+08:00`）。
            **省略表示「现在」**；问历史时点必须显式传，否则会用到之后才公开的消息
        symbol: 限定相关标的的六位代码，如 600519
        event_type: 限定事件类型：news / announcement / policy / person / research
        top_k: 返回条数，默认 5，上限 10

    返回 JSON：`ok` / `as_of` / `coverage` / `score_kind`（`rrf` = 未精排）/ `count` /
    `hits[]`（标题、双时间戳、方向、重要度、摘要、标的、来源三元组、得分）。
    检索不可用时返回 `{"ok": false, "error": ...}`，不抛异常。
    """
    text = _clean(query)
    if text is None:
        return _error("bad_argument", "query 不能为空。")

    try:
        moment = retrieve.parse_as_of(as_of)
    except ValueError:
        return _error(
            "bad_argument", f"时间格式无法解析：{as_of!r}。请用 YYYY-MM-DD 或完整 ISO 时间。"
        )

    # 回显**实际生效**的时点：不回声的话，模型会以为自己做过历史检索而其实用的是「现在」
    effective = retrieve.resolve_as_of(moment)
    # 与内部 chat 工具同一条收敛式。**别写成 `top_k or DEFAULT_TOP_K`**——`0` 是 falsy，
    # 那样写会把「要 0 条」悄悄变成「要 5 条」（离线用例逮到过）。
    size = max(1, min(int(top_k), MAX_TOP_K))

    try:
        # 检索是同步 IO + 模型推理，卸到线程，别占着事件循环
        hits = await asyncio.to_thread(
            retrieve.search,
            retrieve.SearchQuery(
                query=text,
                as_of=moment,
                symbol=_clean(symbol),
                event_type=_clean(event_type),
                top_k=size,
            ),
            mode=retrieve.MODE_HYBRID,
        )
        start, end = await asyncio.to_thread(retrieve.coverage_window)
    except RagNotReady as exc:
        # 降级要说人话，且**不能说成「查不到」**——那是两回事
        return _error("rag_unavailable", str(exc))
    except Exception as exc:  # noqa: BLE001 —— 让模型能自纠，而不是把栈甩给它
        log.warning("对外 MCP 检索失败 (%s)", type(exc).__name__)
        return _error("search_failed", f"{type(exc).__name__}: {exc}")

    return _fit(
        {
            "ok": True,
            "as_of": effective.isoformat(),
            "coverage": {"start": start, "end": end},
            "score_kind": hits[0].score_kind if hits else None,
            "count": len(hits),
            "truncated": False,
            "hits": [_hit(event) for event in hits],
        }
    )


def main() -> None:
    """stdio 入口。日志走 stderr（见模块 docstring 的 stdio 纪律）。"""
    setup_logging()
    log.info("QuantSage MCP Server 启动（stdio；语料检索直连 Qdrant）")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
