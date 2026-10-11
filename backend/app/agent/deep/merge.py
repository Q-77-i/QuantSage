"""证据归并（纯代码，无 LLM）：五个角色的产出 → 一份证据集 + 一份缺口清单。

归并要做对的三件事：

1. **去重按 `event_id|day`**（与 claim 闸门同一个键）——同一事件可能既被新闻角色检索到、
   又被基本面角色按类别取到，重复的只留一份（**先到先得**，顺序即 `ROLES` 的顺序）。
2. **缺口如实列**：谁降级了、为什么、缺了几条——这是「拔掉任一数据源不崩溃」在报告里的
   可见部分，不能只写在日志里。
3. **数字带角色前缀**：五个角色的 `numbers` 汇到一张表，键前缀成「角色标签 · 标签」，
   避免两个角色各有一个「区间收益」互相覆盖（那会让 claim 闸门校验到错的那个数）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agent.deep.types import ROLES, ROLE_LABELS, EvidenceRef, RoleResult


@dataclass(frozen=True, slots=True)
class MergedEvidence:
    """归并产物。`gaps` 与 `degraded` 都是**给人看的如实标注**，UI 与报告直接渲染。"""

    results: tuple[RoleResult, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    numbers: dict[str, str] = field(default_factory=dict)
    gaps: tuple[str, ...] = ()
    duplicates: int = 0

    @property
    def degraded(self) -> tuple[str, ...]:
        """降级的角色名（`errors` 非空）。"""
        return tuple(r.role for r in self.results if r.errors)

    @property
    def ok_roles(self) -> tuple[str, ...]:
        return tuple(r.role for r in self.results if not r.errors)

    def numbers_for(self, role: str) -> dict[str, str]:
        prefix = f"{ROLE_LABELS.get(role, role)} · "
        return {k[len(prefix):]: v for k, v in self.numbers.items() if k.startswith(prefix)}

    def to_payload(self) -> dict[str, Any]:
        return {
            "roles": [r.to_payload() for r in self.results],
            "evidence": [e.to_payload() for e in self.evidence],
            "numbers": dict(self.numbers),
            "gaps": list(self.gaps),
            "degraded": list(self.degraded),
            "duplicates": self.duplicates,
        }


def merge(results: Sequence[RoleResult]) -> MergedEvidence:
    """归并。**先按 `ROLES` 固定排序**再合并。

    并行 fan-out 的完成顺序每次都可能不同，而报告要冻结、要算指纹（`report_hash`）——
    顺序不稳会让同一份输入产出不同的报告。故角色顺序在这里定死，不依赖调用方给的次序。
    """
    order = {role: index for index, role in enumerate(ROLES)}
    results = sorted(results, key=lambda r: order.get(r.role, len(order)))

    evidence: list[EvidenceRef] = []
    seen: set[str] = set()
    duplicates = 0
    numbers: dict[str, str] = {}
    gaps: list[str] = []

    for result in results:
        label = ROLE_LABELS.get(result.role, result.role)
        for key, value in result.numbers.items():
            numbers[f"{label} · {key}"] = value
        for item in result.evidence:
            if item.key in seen:
                duplicates += 1
                continue
            seen.add(item.key)
            evidence.append(item)
        for error in result.errors:
            gaps.append(f"「{label}」不可用：{error}")
        if not result.errors and not result.evidence and not result.numbers:
            gaps.append(f"「{label}」没有产出可用材料")
        if not result.errors and not result.findings:
            gaps.append(f"「{label}」未产出解读要点（数字与证据仍在）")

    return MergedEvidence(
        results=tuple(results),
        evidence=tuple(evidence),
        numbers=numbers,
        gaps=tuple(gaps),
        duplicates=duplicates,
    )
