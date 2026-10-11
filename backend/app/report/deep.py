"""深研报的 blocks 组装（M8b）：接 M7 容器的**同一结构、同一道闸门**。

三条纪律来自 M7 的既有约定，一条都不能松：

1. **产物一次组装即冻结**：最后一步 `plain_json` + `validate_claims`——UUID/Decimal/date 在
   这一步翻成纯 JSON，落库/分享/导出/算 hash 用的是同一棵树（M7 的 UUID 静默错就是这么来的）。
2. **事实块必须能自证**：要么 `numbers` 的键路径在正文里**值对得上**，要么 `evidence` 的键
   真实存在。所以这里把角色数字放进 `facts`（按角色分组的映射），块里引用 `facts.<role>.<标签>`。
   数字路径**不支持列表下标**（`_resolve_path` 只走 Mapping），故这一层是映射而非数组。
3. **推断块不带 numbers**：结论/分歧/风险三段是模型写的，只能挂 `text` 与模型署名。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.agent.deep.budget import Budget
from app.agent.deep.debate import DebateTurn, SIDE_LABELS, citation_index
from app.agent.deep.merge import MergedEvidence
from app.agent.deep.risk_gate import GateResult
from app.agent.deep.synthesize import Draft
from app.agent.deep.types import ROLE_LABELS, Brief
from app.report.builder import _block, validate_claims
from app.report.snapshot import plain_json

#: 深研报是**另一种报告**（与 `paper_account_report` 区分），但共用同一套渲染与导出
REPORT_KIND = "deep_research_report"

#: 块结构变了就递增（M7 的 `REPORT_VERSION` 是同一个用途；这里是深研报自己的版本）
REPORT_VERSION = 1


@dataclass(frozen=True, slots=True)
class DeepRunFacts:
    """运行事实（进 `body["run"]`，也是运行摘要块的数字来源）。"""

    run_id: str
    status: str
    as_of: str
    models: Mapping[str, str]
    thinking: bool
    debate_rounds: int
    evidence_count: int
    degraded_count: int
    llm_calls: int
    tokens: int
    cost_usd: float
    elapsed_s: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "as_of": self.as_of,
            "models": dict(self.models),
            "thinking": self.thinking,
            "debate_rounds": self.debate_rounds,
            "evidence_count": self.evidence_count,
            "degraded_count": self.degraded_count,
            "llm_calls": self.llm_calls,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed_s": round(self.elapsed_s, 2),
        }


def run_facts_of(
    *,
    run_id: str,
    status: str,
    brief: Brief,
    merged: MergedEvidence,
    rounds: int,
    budget: Budget,
    models: Mapping[str, str],
    thinking: bool,
) -> DeepRunFacts:
    return DeepRunFacts(
        run_id=run_id,
        status=status,
        as_of=brief.as_of.isoformat(),
        models=dict(models),
        thinking=thinking,
        debate_rounds=rounds,
        evidence_count=len(merged.evidence),
        degraded_count=len(merged.degraded),
        llm_calls=budget.llm_calls,
        tokens=budget.tokens,
        cost_usd=budget.cost_usd,
        elapsed_s=budget.elapsed_s,
    )


def _path_key(label: str) -> str:
    """标签 → 数字路径的键。

    **把点换成中点**：claim 闸门的路径按 `.` 切分，标签里带点会把路径切错段
    （而错法不是报错而是「路径不存在」——闸门会拒收，但那时已经白跑一次生成了）。
    建表与引用**共用这一个函数**，两边不会漂移。
    """
    return label.replace(".", "·")


def _figures(merged: MergedEvidence) -> dict[str, dict[str, str]]:
    """角色数字的**映射化**（按角色分组）——供块里的 numbers 路径引用（见模块 docstring 第 2 条）。"""
    return {
        result.role: {_path_key(label): value for label, value in merged.numbers_for(result.role).items()}
        for result in merged.results
        if result.numbers
    }


def _role_block(result_payload: Mapping[str, Any], numbers: Mapping[str, str]) -> dict[str, Any]:
    """一个角色一个事实块：有数字走数字路径，没数字就靠证据键自证。"""
    role = str(result_payload.get("role"))
    label = ROLE_LABELS.get(role, role)
    extra: dict[str, Any] = {
        "text": "\n".join(f"- {item}" for item in result_payload.get("findings") or [])
        or "（该角色本次没有解读要点，数字与证据见下）",
        "notes": list(result_payload.get("notes") or []),
        "model": result_payload.get("model"),
    }
    if numbers:
        extra["numbers"] = {f"facts.{role}.{_path_key(key)}": value for key, value in numbers.items()}
    else:
        extra["evidence"] = [item["key"] for item in result_payload.get("evidence") or []]
    return _block(f"role_{role}", label, **extra)


def build_deep_body(
    *,
    brief: Brief,
    facts: DeepRunFacts,
    merged: MergedEvidence,
    turns: Sequence[DebateTurn],
    draft: Draft,
    gate: GateResult | None,
) -> dict[str, Any]:
    """组装深研报正文（**不含 blocks**）——`assemble_deep_report` 会补上块并过闸门。"""
    return {
        "version": REPORT_VERSION,
        "kind": REPORT_KIND,
        "brief": brief.to_payload(),
        "run": facts.to_payload(),
        "facts": _figures(merged),
        "roles": [result.to_payload() for result in merged.results],
        "debate": [turn.to_payload() for turn in turns],
        "gate": (gate.to_payload() if gate else None),
        "gaps": list(merged.gaps),
        "evidence": [item.to_payload() for item in merged.evidence],
    }


def _debate_block(turns: Sequence[DebateTurn], *, rounds: int) -> dict[str, Any]:
    lines: list[str] = []
    for turn in turns:
        label = SIDE_LABELS.get(turn.side, turn.side)
        args = "；".join(turn.arguments) or "（本轮无新增论据）"
        cited = "、".join(turn.citations) or "—"
        lines.append(f"第 {turn.round} 轮 {label}：{turn.stance or '未表态'}｜论据：{args}｜引用：{cited}")
    keys = sorted(citation_index(turns))
    extra: dict[str, Any] = {
        "text": "\n".join(lines) or "（本次没有辩论记录）",
    }
    if keys:
        extra["evidence"] = keys
    else:
        # 一次引用都没有时不能挂 evidence（闸门会判「引用了不存在的证据」），
        # 改挂轮次数字自证——**取正文里的那一个值**（单一真源，别在这里另算一份）
        extra["numbers"] = {"run.debate_rounds": rounds}
    return _block("debate", "多空辩论", **extra)


def assemble_deep_report(
    body: Mapping[str, Any],
    *,
    merged: MergedEvidence,
    turns: Sequence[DebateTurn],
    draft: Draft,
) -> dict[str, Any]:
    """补上 blocks → `plain_json` → **过 claim 闸门**（不过即抛 `ClaimError`，调用方拒收不落库）。"""
    blocks: list[dict[str, Any]] = [
        _block(
            "overview",
            "研究概览",
            text=(
                f"问题：{body['brief']['question']}\n"
                f"标的：{'、'.join(body['brief']['symbols']) or '（未指定）'}\n"
                f"数据截止：{body['brief']['as_of']}（全部证据按 `available_at` 过滤，不偷看未来）"
            ),
            numbers={
                "run.evidence_count": body["run"]["evidence_count"],
                "run.degraded_count": body["run"]["degraded_count"],
                "run.debate_rounds": body["run"]["debate_rounds"],
            },
        )
    ]

    for result in merged.results:
        payload = result.to_payload()
        numbers = merged.numbers_for(result.role)
        if not numbers and not payload.get("evidence"):
            continue  # 空手而归的角色不单独成块，它在 gaps 里如实出现
        blocks.append(_role_block(payload, numbers))

    blocks.append(_debate_block(turns, rounds=body["run"]["debate_rounds"]))
    blocks.append(
        _block(
            "conclusion",
            "结论",
            kind="inference",
            text=draft.conclusion,
            note=draft.note,
            model=draft.model,
            prompt_version=draft.prompt_version,
        )
    )
    for block_id, title, items in (
        ("disagreements", "分歧点", draft.disagreements),
        ("risks", "风险提示", draft.risks),
    ):
        if items:
            blocks.append(_block(block_id, title, kind="inference", text="\n".join(f"- {i}" for i in items)))
    if draft.dropped_citations:
        blocks.append(
            _block(
                "dropped_citations",
                "被丢弃的引用",
                kind="inference",
                text="模型给出的以下引用不在证据集内，已剔除：" + "、".join(draft.dropped_citations),
            )
        )

    if body.get("gaps"):
        blocks.append(
            _block(
                "gaps",
                "如实标注：本次研究的缺口",
                text="\n".join(f"- {gap}" for gap in body["gaps"]),
                numbers={"run.degraded_count": body["run"]["degraded_count"]},
            )
        )

    blocks.append(
        _block(
            "run_summary",
            "运行摘要",
            text=(
                f"模型：取证/路由 {body['run']['models'].get('fast', '')}，"
                f"辩论/综合 {body['run']['models'].get('deep', '')}（思考：{'开' if body['run']['thinking'] else '关'}）"
            ),
            numbers={
                "run.llm_calls": body["run"]["llm_calls"],
                "run.tokens": body["run"]["tokens"],
                "run.cost_usd": body["run"]["cost_usd"],
                "run.elapsed_s": body["run"]["elapsed_s"],
            },
        )
    )

    final = plain_json({**dict(body), "blocks": blocks})
    validate_claims(final)
    return final
