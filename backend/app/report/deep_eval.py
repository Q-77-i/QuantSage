"""深研报 rubric 评测的**纯函数**（P3 收尾项 5c）。

与 `build_rag_eval.py`（检索级 NDCG）分开：这里评的是**报告级**质量，分两半——

* **自动项**：状态、风控闸门、缺口数、辩论轮次、证据条数、PIT 违规（证据 `available_at`
  是否越过 `as_of`）、预算与耗时。全是确定性读数，离线可测。
* **主观项**：LLM-judge 五维 1–5。judge 的输出只作为**读数**，不设及格线、不进 CI；
  失败记 null，绝不伪造分数。

本模块不调模型、不读数据库；脚本 `build_deep_eval.py` 负责跑深路径与写盘。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

#: judge 维度（顺序固定，前端/报告按此排）
DIMENSIONS = ("pit", "evidence", "numbers", "balance", "risk")
DIMENSION_LABELS = {
    "pit": "PIT 合规",
    "evidence": "证据充分",
    "numbers": "数字可追溯",
    "balance": "多空平衡",
    "risk": "风险与边界",
}


def _parse_moment(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        from app.backtest.types import CN_TZ

        moment = moment.replace(tzinfo=CN_TZ)
    return moment


def evidence_pit_violations(report: Mapping[str, Any]) -> list[str]:
    """返回 `available_at > as_of` 的证据键（正常情况下应为空；非空就是红线）。"""
    brief = report.get("brief") or {}
    run = report.get("run") or {}
    as_of = _parse_moment(brief.get("as_of") or run.get("as_of"))
    if as_of is None:
        return []
    violations: list[str] = []
    for evidence in report.get("evidence") or []:
        available = _parse_moment(evidence.get("available_at"))
        if available is not None and available > as_of:
            key = evidence.get("key") or (
                f"{evidence.get('event_id')}|{evidence.get('day')}"
                if evidence.get("event_id")
                else str(evidence.get("title") or "?")
            )
            violations.append(str(key))
    return violations


def auto_score(
    report: Mapping[str, Any] | None,
    *,
    status: str,
    budget: Mapping[str, Any] | None = None,
    elapsed_s: float | None = None,
) -> dict[str, Any]:
    """确定性读数。`report is None`（运行失败/未组装）时只记状态与预算。"""
    budget = budget or {}
    base: dict[str, Any] = {
        "status": status,
        "gate": None,
        "stray_numbers": 0,
        "gaps": 0,
        "debate_rounds": 0,
        "debate_within_cap": True,
        "evidence_count": 0,
        "pit_violations": [],
        "report_assembled": False,
        "llm_calls": budget.get("llm_calls"),
        "tokens": budget.get("tokens"),
        "cost_usd": budget.get("cost_usd"),
        "elapsed_s": elapsed_s,
    }
    if report is None:
        return base

    gate = report.get("gate") or {}
    debate = report.get("debate") or []
    rounds = len(
        {turn.get("round") for turn in debate if turn.get("round") is not None}
    )
    base.update(
        {
            "gate": gate.get("decision"),
            "stray_numbers": len(gate.get("stray_numbers") or []),
            "gaps": len(report.get("gaps") or []),
            "debate_rounds": rounds,
            "debate_within_cap": rounds <= 3,
            "evidence_count": len(report.get("evidence") or []),
            "pit_violations": evidence_pit_violations(report),
            "report_assembled": bool(report.get("blocks")),
        }
    )
    return base


def parse_judge(text: str) -> dict[str, Any] | None:
    """容错解析 judge 的 JSON；五个维度必须齐全且为 1–5，否则返回 None（不猜）。"""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    scores: dict[str, int] = {}
    for dimension in DIMENSIONS:
        value = payload.get(dimension)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        score = int(value)
        if not 1 <= score <= 5:
            return None
        scores[dimension] = score
    reason = str(payload.get("reason") or "").strip()
    return {"scores": scores, "reason": reason[:300]}


def aggregate(items: list[Mapping[str, Any]]) -> dict[str, Any]:
    """聚合读数：自动项计数 + judge 各维均值；没有 judge 的题不参与均值。"""
    judged = [item["judge"] for item in items if item.get("judge")]
    averages: dict[str, float | None] = {}
    for dimension in DIMENSIONS:
        values = [
            judge["scores"][dimension]
            for judge in judged
            if isinstance(judge.get("scores"), Mapping)
            and isinstance(judge["scores"].get(dimension), (int, float))
        ]
        averages[dimension] = round(sum(values) / len(values), 2) if values else None
    return {
        "count": len(items),
        "report_assembled": sum(1 for item in items if item["auto"].get("report_assembled")),
        "pit_violations": sum(len(item["auto"].get("pit_violations") or []) for item in items),
        "debate_within_cap": sum(
            1 for item in items if item["auto"].get("debate_within_cap")
        ),
        "judge_count": len(judged),
        "avg_judge": averages,
    }


def render_markdown(items: list[Mapping[str, Any]], meta: Mapping[str, Any]) -> str:
    """产物的一页纸：读数 + 聚合；**不设及格线**，不把分数写成 CI 结论。"""
    lines = [
        "# 深研报评测（rubric 手动轨）",
        "",
        f"> 数据集：`{meta.get('dataset', '')}`（{meta.get('count', 0)} 题）"
        f" · 生成于 {meta.get('generated_at', '')}",
        f"> 模型：路由/取证 `{meta.get('fast_model', '')}` · 辩论/综合 `{meta.get('deep_model', '')}`"
        f" · judge `{meta.get('judge_model', '')}`",
        "> **不设及格线**：本表是读数，不是 CI 门；judge 失败记「—」，不伪造分数。",
        "",
        "| # | 时点 | 状态 | 闸门 | 证据 | PIT 违规 | 辩论轮 | 缺口 | judge（PIT/证据/数字/平衡/风险） | 说明 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in items:
        auto = item["auto"]
        judge = item.get("judge")
        if judge:
            scores = judge["scores"]
            judge_text = " / ".join(str(scores[d]) for d in DIMENSIONS)
        else:
            judge_text = "—"
        reason = str(judge.get("reason") if judge else "") or str(item.get("error") or "")
        row = (
            "| {id} | {as_of} | {status} | {gate} | {evidence} | {pit} | "
            "{rounds} | {gaps} | {judge} | {reason} |"
        )
        lines.append(
            row.format(
                id=item.get("id", ""),
                as_of=item.get("as_of", ""),
                status=auto.get("status") or "—",
                gate=auto.get("gate") or "—",
                evidence=auto.get("evidence_count", 0),
                pit=len(auto.get("pit_violations") or []),
                rounds=auto.get("debate_rounds", 0),
                gaps=auto.get("gaps", 0),
                judge=judge_text,
                reason=reason.replace("|", "／")[:120],
            )
        )
    summary = aggregate(items)
    lines += [
        "",
        "## 聚合",
        "",
        f"- 报告组装成功：{summary['report_assembled']} / {summary['count']}",
        f"- PIT 违规证据：**{summary['pit_violations']}** 条（应为 0）",
        f"- 辩论 ≤3 轮：{summary['debate_within_cap']} / {summary['count']}",
        f"- judge：{summary['judge_count']} 题参与；各维均值 "
        + " / ".join(
            f"{DIMENSION_LABELS[d]} {summary['avg_judge'][d] if summary['avg_judge'][d] is not None else '—'}"
            for d in DIMENSIONS
        ),
        "",
        "> 读法：自动项是硬事实（PIT 违规=0 是红线）；judge 分数是主观读数，"
        "只用于看趋势与找弱项，不当作「通过/不通过」。",
        "",
    ]
    return "\n".join(lines)
