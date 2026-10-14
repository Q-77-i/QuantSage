"""单点综合（pro 档，关思考）：把归并证据 + 辩论结果写成研报正文。

三条纪律：

1. **「读」并行、「写」单点**——综合只此一处，不并行（并行写会产生冲突决策，合并代价极高）。
2. **只允许引用归并证据集里的键**：模型给的引用过滤后留痕，编的引用进不了报告
   （与 claim 闸门同一条纪律）。
3. **失败降级不阻塞**：超时/报错 → 出一份「代码版结论」（证据汇总 + 缺口清单 + 辩论要点），
   并如实标注「本次未能生成综述」——报告照常落库，缺的是那一段话。
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from app.agent.deep.debate import DebateTurn, SIDE_LABELS, citation_index
from app.agent.deep.merge import MergedEvidence
from app.agent.deep.roles.base import RoleContext
from app.agent.deep.types import Brief, ROLE_LABELS
from app.core import llm

#: prompt 版本（进报告正文——综述以它为身份，重放要认得出来）
PROMPT_VERSION = "m8-synthesis-1"

#: 结论段长度上限（字）。研报正文的一段，不是长篇。
MAX_CONCLUSION_CHARS = 600


@dataclass(frozen=True, slots=True)
class Draft:
    """综合产物（或它的降级形态）。`note` 非空 = 这份是降级版。"""

    conclusion: str
    disagreements: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()
    dropped_citations: tuple[str, ...] = ()
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    usage: dict[str, Any] = field(default_factory=dict)
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.note is None

    def to_payload(self) -> dict[str, Any]:
        return {
            "conclusion": self.conclusion,
            "disagreements": list(self.disagreements),
            "risks": list(self.risks),
            "citations": list(self.citations),
            "dropped_citations": list(self.dropped_citations),
            "model": self.model,
            "prompt_version": self.prompt_version,
            "note": self.note,
        }


def synthesis_prompt(
    *,
    brief: Brief,
    merged: MergedEvidence,
    turns: Sequence[DebateTurn],
    feedback: str | None = None,
) -> str:
    """综合的 prompt。**纯函数**。`feedback` 是风控闸门要求重写时带回来的理由。"""
    lines = [
        "你是投研报告的撰写者。依据下面的取证与辩论，写一段中文结论。",
        f"研究问题：{brief.question}",
        f"研究标的：{'、'.join(brief.symbols) or '（未指定）'}　数据截止：{brief.as_of.date().isoformat()}",
        "",
        f"取证要点（{len(merged.results)} 个角色，其中 {len(merged.degraded)} 个降级）：",
    ]
    for result in merged.results:
        label = ROLE_LABELS.get(result.role, result.role)
        if result.findings:
            lines.append(f"【{label}】" + "；".join(result.findings))
        elif result.errors:
            lines.append(f"【{label}】不可用：{'；'.join(result.errors)}")
    if merged.gaps:
        lines += ["", "**已知缺口（必须在正文里如实提及）**：", *[f"- {gap}" for gap in merged.gaps]]
    lines += [
        "",
        f"可用证据（引用只能从下面的键里挑，共 {len(merged.evidence)} 条）：",
    ]
    for item in merged.evidence:
        lines.append(f"- [{item.key}] {item.title}")
    if turns:
        lines += ["", "辩论记录："]
        for turn in turns:
            label = SIDE_LABELS.get(turn.side, turn.side)
            args = "；".join(turn.arguments) or "（无）"
            lines.append(f"- 第 {turn.round} 轮 {label}（{turn.stance or '未表态'}）：{args}")
    lines += [
        "",
        f"请写一段不超过 {MAX_CONCLUSION_CHARS} 字的结论（可分段），另给分歧点与风险提示。",
        "硬要求：① **只能用上面出现过的数字**，不得计算或引入任何外部信息；",
        "② 不给投资建议、不预测股价、不做绝对化表述（如「必涨」「稳赚」）；",
        "③ 降级的角色与缺口必须在正文里如实提及，不得假装证据齐全。",
    ]
    if feedback:
        lines += ["", f"⚠ 上一版被风控闸门退回，原因：{feedback}。请修正后重写。"]
    lines += [
        "",
        "只输出一个 JSON 对象，不要其他文字：",
        '{"conclusion": "结论正文", "disagreements": ["分歧点1"], "risks": ["风险1"],'
        ' "citations": ["证据键1"]}',
    ]
    return "\n".join(lines)


def parse_draft(text: str, *, allowed_keys: Sequence[str], model: str) -> Draft:
    """解析综合结果。引用先过滤再加留痕；正文做长度截断。"""
    start, end = text.find("{"), text.rfind("}")
    payload: dict[str, Any] = {}
    if start != -1 and end > start:
        try:
            loaded = json.loads(text[start : end + 1])
            if isinstance(loaded, dict):
                payload = loaded
        except json.JSONDecodeError:
            payload = {}

    allowed = set(allowed_keys)
    citations: list[str] = []
    dropped: list[str] = []
    raw = payload.get("citations") or []
    if isinstance(raw, list):
        for item in raw:
            key = str(item).strip()
            if not key:
                continue
            if key in allowed:
                if key not in citations:
                    citations.append(key)
            elif key not in dropped:
                dropped.append(key)

    def _strings(value: Any) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        return tuple(str(item).strip() for item in value if str(item).strip())

    conclusion = str(payload.get("conclusion") or "").strip()
    if not conclusion:
        # 结构没解析出来时，把整段原文当结论用（宁可留白也不丢内容），但仍标注
        conclusion = text.strip()[:MAX_CONCLUSION_CHARS]
    return Draft(
        conclusion=conclusion[:MAX_CONCLUSION_CHARS],
        disagreements=_strings(payload.get("disagreements")),
        risks=_strings(payload.get("risks")),
        citations=tuple(citations),
        dropped_citations=tuple(dropped),
        model=model,
    )


def code_fallback(*, merged: MergedEvidence, turns: Sequence[DebateTurn], note: str) -> Draft:
    """代码版结论：**没有 LLM 也要出一份能落库的正文**。

    组成 = 各角色要点 + 辩论引用分布 + 缺口清单。数字全部来自 M8a 的 `numbers`（可复算），
    故这份降级产物同样过得了 claim 闸门。
    """
    lines: list[str] = ["（本次未能生成综述，以下为程序汇总的取证结果）"]
    for result in merged.results:
        label = ROLE_LABELS.get(result.role, result.role)
        if result.findings:
            lines.append(f"【{label}】" + "；".join(result.findings))
        elif result.errors:
            lines.append(f"【{label}】不可用：{'；'.join(result.errors)}")
    index = citation_index(turns)
    if index:
        rendered = "、".join(f"{key}×{count}" for key, count in sorted(index.items()))
        lines.append(f"辩论引用分布：{rendered}")
    if merged.gaps:
        lines.append("缺口：" + "；".join(merged.gaps))
    return Draft(conclusion="\n".join(lines)[:MAX_CONCLUSION_CHARS], note=note)


async def build_draft(
    ctx: RoleContext,
    *,
    merged: MergedEvidence,
    turns: Sequence[DebateTurn],
    feedback: str | None = None,
) -> Draft:
    """跑一次综合。任何失败都降级为代码版结论（**不向上抛**）。"""
    allowed_keys = [item.key for item in merged.evidence]
    started = time.perf_counter()
    try:
        result = await llm.complete(
            synthesis_prompt(brief=ctx.brief, merged=merged, turns=turns, feedback=feedback),
            model=ctx.deep_model,
            timeout=ctx.timeout,
            chat=ctx.chat,
            thinking=ctx.thinking,
        )
    except TimeoutError:
        ctx.budget.record_wait(time.perf_counter() - started)
        return code_fallback(merged=merged, turns=turns, note=f"综合超时（>{ctx.timeout:.0f}s）")
    except Exception as exc:  # noqa: BLE001
        ctx.budget.record_wait(time.perf_counter() - started)
        return code_fallback(merged=merged, turns=turns, note=f"综合不可用（{type(exc).__name__}）")

    ctx.budget.record_llm(ctx.deep_model, result.usage, seconds=time.perf_counter() - started)
    draft = parse_draft(result.text, allowed_keys=allowed_keys, model=ctx.deep_model)
    return replace(draft, usage=dict(result.usage or {}))
