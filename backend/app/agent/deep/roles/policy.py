"""政策面角色：读 `policy` 语料。**方向是平台给的**（`dovish→bullish` / `hawkish→bearish`
在 M2b 已进 `DIRECTION_MAP`），与公告那套自建映射不是一回事——别把两者混起来说。

政策语料的 `industries` 覆盖 100%（M3 实测），所以这里能给出「受益行业」而新闻/公告不能。
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import timedelta

from app.agent.deep.roles.base import RoleContext, event_ref, interpret, pit_filter
from app.agent.deep.types import EvidenceRef, RoleResult
from app.data import duckdb_client as dc

ROLE = "policy"

TOP_K = 6
LOOKBACK_DAYS = 180

#: 方向标签（平台给的 `direction_norm`）→ 中文。**只做展示翻译，不改判据。**
DIRECTION_LABELS = {"bullish": "偏宽松/利多", "bearish": "偏收紧/利空", "neutral": "中性"}


def industry_counts(rows: Sequence[Mapping[str, object]]) -> list[tuple[str, int]]:
    """受益行业计数（降序）。空 `industries` 不参与。"""
    counter: Counter[str] = Counter()
    for row in rows:
        for name in row.get("industries") or ():
            counter[str(name)] += 1
    return counter.most_common()


def _rank_key(row: Mapping[str, object]) -> tuple[float, str]:
    score = row.get("importance_score")
    return (float(score) if score is not None else 0.0, str(row.get("event_time") or ""))


async def run(ctx: RoleContext) -> RoleResult:
    """政策语料**不按标的过滤**（政策是全局的），故与其他角色不同：只看窗口。"""
    end = ctx.brief.as_of.date().isoformat()
    start = (ctx.brief.as_of.date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    all_rows = await asyncio.to_thread(dc.events, start=start, end=end, data_dir=ctx.data_dir)
    rows = [row for row in all_rows if row.get("event_type") == "policy"]
    visible = pit_filter(rows, ctx.brief.as_of)
    visible.sort(key=_rank_key, reverse=True)
    chosen = visible[:TOP_K]

    if not chosen:
        return RoleResult(ROLE, errors=("该时点之前没有可见的政策事件",))

    evidence: tuple[EvidenceRef, ...] = tuple(event_ref(row) for row in chosen)
    industries = industry_counts(chosen)
    numbers = {
        "可见政策条数": f"{len(visible)} 条",
        "纳入证据条数": f"{len(chosen)} 条",
        "偏宽松": f"{sum(1 for r in chosen if r.get('direction_norm') == 'bullish')} 条",
        "偏收紧": f"{sum(1 for r in chosen if r.get('direction_norm') == 'bearish')} 条",
    }
    if industries:
        numbers["涉及行业数"] = f"{len(industries)} 个"
    material = (
        "政策事件（窗口 " + start + " → " + end + "，**只含该时点平台已发布的**）：\n"
        + "\n".join(
            f"- [{str(r.get('event_time'))[:10]}] {r.get('title')}"
            f"（方向：{DIRECTION_LABELS.get(str(r.get('direction_norm')), '未标注')}；"
            f"行业：{'、'.join(r.get('industries') or ()) or '未标注'}）"
            for r in chosen
        )
        + ("\n\n受益行业分布：" + "、".join(f"{name} {n} 条" for name, n in industries[:5]) if industries else "")
    )
    return await interpret(ROLE, ctx, material=material, numbers=numbers, evidence=evidence)
