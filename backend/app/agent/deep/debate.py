"""Bull ⇄ Bear 辩论：**≤3 轮硬上限**，收敛判据必须可断言。

三条纪律：

1. **硬上限不跟模型商量**（PRD 验收写的是「辩论严格 ≤3 轮」）：`should_continue` 是纯函数，
   `round >= MAX_ROUNDS_HARD` 一律停——一个每轮都能编出新论据的模型也截得住。
   默认轮数取配置（2 轮，省钱且演示得出来），硬上限 3 不随配置走。
2. **收敛判据可断言**，不是「感觉收敛了」：本轮相对上一轮**没有新增论据、也没有新增引用**
   才算收敛（`converged`）。判据纯函数化，测试里造得出「永不收敛」的模型。
3. **引用只能是归并证据集里的键**：模型给的引用先过一遍 `allowed_keys`，不在集合里的丢掉
   并留痕（`dropped_citations`）——辩论不能凭空引用，这与 claim 闸门是同一条纪律。

双方拿**同一份归并证据**（不做信息隔离：那是另一条产品假设，本轮不做）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agent.deep.roles.base import RoleContext
from app.agent.deep.types import Brief, EvidenceRef
from app.core import llm

SIDE_BULL = "bull"
SIDE_BEAR = "bear"
SIDES = (SIDE_BULL, SIDE_BEAR)

SIDE_LABELS = {SIDE_BULL: "多方", SIDE_BEAR: "空方"}

#: PRD 的硬上限：**配置不能突破它**
MAX_ROUNDS_HARD = 3

#: 一轮发言最多几条论据（控 token；辩论阶段的成本是分析阶段的 3–5 倍）
MAX_ARGUMENTS = 4


@dataclass(frozen=True, slots=True)
class DebateTurn:
    """一次发言。`citations` 已过滤到归并证据集内。"""

    round: int
    side: str
    stance: str = ""
    arguments: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def label(self) -> str:
        return SIDE_LABELS.get(self.side, self.side)

    def to_payload(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "side": self.side,
            "label": self.label,
            "stance": self.stance,
            "arguments": list(self.arguments),
            "citations": list(self.citations),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class TurnResult:
    turn: DebateTurn
    error: str | None = None


def debate_prompt(
    side: str,
    *,
    round_: int,
    brief: Brief,
    evidence: Sequence[EvidenceRef],
    transcript: Sequence[DebateTurn],
) -> str:
    """一次发言的 prompt。**纯函数**（可测、可复现）。

    证据**按原样带键**给出：引用只能从这些键里挑，`citations` 因此是可校验的。
    """
    label = SIDE_LABELS.get(side, side)
    lines = [
        f"你是 A 股个股研究里的**{label}**（{'看多' if side == SIDE_BULL else '看空'}）。"
        f"这是第 {round_} 轮发言。",
        f"研究问题：{brief.question}",
        f"研究标的：{'、'.join(brief.symbols) or '（未指定）'}　数据截止：{brief.as_of.date().isoformat()}",
        "",
        "可用证据（**引用只能从下面的键里挑**，写成 citations）：",
    ]
    for item in evidence:
        lines.append(f"- [{item.key}] {item.event_time or item.day}　{item.title}")
    if transcript:
        lines += ["", "前面几轮的发言："]
        for turn in transcript:
            stance = turn.stance or "（未表态）"
            args = "；".join(turn.arguments) or "（无）"
            lines.append(f"- 第 {turn.round} 轮 {turn.label}：{stance}｜论据：{args}")
    lines += [
        "",
        f"请给出你这一轮最有力的 {MAX_ARGUMENTS} 条以内论据（每条 ≤ 40 字）。",
        "硬要求：① 只能用上面证据或前轮发言里出现过的数字，不得计算或引入外部信息；",
        "② 必须正面回应对方上一轮的最强论据（若这是第一轮则陈述你的核心逻辑）；",
        "③ 不给投资建议、不预测股价。",
        "",
        "只输出一个 JSON 对象，不要其他文字：",
        '{"stance": "一句话立场", "arguments": ["论据1", "论据2"],'
        ' "citations": ["证据键1", "证据键2"]}',
    ]
    return "\n".join(lines)


def parse_turn(text: str, *, allowed_keys: Sequence[str]) -> dict[str, Any]:
    """解析一次发言。容忍围栏与前后闲话；引用先过滤再加留痕。"""
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
    raw_citations = payload.get("citations") or []
    citations: list[str] = []
    dropped: list[str] = []
    if isinstance(raw_citations, list):
        for item in raw_citations:
            key = str(item).strip()
            if not key:
                continue
            if key in allowed:
                if key not in citations:
                    citations.append(key)
            elif key not in dropped:
                dropped.append(key)

    raw_arguments = payload.get("arguments") or []
    arguments: tuple[str, ...] = ()
    if isinstance(raw_arguments, list):
        arguments = tuple(
            str(item).strip() for item in raw_arguments[:MAX_ARGUMENTS] if str(item).strip()
        )

    return {
        "stance": str(payload.get("stance") or "").strip(),
        "arguments": arguments,
        "citations": tuple(citations),
        "dropped_citations": tuple(dropped),
    }


def _turn_signature(turn: DebateTurn) -> set[str]:
    """一轮发言的「材料」指纹：论据与引用都算（去重后比对）。"""
    return {f"arg:{a}" for a in turn.arguments} | {f"cite:{c}" for c in turn.citations}


def converged(turns: Sequence[DebateTurn]) -> bool:
    """本轮相对上一轮**没有新增任何材料** ⇒ 收敛（提前停，省一轮钱）。

    需要至少两个完整轮次（双方各发过言）才有「相对上一轮」可言。
    """
    rounds = sorted({turn.round for turn in turns})
    if len(rounds) < 2:
        return False
    previous, current = rounds[-2], rounds[-1]
    if not any(t.round == previous and t.side == SIDE_BULL for t in turns):
        return False
    if not any(t.round == current and t.side == SIDE_BEAR for t in turns):
        return False

    before = set().union(*(_turn_signature(t) for t in turns if t.round <= previous))
    now = set().union(*(_turn_signature(t) for t in turns if t.round == current))
    return not (now - before)


def should_continue(*, round_: int, max_rounds: int, turns: Sequence[DebateTurn]) -> bool:
    """还要不要再来一轮。**硬上限优先于配置**，配置优先于收敛判据。"""
    if round_ >= MAX_ROUNDS_HARD:
        return False
    if round_ >= max(1, max_rounds):
        return False
    return not converged(turns)


async def run_turn(
    ctx: RoleContext,
    *,
    side: str,
    round_: int,
    evidence: Sequence[EvidenceRef],
    transcript: Sequence[DebateTurn],
    model: str | None = None,
) -> TurnResult:
    """跑一次发言。模型失败**降级成空发言**（`error` 留痕）——不让辩论卡死。

    用深度档（pro），但同样**关思考**（F1d：开思考时 pro 单次 107s，辩论要跑 4 次以上）。
    """
    settings_model = model or ctx.deep_model
    allowed_keys = [item.key for item in evidence]
    started = time.perf_counter()
    try:
        result = await llm.complete(
            debate_prompt(side, round_=round_, brief=ctx.brief, evidence=evidence, transcript=transcript),
            model=settings_model,
            timeout=ctx.timeout,
            chat=ctx.chat,
            thinking=ctx.thinking,
        )
    except TimeoutError:
        ctx.budget.record_wait(time.perf_counter() - started)
        return TurnResult(
            DebateTurn(round=round_, side=side, error=f"发言超时（>{ctx.timeout:.0f}s）"),
            error=f"发言超时（>{ctx.timeout:.0f}s）",
        )
    except Exception as exc:  # noqa: BLE001 —— 辩论失败不拖垮整条流程
        ctx.budget.record_wait(time.perf_counter() - started)
        return TurnResult(
            DebateTurn(round=round_, side=side, error=f"发言不可用（{type(exc).__name__}）"),
            error=f"发言不可用（{type(exc).__name__}）",
        )

    # 都记「实际用的那个模型」：降级链换过档时按请求的模型计价是虚高（SPEC §5.2）
    ctx.budget.record_llm(result.model, result.usage, seconds=time.perf_counter() - started)
    parsed = parse_turn(result.text, allowed_keys=allowed_keys)
    return TurnResult(
        DebateTurn(
            round=round_,
            side=side,
            stance=parsed["stance"],
            arguments=parsed["arguments"],
            citations=parsed["citations"],
            usage=result.usage,
        )
    )


def transcript_payload(turns: Sequence[DebateTurn]) -> list[dict[str, Any]]:
    return [turn.to_payload() for turn in turns]


def citation_index(turns: Sequence[DebateTurn]) -> dict[str, int]:
    """每个证据键被双方引用了几次（综合与报告用它看哪些证据真正进了辩论）。"""
    counter: dict[str, int] = {}
    for turn in turns:
        for key in turn.citations:
            counter[key] = counter.get(key, 0) + 1
    return counter
