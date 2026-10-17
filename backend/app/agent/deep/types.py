"""M8 深路径的共享类型：全部是数据、无行为，且**都能过 `plain_json`**（运行记录要落库）。

两条贯穿全段的口径：

1. **证据键 = `event_id|day`**，与 M7 claim 闸门（`report/builder.validate_claims`）**同一个形状**——
   注意 RAG 返回的 `day` 是字符串、`event_time` / `available_at` 是 ISO 串，拼之前统一成字符串。
   形状不一致不会报错，只会让闸门整批拒收（M7 的教训：生产者给的类型与消费者查表的键不一致时，
   系统静默给 0 / 空）。
2. **数字先格式化再进 `numbers`**（沿用 M7 综述的教训：直接给模型原始浮点，它会原样复述
   `0.02509640439999994`）。角色层只允许引用 `numbers` 里已存在的数字或证据原文里的数字，
   校验在 `numbers.untraceable_numbers`。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.urls import safe_source_url

#: 角色名（= 过程事件的节点名 = 报告里「缺口清单」的键）。顺序即渲染顺序。
ROLES: tuple[str, ...] = ("market", "news", "fundamental", "policy", "factor")

#: 角色的中文标签（UI 与 prompt 共用一处，别在两处各写一份）
ROLE_LABELS: dict[str, str] = {
    "market": "行情技术面",
    "news": "新闻事件",
    "fundamental": "基本面事件",
    "policy": "政策面",
    "factor": "因子量化",
}


def evidence_key(event_id: str, day: str) -> str:
    """证据键：`event_id|day`。与 M7 claim 闸门同形（见模块 docstring 第 1 条）。"""
    return f"{event_id}|{day}"


@dataclass(frozen=True, slots=True)
class Brief:
    """一次深度研究的输入：问什么、研究谁、**截止到哪个时点**。

    `as_of` 是护城河在这一段的落点——所有取证都走 `available_at <= as_of` 的 PIT 过滤，
    同一份研报因此可以在历史时点重放。
    """

    question: str
    symbols: tuple[str, ...]
    as_of: datetime
    scope: str = ""  # 关注面（"估值"/"事件驱动"…），可为空
    notes: tuple[str, ...] = ()  # 解析期的如实标注（例如「问题里没听出标的，按默认池」）

    def to_payload(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "symbols": list(self.symbols),
            "as_of": self.as_of.isoformat(),
            "scope": self.scope,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """一条可回链的证据。字段与 M3 检索返回、M6 决策卡、M7 证据面板**同源**（PRD §5 硬性要求）。"""

    event_id: str
    day: str
    title: str
    event_time: str | None = None
    available_at: str | None = None
    event_type: str | None = None
    summary: str | None = None
    source: str | None = None
    original_source: str | None = None
    source_url: str | None = None
    content_hash: str | None = None
    industries: tuple[str, ...] = ()
    direction_norm: str | None = None
    importance_score: float | None = None

    @property
    def key(self) -> str:
        return evidence_key(self.event_id, self.day)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> EvidenceRef:
        """从 `to_payload()` 的形状回程构造（状态过 checkpointer 只能存纯 JSON）。"""
        return cls(
            event_id=str(payload.get("event_id") or ""),
            day=str(payload.get("day") or ""),
            title=str(payload.get("title") or ""),
            event_time=payload.get("event_time"),
            available_at=payload.get("available_at"),
            event_type=payload.get("event_type"),
            summary=payload.get("summary"),
            source=payload.get("source"),
            original_source=payload.get("original_source"),
            source_url=safe_source_url(payload.get("source_url")),
            content_hash=payload.get("content_hash"),
            industries=tuple(payload.get("industries") or ()),
            direction_norm=payload.get("direction_norm"),
            importance_score=payload.get("importance_score"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "event_id": self.event_id,
            "day": self.day,
            "title": self.title,
            "event_time": self.event_time,
            "available_at": self.available_at,
            "event_type": self.event_type,
            "summary": self.summary,
            "source": self.source,
            "original_source": self.original_source,
            "source_url": self.source_url,
            "content_hash": self.content_hash,
            "industries": list(self.industries),
            "direction_norm": self.direction_norm,
            "importance_score": self.importance_score,
        }


@dataclass(frozen=True, slots=True)
class RoleResult:
    """一个取证角色的产出。

    `errors` 非空 = 该角色**降级**（取数失败或解读失败），此时 `findings` / `evidence` 可能为空，
    但**其余角色与整条流程照常**——这是「拔掉任一数据源不崩溃」的实现口径。
    """

    role: str
    findings: tuple[str, ...] = ()
    numbers: dict[str, str] = field(default_factory=dict)  # 已格式化的同源数字（label → 文本）
    evidence: tuple[EvidenceRef, ...] = ()
    notes: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    model: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RoleResult:
        """回程构造（与 `to_payload` 对偶；`label` 是只读派生字段，不进构造）。"""
        return cls(
            role=str(payload.get("role") or ""),
            findings=tuple(payload.get("findings") or ()),
            numbers=dict(payload.get("numbers") or {}),
            evidence=tuple(EvidenceRef.from_payload(e) for e in payload.get("evidence") or ()),
            notes=tuple(payload.get("notes") or ()),
            errors=tuple(payload.get("errors") or ()),
            usage=dict(payload.get("usage") or {}),
            model=payload.get("model"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "label": ROLE_LABELS.get(self.role, self.role),
            "findings": list(self.findings),
            "numbers": dict(self.numbers),
            "evidence": [e.to_payload() for e in self.evidence],
            "notes": list(self.notes),
            "errors": list(self.errors),
            "usage": dict(self.usage),
            "model": self.model,
        }
