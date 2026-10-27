"""检索的一轮纠错：**只在零命中时**把查询改写一次再检索。

口径（SPEC 附录 A.2）：

* 不用低分阈值——RRF / rerank 的分数不在同一量纲，设阈值会把「有结果但排序一般」
  误判成「检索失败」，反而更吵。零命中是唯一确定、可复现的触发条件。
* 改写**只动 query**；`as_of` / `symbol` / `event_type` / `top_k` 逐字保留。PIT 红线：
  两次检索的 `as_of` 必须相等，否则第二次可能看到第一次看不到的消息。
* 改写失败、空、与原查询相同 → 不二次检索，如实返回原空结果；**不静默换查询**。
  调用方把 `Correction` 的说明带给用户/模型，让人知道「第一次没结果、试过改写」。

本模块不碰 Qdrant/模型生命周期：`search_fn` / `rewrite_fn` 都可注入，离线用例不载模型。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace

from app.core.config import get_settings
from app.rag.retrieve import RetrievedEvent, SearchQuery

log = logging.getLogger(__name__)

#: 改写调用的超时（秒）。一次零命中的兜底，不值得让整条对话等太久。
REWRITE_TIMEOUT = 8.0

#: 改写结果的长度上限：关键词串不是小作文。
REWRITE_MAX = 80

REWRITE_PROMPT = """你是中文财经检索的查询改写器。把下面的原查询改写成 2–6 个关键词或短语，
用于检索新闻 / 公告 / 政策语料。必须保留标的、时间、事件类型（如果有）；不要回答问题，
不要添加原查询里没有的事实。只输出改写后的查询，不要解释、不要引号、不要前缀。

标的：{symbol}
时点：{as_of}
事件类型：{event_type}
原查询：{query}
"""


@dataclass(frozen=True, slots=True)
class Correction:
    """一次纠错的如实记录。`rewritten is None` 表示没有真正发起第二次检索。"""

    original: str
    rewritten: str | None
    retried: bool
    reason: str  # rewritten | unavailable | unchanged


@dataclass(frozen=True, slots=True)
class CorrectedSearch:
    """检索结果 + 纠错说明（没有触发纠错时 `correction is None`）。"""

    hits: list[RetrievedEvent]
    correction: Correction | None = None


def _default_search(spec: SearchQuery, **kwargs: object) -> list[RetrievedEvent]:
    """默认检索入口。**在函数内取** `retrieve.search`：离线用例 monkeypatch 的是
    `app.rag.retrieve.search`，模块级绑定会让补丁失效。"""
    from app.rag.retrieve import search

    return search(spec, **kwargs)  # type: ignore[arg-type]


def _clean_rewrite(text: str) -> str:
    """把模型输出收敛成一行查询：去前缀/引号/反引号，压空白，截断。"""
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    for prefix in ("改写后查询：", "改写后的查询：", "查询：", "改写："):
        if line.startswith(prefix):
            line = line[len(prefix) :].strip()
    line = line.strip(" \t\"'`“”‘’")
    return " ".join(line.split())[:REWRITE_MAX]


async def rewrite_query(spec: SearchQuery) -> str:
    """默认改写器：一次 flash 调用。**失败一律返回空串**，由调用方走「未改写」路径。

    `get_settings().rag_query_rewrite` 是总开关：离线套件在 conftest 里默认关掉，
    避免「空结果」用例去调真模型；生产默认开启。
    """
    if not get_settings().rag_query_rewrite:
        return ""
    from app.core.llm import ask_once

    prompt = REWRITE_PROMPT.format(
        symbol=spec.symbol or "未限定",
        as_of=spec.as_of.isoformat() if spec.as_of is not None else "未限定",
        event_type=spec.event_type or "未限定",
        query=spec.query,
    )
    try:
        text = await ask_once(prompt, timeout=REWRITE_TIMEOUT, thinking=False)
    except Exception as exc:  # noqa: BLE001 —— 纠错是兜底，不能让它把检索/对话打穿
        log.warning("查询改写失败（%s），按未改写处理", type(exc).__name__)
        return ""
    return _clean_rewrite(text)


async def search_with_correction(
    spec: SearchQuery,
    *,
    mode: str | None = None,
    request_class: str | None = None,
    search_fn=None,
    rewrite_fn=None,
) -> CorrectedSearch:
    """先检索；零命中才改写一次再检索。

    `mode` / `request_class` 原样传给两次检索：改写只动 query，其余口径（含精排分级）不变。
    """
    searcher = search_fn or _default_search
    kwargs = {}
    if mode is not None:
        kwargs["mode"] = mode
    if request_class is not None:
        kwargs["request_class"] = request_class
    hits = await asyncio.to_thread(searcher, spec, **kwargs)
    if hits:
        return CorrectedSearch(hits)

    try:
        rewritten = _clean_rewrite(await (rewrite_fn or rewrite_query)(spec))
    except Exception as exc:  # noqa: BLE001 —— 纠错是兜底，失败按「未改写」处理
        log.warning("查询改写抛错（%s），按未改写处理", type(exc).__name__)
        rewritten = ""
    if not rewritten:
        return CorrectedSearch([], Correction(spec.query, None, False, "unavailable"))
    if rewritten == spec.query.strip():
        return CorrectedSearch([], Correction(spec.query, rewritten, False, "unchanged"))

    # PIT：只换 query，其余字段（尤其 as_of）逐字保留
    second = replace(spec, query=rewritten)
    hits = await asyncio.to_thread(searcher, second, **kwargs)
    return CorrectedSearch(hits, Correction(spec.query, rewritten, True, "rewritten"))


def correction_note(correction: Correction | None) -> str:
    """给用户/模型看的一句话；没有纠错时返回空串。"""
    if correction is None:
        return ""
    if correction.reason == "rewritten":
        return f"首次检索无结果，已改写为「{correction.rewritten}」再检索（同一 as_of）"
    if correction.reason == "unchanged":
        return f"首次检索无结果；改写结果与原查询相同（「{correction.rewritten}」），未重复检索"
    return "首次检索无结果；查询改写未成功，未重复检索"


__all__ = [
    "Correction",
    "CorrectedSearch",
    "correction_note",
    "rewrite_query",
    "search_with_correction",
]
