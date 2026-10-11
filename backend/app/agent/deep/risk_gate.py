"""代码化风控闸门（M8b）：三类检查，**纯函数——LLM 不可决定合规**（CLAUDE.md 关键决策 1）。

| 检查 | 命中处置 |
|---|---|
| ① 正文数字无来源 | 回综合**重写一次**；仍不过 → 降级标注（不无限重写，每次重写都是真花钱的调用） |
| ② 绝对化 / 买卖指令表述 | `interrupt()` 交人（词表命中，展示命中片段） |
| ③ 预算超限（调用数 / token / 时间 / 成本） | `interrupt()` 交人（加预算继续 / 用部分结果收尾） |

**检查顺序固定：预算 → 措辞 → 数字**。已经没钱了，再去重写或去问措辞都是错的决定顺序。

这个模块**从不抛异常、从不调用模型**：它是整条流水线上唯一「解释权不在模型手里」的一段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.agent.deep.budget import Budget
from app.agent.deep.numbers import untraceable_numbers
from app.agent.deep.synthesize import Draft


class GateDecision(StrEnum):
    PASS = "pass"
    REWRITE = "rewrite"
    APPROVAL = "approval"
    DEGRADE = "degrade"


#: 禁用语表：`(正则, 规则名)`。**只表判断，不含处置**（处置在 `evaluate` 里写死一处）。
#: 注意「不构成投资建议」这类标准免责句必须放行——故买卖指令只认「建议+动作」的组合。
FORBIDDEN_PHRASES: tuple[tuple[str, str], ...] = (
    (r"必涨|必跌|稳赚|包赚|保证收益|无风险(?:收益|套利)|一定(?:会)?(?:涨|跌)|翻倍在即", "绝对化表述"),
    (r"建议(?:买入|卖出|增持|减持|清仓|加仓)|立即(?:买入|卖出)|满仓|全仓(?:买入|杀入)", "买卖指令"),
)


@dataclass(frozen=True, slots=True)
class GateHit:
    rule: str
    snippet: str

    def to_payload(self) -> dict[str, str]:
        return {"rule": self.rule, "snippet": self.snippet}


@dataclass(frozen=True, slots=True)
class GateResult:
    decision: GateDecision
    reason: str = ""
    hits: tuple[GateHit, ...] = ()
    stray_numbers: tuple[str, ...] = ()
    approval: dict[str, Any] | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.decision is GateDecision.PASS

    def to_payload(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "reason": self.reason,
            "hits": [hit.to_payload() for hit in self.hits],
            "stray_numbers": list(self.stray_numbers),
            "approval": self.approval,
            "notes": list(self.notes),
        }


def check_numbers(text: str, allowed: set[str]) -> tuple[str, ...]:
    """① 无来源数字。口径与角色层**同一个函数**（`numbers.untraceable_numbers`）。"""
    return tuple(untraceable_numbers(text, allowed))


def check_forbidden(text: str) -> tuple[GateHit, ...]:
    """② 绝对化 / 买卖指令。**命中即给片段**（人要看到是哪句才批得下去）。"""
    hits: list[GateHit] = []
    for pattern, label in FORBIDDEN_PHRASES:
        found = re.search(pattern, text)
        if found:
            start = max(0, found.start() - 12)
            hits.append(GateHit(rule=label, snippet=text[start : found.end() + 12]))
    return tuple(hits)


def check_budget(budget: Budget) -> str | None:
    """③ 预算四计数器。返回**第一个**被突破的计数器名（顺序固定，便于断言与排障）。"""
    return budget.exceeded()


def evaluate(
    draft: Draft,
    *,
    allowed: set[str],
    budget: Budget,
    rewrite_used: bool,
) -> GateResult:
    """跑三类检查并给出**唯一一处**的处置决定。

    `allowed` 由调用方（图节点）用 `roles.base.allowed_for` 构造——与角色层同一套口径，
    不在这里另发明一套（两套口径必然漂移）。
    """
    exhausted = check_budget(budget)
    if exhausted:
        return GateResult(
            GateDecision.APPROVAL,
            reason=f"预算超限（{exhausted}）",
            approval={
                "kind": "budget",
                "detail": f"已达上限：{exhausted}",
                "usage": budget.to_payload(),
                "options": ["continue", "stop"],
            },
        )

    hits = check_forbidden(draft.conclusion)
    if hits:
        joined = "；".join(f"{hit.rule}：「{hit.snippet}」" for hit in hits)
        return GateResult(
            GateDecision.APPROVAL,
            reason=f"措辞需要人工确认：{joined}",
            hits=hits,
            approval={
                "kind": "wording",
                "detail": joined,
                "options": ["continue", "stop"],
            },
        )

    stray = check_numbers(draft.conclusion, allowed)
    if stray:
        joined = "、".join(stray)
        if rewrite_used:
            return GateResult(
                GateDecision.DEGRADE,
                reason=f"重写后仍有数字无出处（{joined}），降级标注",
                stray_numbers=stray,
            )
        return GateResult(
            GateDecision.REWRITE,
            reason=f"数字无出处：{joined}",
            stray_numbers=stray,
        )

    return GateResult(GateDecision.PASS)
