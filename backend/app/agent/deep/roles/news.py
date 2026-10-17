"""新闻事件角色：走本地 RAG，**PIT 过滤写进 Qdrant 服务端**（不是取回来再筛）。

检索档位用默认的 `rerank`（M3 实测 NDCG@10 0.8142 vs hybrid 0.7602）。代价是 MPS 精排
稳态 ~1.8s 且**并发完全串行**（F3：2 条 3.36s / 4 条 6.98s）——并行取证里这条最长，
过程面板要照实画，不假装五条等长。
"""

from __future__ import annotations

from dataclasses import replace

from app.agent.deep.roles.base import RoleContext, interpret
from app.agent.deep.types import EvidenceRef, RoleResult
from app.rag import RagNotReady
from app.rag.correct import correction_note, search_with_correction
from app.rag.retrieve import SearchQuery, search

ROLE = "news"

#: 进证据集的条数（top_k）
TOP_K = 5


def news_query(ctx: RoleContext) -> str:
    """检索式：问题原文 + 标的（问题里通常没有代码，补上能显著提召回）。"""
    parts = [ctx.brief.question, *ctx.symbols]
    return " ".join(part for part in parts if part)


def evidence_from_hits(hits) -> tuple[EvidenceRef, ...]:
    """RAG 命中 → 证据。`day` 取**事件日**（M3 返回的是字符串，与 claim 闸门的键同形）。"""
    return tuple(
        EvidenceRef(
            event_id=hit.event_id,
            day=hit.day,
            title=hit.title,
            event_time=hit.event_time,
            available_at=hit.available_at,
            event_type=hit.event_type,
            summary=hit.summary,
            source=hit.source,
            original_source=hit.original_source,
            source_url=hit.source_url,
            content_hash=hit.content_hash,
            industries=tuple(hit.industries),
            direction_norm=hit.direction_norm,
            importance_score=hit.importance_score,
        )
        for hit in hits
    )


async def run(ctx: RoleContext) -> RoleResult:
    try:
        # 检索是阻塞的（torch 精排 + Qdrant），`search_with_correction` 内部会卸到线程；
        # 零命中时才改写一次查询（as_of 逐字不变）。`search` 仍从模块全局取，
        # 离线用例 monkeypatch 的正是 `news_mod.search`。
        corrected = await search_with_correction(
            SearchQuery(query=news_query(ctx), as_of=ctx.brief.as_of, top_k=TOP_K),
            search_fn=search,
        )
    except RagNotReady as exc:
        return RoleResult(ROLE, errors=(f"检索不可用：{exc}",))

    note = correction_note(corrected.correction)
    evidence = evidence_from_hits(corrected.hits)
    if not evidence:
        message = "该时点之前没有检索到相关事件"
        return RoleResult(ROLE, errors=(f"{message}（{note}）" if note else message,))

    material = "按相关度排序的事件（每条带事发与平台可得两个时间）：\n" + "\n".join(
        f"- [{e.key}] {e.event_time}（可得 {e.available_at}）{e.title}"
        + (f"：{e.summary}" if e.summary else "")
        for e in evidence
    )
    result = await interpret(ROLE, ctx, material=material, evidence=evidence)
    return replace(result, notes=(*result.notes, note)) if note else result
