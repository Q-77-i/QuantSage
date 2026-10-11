"""深路径图（M8b）：并行取证 → 归并 → 辩论 → 综合 → 代码化风控闸门（+ 超限 `interrupt()`）。

```
START ─┬─ market ─┐
       ├─ news ───┤
       ├─ fundamental ─┼─► merge_evidence ─► bull ─► bear ─┬─(未收敛且未到上限)─► bull
       ├─ policy ─┤                                        └─(收敛或到上限)────► synthesize
       └─ factor ─┘                                                                   │
                                                              ┌──(要求重写，一次)──────┤
                                                              ▼                       ▼
                                                          synthesize            risk_gate ─► END
                                                                                  （超限则 interrupt 等人）
```

四条实现纪律：

1. **每个角色节点自带 try/except**：同 superstep 内任一节点抛异常会取消**全部兄弟节点**，
   所以取数炸掉必须就地翻成 `errors`（这是「拔掉任一数据源不崩溃」的落点）。
2. **`ctx` 走闭包、不进状态**：状态要过 checkpointer（只能存纯 JSON），而 `RoleContext`
   持有 Budget 与（测试注入的）假模型——图因此**按运行构建**（编译开销毫秒级）。
3. **预算快照进状态**：`ctx.budget` 是活的累加器，每个节点结束时把 `to_payload()` 写回状态，
   让运行记录能如实显示「跑到哪一步花了多少」；新进程恢复时由 `Budget.from_payload` 重建。
4. **重写只给一次**：`rewrite_used` 进状态，闸门第二次判到同一问题就转降级（不无限重写）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent.deep.budget import Budget
from app.agent.deep.debate import (
    MAX_ROUNDS_HARD,
    SIDE_BEAR,
    SIDE_BULL,
    DebateTurn,
    run_turn,
    should_continue,
)
from app.agent.deep.merge import merge
from app.agent.deep.risk_gate import GateDecision, evaluate
from app.agent.deep.roles import ROLE_RUNNERS
from app.agent.deep.roles.base import RoleContext, allowed_for
from app.agent.deep.synthesize import Draft, build_draft
from app.agent.deep.types import ROLES, EvidenceRef, RoleResult

#: 节点名（过程事件的键）。与 `ROLES` 同名——角色节点直接叫 market / news / …
MERGE_NODE = "merge_evidence"
BULL_NODE = "bull"
BEAR_NODE = "bear"
SYNTH_NODE = "synthesize"
GATE_NODE = "risk_gate"

STAGE_ORDER: tuple[str, ...] = (*ROLES, MERGE_NODE, BULL_NODE, BEAR_NODE, SYNTH_NODE, GATE_NODE)


def _add(left: list, right: list) -> list:
    return (left or []) + (right or [])


def _freshest_budget(left: dict | None, right: dict | None) -> dict:
    """预算快照的 reducer：五个角色节点在**同一个 superstep** 各写一次，必须能合并。

    计数是单调累加的，所以「调用次数更大的那份快照」就是更新的那份——取大即可，
    不必求和（求和会把同一笔调用算多遍）。
    """
    if not left:
        return right or {}
    if not right:
        return left
    return right if int(right.get("llm_calls") or 0) >= int(left.get("llm_calls") or 0) else left


class DeepState(TypedDict, total=False):
    """图状态。**只放纯 JSON**（checkpointer 要序列化；dataclass 走 payload）。"""

    run_id: str
    brief: dict
    role_results: Annotated[list[dict], _add]
    errors: Annotated[list[str], _add]
    merged: dict
    debate: Annotated[list[dict], _add]
    debate_round: int
    draft: dict
    rewrite_used: bool
    gate: dict
    approval: dict | None
    budget: Annotated[dict, _freshest_budget]


def _role_node(role: str, ctx: RoleContext):
    """造一个角色节点：**异常就地翻成 errors**，绝不上抛（上抛会取消兄弟节点）。"""

    async def node(state: DeepState) -> dict[str, Any]:
        try:
            result = await ROLE_RUNNERS[role](ctx)
        except Exception as exc:  # noqa: BLE001 —— 一处收口，见模块 docstring 第 1 条
            result = RoleResult(role, errors=(f"取证异常（{type(exc).__name__}: {exc}）",))
        payload = result.to_payload()
        update: dict[str, Any] = {"role_results": [payload], "budget": ctx.budget.to_payload()}
        if result.errors:
            update["errors"] = [f"「{role}」：{e}" for e in result.errors]
        return update

    node.__name__ = f"node_{role}"
    return node


def _merged_evidence(state: DeepState):
    return merge([RoleResult.from_payload(p) for p in state.get("role_results") or []])


def _turns(state: DeepState) -> tuple[DebateTurn, ...]:
    return tuple(
        DebateTurn(
            round=int(item.get("round") or 0),
            side=str(item.get("side") or ""),
            stance=str(item.get("stance") or ""),
            arguments=tuple(item.get("arguments") or ()),
            citations=tuple(item.get("citations") or ()),
            error=item.get("error"),
        )
        for item in state.get("debate") or []
    )


def _evidence_of(state: DeepState) -> tuple[EvidenceRef, ...]:
    merged = state.get("merged") or {}
    return tuple(EvidenceRef.from_payload(item) for item in merged.get("evidence") or ())


def build_deep_graph(
    *, ctx: RoleContext, checkpointer: Any = None, max_rounds: int | None = None
):
    """构建并编译深路径图。`ctx` 按运行注入（含 Budget 与可选假模型）。"""
    from app.core.config import get_settings

    rounds = min(max_rounds or get_settings().research_debate_rounds, MAX_ROUNDS_HARD)

    async def merge_node(state: DeepState) -> dict[str, Any]:
        merged = _merged_evidence(state)
        return {"merged": merged.to_payload(), "budget": ctx.budget.to_payload()}

    async def bull_node(state: DeepState) -> dict[str, Any]:
        round_ = int(state.get("debate_round") or 0) + 1
        result = await run_turn(
            ctx,
            side=SIDE_BULL,
            round_=round_,
            evidence=_evidence_of(state),
            transcript=_turns(state),
        )
        update: dict[str, Any] = {
            "debate": [result.turn.to_payload()],
            "budget": ctx.budget.to_payload(),
        }
        if result.error:
            update["errors"] = [f"多方第 {round_} 轮：{result.error}"]
        return update

    async def bear_node(state: DeepState) -> dict[str, Any]:
        round_ = int(state.get("debate_round") or 0) + 1
        result = await run_turn(
            ctx,
            side=SIDE_BEAR,
            round_=round_,
            evidence=_evidence_of(state),
            transcript=_turns(state),
        )
        update: dict[str, Any] = {
            "debate": [result.turn.to_payload()],
            "debate_round": round_,
            "budget": ctx.budget.to_payload(),
        }
        if result.error:
            update["errors"] = [f"空方第 {round_} 轮：{result.error}"]
        return update

    def debate_router(state: DeepState) -> str:
        keep_going = should_continue(
            round_=int(state.get("debate_round") or 0),
            max_rounds=rounds,
            turns=_turns(state),
        )
        return BULL_NODE if keep_going else SYNTH_NODE

    async def synthesize_node(state: DeepState) -> dict[str, Any]:
        gate = state.get("gate") or {}
        feedback = gate.get("reason") if gate.get("decision") == str(GateDecision.REWRITE) else None
        draft = await build_draft(
            ctx, merged=_merged_evidence(state), turns=_turns(state), feedback=feedback
        )
        return {"draft": draft.to_payload(), "budget": ctx.budget.to_payload()}

    async def gate_node(state: DeepState) -> dict[str, Any]:
        draft_payload = state.get("draft") or {}
        draft = Draft(
            conclusion=str(draft_payload.get("conclusion") or ""),
            disagreements=tuple(draft_payload.get("disagreements") or ()),
            risks=tuple(draft_payload.get("risks") or ()),
            citations=tuple(draft_payload.get("citations") or ()),
            dropped_citations=tuple(draft_payload.get("dropped_citations") or ()),
            model=str(draft_payload.get("model") or ""),
            note=draft_payload.get("note"),
        )
        merged = _merged_evidence(state)
        numbers: dict[str, str] = dict(merged.numbers)
        allowed = allowed_for(
            material=synthesis_material(state),
            numbers=numbers,
            evidence=merged.evidence,
            symbols=tuple((state.get("brief") or {}).get("symbols") or ()),
        )

        result = evaluate(
            draft,
            allowed=allowed,
            budget=ctx.budget,
            rewrite_used=bool(state.get("rewrite_used")),
        )

        if result.decision is GateDecision.APPROVAL:
            decision = interrupt(result.approval)
            choice = str((decision or {}).get("choice") or "continue")
            if choice == "stop":
                note = f"人工审批选择停止：{result.reason}"
                return {
                    "gate": {**result.to_payload(), "decision": str(GateDecision.DEGRADE),
                             "approval_choice": choice},
                    "draft": {**draft_payload, "note": note},
                    "budget": ctx.budget.to_payload(),
                }
            return {
                "gate": {**result.to_payload(), "approval_choice": choice},
                "budget": ctx.budget.to_payload(),
            }

        if result.decision is GateDecision.REWRITE:
            return {
                "gate": result.to_payload(),
                "rewrite_used": True,
                "budget": ctx.budget.to_payload(),
            }

        if result.decision is GateDecision.DEGRADE:
            return {
                "gate": result.to_payload(),
                "draft": {**draft_payload, "note": result.reason},
                "budget": ctx.budget.to_payload(),
            }

        return {"gate": result.to_payload(), "budget": ctx.budget.to_payload()}

    def gate_router(state: DeepState) -> str:
        decision = (state.get("gate") or {}).get("decision")
        return SYNTH_NODE if decision == str(GateDecision.REWRITE) else END

    builder = StateGraph(DeepState)
    for role in ROLES:
        builder.add_node(role, _role_node(role, ctx))
        builder.add_edge(START, role)
        builder.add_edge(role, MERGE_NODE)
    builder.add_node(MERGE_NODE, merge_node)
    builder.add_node(BULL_NODE, bull_node)
    builder.add_node(BEAR_NODE, bear_node)
    builder.add_node(SYNTH_NODE, synthesize_node)
    builder.add_node(GATE_NODE, gate_node)
    builder.add_edge(MERGE_NODE, BULL_NODE)
    builder.add_edge(BULL_NODE, BEAR_NODE)
    builder.add_conditional_edges(BEAR_NODE, debate_router, [BULL_NODE, SYNTH_NODE])
    builder.add_edge(SYNTH_NODE, GATE_NODE)
    builder.add_conditional_edges(GATE_NODE, gate_router, [SYNTH_NODE, END])
    return builder.compile(checkpointer=checkpointer)


def synthesis_material(state: DeepState) -> str:
    """闸门构造允许数字集时用的「材料」= **综合当时看到的那份东西**。

    与 `synthesis_prompt` 同源：取证要点与数字 + 证据标题摘要 + 辩论论据。
    口径与 M8a 的角色层一致（闸门写窄会把要点全灭），但**绝不能把结论本身算进来**——
    那会让数字检查变成空转（结论里写什么数字，什么数字就「有出处」）。
    """
    parts: list[str] = []
    merged = state.get("merged") or {}
    for item in merged.get("evidence") or []:
        parts.append(str(item.get("title") or ""))
        parts.append(str(item.get("summary") or ""))
    for role in merged.get("roles") or []:
        parts.extend(role.get("findings") or [])
        parts.extend(f"{k} {v}" for k, v in (role.get("numbers") or {}).items())
        parts.extend(role.get("notes") or [])
    for turn in state.get("debate") or []:
        parts.extend(turn.get("arguments") or [])
        parts.append(str(turn.get("stance") or ""))
    return "\n".join(part for part in parts if part)


def stage_timeline(events: Sequence[dict]) -> list[dict]:
    """节点事件 → 阶段序列（给运行页画阶段环；也是测试里断言顺序的抓手）。"""
    return [
        {"node": event.get("node"), "seconds": event.get("seconds"), "status": event.get("status")}
        for event in events
        if event.get("phase") == "end"
    ]
