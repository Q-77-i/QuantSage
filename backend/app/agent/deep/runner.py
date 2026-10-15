"""深路径的运行编排：建运行 → 跑图（收节点事件）→ 组装报告 → 落库；以及 HITL 恢复。

四条实现口径：

1. **节点事件是过程可视化的真源**：从 `astream_events(v2)` 的 `on_chain_start/end` 取
   `metadata.langgraph_node`，按 `run_id` 配对算耗时——**被 interrupt 的节点只有 start 没有 end**
   （F5 实测），故持久化时把「起了没落」的节点标成 `interrupted`，而不是假装它没跑过。
2. **预算按运行重建**：`ctx.budget` 是进程内的活对象，恢复时必须从运行记录里的
   `budget` 快照接着算（`Budget.from_payload`），否则同一份运行在恢复后能再花一遍全额度。
3. **报告在流结束后组装**：`plain_json` + claim 闸门都在 `report.deep` 里，
   这里只管把状态里的四样东西（归并/辩论/综合/闸门）喂进去。
4. **入口与恢复是同一条路径**（`input=None` vs `input=Command(resume=…)`）——
   两条路径分开写，就会在「恢复后事件怎么记」这种地方漂移。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from langgraph.errors import NodeTimeoutError
from langgraph.types import Command

from app.agent.deep.debate import DebateTurn
from app.agent.deep.graph import DeepState, build_deep_graph
from app.agent.deep.merge import merge
from app.agent.deep.risk_gate import GateDecision, GateHit, GateResult
from app.agent.deep.roles.base import RoleContext
from app.agent.deep.synthesize import Draft
from app.agent.deep.types import Brief, RoleResult
from app.report.deep import assemble_deep_report, build_deep_body, run_facts_of
from app.report.snapshot import data_snapshot, plain_json, report_hash, snapshot_hash

log = logging.getLogger(__name__)

#: 运行状态（与 `research_runs.status` 同一套字面量）
STATUS_RUNNING = "running"
STATUS_AWAITING = "awaiting_approval"
STATUS_DONE = "done"
STATUS_DEGRADED = "degraded"
STATUS_FAILED = "failed"

Frame = tuple[str, dict[str, Any]]


@dataclass
class RunTrace:
    """一次运行的过程记录（落 `research_runs.events`）。

    每个事件都带 **`at`（相对本次运行的偏移秒）**——运行页要画的是**甘特条**，
    只有时长没有起点是画不出来的（并行的那几条得知道「从第几秒开始」）。
    """

    events: list[dict[str, Any]] = field(default_factory=list)
    _open: dict[str, float] = field(default_factory=dict)
    _t0: float = field(default_factory=time.perf_counter)

    def at(self) -> float:
        """相对本次运行的偏移秒（事件与 interrupt 都写它）。"""
        return round(time.perf_counter() - self._t0, 2)

    def node_start(self, node: str) -> dict[str, Any]:
        self._open[node] = time.perf_counter()
        event = {"phase": "start", "node": node, "at": self.at()}
        self.events.append(event)
        return event

    def node_end(self, node: str, *, status: str = "ok") -> dict[str, Any]:
        started = self._open.pop(node, None)
        seconds = round(time.perf_counter() - started, 2) if started else None
        event = {"phase": "end", "node": node, "seconds": seconds, "status": status, "at": self.at()}
        self.events.append(event)
        return event

    def close_open_nodes(self, *, status: str) -> None:
        """收尾：把「起了没落」的节点补成已结束（interrupt 就停在这种节点上）。"""
        for node in list(self._open):
            self.node_end(node, status=status)

    def to_payload(self) -> list[dict[str, Any]]:
        return list(self.events)


def state_turns(state: Mapping[str, Any]) -> tuple[DebateTurn, ...]:
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


def state_draft(state: Mapping[str, Any]) -> Draft:
    payload = state.get("draft") or {}
    return Draft(
        conclusion=str(payload.get("conclusion") or ""),
        disagreements=tuple(payload.get("disagreements") or ()),
        risks=tuple(payload.get("risks") or ()),
        citations=tuple(payload.get("citations") or ()),
        dropped_citations=tuple(payload.get("dropped_citations") or ()),
        model=str(payload.get("model") or ""),
        note=payload.get("note"),
    )


def state_gate(state: Mapping[str, Any]) -> GateResult | None:
    payload = state.get("gate")
    if not payload:
        return None
    return GateResult(
        decision=GateDecision(str(payload.get("decision") or GateDecision.PASS)),
        reason=str(payload.get("reason") or ""),
        hits=tuple(
            GateHit(rule=str(hit.get("rule") or ""), snippet=str(hit.get("snippet") or ""))
            for hit in payload.get("hits") or ()
        ),
        stray_numbers=tuple(payload.get("stray_numbers") or ()),
        approval=payload.get("approval"),
        notes=tuple(payload.get("notes") or ()),
    )


def _pending_of(snapshot: Any) -> dict[str, Any] | None:
    """从状态快照里取待批载荷。**事件流里没有它**——必须读 `tasks[].interrupts`（F5 实测）。"""
    for task in snapshot.tasks:
        for item in task.interrupts or ():
            value = item.value
            return dict(value) if isinstance(value, Mapping) else {"kind": "unknown", "detail": str(value)}
    return None


async def interrupt_payload(graph: Any, config: Mapping[str, Any]) -> dict[str, Any] | None:
    """取待批载荷（async）。

    ⚠ 必须走 **async** 接口：`AsyncPostgresSaver` 上同步 `get_state` 会抛
    `Synchronous calls to AsyncPostgresSaver are only allowed from a different thread`
    ——离线用例的 `InMemorySaver` 两个接口都有，**这条只有真库才现形**（集成用例逮到的）。
    """
    return _pending_of(await graph.aget_state(config))


async def astream_deep_run(
    *,
    ctx: RoleContext,
    checkpointer: Any,
    run_id: str,
    brief: Brief,
    resume: Mapping[str, Any] | None = None,
    max_rounds: int | None = None,
) -> AsyncIterator[Frame]:
    """跑（或恢复）一次深路径，逐帧产出过程事件。**不碰数据库**——落库由调用方做。

    帧集合：`node_start` / `node_end` / `interrupt` / `report` / `error` / `done`。
    """
    graph = build_deep_graph(ctx=ctx, checkpointer=checkpointer, max_rounds=max_rounds)
    config = {"configurable": {"thread_id": run_id}}
    trace = RunTrace()
    payload: Any = Command(resume=dict(resume)) if resume is not None else {
        "run_id": run_id,
        "brief": brief.to_payload(),
    }

    try:
        async for event in graph.astream_events(payload, config, version="v2"):
            kind = event.get("event")
            node = (event.get("metadata") or {}).get("langgraph_node")
            # **判据要两条**（实测）：① 有 `langgraph_node`；② 链名等于节点名。
            # 只判第一条会把**节点内部的子链**也算进来——条件边的 router（`debate_router` /
            # `gate_router`）同样带 `langgraph_node`，它瞬时结束，会把宿主节点的耗时截成 0.0s
            # （真跑读数里 bear 恒为 0.0s / bull 2.1s 就是这么来的）。
            # 另注：同一 superstep 里各链的 `run_id` **相同**，所以不能按 run_id 配对。
            if not node or event.get("name") != node:
                continue
            if kind == "on_chain_start":
                yield "node_start", trace.node_start(str(node))
            elif kind == "on_chain_end" and str(node) in trace._open:
                yield "node_end", trace.node_end(str(node))
    except NodeTimeoutError as exc:
        # 节点级墙钟兜底（E4 L2）：节点**挂住**了，不是「可预期的降级」。命中即整跑失败，
        # 文案点名是哪个节点——通用那句 `NodeTimeoutError: Node 'x' exceeded…` 太像内部堆栈。
        trace.close_open_nodes(status="failed")
        node, seconds = exc.node, exc.timeout or 0.0
        log.warning("深路径节点超时：%s（>%.0fs，已跑 %.1fs）", node, seconds, exc.elapsed)
        yield "error", {"message": f"节点「{node}」超时（>{seconds:.0f}s）——本次运行已中止"}
        yield "done", {
            "run_id": run_id,
            "status": STATUS_FAILED,
            "events": trace.to_payload(),
            "budget": ctx.budget.to_payload(),
        }
        return
    except Exception as exc:  # noqa: BLE001 —— 图整个炸了：如实报，不吞
        trace.close_open_nodes(status="failed")
        yield "error", {"message": f"{type(exc).__name__}: {exc}"}
        yield "done", {
            "run_id": run_id,
            "status": STATUS_FAILED,
            "events": trace.to_payload(),
            "budget": ctx.budget.to_payload(),
        }
        return

    pending = await interrupt_payload(graph, config)
    if pending is not None:
        trace.close_open_nodes(status="interrupted")
        # 待批载荷也记进事件流：**断连重连的客户端只有 `GET /research/runs/{id}` 这条路**
        # （SSE 帧是即时的，错过就没了）。前端从最后一条 `phase=interrupt` 里取它。
        event = {"phase": "interrupt", "at": trace.at(), "payload": pending}
        trace.events.append(event)
        # **帧与落库事件同形**（`{phase, at, payload}`）——前端两条路（实时 / 回放）
        # 因此能用同一套解析。摊平着发过一次，代价是前端多写一个分支且两处慢慢漂移。
        yield "interrupt", dict(event)
        yield "done", {
            "run_id": run_id,
            "status": STATUS_AWAITING,
            "events": trace.to_payload(),
            "budget": ctx.budget.to_payload(),
        }
        return

    state: DeepState = (await graph.aget_state(config)).values  # type: ignore[assignment]
    merged = merge([RoleResult.from_payload(item) for item in state.get("role_results") or []])
    turns = state_turns(state)
    draft = state_draft(state)
    gate = state_gate(state)
    status = STATUS_DEGRADED if (draft.note or merged.degraded) else STATUS_DONE

    facts = run_facts_of(
        run_id=run_id,
        status=status,
        brief=brief,
        merged=merged,
        rounds=int(state.get("debate_round") or 0),
        budget=ctx.budget,
        models={"fast": ctx.fast_model, "deep": ctx.deep_model},
        thinking=ctx.thinking,
    )
    body = build_deep_body(brief=brief, facts=facts, merged=merged, turns=turns, draft=draft, gate=gate)
    try:
        report = assemble_deep_report(body, merged=merged, turns=turns, draft=draft)
    except Exception as exc:  # noqa: BLE001 —— claim 闸门拒收：报告不落库，运行如实标失败
        yield "error", {"message": f"报告未过 claim 闸门：{type(exc).__name__}: {exc}"}
        yield "done", {
            "run_id": run_id,
            "status": STATUS_FAILED,
            "events": trace.to_payload(),
            "budget": ctx.budget.to_payload(),
        }
        return

    yield "report", {"run_id": run_id, "status": status, "report": report}
    yield "done", {
        "run_id": run_id,
        "status": status,
        "events": trace.to_payload(),
        "budget": ctx.budget.to_payload(),
        "report_hash": report_hash(report),
    }


def deep_snapshot() -> dict[str, Any]:
    """深研报的数据快照（与 M7 同一个函数：bars 逐片 sha + 语料 digest + 覆盖窗口）。"""
    return plain_json(data_snapshot())


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    return snapshot_hash(snapshot)
