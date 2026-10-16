"""深路径端点（M8b）：`/api/v1/research`，全部受保护；越权与不存在同为 404。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/research/runs` | 建运行并启动 → **SSE**（`mode=auto` 时先过意图路由，判 quick 就只回一帧 `route`） |
| POST | `/research/runs/{id}/resume` | HITL 恢复 → SSE |
| GET | `/research/runs` | 本人的运行列表（摘要） |
| GET | `/research/runs/{id}` | 运行详情（事件回放：断连重连与事后回看都走它） |

两条与 chat 同源的姿态：

* **断连不取消**：图跑到一半客户端走了，**pump 任务继续跑完并落库**——否则一个中断的
  运行会永久停在 `running`，用户回来什么都看不到（P1 定的口径，这里照搬）。
* **一帧一事件**：帧编码复用 `app.api.sse`；`done` 帧带最终状态与预算，客户端据此收尾。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.agent.deep.budget import Budget, Limits
from app.agent.deep.graph import MAX_ROUNDS_HARD
from app.agent.deep.roles.base import RoleContext
from app.agent.deep.router import PATH_DEEP, RouteDecision, build_brief, route_question
from app.agent.deep.runner import (
    STATUS_AWAITING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    Frame,
    astream_deep_run,
    deep_snapshot,
    snapshot_digest,
)
from app.agent.deep.types import Brief
from app.api.limits import limit
from app.api.sse import sse_frame, sse_response
from app.core import quota
from app.core.auth import require_db, require_user
from app.core.config import get_settings
from app.core.langfuse import TRACE_NAME_DEEP, langfuse_callbacks, make_langfuse_metadata
from app.report.deep import REPORT_KIND
from app.report.snapshot import report_hash

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["research"])

#: 断线后仍在跑的 pump 任务（asyncio 只持弱引用，必须自己拿住），与 `api/chat.py` 同由
_DETACHED: set[asyncio.Task[None]] = set()


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=2, max_length=500)
    symbols: list[str] = Field(default_factory=list, max_length=5)
    as_of: datetime | None = None
    mode: Literal["auto", "deep", "quick"] = "auto"


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choice: Literal["continue", "stop"] = "continue"
    note: str = Field(default="", max_length=200)


def _limits() -> Limits:
    settings = get_settings()
    return Limits(
        llm_calls=settings.research_max_llm_calls,
        tokens=settings.research_max_tokens,
        seconds=settings.research_max_seconds,
        cost_usd=settings.research_max_cost_usd,
    )


def _default_as_of() -> datetime:
    """缺省时点 = 行情末端当日 15:00（北京时间）。**不写死「今天」**——数据是日增的。"""
    from app.data import duckdb_client as dc

    days = dc.trade_dates()
    if not days:
        raise HTTPException(status_code=503, detail="本地没有行情数据")
    return datetime.fromisoformat(f"{days[-1].isoformat()}T15:00:00+08:00")


def _run_payload(row: dict[str, Any], *, with_events: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(row["id"]),
        "question": row["question"],
        "subject": row.get("subject") or {},
        "status": row["status"],
        "report_id": str(row["report_id"]) if row.get("report_id") else None,
        "error": row.get("error"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }
    if with_events:
        payload["events"] = row.get("events") or []
        payload["budget"] = row.get("budget") or {}
    return payload


def build_context(*, brief: Brief, budget_payload: dict[str, Any] | None = None) -> RoleContext:
    """构造这条运行的角色上下文（模型档、预算、超时都从这里进）。

    单列一个函数是为了留一个**测试注入缝**：离线 HTTP 用例把它换成一个带假模型的版本，
    就不必真去调 LLM（慢、要钱、还会 flaky）——与 `ask_once(chat=...)` 同一个姿势。
    """
    return RoleContext(
        brief=brief,
        budget=Budget.from_payload(budget_payload) if budget_payload else Budget(limits=_limits()),
    )


async def _drive(
    *,
    db: Any,
    checkpointer: Any,
    user_id: int,
    run_id: str,
    question: str,
    brief: Brief,
    resume: dict[str, Any] | None,
    budget_payload: dict[str, Any] | None = None,
    callbacks: Sequence[Any] | None = None,
) -> AsyncIterator[Frame]:
    """真正干活的那条流：跑图 → 逐帧产出 → 结束前**落库**（报告 + 运行状态）。"""
    ctx = build_context(brief=brief, budget_payload=budget_payload)
    events: list[dict[str, Any]] = []
    status = STATUS_FAILED
    report_id: str | None = None
    report_hash_value: str | None = None

    async for name, payload in astream_deep_run(
        ctx=ctx,
        checkpointer=checkpointer,
        run_id=run_id,
        brief=brief,
        resume=resume,
        max_rounds=min(get_settings().research_debate_rounds, MAX_ROUNDS_HARD),
        callbacks=callbacks,
        # 四个运行期字段在这里构造（E7c）：会话 = 本次运行（run_id），业务名固定
        # `deep-research`——**不沿用节点名**（节点名是 langgraph 的，业务上不可读）。
        metadata=make_langfuse_metadata(
            session_id=run_id,
            user_id=user_id,
            trace_name=TRACE_NAME_DEEP,
            tags=("deep", "research"),
        ),
    ):
        if name == "report":
            report = payload["report"]
            status_candidate = str(payload["status"])
            try:
                snapshot = deep_snapshot()
                # 数据快照也放进正文（与 M7 的报告同形）：研报页的指纹行要显示它，
                # 缺了就会显示「数据快照 —」——而**快照正是这份报告可复现的凭据**，不能只存库里。
                # ⚠ 必须在算 report_hash **之前**加：hash 覆盖的是落库的同一棵树。
                report = {**report, "snapshot": snapshot}
                row = await db.create_research_report(
                    report_id=str(uuid.uuid4()),
                    user_id=user_id,
                    account_id=None,
                    snapshot=snapshot,
                    snapshot_hash=snapshot_digest(snapshot),
                    report=report,
                    report_hash=report_hash(report),
                    kind=REPORT_KIND,
                    subject={
                        "question": question,
                        "symbols": list(brief.symbols),
                        "as_of": brief.as_of.isoformat(),
                        "run_id": run_id,
                    },
                )
                report_id = str(row["id"])
                report_hash_value = report_hash(report)
                status = status_candidate
            except Exception as exc:  # noqa: BLE001 —— 落库失败如实报，不假装成功
                log.warning("深研报落库失败 run=%s: %r", run_id, exc)
                yield "error", {"message": f"报告落库失败：{type(exc).__name__}: {exc}"}
            else:
                yield "report", {**payload, "report_id": report_id, "report_hash": report_hash_value}
            continue
        if name == "done":
            events = list(payload.get("events") or [])
            status = str(payload.get("status") or status)
            await db.update_research_run(
                run_id,
                status=status,
                events=events,
                budget=ctx.budget.to_payload(),
                report_id=report_id,
                error=None,
            )
            yield name, {**payload, "report_id": report_id, "report_hash": report_hash_value}
            continue
        yield name, payload


async def _pump(queue: asyncio.Queue, producer: AsyncIterator[Frame]) -> None:
    """把生产流的帧搬进队列；**无论客户端在不在，都把它跑完**（断连不取消）。"""
    try:
        async for frame in producer:
            queue.put_nowait(frame)
    except Exception as exc:  # noqa: BLE001 —— 兜底：流本身炸了也要让消费方收到终止信号
        queue.put_nowait(("error", {"message": f"{type(exc).__name__}: {exc}"}))
    finally:
        queue.put_nowait(None)  # 哨兵：消费方据此收尾


def _stream(queue: asyncio.Queue, task: asyncio.Task[None], headers: dict[str, str]):
    async def generator() -> AsyncIterator[str]:
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                name, payload = item
                yield sse_frame(name, payload)
        finally:
            # 客户端断开也不取消 pump：让它跑完并落库（asyncio 只持弱引用，自己拿住）
            if not task.done():
                _DETACHED.add(task)
                task.add_done_callback(_DETACHED.discard)

    return sse_response(generator(), headers=headers)


@router.post("/research/runs")
async def create_run(
    request: Request,
    body: RunRequest,
    user: dict[str, Any] = Depends(require_user),
    db: Any = Depends(require_db),
    _: None = Depends(limit("llm-research")),
) -> Any:
    """建运行并启动。`mode=auto` 时先过意图路由；判 quick 就只回一帧 `route`，不建运行。"""
    checkpointer = getattr(request.app.state, "checkpointer", None)
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="checkpointer 不可用")
    user_id = int(user["id"])

    # 配额（E2）：排在**路由之前**——路由本身就要调一次模型，之后的 422（没解析出标的）
    # 不该退额；quick 路径同样计一次（它也是一次 Agent 请求）
    await quota.enforce(db, user, quota.METRIC_AGENT_CALLS)

    decision: RouteDecision = await route_question(body.question, force=None if body.mode == "auto" else body.mode)
    if decision.path != PATH_DEEP and body.mode == "auto":
        async def quick_stream() -> AsyncIterator[str]:
            yield sse_frame("route", {"path": decision.path, "reason": decision.reason})
            yield sse_frame("done", {"status": "quick", "run_id": None})

        return sse_response(quick_stream())

    as_of = body.as_of or _default_as_of()
    brief = build_brief(
        body.question, decision=decision, as_of=as_of,
        symbols=tuple(body.symbols) if body.symbols else None,
    )
    if not brief.symbols:
        raise HTTPException(status_code=422, detail="没有解析出标的：请在问题里写明代码或全名，或用 symbols 指定")

    run_id = str(uuid.uuid4())
    await db.create_research_run(
        run_id=run_id,
        user_id=user_id,
        question=body.question,
        subject=brief.to_payload(),
        status=STATUS_RUNNING,
    )

    queue: asyncio.Queue = asyncio.Queue()
    producer = _drive(
        db=db, checkpointer=checkpointer, user_id=user_id, run_id=run_id,
        question=body.question, brief=brief, resume=None,
        callbacks=langfuse_callbacks(request.app.state),
    )
    task = asyncio.create_task(_pump(queue, producer))
    _DETACHED.add(task)
    task.add_done_callback(_DETACHED.discard)
    return _stream(queue, task, {"X-Run-Id": run_id})


@router.post("/research/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    request: Request,
    body: ResumeRequest,
    user: dict[str, Any] = Depends(require_user),
    db: Any = Depends(require_db),
) -> Any:
    """HITL 恢复。只有 `awaiting_approval` 的运行可恢复（其余 409）。"""
    checkpointer = getattr(request.app.state, "checkpointer", None)
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="checkpointer 不可用")
    user_id = int(user["id"])
    row = await db.get_research_run(user_id, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="运行不存在")  # 越权与不存在同为 404
    if row["status"] != STATUS_AWAITING:
        raise HTTPException(status_code=409, detail=f"运行当前是 {row['status']}，没有待批事项")

    subject = row.get("subject") or {}
    brief = Brief(
        question=str(subject.get("question") or row["question"]),
        symbols=tuple(subject.get("symbols") or ()),
        as_of=datetime.fromisoformat(str(subject["as_of"])),
        scope=str(subject.get("scope") or ""),
    )
    queue: asyncio.Queue = asyncio.Queue()
    producer = _drive(
        db=db, checkpointer=checkpointer, user_id=user_id, run_id=run_id,
        question=row["question"], brief=brief,
        resume={"choice": body.choice, "note": body.note},
        budget_payload=row.get("budget") or None,
        callbacks=langfuse_callbacks(request.app.state),
    )
    task = asyncio.create_task(_pump(queue, producer))
    _DETACHED.add(task)
    task.add_done_callback(_DETACHED.discard)
    return _stream(queue, task, {"X-Run-Id": run_id})


@router.get("/research/runs")
async def list_runs(
    user: dict[str, Any] = Depends(require_user), db: Any = Depends(require_db)
) -> dict[str, Any]:
    rows = await db.list_research_runs(int(user["id"]))
    return {"runs": [_run_payload(row) for row in rows]}


@router.get("/research/runs/{run_id}")
async def get_run(
    run_id: str,
    user: dict[str, Any] = Depends(require_user),
    db: Any = Depends(require_db),
) -> dict[str, Any]:
    """运行详情（含事件回放）——断连重连与事后回看都走它。"""
    row = await db.get_research_run(int(user["id"]), run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    payload = _run_payload(row, with_events=True)
    if payload["report_id"]:
        report_row = await db.get_research_report(int(user["id"]), payload["report_id"])
        payload["report"] = report_row["report"] if report_row else None
    else:
        payload["report"] = None
    payload["done"] = payload["status"] in {STATUS_DONE, "degraded", STATUS_FAILED}
    return payload
