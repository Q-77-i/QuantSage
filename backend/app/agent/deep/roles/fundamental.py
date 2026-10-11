"""PIT 基本面事件角色：读 `announcement` 语料，按**我们自己的类别→方向表**给方向。

三个口径要先说清楚：

1. **语料侧的方向字段对公告是空的**——平台给全部公告 `direction='中性' / confidence=0.5`
   （实测），类别只在**标题文本**里（「2026 年半年度业绩预告」「股份回购」「股东减持」）。
   所以 D3 的映射表实现为**标题关键词规则**。
2. **映射表是 QuantSage 的业务判断，不是平台标注**——报告里必须署名「按 QuantSage 规则映射」，
   不得写成平台的判断（SPEC §9）。
3. **表只在 M8 应用层生效**，不写进 `etl/store.py::DIRECTION_MAP`：改语料会牵动既有回测与
   体检口径（M5a 的 golden 零位移是硬判据）。

规则表**按顺序首命中**，所以具体的写在前面（「业绩预增」要排在「业绩预告」前）。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from datetime import timedelta

from app.agent.deep.roles.base import RoleContext, event_ref, interpret, pit_filter
from app.agent.deep.types import EvidenceRef, RoleResult
from app.data import duckdb_client as dc

ROLE = "fundamental"

#: 取几条进证据集（按重要度、再按时间倒序）
TOP_K = 6

#: 回看窗口（自然日）——公告的价值随时间衰减，半年够用
LOOKBACK_DAYS = 180

#: **类别 → 方向**（D3，用户 2026-10-11 拍板；顺序即优先级，具体在前）
#: 方向为 None = 不映射（「融资定增」既可能是扩张也可能是摊薄，「资产重组」方向取决于方案）
CATEGORY_RULES: tuple[tuple[str, str, str | None], ...] = (
    (r"业绩预增|预盈|扭亏|业绩快报.*(增长|上升)", "业绩预增", "利多"),
    (r"业绩预减|预亏|首亏|业绩快报.*(下降|下滑)", "业绩预减", "利空"),
    (r"回购", "股份回购", "利多"),
    (r"增持", "股东增持", "利多"),
    (r"减持", "股东减持", "利空"),
    (r"诉讼|仲裁|处罚|立案|问询", "诉讼与监管", "利空"),
    (r"分红|派息|利润分配", "分红派息", "利多"),
    (r"中标|签订|合同", "中标与合同", "利多"),
    (r"业绩预告", "业绩预告", None),
    (r"资产重组|重大资产|收购|并购", "资产重组", None),
    # 真实公告用的是正式措辞（「向特定对象发行股票」= 定增）——只写「定增」会漏掉绝大多数
    (r"定增|向特定对象发行|非公开发行|融资|可转债|配股", "融资定增", None),
    (r"权益变动", "权益变动", None),
)


def classify(title: str) -> tuple[str | None, str | None]:
    """标题 → (类别, 方向)。**首命中**；都没中返回 `(None, None)`（中性，不硬套）。"""
    for pattern, category, direction in CATEGORY_RULES:
        if re.search(pattern, title or ""):
            return category, direction
    return None, None


def direction_payload(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """方向分布（报告与 UI 直接消费）：`{利多: n, 利空: n, 中性: n, 未分类: n}` + 命中明细。"""
    counts = {"利多": 0, "利空": 0, "中性": 0}
    unseen = 0
    detail: list[dict[str, object]] = []
    for row in rows:
        category, direction = classify(str(row.get("title") or ""))
        if direction is None:
            unseen += 1
        else:
            counts[direction] += 1
        detail.append(
            {
                "event_id": row.get("event_id"),
                "day": str(row.get("event_time"))[:10],
                "title": row.get("title"),
                "category": category,
                "direction": direction,
            }
        )
    return {"counts": counts, "unmapped": unseen, "detail": detail}


def _rank_key(row: Mapping[str, object]) -> tuple[float, str]:
    score = row.get("importance_score")
    return (float(score) if score is not None else 0.0, str(row.get("event_time") or ""))


async def run(ctx: RoleContext) -> RoleResult:
    if not ctx.symbols:
        return RoleResult(ROLE, errors=("没有标的，跳过基本面取证",))

    collected: list[Mapping[str, object]] = []
    start = (ctx.brief.as_of.date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    for symbol in ctx.symbols:
        rows = await asyncio.to_thread(
            dc.events,
            symbol,
            start=start,
            end=ctx.brief.as_of.date().isoformat(),
            data_dir=ctx.data_dir,
        )
        collected.extend(row for row in rows if row.get("event_type") == "announcement")

    # **PIT 闸门**：只看该时点平台已经给出的公告——这一步不能省
    visible = pit_filter(collected, ctx.brief.as_of)
    visible.sort(key=_rank_key, reverse=True)
    chosen = visible[:TOP_K]

    if not chosen:
        return RoleResult(ROLE, errors=("该时点之前没有可见的公告事件",))

    evidence: tuple[EvidenceRef, ...] = tuple(event_ref(row) for row in chosen)
    payload = direction_payload(chosen)
    counts = payload["counts"]
    numbers = {
        "可见公告条数": f"{len(visible)} 条",
        "纳入证据条数": f"{len(chosen)} 条",
        "利多": f"{counts['利多']} 条",
        "利空": f"{counts['利空']} 条",
        "未映射类别": f"{payload['unmapped']} 条",
    }
    material = (
        "公告事件（窗口 " + start + " → " + ctx.brief.as_of.date().isoformat() + "，"
        "**只含该时点平台已发布的**）：\n"
        + "\n".join(
            f"- [{item['day']}] {item['title']}（类别：{item['category'] or '未分类'}；"
            f"方向：{item['direction'] or '不映射'}）"
            for item in payload["detail"]
        )
        + "\n\n注：类别与方向**按 QuantSage 规则映射**（业绩预增/回购/增持/中标/分红=利多；"
        "减持/诉讼=利空；融资定增/资产重组等方向取决于方案，不映射）。措辞时请带上这一署名。"
    )
    return await interpret(ROLE, ctx, material=material, numbers=numbers, evidence=evidence)
